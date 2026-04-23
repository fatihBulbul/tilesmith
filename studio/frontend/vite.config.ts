import { defineConfig } from "vite";

// Dev server: bridge'in static mount'u yerine frontend dev server çalışırken
// /state, /sprite/*, /ws gibi istekler bridge'e proxy'lenir.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/state": "http://127.0.0.1:3024",
      "/open":  "http://127.0.0.1:3024",
      "/health": "http://127.0.0.1:3024",
      "/sprite": "http://127.0.0.1:3024",
      "/ws":    { target: "ws://127.0.0.1:3024", ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
