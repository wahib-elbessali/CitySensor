"""
Redis Store
===========
Centralized wrapper for all Redis operations used by the city sensor system.

Data structures used:
  - Strings    : Latest sensor reading per zone (with TTL) → stockage capteurs
  - Hashes     : Structured zone state, field-level access → stockage capteurs
  - Sorted Sets: Active alerts by severity + hot zone LRU cache → alertes + cache
  - Pub/Sub    : Real-time broadcast of readings and alerts → simulation streaming
"""

import json
import time
import redis


# ─── Alert thresholds ─────────────────────────────────────────────────────────

ALERT_THRESHOLDS = {
    "pm25":        100.0,   # µg/m³  — air quality danger
    "no2":         150.0,   # µg/m³
    "temperature":  38.0,   # °C     — heat alert
    "flow_rate":     3.0,   # m³/s   — flood risk
}

# ─── Pub/Sub channel names ────────────────────────────────────────────────────

CHANNELS = {
    "stream": "city:stream",   # all sensor readings (published by A and B)
    "alerts": "city:alerts",   # threshold-crossing events only
}


# ─── Store class ──────────────────────────────────────────────────────────────

class RedisStore:
    def __init__(self, host="127.0.0.1", port=6379, db=0):
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True, socket_timeout=5.0)

    def ping(self) -> bool:
        return self.r.ping()

    # ── Strings: latest reading per zone (TTL = hot window) ──────────────────

    def set_latest(self, zone_id: int, reading: dict, ttl: int = 300):
        """Store the latest full reading for a zone as a JSON string with TTL."""
        self.r.set(f"sensor:{zone_id}:latest", json.dumps(reading), ex=ttl)

    def get_latest(self, zone_id: int) -> dict | None:
        """Get the latest reading for a zone, or None if expired/missing."""
        val = self.r.get(f"sensor:{zone_id}:latest")
        return json.loads(val) if val else None

    # ── Hashes: structured zone state (individual fields queryable) ───────────

    def set_zone_state(self, zone_id: int, reading: dict, ttl: int = 86400):
        """
        Store key sensor fields in a Hash.
        Appends the unix timestamp to the key to keep a history of readings.
        """
        timestamp = reading["unix_timestamp"]
        key = f"zone:{zone_id}:state:{timestamp}"
        
        self.r.hset(key, mapping={
            "timestamp":     reading["timestamp"],
            "temperature":   reading["weather"]["temperature"],
            "humidity":      reading["weather"]["humidity"],
            "precipitation": reading["weather"]["precipitation"],
            "pm25":          reading["pollution"]["pm25"],
            "no2":           reading["pollution"]["no2"],
            "co2":           reading["pollution"]["co2_level"],
            "flow_rate":     reading["water"]["flow_rate"],
            "noise":         reading["noise"]["noise_level"],
            "lat":           reading["zone_position"]["latitude"],
            "lon":           reading["zone_position"]["longitude"],
        })
        self.r.expire(key, ttl)

    def get_zone_state(self, zone_id: int, timestamp: int) -> dict:
        """Return all Hash fields for a zone's state at a specific timestamp."""
        return self.r.hgetall(f"zone:{zone_id}:state:{timestamp}")

    def get_zone_field(self, zone_id: int, timestamp: int, field: str) -> str | None:
        """Return a single field from a zone's state Hash at a specific timestamp."""
        return self.r.hget(f"zone:{zone_id}:state:{timestamp}", field)

    def get_zone_history_keys(self, zone_id: int) -> list:
        """
        Return all historical state keys for a given zone.
        Uses SCAN instead of KEYS to avoid blocking Redis.
        """
        keys = []
        cursor = 0
        pattern = f"zone:{zone_id}:state:*"
        while True:
            cursor, batch = self.r.scan(cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        return sorted(keys)  # sorted by timestamp (it's in the key name)

    # ── Sorted Sets: real-time alerts ────────────────────────────────────────

    def check_and_add_alert(self, zone_id: int, reading: dict) -> list:
        """
        Compare reading against thresholds. Add triggered metrics to the
        'alerts:active' Sorted Set scored by value (higher = more severe).
        Returns a list of triggered alert dicts.
        """
        triggered = []
        checks = {
            "pm25":        reading["pollution"]["pm25"],
            "no2":         reading["pollution"]["no2"],
            "temperature": reading["weather"]["temperature"],
            "flow_rate":   reading["water"]["flow_rate"],
        }
        ts = reading["unix_timestamp"]
        for metric, value in checks.items():
            if value > ALERT_THRESHOLDS[metric]:
                # Include timestamp in member so each alert event is unique
                # and past alerts don't overwrite current ones in the Sorted Set
                member = f"zone:{zone_id}:{metric}:{ts}"
                self.r.zadd("alerts:active", {member: value})
                triggered.append({
                    "event":     "alert",   # explicit discriminator for the frontend
                    "zone_id":   zone_id,
                    "metric":    metric,
                    "value":     value,
                    "threshold": ALERT_THRESHOLDS[metric],
                    "timestamp": reading["timestamp"],
                })
        return triggered

    def get_top_alerts(self, n: int = 10) -> list:
        """Return the top N alerts by severity (highest score first)."""
        return self.r.zrevrange("alerts:active", 0, n - 1, withscores=True)

    def clear_old_alerts(self, max_score: float = 0):
        """Remove alerts below a severity score (basic cleanup)."""
        self.r.zremrangebyscore("alerts:active", "-inf", max_score)

    # ── Sorted Sets: hot data cache ───────────────────────────────────────────

    def update_hot_cache(self, zone_id: int, reading: dict, ttl: int = 300):
        """
        Mark a zone as recently active:
          - Sorted Set 'cache:hot_zones' tracks last-access timestamps for LRU eviction.
          - String 'cache:zone:<id>:hot' caches the full reading payload with TTL.
        """
        self.r.zadd("cache:hot_zones", {str(zone_id): time.time()})
        self.r.set(f"cache:zone:{zone_id}:hot", json.dumps(reading), ex=ttl)

    def get_hot(self, zone_id: int) -> dict | None:
        """Return the cached hot reading for a zone, or None if cold/expired."""
        val = self.r.get(f"cache:zone:{zone_id}:hot")
        return json.loads(val) if val else None

    def get_hot_zones(self, n: int = 10) -> list:
        """
        Return the N most recently active zone IDs.
        Evicts zones not accessed in the last 5 minutes before returning.
        """
        cutoff = time.time() - 300
        self.r.zremrangebyscore("cache:hot_zones", "-inf", cutoff)
        return self.r.zrevrange("cache:hot_zones", 0, n - 1)

    # ── Pub/Sub ───────────────────────────────────────────────────────────────

    def publish(self, channel: str, data: dict) -> int:
        """Publish a dict to a channel. Returns the number of subscribers."""
        return self.r.publish(channel, json.dumps(data))

    def get_pubsub(self):
        """Return a sync Pub/Sub object for subscribing."""
        return self.r.pubsub()
