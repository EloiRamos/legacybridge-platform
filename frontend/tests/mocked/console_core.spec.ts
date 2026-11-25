import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { CSRF, detail, mock, OP, summary } from "./fixtures";

// Uncertain outcomes and the assistant drawer. Mocked API; nothing leaves the page.
const UNKNOWN = "The outcome of this request is not known. Check the current state before repeating it.";
const CLAIM = "Nothing was changed";
const review = { queue: [summary("READY")], detail: detail("READY", ["approve", "decline"]) };
const problem = (status: number, code: string, text: string) => ({ status, body: { type: "about:blank", title: code, status, code, detail: text, request_id: "r1" } });

async function escalate(page: Page) {
  await page.goto(`/operations/${OP}`);
  await page.getByRole("button", { name: "Escalate", exact: true }).click();
  await page.getByRole("button", { name: "Escalate order", exact: true }).click();
}

test.describe("RR-1", () => {
  for (const [status, code] of [[503, "DATABASE_UNAVAILABLE"], [500, "INTERNAL"]] as const) {
    test(`${status} ${code}: no claim that nothing changed, and the order is read again`, async ({ page }) => {
      await mock(page, { onPost: () => problem(status, code, "It may or may not have been applied.") });
      const reads: string[] = [];
      page.on("request", (r) => { if (r.method() === "GET" && r.url().endsWith(`/operations/${OP}`)) reads.push(r.url()); });
      await escalate(page);
      const alert = page.getByRole("alert").filter({ hasText: code });
      await expect(alert).toContainText(UNKNOWN);
      await expect(alert).not.toContainText(CLAIM);
      await expect.poll(() => reads.filter((u) => u.includes("/api/")).length).toBeGreaterThan(1);
    });
  }
  test("a response lost on the way: outcome unknown", async ({ page }) => {
    await mock(page);
    await page.route(`**/api/v1/operations/${OP}/escalate`, (r) => r.abort("connectionreset"));
    await escalate(page);
    await expect(page.getByRole("alert").filter({ hasText: UNKNOWN })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(CLAIM);
  });
  test("a definite refusal shows the server's reason only", async ({ page }) => {
    await mock(page, { onPost: () => problem(409, "CONFLICT_STATE", "The order is no longer UNKNOWN.") });
    await escalate(page);
    const alert = page.getByRole("alert").filter({ hasText: "CONFLICT_STATE" });
    await expect(alert).toContainText("no longer UNKNOWN");
    await expect(alert).not.toContainText("not known");
    await expect(alert).not.toContainText(CLAIM);
  });
  test("a failed read still says that nothing was changed", async ({ page }) => {
    await mock(page, { inventory: "503" });
    await page.goto("/inventory");
    await expect(page.getByRole("alert").filter({ hasText: CLAIM })).toBeVisible();
  });
});

test.describe("assistant drawer", () => {
  const turn = { turn_id: "00000000-0000-4000-8000-0000000000aa", mode: "disabled", fallback_used: true, fallback_reason: "DISABLED", notice: null,
    answer: { summary: "One order is unresolved.", facts: [], warnings: [], draft_operation_id: OP, next_human_actions: [] },
    tool_calls: [{ name: "list_operations", permitted: true }], refused_calls: [{ name: "approve_operation", reason: "UNKNOWN_TOOL" }] };
  async function withAssistant(page: Page) {
    await mock(page, review);
    const asked: { text: unknown; csrf: string | undefined }[] = [];
    await page.route("**/api/v1/assistant/status", (r) => r.fulfill({ json: { mode: "disabled", live_inference: false, grammar: ["unresolved"] } }));
    await page.route("**/api/v1/assistant/turns", (r) => {
      asked.push({ text: r.request().postDataJSON(), csrf: r.request().headers()["x-csrf-token"] });
      return r.fulfill({ json: turn });
    });
    return asked;
  }
  const opener = (page: Page) => page.getByRole("button", { name: "Open assistant" });

  for (const path of ["/customers?q=nordkap", "/inventory", "/operations", "/operations/new", `/operations/${OP}`, "/approvals", "/reconciliation", "/admin"]) {
    test(`opens on ${path}`, async ({ page }) => {
      await withAssistant(page);
      await page.goto(path);
      await opener(page).click();
      const d = page.getByRole("dialog", { name: "Assistant" });
      await expect(d).toContainText("deterministic commands (no model)");
      await expect(d).toContainText("It can’t");
    });
  }
  for (const [w, h] of [[1440, 900], [390, 844], [360, 800]] as const) {
    test(`${w}: ask, see refusals and draft link, axe clean, Escape returns focus`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      const asked = await withAssistant(page);
      await page.goto("/inventory");
      await opener(page).click();
      const d = page.getByRole("dialog", { name: "Assistant" });
      const input = (await d.getByLabel("Ask the assistant").boundingBox())!, send = (await d.getByRole("button", { name: "Ask", exact: true }).boundingBox())!;
      expect(send.x).toBeGreaterThanOrEqual(input.x + input.width); // side by side, as on the page
      await d.getByLabel("Ask the assistant").fill("unresolved");
      await d.getByRole("button", { name: "Ask", exact: true }).click();
      await expect(d).toContainText("One order is unresolved.");
      await expect(d.getByText("One order is unresolved.")).toBeInViewport();
      await expect(d).toContainText("approve_operation — refused");
      expect(asked).toEqual([{ text: { text: "unresolved" }, csrf: CSRF }]);
      const bad = (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze())
        .violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      expect(bad.map((v) => v.id)).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
      await page.keyboard.press("Escape");
      await expect(d).toBeHidden();
      await expect(opener(page)).toBeFocused();
    });
  }
  test("the conversation follows the person between pages; the draft link closes the drawer", async ({ page }) => {
    await withAssistant(page);
    await page.goto("/inventory");
    await opener(page).click();
    const d = page.getByRole("dialog", { name: "Assistant" });
    await d.getByLabel("Ask the assistant").fill("unresolved");
    await page.keyboard.press("Enter");
    await d.getByRole("link", { name: /assistant-prepared draft/ }).click();
    await expect(page).toHaveURL(new RegExp(`/operations/${OP}$`));
    await expect(d).toBeHidden();
    await opener(page).click();
    await expect(d).toContainText("One order is unresolved.");
    await page.keyboard.press("Escape");
    await page.locator("nav[aria-label=Main]").getByRole("link", { name: "Assistant" }).click();
    await expect(page.locator("main")).toContainText("One order is unresolved.");
    await expect(opener(page)).toHaveCount(0); // the page itself is the panel
  });
});
