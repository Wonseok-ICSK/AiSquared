import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Dev server on 5174 (5173 is used by another app on this machine), proxying /api to the
 * FastAPI backend. Set API_TARGET to point the dev/preview proxy elsewhere (e.g. the
 * throwaway mock API in dev-mock/). The build goes to dist/, which FastAPI serves at /.
 */
const apiTarget = process.env.API_TARGET ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    strictPort: true,
    proxy: { "/api": { target: apiTarget, changeOrigin: false } },
  },
  preview: {
    port: 5175,
    strictPort: true,
    proxy: { "/api": { target: apiTarget, changeOrigin: false } },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
