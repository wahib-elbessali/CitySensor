"""
City Data Sensor Generator
=========================
Generates simulated sensor data for a city over a specific duration and outputs to JSON.

Improvements included:
  - Time-Aware Realism    : Traffic/temp/pollution follow realistic 24-hour diurnal curves.
  - Weather Correlation   : High humidity suppresses pollution; high wind disperses it.
  - Wind-Driven Pollution : Pollution noise field is offset in the wind direction each step.
  - River Flow Rate       : Water block includes a flow_rate driven by rainfall/humidity.

Usage:
    python generate_data.py
    
    # or with custom image paths:
    python generate_data.py --roads my_roads.png --river my_river.png --elevation my_terrain.png
"""

import argparse
import os
import json
import math
import random
import datetime
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, shift, distance_transform_edt
from noise import snoise2, snoise3


# ─────────────────────────────────────────────────────────────────────────────
# Global Settings
# ─────────────────────────────────────────────────────────────────────────────
SETTINGS = {
    "roads_image":     "roads.png",
    "river_image":     "water.png",
    "elevation_image": "terrain.png",
    "map_image":       "map.png",
    "num_sensors": 100,             # Number of random sensor locations
    "sim_duration_hours": 24,       # Total simulation time in hours
    "reading_interval_mins": 10,    # How often sensors record data
    "out_file": "city_data/sensor_data.json",
    "base_lat": 32.49,
    "base_lon": -6.18,

    # ── Time-of-Day curve strengths (0.0 = no effect, 1.0 = full effect) ──
    "traffic_tod_strength": 1.0,    # How strongly rush-hour shapes traffic
    "temp_tod_strength":    0.6,    # How strongly the diurnal cycle shifts temperature
    "pollution_tod_strength": 0.8,  # How strongly traffic peaks push pollution

    # ── Wind-Driven Pollution ──
    "wind_drift_pixels_per_step": 0.5,  # How far (in grid pixels) pollution drifts each step

    # ── Weather Correlation ──
    "humidity_pollution_suppression": 0.5,  # 0=no suppression, 1=full suppression at max humidity
    "wind_pollution_dispersal": 0.4,        # 0=no dispersal, 1=full dispersal at max wind
}

# ─────────────────────────────────────────────────────────────────────────────
# Noise Generation Parameters: (t_scale, scale, octaves, seed, multiplier, offset)
# These control the underlying math for each metric's evolution over time and space.
# ─────────────────────────────────────────────────────────────────────────────
NOISE_PARAMS = {
    # Large scale = broad continental patterns; small scale = local variation
    "temperature_c":         dict(t_scale=0.10, scale=120, octaves=2, seed=0,    mult=18,  offset=8),   # ~8-26°C
    "humidity_percent":      dict(t_scale=0.12, scale=100, octaves=2, seed=50,   mult=55,  offset=25),  # ~25-80%
    "wind_speed_kmh":        dict(t_scale=0.15, scale=90,  octaves=2, seed=100,  mult=45,  offset=5),
    "wind_direction_deg":    dict(t_scale=0.05, scale=120, octaves=1, seed=200,  mult=360, offset=0),
    "traffic_vehicles_min":  dict(t_scale=2.00, scale=10,  octaves=3, seed=300,  mult=120, offset=0),
    # Larger scale so pollution blobs span city blocks, not individual pixels
    "pm25_ug_m3":            dict(t_scale=0.50, scale=60,  octaves=2, seed=400,  mult=60,  offset=5),
    "no2_ug_m3":             dict(t_scale=0.45, scale=70,  octaves=2, seed=500,  mult=50,  offset=5),
    "co2_ppm":               dict(t_scale=0.40, scale=80,  octaves=2, seed=600,  mult=120, offset=400),
    "precipitation_mm":      dict(t_scale=0.18, scale=90,  octaves=2, seed=1200, mult=12,  offset=0),   # kept >= 0 by design
    "soil_moisture_percent": dict(t_scale=0.02, scale=40,  octaves=2, seed=700,  mult=100, offset=0, noise_weight=0.4, elev_weight=0.6),
    "soil_ph":               dict(t_scale=0.01, scale=50,  octaves=2, seed=800,  mult=4,   offset=4.5),
    "water_turbidity_ntu":   dict(t_scale=1.00, scale=20,  octaves=3, seed=900,  mult=80,  offset=0),
    "water_ph":              dict(t_scale=0.10, scale=40,  octaves=2, seed=1000, mult=2,   offset=6),
    "water_flow_rate":       dict(t_scale=0.08, scale=60,  octaves=2, seed=1100, mult=3.0, offset=0.5),
}


# ─────────────────────────────────────────────────────────────────────────────
# Freeze the baseline seeds at import time so apply_seed_offset always offsets
# from the original values, not from a previously-modified (compounded) value.
# ─────────────────────────────────────────────────────────────────────────────
_BASELINE_SEEDS: dict[str, int] = {k: v["seed"] for k, v in NOISE_PARAMS.items() if "seed" in v}


def apply_seed_offset(offset: int):
    """
    Shift all noise seeds by a fixed offset to randomize each simulation run.
    Always applies the offset relative to the original baseline seeds so that
    repeated calls (e.g. across simulation loop restarts) are idempotent.
    """
    for key, cfg in NOISE_PARAMS.items():
        if "seed" in cfg:
            cfg["seed"] = _BASELINE_SEEDS[key] + offset



# ─────────────────────────────────────────────────────────────────────────────
# Map dimension detection
# ─────────────────────────────────────────────────────────────────────────────

def get_map_dimensions(image_path=None):
    """Returns (width, height) of the map image."""
    path = image_path or SETTINGS.get("map_image", "map.png")
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.width, img.height
    except Exception as e:
        print(f"[WARN] Could not read dimensions from {path}: {e}. Defaulting to 150x150.")
        return 150, 150

# ─────────────────────────────────────────────────────────────────────────────
# Time-of-Day Curve Helpers
# ─────────────────────────────────────────────────────────────────────────────

def traffic_tod_multiplier(hour_of_day: float) -> float:
    """
    Returns a 0.0–1.0 multiplier shaped like a realistic traffic curve:
    - Near zero from midnight to ~5am.
    - Morning rush peak at 8am.
    - Midday dip at 1pm.
    - Evening rush peak at 6pm.
    - Declines to near zero by 11pm.
    """
    h = hour_of_day % 24
    # Two Gaussian peaks: morning (8am) and evening (18pm), plus a smooth night trough.
    morning = math.exp(-0.5 * ((h - 8.0) / 1.5) ** 2)
    evening = math.exp(-0.5 * ((h - 18.0) / 1.8) ** 2)
    night_suppression = max(0.0, 1.0 - math.exp(-0.5 * ((h - 3.0) / 2.5) ** 2))
    return min(1.0, (morning * 0.9 + evening * 1.0) * night_suppression)


def temperature_tod_offset(hour_of_day: float) -> float:
    """
    Returns a –1.0 to +1.0 value representing how temperature deviates from the
    daily mean due to the diurnal cycle:
    - Coldest just before sunrise (~5am) → -1.0
    - Hottest in mid-afternoon (~15:00) → +1.0
    """
    h = hour_of_day % 24
    # Simple cosine shifted so trough is at ~5am and peak at ~15pm
    return math.cos(math.pi * (h - 15.0) / 12.0)


# ─────────────────────────────────────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────────────────────────────────────

class CityDataGenerator:
    def __init__(self, road_image_path=None, river_image_path=None, elevation_image_path=None,
                 width=150, height=150):
        self.width = width
        self.height = height

        # Auto-extract masks from map.png (ignores road_image_path etc.)
        map_path = SETTINGS.get("map_image", "map.png")
        self.road_mask, self.river_mask, self.elevation_map = self._extract_masks_from_map(map_path)

        grad = gaussian_filter(self.road_mask, sigma=8.0) * 2.5
        self.gradient_road_mask = np.clip(grad, 0.0, 1.0)

        # Pre-compute the smoothed river mask once — it is used every step in
        # generate_metrics() for water flow rate, so caching it here avoids
        # rerunning gaussian_filter on every simulation step.
        self.smooth_river_mask = gaussian_filter(self.river_mask, sigma=5.0)

    def _extract_masks_from_map(self, map_path):
        try:
            img = Image.open(map_path).convert("RGB")
            img = img.resize((self.width, self.height), Image.Resampling.LANCZOS)
            
            # Flip vertically: image (0,0) = top-left (north), but our
            # sensor grid has y=0 = south (base_lat).
            img_np = np.flipud(np.array(img, dtype=np.float32))
            
            # 1. Water Extraction: Hex #a7c6ec = RGB(167, 198, 236)
            dist_water = np.sqrt(
                (img_np[:, :, 0] - 167.0) ** 2 +
                (img_np[:, :, 1] - 198.0) ** 2 +
                (img_np[:, :, 2] - 236.0) ** 2
            )
            river_mask = np.where(dist_water < 25.0, 1.0, 0.0).astype(np.float32)
            
            # 2. Road Extraction: Hex #fdfdfd = RGB(253, 253, 253)
            dist_road = np.sqrt(
                (img_np[:, :, 0] - 253.0) ** 2 +
                (img_np[:, :, 1] - 253.0) ** 2 +
                (img_np[:, :, 2] - 253.0) ** 2
            )
            road_mask = np.where(dist_road < 10.0, 1.0, 0.0).astype(np.float32)
            
            # 3. Elevation Map Generation: Derived from water mask (water is the lowest)
            dist_from_water = distance_transform_edt(river_mask == 0)
            max_dist = np.max(dist_from_water)
            if max_dist > 0:
                norm_dist = dist_from_water / max_dist
            else:
                norm_dist = np.zeros_like(dist_from_water)
            
            # River is lowest (0.05), land rises up to 1.0 far from water
            elevation_map = 0.05 + 0.95 * norm_dist
            
            return road_mask, river_mask, elevation_map
            
        except Exception as e:
            print(f"[WARN] Could not extract masks from '{map_path}': {e} -> using blank masks.")
            blank = np.zeros((self.height, self.width), dtype=np.float32)
            return blank, blank, blank

    def _noise(self, t, scale, octaves, persistence=0.5, lacunarity=2.0, seed=0):
        # Generate noise at 1/8 resolution then upsample — reduces snoise2 calls
        # from ~967k to ~15k (58x speedup) while preserving visual smoothness.
        from scipy.ndimage import zoom as nd_zoom
        DOWNSAMPLE = 8
        lh = max(2, self.height // DOWNSAMPLE)
        lw = max(2, self.width  // DOWNSAMPLE)

        time_drift = t * 6.0  # drift in downsampled-pixel units
        eff_scale = scale / DOWNSAMPLE
        xs = (np.arange(lw, dtype=np.float64) + seed + time_drift) / eff_scale
        ys = (np.arange(lh, dtype=np.float64) + seed) / eff_scale

        g_low = np.array(
            [[snoise2(float(xs[ix]), float(ys[iy]), octaves=octaves,
                      persistence=persistence, lacunarity=lacunarity)
              for ix in range(lw)]
             for iy in range(lh)],
            dtype=np.float32
        )

        zy = self.height / lh
        zx = self.width  / lw
        g = nd_zoom(g_low, (zy, zx), order=1)  # bilinear — fast and smooth
        g = g[:self.height, :self.width]        # trim rounding artefacts
        return (g + 1.0) / 2.0

    def generate_metrics(self, t, hour_of_day: float):
        n = self._noise
        p = NOISE_PARAMS
        s = SETTINGS

        def calc(name, mask=1.0, apply_mask_to_offset=False):
            cfg = p[name]
            val = n(t * cfg["t_scale"], cfg["scale"], cfg["octaves"], seed=cfg["seed"])
            if apply_mask_to_offset:
                return (val * cfg["mult"] + cfg["offset"]) * mask
            else:
                return (val * cfg["mult"] * mask) + cfg["offset"]

        # ── Base weather fields ──────────────────────────────────────────────
        base_temperature = calc("temperature_c")
        humidity_percent = calc("humidity_percent")
        wind_speed_kmh   = calc("wind_speed_kmh")
        wind_direction_deg = calc("wind_direction_deg")

        # Precipitation is physically anti-correlated with temperature:
        # warm air rises and stabilizes; cold/humid air produces rain.
        # We generate a raw precip field then subtract a fraction of the
        # normalized temperature so that hot spots are consistently drier.
        raw_precip = calc("precipitation_mm")
        # Normalize temperature to 0-1 and use it to suppress rain in hot zones
        temp_norm = np.clip((base_temperature - 8.0) / 18.0, 0.0, 1.0)  # 8°C→0, 26°C→1
        precipitation_mm = np.clip(raw_precip * (1.0 - 0.6 * temp_norm), 0.0, None)

        # ── Time-of-Day: Temperature diurnal cycle ───────────────────────────
        # temperature_c ranges ~5-25°C so daily swing is ±3°C by default.
        tod_temp_delta   = temperature_tod_offset(hour_of_day) * 3.0 * s["temp_tod_strength"]
        temperature_c    = base_temperature + tod_temp_delta

        # ── Time-of-Day: Traffic rush-hour curve ─────────────────────────────
        tod_traffic_mult = traffic_tod_multiplier(hour_of_day) * s["traffic_tod_strength"]
        # Blend: (1 - strength) keeps the base noise; strength applies the curve.
        traffic_blend    = (1.0 - s["traffic_tod_strength"]) + tod_traffic_mult
        raw_traffic      = calc("traffic_vehicles_min", mask=self.road_mask)
        traffic_vehicles_min = raw_traffic * traffic_blend

        # ── Wind-Driven Pollution Drift ──────────────────────────────────────
        # Convert wind direction (meteorological: 0° = wind FROM north) to a
        # shift vector in grid pixels. Wind blows TO the opposite direction.
        drift = s["wind_drift_pixels_per_step"]
        wind_deg_mean = float(np.mean(wind_direction_deg))     # one representative angle for the step
        wind_rad = math.radians(wind_deg_mean)
        # shift = (row_shift, col_shift); scipy.ndimage.shift uses (y, x)
        dy = drift * math.cos(wind_rad)
        dx = drift * math.sin(wind_rad)

        raw_pm25  = calc("pm25_ug_m3",  mask=self.gradient_road_mask)
        raw_no2   = calc("no2_ug_m3",   mask=self.gradient_road_mask)
        raw_co2   = calc("co2_ppm",     mask=self.gradient_road_mask)

        # Drift the pollution fields in the wind direction (wrap=False so it
        # simply vanishes at the edges — simulating open-air dispersal)
        pm25_ug_m3 = shift(raw_pm25, shift=[dy, dx], mode="constant", cval=0.0).astype(np.float32)
        no2_ug_m3  = shift(raw_no2,  shift=[dy, dx], mode="constant", cval=0.0).astype(np.float32)
        co2_ppm    = shift(raw_co2,  shift=[dy, dx], mode="constant", cval=400.0).astype(np.float32)

        # ── Time-of-Day: Pollution follows traffic peaks ──────────────────────
        # At rush hour the pollution ceiling rises; at 3am it drops back down.
        pollution_tod_mult = (1.0 - s["pollution_tod_strength"]) + tod_traffic_mult * s["pollution_tod_strength"]
        pm25_ug_m3 = pm25_ug_m3 * pollution_tod_mult
        no2_ug_m3  = no2_ug_m3  * pollution_tod_mult
        # CO₂ has a guaranteed 400ppm atmospheric baseline — only the road-excess scales.
        co2_excess = np.clip(co2_ppm - 400.0, 0.0, None)
        co2_ppm    = 400.0 + co2_excess * pollution_tod_mult

        # ── Weather Correlation: humidity, wind & rain suppress pollution ───────────
        # Local suppression: higher humidity/wind/rain suppress pollution at that location
        humidity_scalar = humidity_percent / 100.0
        wind_scalar     = wind_speed_kmh / 50.0      # normalise to 0-1
        rain_scalar     = precipitation_mm / 5.0     # normalise to roughly 0-1
        suppression = 1.0 - (
            humidity_scalar * s["humidity_pollution_suppression"] +
            wind_scalar     * s["wind_pollution_dispersal"] +
            rain_scalar     * 0.6  # Rain heavily washes away pollution
        )
        suppression = np.clip(suppression, 0.1, 1.0)    # never fully zero

        pm25_ug_m3 = pm25_ug_m3 * suppression
        no2_ug_m3  = no2_ug_m3  * suppression
        co2_excess  = np.clip(co2_ppm - 400.0, 0.0, None)
        co2_ppm     = 400.0 + co2_excess * suppression

        # ── Soil ─────────────────────────────────────────────────────────────
        sm_cfg = p["soil_moisture_percent"]
        sm_val = n(t * sm_cfg["t_scale"], sm_cfg["scale"], sm_cfg["octaves"], seed=sm_cfg["seed"])
        inverse_elev = 1.0 - self.elevation_map
        base_sm = (sm_val * sm_cfg["noise_weight"] + inverse_elev * sm_cfg["elev_weight"]) * sm_cfg["mult"]
        soil_moisture_percent = np.clip(base_sm + (precipitation_mm * 5.0), 0.0, 100.0)
        soil_ph = calc("soil_ph")

        # ── Water ─────────────────────────────────────────────────────────────
        water_turbidity_ntu = calc("water_turbidity_ntu", mask=self.river_mask)
        water_ph            = calc("water_ph",  mask=self.river_mask, apply_mask_to_offset=True)

        # River flow rate: base noise with local variation from rain/humidity
        raw_flow       = calc("water_flow_rate")
        rain_boost     = precipitation_mm * 1.5
        humidity_boost = (humidity_percent / 100.0) * 0.5
        # Use the cached smooth river mask (computed once in __init__)
        water_flow_rate = (raw_flow + humidity_boost + rain_boost) * self.smooth_river_mask

        return {
            "temperature_c":         temperature_c,
            "humidity_percent":      humidity_percent,
            "precipitation_mm":      precipitation_mm,
            "wind_speed_kmh":        wind_speed_kmh,
            "wind_direction_deg":    wind_direction_deg,
            "traffic_vehicles_min":  traffic_vehicles_min,
            "pm25_ug_m3":            pm25_ug_m3,
            "no2_ug_m3":             no2_ug_m3,
            "co2_ppm":               co2_ppm,
            "water_ph":              water_ph,
            "water_turbidity_ntu":   water_turbidity_ntu,
            "water_flow_rate":       water_flow_rate,
            "soil_ph":               soil_ph,
            "soil_moisture_percent": soil_moisture_percent,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Reusable helpers (used by both batch export and SSE streaming)
# ─────────────────────────────────────────────────────────────────────────────

def build_sensors(width, height, num_sensors, base_lat, base_lon,
                   zone_id_start=1, x_start=None, x_end=None,
                   river_mask: np.ndarray | None = None):
    """
    Distribute sensors evenly on a grid within a spatial bounding box.
    Minimum spacing enforced to prevent clustering from water displacement.

    Args:
        river_mask: Optional pre-computed river mask from a CityDataGenerator
                    instance. When supplied, the map image is NOT re-opened,
                    avoiding a redundant disk read on every server startup.
    """
    if x_start is None:
        x_start = 0
    if x_end is None:
        x_end = width

    region_w = x_end - x_start

    # Use the provided mask or load it from disk as a fallback
    dist_to_land = np.zeros((height, width), dtype=np.float32)
    if river_mask is not None:
        dist_to_land = distance_transform_edt(river_mask > 0.5)
    else:
        map_path = SETTINGS.get("map_image", "map.png")
        river_mask = np.zeros((height, width), dtype=np.float32)
        try:
            img = Image.open(map_path).convert("RGB")
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            img_np = np.flipud(np.array(img, dtype=np.float32))
            dist_water = np.sqrt(
                (img_np[:, :, 0] - 167.0) ** 2 +
                (img_np[:, :, 1] - 198.0) ** 2 +
                (img_np[:, :, 2] - 236.0) ** 2
            )
            river_mask = np.where(dist_water < 25.0, 1.0, 0.0).astype(np.float32)
            dist_to_land = distance_transform_edt(river_mask > 0.5)
        except Exception as e:
            print(f"[WARN] build_sensors could not load river mask: {e}")

    # Calculate grid dimensions
    aspect = region_w / height
    n_y = max(1, int(math.sqrt(num_sensors / aspect)))
    n_x = max(1, int(num_sensors / n_y))
    while n_x * n_y < num_sensors:
        if n_x / n_y < aspect:
            n_x += 1
        else:
            n_y += 1

    x_spacing = region_w / (n_x + 1)
    y_spacing = height / (n_y + 1)

    # Minimum pixel spacing to prevent stacking (adjusted from water nudging)
    min_spacing = max(8, int(math.sqrt((region_w * height) / num_sensors) / 2))

    cos_lat = math.cos(math.radians(base_lat))
    lon_scale = 0.00015 / cos_lat

    sensors = []
    placed_positions = set()  # Track placed sensor positions for minimum spacing
    sensor_idx = 0

    for iy in range(n_y):
        for ix in range(n_x):
            if sensor_idx >= num_sensors:
                break

            x = int(x_start + (ix + 1) * x_spacing)
            y = int((iy + 1) * y_spacing)
            x = min(x, width - 1)
            y = min(y, height - 1)

            # If on water, nudge to nearby land
            if river_mask[y, x] > 0.5:
                if dist_to_land[y, x] > 4.0:
                    found_target = False
                    for r in range(1, max(width, height)):
                        candidates = []
                        for dy in range(-r, r + 1):
                            for dx in range(-r, r + 1):
                                if abs(dx) == r or abs(dy) == r:
                                    nx = x + dx
                                    ny = y + dy
                                    if 0 <= nx < width and 0 <= ny < height:
                                        if river_mask[ny, nx] <= 0.5 and dist_to_land[ny, nx] <= 4.0:
                                            candidates.append((nx, ny))
                        if candidates:
                            candidates.sort(key=lambda p: (p[0] - x)**2 + (p[1] - y)**2)
                            x, y = candidates[0]
                            found_target = True
                            break

            # Check minimum spacing from already placed sensors
            pos = (x, y)
            too_close = False
            for placed_x, placed_y in placed_positions:
                dist = math.sqrt((x - placed_x)**2 + (y - placed_y)**2)
                if dist < min_spacing:
                    too_close = True
                    break

            if too_close:
                continue  # Skip this sensor, don't place it

            placed_positions.add(pos)
            lat = base_lat + (y * 0.00015)
            lon = base_lon + (x * lon_scale)
            sensors.append({
                "zone_id": zone_id_start + sensor_idx,
                "x": x, "y": y,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            })
            sensor_idx += 1

    return sensors


def simulate_stream(city, sensors, duration_hours, interval_mins, start_time=None):
    """
    Generator that yields one sensor reading dict at a time.

    Each call to next() produces the next reading from the next sensor in the
    current time-step, then advances to the next step when all sensors are done.

    Args:
        city           : CityDataGenerator instance
        sensors        : list of sensor dicts from build_sensors()
        duration_hours : total simulation duration
        interval_mins  : minutes between each time step
        start_time     : datetime to start from (defaults to now)

    Yields:
        dict: one complete sensor reading
    """
    if start_time is None:
        start_time = datetime.datetime.now().replace(microsecond=0)

    total_minutes = duration_hours * 60
    num_steps = total_minutes // interval_mins

    for step in range(num_steps):
        current_time  = start_time + datetime.timedelta(minutes=step * interval_mins)
        timestamp_str = current_time.isoformat() + "Z"
        unix_timestamp = int(current_time.timestamp())
        hour_of_day   = current_time.hour + current_time.minute / 60.0

        t = step * 0.1
        metrics_grid = city.generate_metrics(t, hour_of_day)

        for sensor in sensors:
            sx, sy = sensor["x"], sensor["y"]

            # ── Raw grid reads ────────────────────────────────────────────────
            veh_count  = round(float(metrics_grid["traffic_vehicles_min"][sy, sx]), 1)
            temp       = round(float(metrics_grid["temperature_c"][sy, sx]), 1)
            humidity   = round(float(metrics_grid["humidity_percent"][sy, sx]), 1)
            precip     = round(float(metrics_grid["precipitation_mm"][sy, sx]), 1)
            wind_spd   = round(float(metrics_grid["wind_speed_kmh"][sy, sx]), 1)
            wind_dir   = round(float(metrics_grid["wind_direction_deg"][sy, sx]), 1)
            pm25       = round(float(metrics_grid["pm25_ug_m3"][sy, sx]), 2)
            no2        = round(float(metrics_grid["no2_ug_m3"][sy, sx]), 2)
            co2        = round(float(metrics_grid["co2_ppm"][sy, sx]), 1)
            w_ph       = round(float(metrics_grid["water_ph"][sy, sx]), 2)
            w_turb     = round(float(metrics_grid["water_turbidity_ntu"][sy, sx]), 1)
            w_flow     = round(float(metrics_grid["water_flow_rate"][sy, sx]), 3)
            s_ph       = round(float(metrics_grid["soil_ph"][sy, sx]), 2)
            s_moist    = round(float(metrics_grid["soil_moisture_percent"][sy, sx]), 1)

            # ── Derived traffic metrics ───────────────────────────────────────
            traffic_density = round((veh_count / 120.0) * 100.0, 1)
            if veh_count > 0:
                avg_speed   = round(max(5.0, 60.0 - traffic_density * 0.5), 1)
                noise_level = round(45.0 + traffic_density * 0.4, 1)
            else:
                avg_speed   = 0.0
                noise_level = round(35.0 + random.uniform(0, 5), 1)

            yield {
                "zone_id":        sensor["zone_id"],
                "timestamp":      timestamp_str,
                "unix_timestamp": unix_timestamp,
                "zone_position": {
                    "latitude":  sensor["lat"],
                    "longitude": sensor["lon"],
                },
                "traffic": {
                    "traffic_density": traffic_density,
                    "vehicle_count":   veh_count,
                    "average_speed":   avg_speed,
                },
                "weather": {
                    "temperature":    temp,
                    "humidity":       humidity,
                    "precipitation":  precip,
                    "wind_speed":     wind_spd,
                    "wind_direction": wind_dir,
                },
                "pollution": {
                    "pm25":      pm25,
                    "no2":       no2,
                    "co2_level": co2,
                },
                "noise": {
                    "noise_level": noise_level,
                },
                "water": {
                    "ph":        w_ph,
                    "turbidity": w_turb,
                    "flow_rate": w_flow,
                },
                "soil": {
                    "ph":       s_ph,
                    "moisture": s_moist,
                },
            }


# ─────────────────────────────────────────────────────────────────────────────
# Batch file-export entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--roads",       default=SETTINGS["roads_image"])
    parser.add_argument("--river",       default=SETTINGS["river_image"])
    parser.add_argument("--elevation",   default=SETTINGS["elevation_image"])
    w, h = get_map_dimensions()
    parser.add_argument("--width",       type=int, default=w)
    parser.add_argument("--height",      type=int, default=h)
    parser.add_argument("--num_sensors", type=int, default=SETTINGS["num_sensors"])
    parser.add_argument("--duration",    type=int, default=SETTINGS["sim_duration_hours"])
    parser.add_argument("--interval",    type=int, default=SETTINGS["reading_interval_mins"])
    parser.add_argument("--out",                   default=SETTINGS["out_file"])
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print(f"Initializing City Data Generator ({args.width}x{args.height})...")
    city = CityDataGenerator(
        road_image_path=args.roads,
        river_image_path=args.river,
        elevation_image_path=args.elevation,
        width=args.width,
        height=args.height,
    )

    sensors = build_sensors(args.width, args.height, args.num_sensors,
                            SETTINGS["base_lat"], SETTINGS["base_lon"])
    print(f"Distributed {len(sensors)} sensors randomly across the grid.")

    total_steps = (args.duration * 60) // args.interval
    print(f"Simulating {args.duration} hours at {args.interval}m intervals ({total_steps} steps).")

    all_readings = []
    step = 0
    for reading in simulate_stream(city, sensors, args.duration, args.interval):
        all_readings.append(reading)
        if len(all_readings) % len(sensors) == 0:
            step += 1
            if step % 10 == 0 or step == total_steps:
                hour = reading["timestamp"][11:16]
                print(f"  Processed step {step}/{total_steps} ({hour} UTC)...")

    print(f"Writing {len(all_readings)} records to {args.out}...")
    with open(args.out, "w") as f:
        json.dump(all_readings, f, indent=2)

    print("Done!")


if __name__ == "__main__":
    main()
