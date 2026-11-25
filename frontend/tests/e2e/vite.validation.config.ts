import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Validation only: serves the working-tree SPA on 127.0.0.1:5199 and proxies /api to the
// running local stack on 127.0.0.1:8080. The proxy presents the stack's public origin so the server's unchanged
// Origin/CSRF check (lbf.auth.sessions.origin_allowed) applies exactly as for the built-in SPA. Never used by the build.
const APP = "http://127.0.0.1:8080";
export default defineConfig({
  root: new URL("../..", import.meta.url).pathname,
  plugins: [react()],
  server: {
    host: "127.0.0.1", port: 5199, strictPort: true,
    proxy: { "/api": { target: APP, configure: (proxy) => { proxy.on("proxyReq", (req) => { if (req.getHeader("origin")) req.setHeader("origin", APP); if (req.getHeader("referer")) req.setHeader("referer", APP + "/"); }); } } },
  },
});
