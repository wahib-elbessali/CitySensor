import { create } from 'zustand';

export const useSensorStore = create((set) => ({
  sensorData: { type: 'FeatureCollection', features: [] },
  activeMetric: 'pm25',
  
  // Center over actual map (1169x827 grid, base_lat=32.49, base_lon=-6.18)
  // Calculate map center: lat from bottom to top, lon left to right
  viewState: {
    longitude: -6.0925,  // Centered horizontally
    latitude: 32.5519,   // Centered vertically
    zoom: 11.5,
    pitch: 0,
    bearing: 0
  },
  
  setSensorData: (data) => set({ sensorData: data }),
  setActiveMetric: (metric) => set({ activeMetric: metric }),
  setViewState: (viewState) => set({ viewState })
}));