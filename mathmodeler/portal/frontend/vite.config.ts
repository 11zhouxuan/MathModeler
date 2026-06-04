import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Build to ../frontend/out so the portal Docker image picks it up unchanged
// (the backend serves STATIC_DIR=/app/static <- portal/frontend/out).
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'out', emptyOutDir: true },
  server: {
    // Local dev: proxy API calls to the FastAPI server on :8080.
    proxy: {
      '/api': 'http://localhost:8080',
      '/healthz': 'http://localhost:8080',
    },
  },
});
