import { defineConfig } from "@playwright/test";

// Focused tests: the built bundle, a mocked API, no live stack and no global setup (no sign-in).
// Outside tests/e2e on purpose, so the live suite never collects these files and vice versa.
const out = process.env.LBF_MOCKED_OUT;
if (!out) throw new Error("LBF_MOCKED_OUT must name a scratch output directory");
export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  outputDir: `${out}/test-results`,
  timeout: 30_000,
  retries: 0,
  workers: 1,
  forbidOnly: true,
  use: { browserName: (process.env.LBF_BROWSER ?? "chromium") as "chromium" | "firefox" | "webkit", baseURL: "http://127.0.0.1:5212", trace: "off", timezoneId: "UTC", locale: "en-GB", serviceWorkers: "block" },
  webServer: {
    command: "npx --no-install vite preview --config tests/mocked/vite.config.ts",
    cwd: new URL("../..", import.meta.url).pathname, url: "http://127.0.0.1:5212/", reuseExistingServer: false,
  },
  reporter: [["list"]],
});
