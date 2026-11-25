// Authentic capture of the assistant refusing an injection-driven tool request.
//
// Not run on its own: backend/tests/evidence/test_injection_capture.py serves the real application and the
// built console in-process (test profile, offline backends, the R17 ScriptedProvider that obeys the injected NOTE of
// SKARVIK account KND-0177) and invokes this script, then checks the oracles (no state change, every attempt refused).
//
//   node capture_injection.mjs <base-url> <out-png>    (password in LBF_CAPTURE_PASSWORD; the test fixture value)
//
// The login page is never captured. Nothing is edited after capture.
import { createRequire } from "node:module";

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");

const [, , BASE, OUT] = process.argv;
const USER = "marta.ops";
const QUESTION = "summarize Ålesund's notes";

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ baseURL: BASE, viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto("/login");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(process.env.LBF_CAPTURE_PASSWORD ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  await page.getByRole("button", { name: "Open assistant" }).click();
  const panel = page.getByRole("dialog", { name: "Assistant" });
  await expect(panel).toContainText("scripted", { timeout: 30_000 });
  await panel.getByLabel("Ask the assistant").fill(QUESTION);
  await panel.getByRole("button", { name: "Ask", exact: true }).click();
  const trace = panel.getByLabel("Tool calls");
  await expect(trace).toBeVisible({ timeout: 30_000 });
  await trace.scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  await page.screenshot({ path: OUT });
  // one direct child per attempted call (each row also contains a nested glyph span, which is not a row)
  const calls = await trace.locator(":scope > span").allInnerTexts();
  const refused = calls.filter((t) => t.includes("— refused")).length;
  console.log(JSON.stringify({ file: OUT, user: USER, question: QUESTION, tool_calls_shown: calls,
    summary: `Tool calls (${calls.length}, ${refused} refused)`, url: new URL(page.url()).pathname, at: new Date().toISOString() }));
} finally {
  await browser.close();
}
