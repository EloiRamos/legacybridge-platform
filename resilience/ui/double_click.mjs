// R08 variant B: double-click the confirmation button of the approval dialog in the real console (Playwright,
// Chromium). The browser reuses the harness's existing session (cookie passed in the environment, never printed)
// so no additional sign-in counts against the login rate limit. Prints the number of approve requests the page sent.
import { createRequire } from "node:module";

const require = createRequire(new URL("../../frontend/package.json", import.meta.url));
const { chromium } = require("@playwright/test");

const [, , base, opId] = process.argv;
const cookie = process.env.LBF_RES_SESSION_COOKIE;
if (!base || !opId || !cookie) {
  console.error("usage: LBF_RES_SESSION_COOKIE=… node double_click.mjs <base-url> <operation-id>");
  process.exit(2);
}
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ baseURL: base });
  await context.addCookies([{ name: "lbf_session", value: cookie, url: base }]);
  const page = await context.newPage();
  const sent = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith(`/api/v1/operations/${opId}/approve`)) sent.push(r.url());
  });
  await page.goto(`/operations/${opId}`);
  await page.getByRole("button", { name: "Approve…" }).click();
  await page.getByRole("button", { name: "Approve", exact: true }).dblclick();
  await page.waitForTimeout(2000);
  console.log(JSON.stringify({ approve_requests_sent: sent.length }));
} finally {
  await browser.close();
}
