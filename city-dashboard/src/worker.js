let eventSource = null;
let currentFeatures = new Map();
let lastTimestamp = null;

// ── Connection state ──────────────────────────────────────────────────────────

function postConnectionStatus(status) {
  // status: 'connecting' | 'live' | 'error'
  self.postMessage({ type: 'CONNECTION_STATUS', status });
}

// ── Stream ────────────────────────────────────────────────────────────────────

self.onmessage = (e) => {
  const { type, url } = e.data;
  if (type === 'CONNECT' || type === 'START_STREAM') {
    connectStream(url);
  } else if (type === 'DISCONNECT') {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }
};

function connectStream(url) {
  if (eventSource) {
    eventSource.close();
  }

  postConnectionStatus('connecting');
  eventSource = new EventSource(url);

  eventSource.addEventListener('connected', () => {
    postConnectionStatus('live');
  });

  eventSource.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);

      // Use the explicit "event": "alert" discriminator added by redis_store.py
      if (payload.event === 'alert') {
        self.postMessage({
          type: 'NEW_ALERT',
          payload: payload
        });
        return;
      }

      if (!payload.batch) return;

      payload.batch.forEach(reading => {
        const feature = {
          type: 'Feature',
          properties: {
            zone_id: reading.zone_id,
            timestamp: reading.timestamp,
            traffic_density: reading.traffic.traffic_density,
            vehicle_count: reading.traffic.vehicle_count,
            average_speed: reading.traffic.average_speed,
            temperature: reading.weather.temperature,
            humidity: reading.weather.humidity,
            precipitation: reading.weather.precipitation,
            wind_speed: reading.weather.wind_speed,
            wind_direction: reading.weather.wind_direction,
            pm25: reading.pollution.pm25,
            no2: reading.pollution.no2,
            co2_level: reading.pollution.co2_level,
            noise_level: reading.noise.noise_level,
            water_ph: reading.water.ph,
            water_turbidity: reading.water.turbidity,
            water_flow_rate: reading.water.flow_rate,
            soil_ph: reading.soil.ph,
            soil_moisture: reading.soil.moisture
          },
          geometry: {
            type: 'Point',
            coordinates: [
              reading.zone_position.longitude,
              reading.zone_position.latitude
            ]
          }
        };
        currentFeatures.set(reading.zone_id, feature);
      });

      // Only move the time forward, never backward (guards against async batch ordering)
      const sortedBatch = [...payload.batch].sort((a, b) =>
        new Date(b.timestamp) - new Date(a.timestamp)
      );
      const batchLatest = sortedBatch[0].timestamp;
      if (!lastTimestamp || new Date(batchLatest) > new Date(lastTimestamp)) {
        lastTimestamp = batchLatest;
      }

      self.postMessage({
        type: 'UPDATE_DATA',
        payload: {
          type: 'FeatureCollection',
          features: Array.from(currentFeatures.values())
        },
        timestamp: lastTimestamp
      });
    } catch (err) {
      console.error('Worker SSE parse error:', err);
    }
  };

  eventSource.onerror = () => {
    postConnectionStatus('error');
    console.error('SSE connection error. Reconnecting in 3s...');
    eventSource.close();
    setTimeout(() => connectStream(url), 3000);
  };
}