import { defineConfig } from "@playwright/test";

// Runs against the running stack (`make up`); never against external hosts.
export default defineConfig({
  testDir: "tests/e2e",
  globalSetup: "./tests/e2e/global-setup.ts",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  use: { browserName: (process.env.LBF_BROWSER ?? "chromium") as "chromium" | "firefox" | "webkit", baseURL: process.env.LBF_E2E_BASE_URL ?? "http://127.0.0.1:8080", trace: "off" },
  reporter: [["list"]],
});
