/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API and the socket to a running JARVIS (start it with
// JARVIS_UI_DEV_ORIGIN=http://localhost:5173 so its Origin check accepts this page). changeOrigin
// rewrites Host to 127.0.0.1:<port>, which the server's anti-rebinding Host check requires.
const jarvisPort = process.env.JARVIS_UI_PORT || "8765";
const jarvis = `http://127.0.0.1:${jarvisPort}`;

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs, so the built page works however the server mounts it.
  base: "./",
  server: {
    proxy: {
      "/api": { target: jarvis, changeOrigin: true },
      "/ws": { target: jarvis, ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // The page is served under a strict CSP: no inline module-preload polyfill script.
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/three/")) return "three";
          if (id.includes("node_modules/@react-three/")) return "r3f";
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/test/**/*.test.ts", "src/test/**/*.test.tsx"],
  },
});
