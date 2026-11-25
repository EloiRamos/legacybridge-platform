import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The SPA is served by the application itself in production (same origin, no CORS).
// In development, API calls are proxied to the local app on 127.0.0.1:8080.
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", proxy: { "/api": "http://127.0.0.1:8080" } },
  build: { outDir: "dist", sourcemap: false },
  test: { include: ["src/**/*.test.ts"] },
});
