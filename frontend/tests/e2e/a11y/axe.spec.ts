import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { as, createDraft } from "../helpers";

// UI-02 / NFR-05: no serious or critical axe-core violations on the main routes.
async function audit(page: import("@playwright/test").Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(bad.map((v) => `${label}: ${v.id} (${v.impact}) ${v.nodes.length} node(s)`)).toEqual([]);
  // structure a screen-reader user navigates by: heading levels without gaps, distinguishable landmarks
  const structure = await new AxeBuilder({ page }).withRules(["heading-order", "landmark-unique", "page-has-heading-one"]).analyze();
  expect(structure.violations.map((v) => `${label}: ${v.id} ${v.nodes.map((n) => n.html.slice(0, 80)).join(" | ")}`)).toEqual([]);
}

test("login page", async ({ page }) => {
  await page.goto("/login");
  await audit(page, "/login");
});

test("main routes", async ({ browser }) => {
  const page = await as(browser, "marta.ops");
  const id = await createDraft(page, "00417", "BW-1004", "1");
  for (const path of ["/", "/customers", "/customers/halden/00417", "/inventory", "/operations", "/operations/new",
    `/operations/${id}`, "/approvals", "/reconciliation", "/audit"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("status")).toHaveCount(0, { timeout: 30_000 }); // loaded (live HALDEN reads)
    await page.waitForLoadState("networkidle");
    await audit(page, path);
  }
});

test("admin route", async ({ browser }) => {
  const page = await as(browser, "tomas.admin");
  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await audit(page, "/admin");
});

test("approve dialog is keyboard operable and shows the content hash prefix", async ({ browser }) => {
  const page = await as(browser, "daniel.sales");
  const id = await createDraft(page, "00417", "BW-1001", "3");
  await page.getByRole("button", { name: "Submit for approval" }).click();
  const other = await as(browser, "priya.sales");
  await other.goto(`/operations/${id}`);
  await other.getByRole("button", { name: "Approve…" }).focus();
  await other.keyboard.press("Enter");
  const dialog = other.getByRole("dialog");
  await expect(dialog).toContainText("content hash");
  await audit(other, "approve dialog");
  await other.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});
