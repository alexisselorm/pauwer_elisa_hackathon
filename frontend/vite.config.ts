import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The console calls the API at /api/ui (same origin). In dev, Vite proxies it; in compose,
// nginx does (frontend/nginx.conf).
export default defineConfig({
  plugins: [react()],
  // One page app; Cytoscape (+ fcose) is most of the bundle and is needed on the first view.
  build: { chunkSizeWarningLimit: 1000 },
  server: {
    proxy: {
      '/api': { target: process.env.API_URL ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
})
