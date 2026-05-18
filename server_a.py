"""
Server A — City Sensor Server (Zone 1)
=======================================
Handles zone 1. Simulation starts automatically on startup.

Also relays the full city stream (zone 1 + zone 2 from Server B) to the client
via Redis Pub/Sub — so the client sees everything through one SSE connection.

Usage:
    python server_a.py

    # Client connects with default params:
    curl "http://localhost:8001/stream"
"""

import json
import time
import sys
import asyncio
import threading
from contextlib import asynccontextmanager

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

import redis.asyncio as aioredis
from fastapi import FastAPI, Query, Path
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config import (
    REDIS_HOST, REDIS_PORT, STEP_DELAY,
    NUM_SENSORS_PER_SERVER, ZONE_A_START, CORS_ORIGINS,
)
from generate_data import CityDataGenerator, SETTINGS, build_sensors, simulate_stream, get_map_dimensions, apply_seed_offset
from redis_store import RedisStore, CHANNELS

# ─── Configuration ────────────────────────────────────────────────────────────

SERVER_NAME = "Server-A"
SERVER_PORT = 8001
ZONE_ID     = ZONE_A_START   # 1

# ─── Redis store (sync, used by the simulation thread) ────────────────────────

store = RedisStore(host=REDIS_HOST, port=REDIS_PORT)

# ─── Background simulation ────────────────────────────────────────────────────

def simulation_loop(stop: threading.Event):
    """
    Runs in a daemon thread. Generates readings for ZONE_ID,
    writes them to Redis (Strings, Hashes, Sorted Sets), and publishes each
    completed time-step batch to city:stream via Pub/Sub.
    Loops infinitely until the stop event is set, and synchronizes
    starting times across instances via Redis.
    """
    import random

    w, h = get_map_dimensions()
    city = CityDataGenerator(
        road_image_path=SETTINGS["roads_image"],
        river_image_path=SETTINGS["river_image"],
        elevation_image_path=SETTINGS["elevation_image"],
        width=w,
        height=h,
    )
    # Pass the pre-computed river mask so build_sensors skips a redundant map load
    sensors = build_sensors(
        w, h,
        num_sensors=NUM_SENSORS_PER_SERVER,
        base_lat=SETTINGS["base_lat"],
        base_lon=SETTINGS["base_lon"],
        zone_id_start=ZONE_ID,
        x_start=0,
        x_end=w // 2,
        river_mask=city.river_mask,
    )

    while not stop.is_set():
        # ── Wait for Redis connection to be fully established and healthy ────
        while not stop.is_set():
            try:
                store.r.ping()
                break
            except Exception as e:
                print(f"[{SERVER_NAME}] Waiting for Redis connection: {e}. Retrying in 1s...")
                time.sleep(1)

        if stop.is_set():
            break

        # ── Wait for a client to connect and subscribe to the stream ──────────
        print(f"[{SERVER_NAME}] Waiting for active dashboard client to connect...", flush=True)
        while not stop.is_set():
            try:
                res = store.r.pubsub_numsub(CHANNELS["stream"])
                if res and res[0][1] > 0:
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if stop.is_set():
            break
        print(f"[{SERVER_NAME}] Client connected. Starting simulation...", flush=True)

        # ── Get or create a shared simulation seed from Redis ─────────────────
        shared_offset = store.r.get("sim:seed_offset")
        if shared_offset is None:
            shared_offset = random.randint(1, 100000)
            store.r.setex("sim:seed_offset", 300, str(shared_offset))
            print(f"[{SERVER_NAME}] Generated new shared seed offset: {shared_offset}")
        else:
            shared_offset = int(shared_offset)
            print(f"[{SERVER_NAME}] Using existing shared seed offset: {shared_offset}")

        apply_seed_offset(shared_offset)

        # Clear old active alerts on startup of new run
        store.r.delete("alerts:active")

        # ── Synchronize starting time with other servers via Redis ─────────────
        start_time_key = "sim:start_time"
        start_time_val = store.r.get(start_time_key)
        if start_time_val is None:
            # We are the first server to initiate this run. Set start time 2s from now.
            # Use SET NX EX to avoid a race where both servers set the key simultaneously.
            start_time = time.time() + 2.0
            store.r.set(start_time_key, str(start_time), nx=True, ex=60)
            # Re-read in case Server B won the race
            start_time = float(store.r.get(start_time_key))
            print(f"[{SERVER_NAME}] Set/resolved synchronized start time to: {start_time}")
        else:
            start_time = float(start_time_val)
            print(f"[{SERVER_NAME}] Synchronizing start time to: {start_time}")

        # Wait until synchronized start time
        sleep_dur = start_time - time.time()
        if sleep_dur > 0:
            time.sleep(sleep_dur)

        if stop.is_set():
            break

        print(f"[{SERVER_NAME}] Run started — zone {ZONE_ID} "
              f"(duration={SETTINGS['sim_duration_hours']}h, interval={SETTINGS['reading_interval_mins']}min, step_delay={STEP_DELAY}s)")

        current_ts = None
        batch      = []

        for reading in simulate_stream(
            city, sensors,
            SETTINGS["sim_duration_hours"],
            SETTINGS["reading_interval_mins"],
        ):
            if stop.is_set():
                break

            zone_id = reading["zone_id"]
            ts      = reading["timestamp"]

            # ── Write to Redis (all 4 structures) ────────────────────────────
            store.set_latest(zone_id, reading)        # String
            store.set_zone_state(zone_id, reading)    # Hash
            store.update_hot_cache(zone_id, reading)  # Sorted Set + String cache

            alerts = store.check_and_add_alert(zone_id, reading)  # Sorted Set
            for alert in alerts:
                store.publish(CHANNELS["alerts"], alert)
                print(f"[{SERVER_NAME}] [ALERT] Alert zone {zone_id}: "
                      f"{alert['metric']} = {alert['value']:.1f}")

            # ── Batch by timestamp then publish via Pub/Sub ───────────────────
            if ts != current_ts:
                if batch:
                    store.publish(CHANNELS["stream"], {"server": SERVER_NAME, "batch": batch})
                    time.sleep(STEP_DELAY)
                batch      = [reading]
                current_ts = ts
            else:
                batch.append(reading)

        if batch and not stop.is_set():
            store.publish(CHANNELS["stream"], {"server": SERVER_NAME, "batch": batch})

        store.publish(CHANNELS["stream"], {"event": "done", "server": SERVER_NAME})
        print(f"[{SERVER_NAME}] Run complete.")

        # Only Server A is responsible for deleting the start_time_key so
        # there is no race where both servers delete it simultaneously.
        store.r.delete(start_time_key)

        if not stop.is_set():
            print(f"[{SERVER_NAME}] Restarting next run in 5 seconds...")
            # Use small sleep slices so the stop event is checked promptly
            for _ in range(50):
                if stop.is_set():
                    break
                time.sleep(0.1)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    thread = threading.Thread(target=simulation_loop, args=(stop,), daemon=True)
    thread.start()
    yield
    print(f"[{SERVER_NAME}] Shutdown requested — stopping simulation thread...")
    stop.set()
    thread.join(timeout=5)
    print(f"[{SERVER_NAME}] Simulation thread stopped.")


# ─── App setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="City Sensor Server A",
    description=f"Zone {ZONE_ID}. Relays full city stream (A + B) to clients via SSE.",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ─── SSE relay ────────────────────────────────────────────────────────────────

async def city_stream_relay():
    """
    Subscribes to city:stream and relays ALL messages (zone 1 from A, zone 2 from B)
    as SSE events. Only closes when ALL servers have reported 'done'.
    """
    r = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_timeout=5.0)
    pubsub = r.pubsub()
    await pubsub.subscribe(CHANNELS["stream"], CHANNELS["alerts"])

    w, h = get_map_dimensions()
    meta = {
        "server":      SERVER_NAME,
        "zone":        ZONE_ID,
        "step_delay":  STEP_DELAY,
        "duration":    SETTINGS["sim_duration_hours"],
        "interval":    SETTINGS["reading_interval_mins"],
        "grid_width":  w,
        "grid_height": h,
        "note":        "Full city stream — data from all servers via Redis Pub/Sub",
    }
    yield f"event: connected\ndata: {json.dumps(meta)}\n\n"

    done_servers = set()
    expected_servers = {SERVER_NAME, "Server-B"}  # all servers in the cluster

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue

        payload = json.loads(message["data"])

        if "batch" in payload:
            print(f"[{SERVER_NAME}] Relay received batch from {payload.get('server')}: {len(payload['batch'])} readings", flush=True)

        if payload.get("event") == "done":
            done_servers.add(payload.get("server"))
            yield f"event: done\ndata: {json.dumps(payload)}\n\n"
            # Only close when ALL servers have finished
            if done_servers >= expected_servers:
                break
            continue

        yield f"data: {json.dumps(payload)}\n\n"

    await pubsub.unsubscribe(CHANNELS["stream"], CHANNELS["alerts"])
    await r.aclose()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "server":     SERVER_NAME,
        "zone":       ZONE_ID,
        "endpoints": {
            "stream":    "/stream",
            "snapshot":  "/snapshot/{zone_id}",
            "history":   "/history/{zone_id}",
            "state":     "/state/{zone_id}/{timestamp}",
            "alerts":    "/alerts",
            "hot":       "/hot",
        },
    }


@app.get("/stream", summary="Full city SSE stream")
async def stream():
    """
    SSE stream for the full city (all zones from all servers).
    Clients simply connect to stream pre-existing simulated data.
    """
    return StreamingResponse(
        city_stream_relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/snapshot/{zone_id}", summary="Latest reading for a zone")
async def snapshot(zone_id: int = Path(ge=1)):
    reading = store.get_hot(zone_id) or store.get_latest(zone_id)
    if not reading:
        return JSONResponse({"error": f"No data for zone {zone_id}"}, status_code=404)
    return reading


@app.get("/history/{zone_id}", summary="Get all state history keys for a zone")
async def zone_history(zone_id: int = Path(ge=1)):
    keys = store.get_zone_history_keys(zone_id)
    return {"zone_id": zone_id, "history_keys": keys}


@app.get("/state/{zone_id}/{timestamp}", summary="Zone Hash fields at a specific timestamp")
async def zone_state(zone_id: int, timestamp: int):
    state = store.get_zone_state(zone_id, timestamp)
    if not state:
        return JSONResponse({"error": f"No state for zone {zone_id} at timestamp {timestamp}"}, status_code=404)
    return state


@app.get("/state/{zone_id}/{timestamp}/{field}", summary="Single Hash field at a specific timestamp")
async def zone_field(zone_id: int, timestamp: int, field: str):
    value = store.get_zone_field(zone_id, timestamp, field)
    if value is None:
        return JSONResponse({"error": f"Field '{field}' not found for zone {zone_id} at timestamp {timestamp}"}, status_code=404)
    return {field: value}


@app.get("/alerts", summary="Top active alerts")
async def alerts(n: int = Query(10, ge=1, le=100)):
    raw = store.get_top_alerts(n)
    return [{"member": m, "severity_score": s} for m, s in raw]


@app.get("/hot", summary="Most recently active zones")
async def hot_zones(n: int = Query(10, ge=1, le=100)):
    return {"hot_zones": store.get_hot_zones(n)}


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server_a:app", host="0.0.0.0", port=SERVER_PORT, reload=False)
