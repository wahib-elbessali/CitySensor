import React, { useEffect, useMemo, useRef, useState } from 'react';
import DeckGL from '@deck.gl/react';

import { useSensorStore } from './store';
import { getLayers, METRIC_CONFIG } from './MapLayers';

const STREAM_URL = 'http://localhost:8001/stream';

// ── Alert color mapping by metric ─────────────────────────────────────────────
const ALERT_COLORS = {
  pm25:        { bg: 'rgba(200,60,60,0.12)',  border: 'rgba(200,60,60,0.25)',  accent: '#f87171', label: 'PM2.5' },
  no2:         { bg: 'rgba(200,100,40,0.12)', border: 'rgba(200,100,40,0.25)', accent: '#fb923c', label: 'NO2' },
  temperature: { bg: 'rgba(220,150,0,0.12)',  border: 'rgba(220,150,0,0.25)',  accent: '#fbbf24', label: 'Temp' },
  flow_rate:   { bg: 'rgba(40,120,220,0.12)', border: 'rgba(40,120,220,0.25)', accent: '#60a5fa', label: 'Flow' },
};
const ALERT_DEFAULT = { bg: 'rgba(255,80,80,0.06)', border: 'rgba(255,80,80,0.12)', accent: '#f87171', label: '⚠' };

// ── Connection status styles ──────────────────────────────────────────────────
const STATUS_STYLES = {
  connecting: { color: '#fbbf24', dot: '#fbbf24', text: 'Connecting...' },
  live:       { color: '#4ade80', dot: '#4ade80', text: 'Live' },
  error:      { color: '#f87171', dot: '#f87171', text: 'Reconnecting...' },
};

export default function App() {
  const {
    sensorData,
    activeMetric,
    viewState,
    setSensorData,
    setViewState,
    setActiveMetric
  } = useSensorStore();

  const [isPaused, setIsPaused]           = useState(false);
  const [currentTime, setCurrentTime]     = useState('—');
  const [zoneCount, setZoneCount]         = useState(0);
  const [showSensors, setShowSensors]     = useState(true);
  const [alerts, setAlerts]               = useState([]);
  const [connStatus, setConnStatus]       = useState('connecting');

  const workerRef    = useRef(null);
  const isPausedRef  = useRef(isPaused);

  // Sync isPaused state to ref so the worker message handler can access it
  // without restarting the worker on every toggle
  useEffect(() => {
    isPausedRef.current = isPaused;
  }, [isPaused]);

  useEffect(() => {
    workerRef.current = new Worker(new URL('./worker.js', import.meta.url), {
      type: 'module'
    });

    workerRef.current.onmessage = (e) => {
      if (e.data.type === 'CONNECTION_STATUS') {
        setConnStatus(e.data.status);
      } else if (e.data.type === 'UPDATE_DATA') {
        if (!isPausedRef.current) {
          setSensorData(e.data.payload);
          setZoneCount(e.data.payload.features.length);
          if (e.data.timestamp) {
            setCurrentTime(e.data.timestamp.slice(0, 19).replace('T', ' '));
          }
        }
      } else if (e.data.type === 'NEW_ALERT') {
        if (!isPausedRef.current) {
          setAlerts((prev) => {
            const updated = [e.data.payload, ...prev];
            return updated.slice(0, 8);
          });
        }
      }
    };

    workerRef.current.postMessage({ type: 'CONNECT', url: STREAM_URL });

    return () => {
      if (workerRef.current) {
        workerRef.current.postMessage({ type: 'DISCONNECT' });
        workerRef.current.terminate();
      }
    };
  }, [setSensorData]);

  const layers = useMemo(
    () => getLayers(sensorData, activeMetric, showSensors),
    [sensorData, activeMetric, showSensors]
  );

  const activeConfig = METRIC_CONFIG[activeMetric] || METRIC_CONFIG['pm25'];

  const getTooltip = ({ object }) => {
    if (!object || !object.properties) return null;
    const val = (object.properties[activeMetric] || 0).toFixed(2);
    return `Sensor ${object.properties.zone_id} • ${activeMetric.toUpperCase()}: ${val} ${activeConfig.unit}`;
  };

  // Grouped metrics for the UI Side Panel
  const metricGroups = {
    'Pollution':    { pm25: 'PM2.5', no2: 'NO2', co2_level: 'CO2' },
    'Weather':      { temperature: 'Temp (°C)', humidity: 'Humidity (%)', precipitation: 'Precip (mm)', wind_speed: 'Wind Spd', wind_direction: 'Wind Dir' },
    'Traffic':      { traffic_density: 'Traffic Density', vehicle_count: 'Vehicles', average_speed: 'Avg Speed' },
    'Environment':  { noise_level: 'Noise (dB)', soil_ph: 'Soil pH', soil_moisture: 'Soil Moist (%)' },
    'Water':        { water_ph: 'Water pH', water_turbidity: 'Turbidity', water_flow_rate: 'Flow Rate' },
  };

  const statusStyle = STATUS_STYLES[connStatus] || STATUS_STYLES.connecting;

  return (
    <div style={{ width: '100vw', height: '100vh', position: 'relative', overflow: 'hidden', background: '#080c18' }}>

      <DeckGL
        layers={layers}
        viewState={viewState}
        onViewStateChange={({ viewState }) => setViewState(viewState)}
        controller={true}
        getTooltip={getTooltip}
      />

      {/* ── Connection status banner ──────────────────────────────────────────── */}
      {connStatus !== 'live' && (
        <div style={{
          position: 'absolute', top: 0, left: 0, right: 0,
          background: connStatus === 'error' ? 'rgba(200,40,40,0.85)' : 'rgba(180,120,0,0.85)',
          backdropFilter: 'blur(8px)',
          color: '#fff', textAlign: 'center', fontSize: '13px',
          padding: '8px 16px', zIndex: 100,
          fontFamily: '"Inter", sans-serif', fontWeight: 600,
          letterSpacing: '0.03em',
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px'
        }}>
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: statusStyle.dot, display: 'inline-block', flexShrink: 0 }} />
          {connStatus === 'connecting'
            ? '⏳ Connecting to city stream at localhost:8001 — make sure server_a.py is running.'
            : '⚠ Connection lost. Reconnecting...'}
        </div>
      )}

      {/* ── Top Panel - Playback Controls ────────────────────────────────────── */}
      <div style={{
        position: 'absolute', top: connStatus !== 'live' ? 44 : 20, right: 20,
        background: 'rgba(10, 16, 32, 0.85)', backdropFilter: 'blur(10px)',
        padding: '12px 18px', borderRadius: '10px', color: '#e2e8f0',
        fontFamily: '"Inter", sans-serif', border: '1px solid rgba(80,120,255,0.18)',
        boxShadow: '0 4px 6px rgba(0,0,0,0.3)', zIndex: 10,
        display: 'flex', gap: '16px', alignItems: 'center', fontSize: '13px',
        transition: 'top 0.2s ease'
      }}>
        {/* Connection dot */}
        <span style={{
          width: 8, height: 8, borderRadius: '50%',
          background: statusStyle.dot,
          display: 'inline-block', flexShrink: 0,
          boxShadow: connStatus === 'live' ? '0 0 6px #4ade80' : 'none'
        }} title={statusStyle.text} />
        <button
          id="btn-pause-play"
          onClick={() => setIsPaused(!isPaused)}
          style={{
            background: 'rgba(79,124,255,0.2)', border: '1px solid rgba(79,124,255,0.3)',
            color: '#7eb3ff', padding: '6px 12px', borderRadius: '6px', cursor: 'pointer',
            fontFamily: 'inherit', fontSize: '12px'
          }}
        >
          {isPaused ? '▶ Play' : '⏸ Pause'}
        </button>
        <span style={{ color: '#64748b' }}>🕒 {currentTime}</span>
        <span style={{ color: '#64748b' }}>📍 {zoneCount} zones</span>
      </div>

      {/* ── Left Panel - Metrics ─────────────────────────────────────────────── */}
      <div style={{
        position: 'absolute', top: connStatus !== 'live' ? 44 : 20, left: 20,
        background: 'rgba(10, 16, 32, 0.85)', backdropFilter: 'blur(10px)',
        padding: '20px', borderRadius: '12px', color: '#e2e8f0',
        fontFamily: '"Inter", sans-serif', border: '1px solid rgba(80,120,255,0.18)',
        boxShadow: '0 4px 6px rgba(0,0,0,0.3)', zIndex: 10,
        maxHeight: '90vh', overflowY: 'auto',
        minWidth: '220px',
        scrollbarWidth: 'thin',
        transition: 'top 0.2s ease'
      }}>
        <h2 style={{ margin: '0 0 4px 0', fontSize: '18px', color: '#fff' }}>🏙️ City Sensors</h2>
        <p style={{ margin: '0 0 14px 0', fontSize: '12px', color: '#64748b' }}>
          Active Sensors: {sensorData.features.length}
        </p>

        {/* Toggle sensor points */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px', paddingBottom: '12px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
          <span style={{ fontSize: '12px', color: '#94a3b8' }}>Sensor Locations:</span>
          <button
            id="btn-toggle-sensors"
            onClick={() => setShowSensors(!showSensors)}
            style={{
              background: showSensors ? 'rgba(79,124,255,0.2)' : 'rgba(255,255,255,0.05)',
              border: `1px solid ${showSensors ? 'rgba(79,124,255,0.4)' : 'rgba(255,255,255,0.1)'}`,
              color: showSensors ? '#cbd5e1' : '#94a3b8',
              padding: '4px 10px', borderRadius: '6px', cursor: 'pointer',
              fontSize: '11px', fontWeight: '600', transition: 'all 0.2s', outline: 'none'
            }}
          >
            {showSensors ? 'VISIBLE' : 'HIDDEN'}
          </button>
        </div>

        {/* Metric groups */}
        {Object.entries(metricGroups).map(([groupName, metrics]) => (
          <div key={groupName} style={{ marginBottom: '16px' }}>
            <h3 style={{ margin: '0 0 8px 0', fontSize: '11px', textTransform: 'uppercase', color: '#64748b', letterSpacing: '0.05em' }}>
              {groupName}
            </h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {Object.entries(metrics).map(([key, label]) => (
                <button
                  key={key}
                  id={`btn-metric-${key}`}
                  onClick={() => setActiveMetric(key)}
                  style={btnStyle(activeMetric === key)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        ))}

        {/* ── Color Legend ─────────────────────────────────────────────────── */}
        <div style={{ marginTop: '8px', paddingTop: '14px', borderTop: '1px solid rgba(255,255,255,0.06)' }}>
          <h3 style={{ margin: '0 0 8px 0', fontSize: '11px', textTransform: 'uppercase', color: '#64748b', letterSpacing: '0.05em' }}>
            Color Scale
          </h3>
          <ColorLegend config={activeConfig} metric={activeMetric} />
        </div>
      </div>

      {/* ── Bottom Right Panel - Real-time Alerts ───────────────────────────── */}
      <div style={{
        position: 'absolute', bottom: 20, right: 20,
        background: 'rgba(10, 16, 32, 0.85)', backdropFilter: 'blur(10px)',
        padding: '16px 20px', borderRadius: '12px', color: '#e2e8f0',
        fontFamily: '"Inter", sans-serif', border: '1px solid rgba(255,80,80,0.15)',
        boxShadow: '0 4px 15px rgba(0,0,0,0.4)', zIndex: 10,
        minWidth: '300px', maxWidth: '380px', maxHeight: '40vh', overflowY: 'auto',
        scrollbarWidth: 'thin'
      }}>
        <h3 style={{ margin: '0 0 10px 0', fontSize: '13px', color: '#ff6b6b', display: 'flex', alignItems: 'center', gap: '6px', letterSpacing: '0.05em', textTransform: 'uppercase', fontWeight: '700' }}>
          🚨 Real-Time Server Alerts
        </h3>
        {alerts.length === 0 ? (
          <p style={{ margin: 0, fontSize: '12px', color: '#64748b', fontStyle: 'italic' }}>
            Monitoring stream… No active threshold breaches.
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {alerts.map((alert, idx) => {
              const style = ALERT_COLORS[alert.metric] || ALERT_DEFAULT;
              return (
                <div
                  key={`${alert.zone_id}-${alert.metric}-${alert.timestamp}-${idx}`}
                  style={{
                    background: style.bg,
                    border: `1px solid ${style.border}`,
                    borderLeft: `3px solid ${style.accent}`,
                    borderRadius: '6px',
                    padding: '8px 10px',
                    fontSize: '11px',
                    lineHeight: '1.4'
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px', fontWeight: '600' }}>
                    <span style={{ color: style.accent }}>
                      Zone {alert.zone_id} • {alert.metric.toUpperCase()}
                    </span>
                    <span style={{ color: '#64748b' }}>{alert.timestamp.slice(11, 19)}</span>
                  </div>
                  <div style={{ color: '#cbd5e1' }}>
                    Reading: <span style={{ color: style.accent, fontWeight: 'bold' }}>{alert.value.toFixed(1)}</span>
                    {' '}(threshold {alert.threshold})
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

    </div>
  );
}

// ── Color Legend Component ─────────────────────────────────────────────────────
function ColorLegend({ config, metric }) {
  if (!config) return null;

  // Build a CSS gradient from the color range (skip fully transparent first stop)
  const stops = config.colors
    .map(([r, g, b, a]) => `rgba(${r},${g},${b},${Math.round(a / 255 * 100) / 100})`)
    .join(', ');

  return (
    <div>
      <div style={{
        height: 10, borderRadius: 5,
        background: `linear-gradient(to right, ${stops})`,
        marginBottom: 6,
      }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: '#94a3b8' }}>
        <span>{config.minVal} {config.unit}</span>
        <span>{Math.round((config.minVal + config.maxVal) / 2)} {config.unit}</span>
        <span>{config.maxVal} {config.unit}</span>
      </div>
    </div>
  );
}

function btnStyle(isActive) {
  return {
    background: isActive ? 'rgba(79,124,255,0.2)' : 'transparent',
    color: isActive ? '#7eb3ff' : '#94a3b8',
    border: `1px solid ${isActive ? 'rgba(79,124,255,0.5)' : 'rgba(255,255,255,0.1)'}`,
    padding: '6px 10px', borderRadius: '6px', cursor: 'pointer', textAlign: 'left',
    fontSize: '12px', transition: 'all 0.2s ease', fontWeight: isActive ? '600' : '400'
  };
}