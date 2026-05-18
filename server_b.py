"""
Server B — City Sensor Server (Zones 151–300)
=============================================
Handles data ingestion for the SECOND half of the city.

On startup, a background thread runs the simulation for zones 151–300,
writes every reading into the shared Redis instance, and publishes
each time-step batch to the 'city:stream' Pub/Sub channel.

Server A subscribes to that channel and relays everything (including
Server B's data) to its connected clients — so clients never need
to connect here directly.

Usage:
    python server_b.py

    # Server A must also be running for the client to see the full city:
    python server_a.py
"""

import json
import time
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from config import (
    REDIS_HOST, REDIS_PORT, STEP_DELAY,
    NUM_SENSORS_PER_SERVER, ZONE_B_START, CORS_ORIGINS,
)
from generate_data import CityDataGenerator, SETTINGS, build_sensors, simulate_stream, get_map_dimensions, apply_seed_offset
from redis_store import RedisStore, CHANNELS

# ─── Configuration ────────────────────────────────────────────────────────────

SERVER_NAME = "Server-B"
SERVER_PORT = 8002
ZONE_START  = ZONE_B_START   # 151

# ─── Sync Redis store ─────────────────────────────────────────────────────────

store = RedisStore(host=REDIS_HOST, port=REDIS_PORT)

# ─── Background simulation ────────────────────────────────────────────────────

def simulation_loop(stop: threading.Event):
    """
    Runs in a daemon thread. Generates readings for zones ZONE_START–(ZONE_START+NUM_SENSORS-1),
    writes them to Redis (Strings, Hashes, Sorted Sets), and publishes each
    completed time-step batch to city:stream via Pub/Sub.
    Loops until the stop event is set, and synchronizes starting times
    across instances via Redis.
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
        NUM_SENSORS_PER_SERVER,
        SETTINGS["base_lat"], SETTINGS["base_lon"],
        zone_id_start=ZONE_START,
        x_start=w // 2,
        x_end=w,
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

        # Wait up to 5 seconds for Server A to generate the seed offset if not present
        shared_offset = None
        for _ in range(10):
            try:
                shared_offset = store.r.get("sim:seed_offset")
                if shared_offset is not None:
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if shared_offset is None:
            shared_offset = random.randint(1, 100000)
            store.r.setex("sim:seed_offset", 300, str(shared_offset))
            print(f"[{SERVER_NAME}] Generated backup shared seed offset: {shared_offset}")
        else:
            shared_offset = int(shared_offset)
            print(f"[{SERVER_NAME}] Using existing shared seed offset: {shared_offset}")

        apply_seed_offset(shared_offset)

        # ── Synchronize starting time with other servers via Redis ─────────────
        start_time_key = "sim:start_time"
        start_time_val = store.r.get(start_time_key)
        if start_time_val is None:
            # Use SET NX EX so only one server wins if both arrive simultaneously
            start_time = time.time() + 2.0
            store.r.set(start_time_key, str(start_time), nx=True, ex=60)
            # Re-read in case Server A won the race
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

        print(f"[{SERVER_NAME}] Run started — zones {ZONE_START}–{ZONE_START + NUM_SENSORS_PER_SERVER - 1}")

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

            # ── Write to Redis ────────────────────────────────────────────────
            store.set_latest(zone_id, reading)        # String
            store.set_zone_state(zone_id, reading)    # Hash
            store.update_hot_cache(zone_id, reading)  # Sorted Set + String cache

            alerts = store.check_and_add_alert(zone_id, reading)  # Sorted Set
            for alert in alerts:
                store.publish(CHANNELS["alerts"], alert)
                print(f"[{SERVER_NAME}] [ALERT] Alert zone {zone_id}: "
                      f"{alert['metric']} = {alert['value']:.1f} "
                      f"(threshold {alert['threshold']})")

            # ── Batch by timestamp then publish ───────────────────────────────
            if ts != current_ts:
                if batch:
                    store.publish(CHANNELS["stream"], {
                        "server": SERVER_NAME,
                        "batch":  batch,
                    })
                    time.sleep(STEP_DELAY)
                batch      = [reading]
                current_ts = ts
            else:
                batch.append(reading)

        # Flush final batch
        if batch and not stop.is_set():
            store.publish(CHANNELS["stream"], {"server": SERVER_NAME, "batch": batch})

        # Signal end of simulation
        store.publish(CHANNELS["stream"], {"event": "done", "server": SERVER_NAME})
        print(f"[{SERVER_NAME}] Run complete.")

        # Server A is responsible for deleting start_time_key to avoid the
        # race where both servers delete it and then re-create it independently.
        # Server B simply waits for the key to disappear (set by Server A's delete).

        if not stop.is_set():
            print(f"[{SERVER_NAME}] Restarting next run in 5 seconds...")
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
    title="City Sensor Server B",
    description=f"Zones {ZONE_START}–{ZONE_START + NUM_SENSORS_PER_SERVER - 1}. Writes to shared Redis. Clients connect to Server A.",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", summary="Server info")
async def root():
    return {
        "server":     SERVER_NAME,
        "zone_range": f"{ZONE_START}–{ZONE_START + NUM_SENSORS_PER_SERVER - 1}",
        "note":       "Data is published to Redis. Connect to Server A for the full city SSE stream.",
    }


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server_b:app", host="0.0.0.0", port=SERVER_PORT, reload=False)
