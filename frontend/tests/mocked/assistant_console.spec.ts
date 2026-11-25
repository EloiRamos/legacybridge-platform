import { expect, type Page, test } from "@playwright/test";

import { detail, mock, OP, summary } from "./fixtures";

// Draft receipts survive every fallback, proposals need an explicit confirmation, the trace and the unknown-order
// banner say what is true. Mocked API; nothing leaves the page.
const receipt = { call_id: "c1", tool: "prepare_draft", status: "STORED", operation_id: OP, correlation_value: "LBTEST0001", version: 1,
  content_hash: "ab".repeat(32), state: "DRAFT", lines: [{ canonical_sku: "BW-1001", native_item: "BW-1001", quantity_base: 2 }], audit_seq: 41, audit_event_hash: "cd".repeat(32) };
const turn = (over: object) => ({ turn_id: crypto.randomUUID(), mode: "openai mock", fallback_used: false, fallback_reason: null, notice: null,
  answer: { summary: "Done.", facts: [], warnings: [], draft_operation_id: null, next_human_actions: ["none"] }, tool_calls: [], refused_calls: [], receipts: [], proposal: null, ...over });

async function assistant(page: Page, turns: object[]) {
  await mock(page, {});
  const asked: string[] = [];
  await page.route("**/api/v1/assistant/status", (r) => r.fulfill({ json: { mode: "openai mock", live_inference: false, grammar: [] } }));
  await page.route("**/api/v1/assistant/turns", (r) => { asked.push(r.request().postDataJSON().text); return r.fulfill({ json: turns[asked.length - 1] }); });
  await page.goto("/assistant");
  return asked;
}
const ask = async (page: Page, q: string) => { await page.getByLabel("Ask the assistant").fill(q); await page.getByRole("button", { name: "Ask", exact: true }).click(); };

for (const reason of ["GROUNDING_FAILED", "TIMEOUT", "MALFORMED_RESPONSE", "LIMIT", "PROVIDER_FAILED", "BUDGET_DENIED"]) {
  test(`receipt is shown when the answer falls back (${reason})`, async ({ page }) => {
    await assistant(page, [turn({ fallback_used: true, fallback_reason: reason, notice: "The model's answer could not be used.", receipts: [receipt],
      answer: { summary: "The model's answer could not be used.", facts: [], warnings: [], draft_operation_id: OP, next_human_actions: ["none"] } })]);
    await ask(page, "draft two BW-1001");
    const r = page.getByLabel("Draft receipt");
    await expect(r).toContainText("Draft stored");
    await expect(r).toContainText("LBTEST0001");
    await expect(r).toContainText("version 1 · 2 × BW-1001");
    await expect(r).toContainText("#41");
    await expect(r.getByRole("link", { name: /assistant-prepared draft/ })).toHaveAttribute("href", `/operations/${OP}`);
    await expect(page.locator("main")).toContainText("Fallback: the model answer was not used");
  });
}

test("an unknown draft outcome is neither success nor nothing", async ({ page }) => {
  await assistant(page, [turn({ receipts: [{ ...receipt, status: "UNKNOWN", operation_id: null, version: null, content_hash: null, correlation_value: null, lines: [], audit_seq: null }] })]);
  await ask(page, "draft two BW-1001");
  await expect(page.getByLabel("Draft receipt")).toContainText("It is not known whether this draft was stored");
  await expect(page.getByLabel("Draft receipt")).not.toContainText("Draft stored");
});

test("an unverified write is not presented as read back", async ({ page }) => {
  await assistant(page, [turn({ receipts: [{ ...receipt, status: "UNVERIFIED" }] })]);
  await ask(page, "draft two BW-1001");
  const r = page.getByLabel("Draft receipt");
  await expect(r).toContainText("not read back");
  await expect(r).not.toHaveClass(/\bok\b/);
  await expect(r).not.toContainText(/Draft (stored|revised)/);
  await expect(r).not.toContainText("#41"); // no audit event is claimed for something that was not verified
  await expect(r.getByRole("link", { name: "Orders" })).toBeVisible();
});

test("a scripted statement is not labelled as verified, a typed fact names its record", async ({ page }) => {
  const facts = [{ claim: "available_base = 120", record: "HALDEN · 00417", source: { tool_call_id: "c1", field_path: "x" } }];
  const answer = { summary: "ok", facts, warnings: [], draft_operation_id: null, next_human_actions: ["none"] };
  await assistant(page, [turn({ mode: "scripted", answer }), turn({ answer })]);
  await ask(page, "one");
  await expect(page.locator("main")).toContainText("Scripted statements with cited fields");
  await expect(page.locator("main")).not.toContainText("Verified against system records");
  await ask(page, "two");
  await expect(page.locator("main")).toContainText("Exact values from system records");
});

test("a response without the optional receipt fields renders", async ({ page }) => {
  await assistant(page, [turn({ receipts: undefined, proposal: undefined })]); // absent in the JSON
  await ask(page, "hello");
  await expect(page.locator("main")).toContainText("Done.");
});

test("a proposal writes nothing until the person confirms exactly it", async ({ page }) => {
  const asked = await assistant(page, [
    turn({ proposal: { code: "A1B2C3", tool: "prepare_draft", description: "Create a DRAFT in HALDEN for account 00417: 20 base unit(s) of BW-1001.", reasons: ["'boxes' leaves the requested quantity or intent open"], expires_at: "2026-11-24T12:00:00Z" },
      refused_calls: [{ name: "prepare_draft", reason: "INTENT_UNCONFIRMED" }], tool_calls: [{ name: "prepare_draft", permitted: false, outcome: "REFUSED", refused_reason: "INTENT_UNCONFIRMED" }] }),
    turn({ receipts: [receipt] })]);
  await ask(page, "two boxes of BW-1001");
  const p = page.getByLabel("Draft proposal");
  await expect(p).toContainText("Nothing was written");
  await expect(p).toContainText("20 base unit(s) of BW-1001");
  await expect(page.getByLabel("Draft receipt")).toHaveCount(0);
  await p.getByRole("button", { name: "Confirm this draft" }).click();
  await expect(page.getByLabel("Draft receipt")).toBeVisible();
  expect(asked).toEqual(["two boxes of BW-1001", "confirm A1B2C3"]);
});

test("trace: one line per call, failures and refusals are not successes, a mock is not live", async ({ page }) => {
  await assistant(page, [turn({ tool_calls: [{ name: "search_customers", permitted: true, outcome: "SUCCEEDED" }, { name: "get_operation_timeline", permitted: true, outcome: "FAILED", error_code: "NOT_FOUND" },
    { name: "approve_operation", permitted: false, outcome: "REFUSED", refused_reason: "UNKNOWN_TOOL" }], refused_calls: [{ name: "approve_operation", reason: "UNKNOWN_TOOL" }] })]);
  await ask(page, "look");
  const trace = page.getByLabel("Tool calls");
  await expect(trace.locator("> span")).toHaveCount(3);
  await expect(trace).toContainText("✓ search_customers — executed successfully");
  await expect(trace).toContainText("! get_operation_timeline — executed, returned an error (NOT_FOUND)");
  await expect(trace).toContainText("✕ approve_operation — refused, not executed");
  await expect(page.locator("main")).toContainText("simulated OpenAI transport (test, not live)");
  await expect(page.locator("main")).not.toContainText("OpenAI model (live)");
});

test("a lost answer does not claim that nothing happened", async ({ page }) => {
  await mock(page, {});
  await page.route("**/api/v1/assistant/status", (r) => r.fulfill({ json: { mode: "openai mock", live_inference: false, grammar: [] } }));
  await page.route("**/api/v1/assistant/turns", (r) => r.abort("connectionreset"));
  await page.goto("/assistant");
  await ask(page, "draft two BW-1001");
  await expect(page.locator("main")).toContainText("check your drafts before asking again");
});

test.describe("unknown-order banner", () => {
  for (const [backend, actions, has, not] of [
    ["skarvik", ["authorize_redelivery", "authorize_withdraw_unclaimed", "escalate", "recheck"], "authorize redelivery of the same message", "Only read-only"],
    ["skarvik", ["escalate", "recheck"], "Available to you now: escalate for a decision; re-check (read-only).", "now: authorize"],
    ["halden", ["escalate", "recheck"], "never sent again", "redeliver"]] as const) {
    test(`${backend} ${actions.length}`, async ({ page }) => {
      const base = detail("UNKNOWN", [...actions]);
      await mock(page, { queue: [summary("UNKNOWN")], detail: { ...base, operation: { ...base.operation, backend_id: backend } } as typeof base });
      await page.goto(`/operations/${OP}`);
      const b = page.getByRole("note").filter({ hasText: "We don’t know whether" });
      await expect(b).toContainText(has);
      await expect(b).not.toContainText(not);
    });
  }
  test("phone: the banner comes before the commands and fits", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mock(page, { queue: [summary("UNKNOWN")], detail: detail("UNKNOWN", ["escalate", "recheck"]) });
    await page.goto(`/operations/${OP}`);
    const banner = (await page.getByRole("note").filter({ hasText: "We don’t know whether" }).boundingBox())!;
    const actions = (await page.getByRole("region", { name: "Actions" }).boundingBox())!;
    expect(banner.y).toBeLessThan(actions.y);
    expect(actions.y).toBeLessThan(844);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  });
});
