import { mkdirSync, writeFileSync } from "node:fs";

import { type Browser, expect, type Page, test } from "@playwright/test";

import { approve, as, ctl, USERS, waitStatus } from "../helpers";

/* Console: browser validation and screenshot evidence (opt-in).
 *
 *   LBF_VISUAL_DIR=<dir> LBF_E2E_BASE_URL=http://127.0.0.1:5199 npx playwright test tests/e2e/visual
 *
 * Runs against the live test-profile stack. States are produced by the real workflow (drafts, submissions, approvals
 * by a second person) and the test-profile fault controls used by the existing e2e suite (acceptance/e2e_ctl.py). Only
 * the loading and error captures use browser-side route interception; those are labelled "mocked" in the manifest. */
const DIR = process.env.LBF_VISUAL_DIR;
test.skip(!DIR, "set LBF_VISUAL_DIR to capture the console");
test.setTimeout(900_000);

type Persona = (typeof USERS)[number];
interface Shot { file: string; route: string; user: string; viewport: string; data: "live" | "mocked"; h1: number; mainNavVisible: number; docOverflowPx: number; consoleErrors: string[]; note?: string }
const shots: Shot[] = [];
const checks: { id: string; result: "pass" | "fail"; detail: string }[] = [];
const VP: Record<string, { width: number; height: number }> = {
  "1440": { width: 1440, height: 900 }, "1280": { width: 1280, height: 800 }, "1024": { width: 1024, height: 768 },
  "390": { width: 390, height: 844 }, "360": { width: 360, height: 780 },
};

async function open(browser: Browser, user: Persona, vp: string): Promise<{ page: Page; errors: string[] }> {
  const page = await as(browser, user);
  await page.setViewportSize(VP[vp]!);
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") errors.push(`${m.type()}: ${m.text()}`); });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  return { page, errors };
}

async function settle(page: Page) {
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[role="status"]').filter({ hasText: /^Loading/ })).toHaveCount(0, { timeout: 30_000 });
  await page.waitForTimeout(400);
}

async function shot(page: Page, errors: string[], name: string, vp: string, user: string, opts: { data?: "live" | "mocked"; note?: string; full?: boolean } = {}) {
  const m = await page.evaluate(() => ({
    h1: document.querySelectorAll("h1").length,
    nav: [...document.querySelectorAll('nav[aria-label^="Main"]')].filter((n) => (n as HTMLElement).offsetParent !== null && getComputedStyle(n).display !== "none").length,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  const file = `${vp}-${name}.png`;
  await page.screenshot({ path: `${DIR}/${file}`, fullPage: opts.full ?? true });
  shots.push({ file, route: new URL(page.url()).pathname + new URL(page.url()).search, user, viewport: `${VP[vp]!.width}x${VP[vp]!.height}`, data: opts.data ?? "live",
    h1: m.h1, mainNavVisible: m.nav, docOverflowPx: m.overflow, consoleErrors: [...errors], note: opts.note });
}

async function capture(browser: Browser, user: Persona, vp: string, path: string, name: string, note?: string) {
  const { page, errors } = await open(browser, user, vp);
  await page.goto(path);
  await settle(page);
  await shot(page, errors, name, vp, user, { note });
  await page.context().close();
}


/** Draft + submit with a longer wait than helpers.readyOrder: validation reads HALDEN live and its frozen rate limit
 * can delay the answer. A genuinely blocked submission (role=alert) fails with the server's reason. */
async function ready(page: Page, backend: "halden" | "skarvik", account: string, sku: string, qty: string): Promise<[string, string]> {
  await page.goto("/operations/new");
  await page.getByLabel("Backend").selectOption(backend);
  await page.getByLabel("Account number").fill(account);
  await page.getByLabel("Group SKU").fill(sku);
  await page.getByLabel("Quantity").fill(qty);
  await page.getByRole("button", { name: "Create draft" }).click();
  await expect(page).toHaveURL(/\/operations\/[0-9a-f-]{36}$/, { timeout: 60_000 });
  const id = page.url().split("/").pop()!;
  await page.getByRole("button", { name: "Submit for approval" }).click();
  try {
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Awaiting approval", { timeout: 90_000 });
  } catch (e) {
    throw new Error(`submit not accepted for ${backend} ${account} ${sku}x${qty}: ${(await page.getByRole("alert").allInnerTexts()).join(" | ")}  `, { cause: e });
  }
  return [id, (await page.getByRole("heading", { level: 1 }).locator(".mono").first().innerText()).trim()];
}

const check = (id: string, ok: boolean, detail: string) => checks.push({ id, result: ok ? "pass" : "fail", detail });

test("seed real states, validate and capture", async ({ browser }) => {
  mkdirSync(DIR!, { recursive: true });
  ctl("reset");

  // ---- empty states (real: freshly reset stack) ----
  for (const [p, n] of [["/operations", "orders-empty"], ["/reconciliation", "reconciliation-empty"], ["/approvals", "approvals-empty"]] as const) await capture(browser, "marta.ops", "1440", p, n);

  // ---- seed through the real workflow ----
  const daniel = await as(browser, "daniel.sales");
  const priya = await as(browser, "priya.sales");
  const [confirmed] = await ready(daniel, "halden", "00417", "BW-1001", "2");
  await approve(priya, confirmed);
  await waitStatus(priya, confirmed, /Confirmed in HALDEN|Acknowledged/, 120_000);

  // HALDEN commits the order but answers 500, and every read fails for a while (as in e2e F2-09)
  const [escalated] = await ready(daniel, "halden", "00417", "BW-1004", "1");
  ctl("halden-fault", "POST", "/halden/orders", "commit_then_500");
  ctl("halden-fault", "GET", "/halden/orders", "error_500", "--count", "60");
  await approve(priya, escalated);
  await waitStatus(priya, escalated, "Needs decision", 180_000);
  ctl("halden-clear-faults");

  const [readyDaniel] = await ready(daniel, "halden", "00417", "BW-1006", "3");
  const [readyPriya] = await ready(priya, "halden", "00417", "BW-1001", "2"); // BW-1009 is UNDETERMINED in HALDEN → override-only
  const [readyOverride] = await ready(daniel, "halden", "00417", "BW-1006", "900");
  // a real blocking policy check: account 10871 is on credit hold in HALDEN — the submit is refused, the order stays a draft
  {
    const { page, errors } = await open(browser, "daniel.sales", "1440");
    await page.goto("/operations/new");
    await page.getByLabel("Account number").fill("10871");
    await page.getByLabel("Group SKU").fill("BW-1006");
    await page.getByLabel("Quantity").fill("3");
    await page.getByRole("button", { name: "Create draft" }).click();
    await expect(page).toHaveURL(/\/operations\/[0-9a-f-]{36}$/, { timeout: 60_000 });
    await page.getByRole("button", { name: "Submit for approval" }).click();
    await expect(page.getByRole("alert")).toContainText("still a draft", { timeout: 90_000 });
    check("blocked-submit-notice", (await page.getByRole("heading", { level: 1 }).innerText()).includes("Draft"), `credit hold: ${(await page.getByRole("alert").innerText()).replace(/\s+/g, " ")}`);
    await shot(page, errors, "detail-blocked-submit", "1440", "daniel.sales");
    await page.context().close();
  }
  await daniel.goto("/operations/new");
  await daniel.getByLabel("Backend").selectOption("skarvik");
  await daniel.getByLabel("Account number").fill("KND-0081");
  await daniel.getByLabel("Group SKU").fill("BW-1002");
  await daniel.getByLabel("Quantity").fill("1");
  await daniel.getByRole("button", { name: "Create draft" }).click();
  await expect(daniel).toHaveURL(/\/operations\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  const draft = daniel.url().split("/").pop()!;

  // ---- UNKNOWN (SKARVIK: pickup paused → acknowledgement overdue). The test profile scales re-checks and the
  //      escalation timer (LBF_TIME_SCALE), so UNKNOWN is captured immediately. ----
  ctl("skarvik-fault", "pause_pickup", "--value", "true");
  const [unknown] = await ready(daniel, "skarvik", "KND-0081", "BW-1001", "5");
  await approve(priya, unknown);
  await waitStatus(priya, unknown, "Outcome unknown", 150_000);
  for (const vp of ["1440", "390"]) {
    const { page, errors } = await open(browser, "marta.ops", vp);
    await page.goto(`/operations/${unknown}`);
    await settle(page);
    const panel = page.getByRole("region", { name: "What is observed and what is not known" });
    const visible = await panel.isVisible();
    if (await page.getByRole("heading", { level: 1 }).innerText().then((t) => t.includes("Outcome unknown"))) {
      check(`unknown-panel-${vp}`, visible, "Observed / Not known panel visible on an UNKNOWN order");
      await shot(page, errors, "detail-unknown", vp, "marta.ops");
    } else {
      check(`unknown-panel-${vp}`, true, "order already escalated by the scaled timer before capture; UNKNOWN shot skipped at this width");
    }
    await page.context().close();
  }
  await capture(browser, "rafael.integration", "1440", "/reconciliation", "reconciliation");
  ctl("skarvik-fault", "pause_pickup", "--value", "false");

  // ---- desktop captures (marta.ops = ops manager) ----
  await capture(browser, "marta.ops", "1440", "/", "overview");
  await capture(browser, "marta.ops", "1440", "/operations", "orders-grouped");
  await capture(browser, "marta.ops", "1440", "/operations?view=table", "orders-table");
  {
    const { page, errors } = await open(browser, "marta.ops", "1440");
    await page.goto("/operations?view=table");
    await settle(page);
    const rows = page.locator("table.ot tbody tr");
    await rows.nth(0).focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press(" ");
    const sel = await rows.nth(1).getAttribute("aria-selected");
    check("table-keyboard-select", sel === "true", `ArrowDown + Space selects row 2 (aria-selected=${sel})`);
    const previewName = await page.locator("aside.preview").getAttribute("aria-label");
    check("table-preview-1440", !!previewName && (await page.locator("aside.preview").isVisible()), `preview visible at 1440: ${previewName}`);
    await shot(page, errors, "orders-table-selected", "1440", "marta.ops");
    await page.getByRole("columnheader", { name: /Est\. EUR/ }).getByRole("button").click();
    await expect(page).toHaveURL(/sort=total/);
    await expect(page.getByRole("columnheader", { name: /Est\. EUR/ })).toHaveAttribute("aria-sort", "descending");
    const sort = await page.getByRole("columnheader", { name: /Est\. EUR/ }).getAttribute("aria-sort");
    check("table-sort-aria", sort === "descending", `Est. EUR header aria-sort=${sort} after one click`);
    await page.getByRole("button", { name: /Needs attention/ }).click();
    await expect(page.getByRole("button", { name: /Needs attention/ })).toHaveAttribute("aria-pressed", "true");
    await expect(page).toHaveURL(/stage=attention/);
    const n = await rows.count();
    check("table-filter-stage", n >= 1 && (await rows.allInnerTexts()).every((t) => /Outcome unknown|Needs decision/.test(t)), `Needs attention filter rows=${n}`);
    await page.getByRole("button", { name: "Grouped" }).click();
    await expect(page.locator(".grp").first()).toBeVisible();
    check("view-switch-keeps-filter", new URL(page.url()).searchParams.get("stage") === "attention" && (await page.locator(".grp").count()) === 1, "switching to Grouped keeps the stage filter");
    await page.getByRole("button", { name: "Table" }).click();
    await rows.nth(0).focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/operations\/[0-9a-f-]{36}$/);
    check("table-keyboard-open", true, "Enter on a focused row opens the order");
    await page.context().close();
  }
  for (const vp of ["1280", "1024"]) await capture(browser, "marta.ops", vp, "/operations?view=table", "orders-table");
  await capture(browser, "marta.ops", "1440", `/operations/${escalated}`, "detail-escalated");
  await capture(browser, "marta.ops", "1440", `/operations/${confirmed}`, "detail-confirmed");
  await capture(browser, "marta.ops", "1440", `/operations/${draft}`, "detail-draft");
  {
    const { page } = await open(browser, "marta.ops", "1440");
    for (const [id, label] of [[confirmed, "CONFIRMED"], [readyDaniel, "READY"], [draft, "DRAFT"]] as const) {
      await page.goto(`/operations/${id}`);
      await settle(page);
      check(`no-uncertainty-panel-${label}`, (await page.getByRole("region", { name: "What is observed and what is not known" }).count()) === 0, `${label}: panel absent`);
    }
    await page.goto(`/operations/${escalated}`);
    await settle(page);
    check("escalated-panel", await page.getByRole("region", { name: "What is observed and what is not known" }).isVisible(), "Needs decision: panel visible");
    const notKnown = await page.locator(".kn.nk").innerText();
    check("escalated-not-failed", !/(?<!not )\bfailed\b/i.test(notKnown) && /not failed/.test(notKnown), "NOT KNOWN / rule text never calls the outcome failed");
    await page.context().close();
  }

  // ---- approvals: four-eyes, exact version, override policy (server-decided) ----
  {
    const { page } = await open(browser, "daniel.sales", "1440");
    await page.goto(`/operations/${readyDaniel}`);
    await settle(page);
    check("self-approval-hidden", (await page.getByRole("button", { name: /Approve/ }).count()) === 0, "preparer sees no Approve on own order");
    await page.goto("/approvals");
    await settle(page);
    const queue = await page.getByRole("navigation", { name: "Approval queue" }).innerText().catch(() => "");
    const dCv = await (await as(browser, "marta.ops")).request.get(`/api/v1/operations/${readyDaniel}`).then((r) => r.json()).then((j) => j.operation.correlation_value as string);
    check("self-approval-queue", !queue.includes(dCv), "own READY order not in daniel's queue");
    await page.context().close();
  }
  await capture(browser, "priya.sales", "1440", "/approvals", "approvals", "priya.sales (sales_ops) — standard approvals of others' orders; override order listed under 'waiting for someone else'");
  {
    const { page, errors } = await open(browser, "priya.sales", "1440");
    await page.goto(`/operations/${readyDaniel}`);
    await settle(page);
    check("sales-may-approve-others", (await page.getByRole("button", { name: "Approve…" }).count()) === 1, "priya.sales is offered Approve on daniel's standard order");
    await page.getByRole("button", { name: "Approve…" }).click();
    await expect(page.getByRole("dialog")).toContainText("content hash");
    await shot(page, errors, "approve-dialog", "1440", "priya.sales", { full: false });
    // stale approval: daniel revises (new version) while the dialog is open → the server refuses the old version
    const v = await page.request.get(`/api/v1/operations/${readyDaniel}`).then((r) => r.json());
    const line = v.versions.at(-1).content.lines[0];
    await daniel.goto(`/operations/${readyDaniel}`);
    await daniel.getByRole("button", { name: "Revise" }).click();
    await daniel.getByLabel("Quantity (base)").fill(String(line.quantity_base + 1));
    await daniel.getByRole("button", { name: "Save new version" }).click();
    await waitStatus(daniel, readyDaniel, "Draft", 30_000);
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible({ timeout: 30_000 });
    const alertText = await page.getByRole("alert").innerText();
    check("stale-approval-refused", true, `approving the superseded version is refused by the server: ${alertText.replace(/\s+/g, " ").slice(0, 160)}`);
    await shot(page, errors, "approve-stale-refused", "1440", "priya.sales", { full: false });
    await page.context().close();
  }
  {
    const { page } = await open(browser, "priya.sales", "1440");
    await page.goto(`/operations/${readyOverride}`);
    await settle(page);
    const hasApprove = (await page.getByRole("button", { name: "Approve…" }).count()) > 0;
    const pa = await page.request.get(`/api/v1/operations/${readyOverride}`).then((r) => r.json());
    check("buttons-equal-permitted-actions", hasApprove === pa.permitted_actions.includes("approve"), `override-candidate order: Approve rendered=${hasApprove}, server permitted=${JSON.stringify(pa.permitted_actions)}`);
    await page.context().close();
  }

  // ---- evidence, assistant and the remaining areas ----
  await capture(browser, "marta.ops", "1440", "/audit", "evidence");
  {
    const { page, errors } = await open(browser, "helena.compliance", "1440");
    await page.goto("/audit");
    await settle(page);
    await page.getByRole("button", { name: "Verify the chain" }).click();
    await expect(page.locator(".verify")).toBeVisible({ timeout: 30_000 });
    const t = await page.locator(".verify").innerText();
    check("verify-server-result", /Server verification/.test(t) && /recomputed by the server/.test(t), `verification shown as server result: ${t.replace(/\s+/g, " ").slice(0, 140)}`);
    await page.locator(".blkc").first().click();
    await shot(page, errors, "evidence-verified-compliance", "1440", "helena.compliance");
    await page.context().close();
  }
  {
    const { page, errors } = await open(browser, "marta.ops", "1440");
    await page.goto("/assistant");
    await settle(page);
    await page.getByRole("button", { name: "unresolved" }).click();
    await expect(page.locator("section.card .ans").first()).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "approve the oldest order" }).click();
    await expect(page.locator("section.card .ans")).toHaveCount(2, { timeout: 30_000 });
    const answer = await page.locator("section.card .ans").first().innerText();
    const ops = await page.request.get("/api/v1/operations").then((r) => r.json());
    const stillReady = ops.items.filter((o: { state: string }) => o.state === "READY").length;
    check("assistant-cannot-approve", !/approved/i.test(answer.split("\n")[0] ?? "") && stillReady >= 1, `assistant answer to an approval request: "${answer.replace(/\s+/g, " ").slice(0, 160)}"; READY orders still ${stillReady}`);
    await shot(page, errors, "assistant", "1440", "marta.ops");
    await page.context().close();
  }
  await capture(browser, "marta.ops", "1440", "/inventory", "inventory");
  await capture(browser, "marta.ops", "1440", "/customers?q=nordkap", "customers");
  await capture(browser, "marta.ops", "1440", "/customers/halden/00417", "customer");
  await capture(browser, "marta.ops", "1440", "/operations/new", "neworder");
  await capture(browser, "marta.ops", "1440", "/admin", "admin", "ops_manager: engine + unmatched acks only");
  await capture(browser, "tomas.admin", "1440", "/admin", "admin-sysadmin", "sysadmin: no primary navigation (no operation.read)");
  {
    const { page, errors } = await open(browser, "marta.ops", "1440");
    await page.goto("/");
    await settle(page);
    await page.getByRole("button", { name: /account menu/ }).click();
    check("admin-in-account-menu", await page.getByRole("link", { name: "Administration" }).isVisible(), "Administration reachable from the account menu");
    await shot(page, errors, "account-menu", "1440", "marta.ops", { full: false });
    await page.keyboard.press("Escape");
    check("account-menu-escape", (await page.locator("#acct-menu").count()) === 0, "Escape closes the account menu");
    await page.context().close();
  }

  // ---- access denied, error and loading (error/loading are mocked in the browser) ----
  await capture(browser, "sanna.inventory", "1440", "/approvals", "access-denied", "inventory_coordinator lacks approval.grant.*");
  {
    const { page, errors } = await open(browser, "marta.ops", "1440");
    await page.route("**/api/v1/inventory", (r) => r.fulfill({ status: 503, contentType: "application/problem+json",
      body: JSON.stringify({ status: 503, code: "BACKEND_UNAVAILABLE", title: "Backend unavailable", detail: "HALDEN did not answer within 5 s (mocked for this capture).", request_id: "req_mocked_capture" }) }));
    await page.goto("/inventory");
    await expect(page.getByRole("alert")).toBeVisible();
    await shot(page, errors.filter((e) => !/503/.test(e)), "inventory-error", "1440", "marta.ops", { data: "mocked", note: "503 problem injected by page.route; the API was not called" });
    await page.unroute("**/api/v1/inventory");
    await page.route("**/api/v1/operations", async (r) => { await new Promise((res) => setTimeout(res, 4000)); await r.continue(); });
    await page.goto("/operations");
    await expect(page.getByRole("navigation", { name: "Main", exact: true })).toBeVisible();
    await expect(page.locator('main [aria-busy="true"]')).toBeVisible();
    await shot(page, errors, "orders-loading", "1440", "marta.ops", { data: "mocked", note: "response delayed 4 s by page.route", full: false });
    await page.context().close();
  }

  // ---- mobile ----
  for (const [p, n] of [["/", "overview"], ["/operations", "orders-grouped"], ["/operations?view=table", "orders-table"], [`/operations/${escalated}`, "detail-escalated"],
    ["/reconciliation", "reconciliation"], ["/audit", "evidence"], ["/assistant", "assistant"], ["/inventory", "inventory"], ["/customers?q=nordkap", "customers"],
    ["/customers/halden/00417", "customer"], ["/operations/new", "neworder"], ["/admin", "admin"]] as const) await capture(browser, "marta.ops", "390", p, n);
  await capture(browser, "marta.ops", "360", "/operations?view=table", "orders-table");
  await capture(browser, "marta.ops", "360", `/operations/${escalated}`, "detail-escalated");
  await capture(browser, "priya.sales", "390", "/approvals", "approvals");
  {
    const { page } = await open(browser, "priya.sales", "390");
    await page.goto(`/operations/${readyPriya}`);
    await settle(page);
    // priya prepared this one: no approval offered to her (four-eyes) — capture daniel's view instead
    check("mobile-self-approval-hidden", (await page.getByRole("button", { name: /Approve/ }).count()) === 0, "priya sees no Approve on her own order (390)");
    await page.context().close();
    const d = await open(browser, "daniel.sales", "390");
    await d.page.goto(`/operations/${readyPriya}`);
    await settle(d.page);
    await expect(d.page.getByRole("button", { name: "Approve…" }), "server offers daniel an approval of priya's standard order").toHaveCount(1, { timeout: 15_000 });
    await d.page.getByRole("button", { name: "Approve…" }).click();
    await expect(d.page.getByRole("dialog")).toBeVisible();
    const box = await d.page.getByRole("dialog").boundingBox();
    const btn = await d.page.getByRole("button", { name: "Approve", exact: true }).boundingBox();
    check("mobile-sheet-in-viewport", !!box && !!btn && box.y >= 0 && btn.y + btn.height <= 844 && box.x >= 0 && box.width <= 390, `bottom sheet ${JSON.stringify(box)}; approve button ${JSON.stringify(btn)}`);
    await shot(d.page, d.errors, "approve-sheet", "390", "daniel.sales", { full: false });
    await d.page.context().close();
  }

  // ---- 1280 / 1024 spot checks ----
  for (const vp of ["1280", "1024"]) {
    await capture(browser, "marta.ops", vp, "/", "overview");
    await capture(browser, "marta.ops", vp, `/operations/${escalated}`, "detail-escalated");
    await capture(browser, "priya.sales", vp, "/approvals", "approvals");
  }

  // ---- invariants over every capture ----
  const overflow = shots.filter((s) => s.docOverflowPx > 0);
  const navs = shots.filter((s) => s.user !== "tomas.admin" && s.mainNavVisible !== 1);
  // the browser logs every non-2xx response; the deliberate ones are the server refusing the superseded version (409)
  // and the mocked 503 — they are expected and reported separately, never hidden
  const expected = (s: Shot, e: string) => (s.file.includes("stale-refused") && /status of 409/.test(e)) || (s.data === "mocked" && /status of 503/.test(e));
  for (const s of shots) (s as Shot & { expectedErrors?: string[] }).expectedErrors = s.consoleErrors.filter((e) => expected(s, e));
  const errs = shots.filter((s) => s.consoleErrors.some((e) => !expected(s, e)));
  check("no-document-overflow", overflow.length === 0, overflow.length ? overflow.map((s) => `${s.file}: ${s.docOverflowPx}px`).join(", ") : `0 of ${shots.length} captures overflow`);
  check("single-primary-nav", navs.length === 0, navs.length ? navs.map((s) => `${s.file}: ${s.mainNavVisible}`).join(", ") : "exactly one visible primary navigation on every capture (sysadmin: none, by permission)");
  check("no-console-errors", errs.length === 0, errs.length ? errs.map((s) => `${s.file}: ${s.consoleErrors.join(" / ")}`).join(" | ") : "no unexpected console errors or warnings (expected: 409 stale-version refusal, mocked 503)");
  writeFileSync(`${DIR}/capture_manifest.json`, JSON.stringify({ base_url: process.env.LBF_E2E_BASE_URL, generated_at: new Date().toISOString(), shots, checks,
    seeded: { confirmed, escalated, unknown, readyDaniel, readyPriya, readyOverride, draft } }, null, 2));
  expect(checks.filter((c) => c.result === "fail"), JSON.stringify(checks.filter((c) => c.result === "fail"), null, 2)).toEqual([]);
});
