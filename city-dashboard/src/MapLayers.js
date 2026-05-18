import { HeatmapLayer } from '@deck.gl/aggregation-layers';
import { ScatterplotLayer, BitmapLayer } from '@deck.gl/layers';

// Define Premium Color Palettes for different metric types
const PALETTES = {
  ALERT: [
    [0, 255, 0, 0],       // Green (Transparent)
    [150, 255, 0, 100],   // Yellow-Green
    [255, 255, 0, 150],   // Yellow
    [255, 150, 0, 200],   // Orange
    [255, 0, 0, 230],     // Red
    [139, 0, 0, 255]      // Dark Red
  ],
  WATER: [
    [167, 198, 236, 0],   // Light Blue (Transparent)
    [120, 180, 240, 100],
    [70, 150, 230, 150],
    [20, 120, 210, 200],
    [0, 80, 180, 230],
    [0, 40, 150, 255]     // Deep Blue
  ],
  TEMP: [
    [0, 200, 255, 0],     // Cyan (Transparent)
    [0, 255, 150, 100],   // Light Green
    [150, 255, 0, 150],   // Yellow-Green
    [255, 200, 0, 200],   // Orange
    [255, 100, 0, 230],   // Red-Orange
    [255, 0, 0, 255]      // Red
  ],
  EARTH: [
    [200, 230, 200, 0],   // Light Green
    [150, 200, 150, 100],
    [180, 160, 100, 150],
    [150, 120, 80, 200],
    [120, 80, 50, 230],
    [80, 50, 30, 255]     // Brown
  ]
};

// Define configurations with normalization max values so all heatmaps scale identically.
// Exported so App.jsx can render a matching color legend.
export const METRIC_CONFIG = {
  // Traffic (localized to roads)
  traffic_density: { minVal: 0,   maxVal: 100, intensity: 1.2, radiusPixels: 30, colors: PALETTES.ALERT,  unit: '%' },
  vehicle_count:   { minVal: 0,   maxVal: 120, intensity: 1.0, radiusPixels: 30, colors: PALETTES.ALERT,  unit: 'veh' },
  average_speed:   { minVal: 0,   maxVal: 60,  intensity: 1.0, radiusPixels: 30, colors: PALETTES.TEMP,   unit: 'km/h' },

  // Weather (global city-wide metrics, lower intensity to prevent aggregation blowout)
  temperature:     { minVal: 8,   maxVal: 28,  intensity: 0.5, radiusPixels: 40, colors: PALETTES.TEMP,   unit: '°C' },
  humidity:        { minVal: 25,  maxVal: 85,  intensity: 0.5, radiusPixels: 40, colors: PALETTES.WATER,  unit: '%' },
  precipitation:   { minVal: 0,   maxVal: 8,   intensity: 0.6, radiusPixels: 35, colors: PALETTES.WATER,  unit: 'mm' },
  wind_speed:      { minVal: 0,   maxVal: 45,  intensity: 0.5, radiusPixels: 45, colors: PALETTES.ALERT,  unit: 'km/h' },
  wind_direction:  { minVal: 0,   maxVal: 360, intensity: 0.4, radiusPixels: 35, colors: PALETTES.TEMP,   unit: '°' },

  // Pollution (localized to roads but drifts)
  pm25:            { minVal: 5,   maxVal: 60,  intensity: 1.0, radiusPixels: 35, colors: PALETTES.ALERT,  unit: 'µg/m³' },
  no2:             { minVal: 5,   maxVal: 50,  intensity: 1.0, radiusPixels: 35, colors: PALETTES.ALERT,  unit: 'µg/m³' },
  co2_level:       { minVal: 400, maxVal: 550, intensity: 0.8, radiusPixels: 35, colors: PALETTES.ALERT,  unit: 'ppm' },

  // Noise
  noise_level:     { minVal: 35,  maxVal: 80,  intensity: 1.0, radiusPixels: 35, colors: PALETTES.ALERT,  unit: 'dB' },

  // Water (localized to rivers)
  water_ph:        { minVal: 6.5, maxVal: 8.0, intensity: 1.2, radiusPixels: 35, colors: PALETTES.WATER,  unit: 'pH' },
  water_turbidity: { minVal: 0,   maxVal: 60,  intensity: 1.2, radiusPixels: 35, colors: PALETTES.EARTH,  unit: 'NTU' },
  water_flow_rate: { minVal: 0,   maxVal: 3,   intensity: 1.2, radiusPixels: 35, colors: PALETTES.WATER,  unit: 'm³/s' },

  // Soil
  soil_ph:         { minVal: 5.0, maxVal: 8.0, intensity: 0.6, radiusPixels: 40, colors: PALETTES.EARTH,  unit: 'pH' },
  soil_moisture:   { minVal: 10,  maxVal: 90,  intensity: 0.6, radiusPixels: 40, colors: PALETTES.WATER,  unit: '%' },
};

export function getLayers(data, activeMetric, showSensors = true) {
  const currentConfig = METRIC_CONFIG[activeMetric] || METRIC_CONFIG['pm25'];

  // Filter out sensors that have no meaningful data for the active metric.
  const relevantFeatures = data.features.filter(
    d => (d.properties[activeMetric] || 0) > 0
  );

  const layers = [
    new BitmapLayer({
      id: 'static-map-layer',
      bounds: [-6.18, 32.49, -5.9721125, 32.61405],
      image: '/map.png',
      opacity: 0.7
    }),

    // Use a stable id so deck.gl updates props in-place (smooth WebGL transitions)
    // instead of destroying and recreating the layer on every metric switch.
    new HeatmapLayer({
      id: 'heatmap',
      data: relevantFeatures,
      pickable: false,
      getPosition: d => d.geometry.coordinates,
      // Normalize weight (0 to 10 scale) so intensity works consistently across metrics
      getWeight: d => {
        const val = d.properties[activeMetric] || 0;
        const min = currentConfig.minVal !== undefined ? currentConfig.minVal : 0;
        const max = currentConfig.maxVal;
        const norm = (val - min) / (max - min);
        return Math.max(0, Math.min(1, norm)) * 10;
      },
      radiusPixels: currentConfig.radiusPixels,
      intensity: currentConfig.intensity,
      threshold: 0.05,
      colorRange: currentConfig.colors,
      transitions: {
        intensity: 300
      }
    })
  ];

  if (showSensors) {
    layers.push(
      new ScatterplotLayer({
        id: 'sensor-points',
        data: data.features,
        getPosition: d => d.geometry.coordinates,
        getFillColor: [255, 255, 255, 200],
        getRadius: 10,
        pickable: true,
        radiusMinPixels: 2,
        radiusMaxPixels: 5
      })
    );
  }

  return layers;
}