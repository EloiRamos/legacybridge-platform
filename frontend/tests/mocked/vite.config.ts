import { defineConfig } from "vite";

// Serves an already-built bundle (LBF_MOCKED_DIST) with an explicitly EMPTY proxy. The repository's
// own vite.config.ts proxies /api to 127.0.0.1:8080 (the owner's stack) and `vite preview` inherits that; this does not.
const dist = process.env.LBF_MOCKED_DIST;
if (!dist) throw new Error("LBF_MOCKED_DIST must name the built bundle under test");
export default defineConfig({
  build: { outDir: dist },
  server: { proxy: {} },
  preview: { host: "127.0.0.1", port: 5212, strictPort: true, proxy: {} },
});
