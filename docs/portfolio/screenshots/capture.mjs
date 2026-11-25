// Authentic portfolio screenshots: Playwright/Chromium against the running isolated stack.
//
//   tools/isolated.sh up
//   node docs/portfolio/screenshots/capture.mjs http://127.0.0.1:8088 deploy/secrets/lbf-demo-credentials.txt 58195 58190
//
// Every state shown is produced through the console, its API and the simulators' own fault controls: no image is
// edited. Personas sign in through the login page with the git-ignored generated credentials; the login page itself is
// never captured. Writes the PNGs next to this script and prints a JSON capture log.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../../../frontend/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");

const [, , BASE = "http://127.0.0.1:8088", CREDENTIALS = "deploy/secrets/lbf-demo-credentials.txt", SKARVIK = "58195", HALDEN = "58190"] = process.argv;
const OUT = fileURLToPath(new URL("./", import.meta.url));
const CREDS = CREDENTIALS;
const password = (u) => readFileSync(CREDS, "utf-8").split("\n").find((l) => l.startsWith(u + "\t")).split("\t")[1];
const log = [];

const browser = await chromium.launch();
async function signIn(username) {
  const context = await browser.newContext({ baseURL: BASE, viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password(username));
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  return page;
}
async function api(page, method, path, body) {
  const s = await (await page.request.get("/api/v1/auth/session")).json();
  const r = await page.request.fetch(path, { method, data: body, headers: { Origin: BASE, "X-CSRF-Token": s.csrf_token } });
  if (!r.ok()) throw new Error(`${method} ${path} -> ${r.status()} ${await r.text()}`);
  return r.json().catch(() => ({}));
}
const heading = (page) => page.getByRole("heading", { level: 1 });
async function approve(page, id) {
  await page.goto(`/operations/${id}`);
  await page.getByRole("button", { name: "Approve…" }).click();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
}
async function shot(page, name, scene, fullPage = true) {
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}${name}`, fullPage, animations: "disabled" });
  log.push({ file: name, scene, url: new URL(page.url()).pathname, at: new Date().toISOString() });
}
async function draft(page, backend, account, sku, qty) {
  const r = await api(page, "POST", "/api/v1/operations", { backend_id: backend, native_customer_id: account,
    lines: [{ canonical_sku: sku, quantity: qty }] });
  await api(page, "POST", `/api/v1/operations/${r.operation_id}/submit`, { expected_version: 1 });
  return r.operation_id;
}
async function waitState(page, id, states, ms = 60_000) {
  const end = Date.now() + ms;
  for (;;) {
    const d = await (await page.request.get(`/api/v1/operations/${id}`)).json();
    if (states.includes(d.operation.state)) return d.operation.state;
    if (Date.now() > end) throw new Error(`${id} stayed ${d.operation.state}`);
    await page.waitForTimeout(1000);
  }
}

try {
  const tomas = await signIn("tomas.admin"), daniel = await signIn("daniel.sales"), priya = await signIn("priya.sales");
  const marta = await signIn("marta.ops"), sanna = await signIn("sanna.inventory"), helena = await signIn("helena.compliance");
  const simulator = (fault, value) => fetch(`http://127.0.0.1:${SKARVIK}/_sim/faults`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fault, value, count: 1 }) });
  const arm = (preset, count = 1) => api(tomas, "POST", "/api/v1/demo/faults", { preset, count });
  const accept = async (page, id) => { const d = await (await page.request.get(`/api/v1/operations/${id}`)).json(); const v = d.versions.at(-1);
    await api(page, "POST", `/api/v1/operations/${id}/approve`, { version: v.version, content_hash: v.content_hash, override_reason: null }); };
  await simulator("stale_extracts", false);
  await fetch(`http://127.0.0.1:${HALDEN}/_sim/reset`, { method: "POST", body: new URLSearchParams({ rate_limit: "100000" }) });

  // confirmed outcomes on both systems
  for (const [backend, account, sku, qty] of [["halden", "10233", "BW-1001", 12], ["skarvik", "KND-0215", "BW-1002", 4], ["halden", "52290", "BW-1004", 3], ["skarvik", "KND-0203", "BW-1001", 6]]) {
    const id = await draft(daniel, backend, account, sku, qty);
    await accept(priya, id);
    await waitState(marta, id, ["CONFIRMED"], 90_000);
  }
  // HALDEN: committed, then answered 500, and the read-back listing fails: a person escalates it
  const escalated = await draft(daniel, "halden", "00417", "BW-1001", 10);
  await arm("halden_commit_then_500");
  await arm("halden_listing_500x3", 3);
  await accept(priya, escalated);
  await waitState(marta, escalated, ["UNKNOWN"]);
  for (let i = 0; i < 20; i++) {
    try { await api(marta, "POST", `/api/v1/operations/${escalated}/escalate`, { note: "Customer service is phoning the HALDEN desk." }); break; } catch { await marta.waitForTimeout(400); }
  }
  await waitState(marta, escalated, ["ESCALATED"]);

  // SKARVIK: acknowledgement lost, extracts held back: UNKNOWN with an open redelivery window
  await simulator("stale_extracts", true);
  const unknown = await draft(daniel, "skarvik", "KND-0081", "BW-1001", 10);
  await arm("skarvik_drop_ack");
  await accept(priya, unknown);
  await waitState(marta, unknown, ["UNKNOWN"], 90_000);
  await marta.goto(`/operations/${unknown}`);
  await expect(heading(marta)).toContainText("Outcome unknown");
  await expect(marta.getByRole("button", { name: "Authorize redelivery…" })).toBeVisible({ timeout: 30_000 });
  await shot(marta, "05b-unknown-order-detail.png", "SKARVIK order, acknowledgement lost: observed, not known, and the actions available to this person", false);
  // an order waiting for a second person, and one not yet acknowledged
  const ready = await draft(daniel, "halden", "00417", "BW-1003", 2);
  await draft(priya, "halden", "10233", "BW-1007", 1);
  await arm("skarvik_ack_delay_30s");
  const awaiting = await draft(daniel, "skarvik", "KND-0215", "BW-1001", 5);
  await accept(priya, awaiting);
  await waitState(marta, awaiting, ["AWAITING_ACK"]);
  await marta.goto("/");
  await expect(marta.getByRole("heading", { name: "The bridge" })).toBeVisible();
  await expect(marta.getByText("in flight")).toBeVisible();
  await marta.waitForTimeout(1500);
  await shot(marta, "01-dashboard.png", "Overview: what needs a person, both systems, recent outcomes");
  await marta.goto(`/operations/${awaiting}`);
  await expect(marta.getByText("Acknowledgement deadline", { exact: true })).toBeVisible();
  await shot(marta, "06-skarvik-awaiting-ack.png", "SKARVIK order delivered to the inbox, awaiting acknowledgement (deadline shown)", false);
  await marta.goto("/reconciliation");
  await expect(marta.getByRole("region", { name: "Reconciliation queue" })).toBeVisible();
  await marta.waitForTimeout(1500);
  await shot(marta, "05-reconciliation-unknown.png", "Reconciliation queue: one unknown outcome and one order that needs a decision", false);
  await marta.goto(`/operations/${ready}`);
  await marta.getByRole("button", { name: "Approve…" }).click();
  await expect(marta.getByRole("dialog")).toBeVisible();
  await shot(marta, "04-approval-panel.png", "Approval dialog bound to the exact version and content hash", false);
  await marta.keyboard.press("Escape");

  await daniel.goto("/customers");
  await daniel.getByLabel("Search both ERPs").fill("alder");
  await daniel.getByRole("button", { name: "Search", exact: true }).click();
  await expect(daniel.getByText("Similar names found in the same system")).toBeVisible();
  await shot(daniel, "02a-customers-ambiguity.png", "Customer search: two similar HALDEN accounts kept separate (ambiguity group)", false);
  await daniel.getByLabel("Search both ERPs").fill("vantaa");
  await daniel.getByRole("button", { name: "Search", exact: true }).click();
  await expect(daniel.getByRole("region", { name: /Party Vantaa/ })).toBeVisible();
  await shot(daniel, "02b-customers-hold-conflict.png", "Customer search: cross-backend credit-hold conflict on a linked party", false);

  await sanna.goto("/inventory");
  await sanna.locator("button.tech").first().click();
  await shot(sanna, "03-inventory-raw-vs-normalized.png", "Inventory per backend with snapshot age and the raw legacy rows behind a figure", false);

  await simulator("stale_extracts", false);
  await waitState(marta, unknown, ["CONFIRMED", "ESCALATED", "UNKNOWN"], 60_000);
  await helena.goto("/audit");
  await helena.getByRole("button", { name: /Verify/ }).first().click();
  await expect(helena.getByRole("status").filter({ hasText: /verif|valid/i })).toBeVisible({ timeout: 30_000 });
  await shot(helena, "07-audit-verify-valid.png", "Audit chain recomputed by the server and reported valid", false);

  await sanna.goto("/");
  await sanna.getByRole("button", { name: "Open assistant" }).click();
  const drawer = sanna.getByRole("dialog", { name: "Assistant" });
  await drawer.getByLabel("Ask the assistant").fill("draft halden 00417 BW-1001x1");
  await drawer.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(drawer).toContainText(/refused/i, { timeout: 30_000 });
  await shot(sanna, "08-assistant-refused-tool.png", "Assistant drawer without a model: a draft request refused for the inventory coordinator's role", false);
} finally {
  await browser.close();
}
console.log(JSON.stringify(log, null, 1));
