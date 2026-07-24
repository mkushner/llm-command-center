import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: `npm run dev` (5173) proxies API + WS to the Python backend (`llm-cc --web`, 7420).
// Prod: `npm run build` → dist/, served directly by the Python server.
export default defineConfig({
  base: "/",
  plugins: [react()],
  // Build straight into the Python package so it ships inside the wheel.
  build: { outDir: "../src/llm_cc/web/static", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:7420",
      "/ws": { target: "ws://127.0.0.1:7420", ws: true },
    },
  },
});
