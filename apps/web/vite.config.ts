import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server: bind to all interfaces, accept any host (sandbox/preview proxies),
// and proxy /api to the FastAPI backend so the browser never needs the API URL.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    allowedHosts: true,
    proxy: { "/api": { target: process.env.OT_API_URL || "http://127.0.0.1:8000", changeOrigin: true } },
  },
  preview: { host: "0.0.0.0", port: 5173, allowedHosts: true, proxy: { "/api": { target: process.env.OT_API_URL || "http://127.0.0.1:8000", changeOrigin: true } } },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions: { output: { manualChunks: { maplibre: ["maplibre-gl"], react: ["react", "react-dom", "zustand"] } } },
  },
});
