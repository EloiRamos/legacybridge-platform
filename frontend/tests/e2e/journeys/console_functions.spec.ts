import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import { as, createDraft } from "../helpers";

// console functions on the running stack.
const VERIFIER = fileURLToPath(new URL("../../../../oracles/audit_verify.py", import.meta.url));

test("compliance exports the audit from the console and the standalone verifier accepts the files", async ({ browser }) => {
  const page = await as(browser, "helena.compliance");
  await page.goto("/audit");
  await page.getByRole("button", { name: "Export audit" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Export ready" })).toBeVisible({ timeout: 30_000 });
  const dir = mkdtempSync(`${tmpdir()}/lbf-export-`);
  const save = async (name: string) => {
    const [d] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name }).click()]);
    const path = `${dir}/${d.suggestedFilename()}`;
    await d.saveAs(path);
    return path;
  };
  const out = execFileSync("python3", [VERIFIER, await save("Download events"), await save("Download manifest")], { encoding: "utf-8" });
  expect(out).toContain("VALID");
  expect(out).not.toContain("INVALID");
  const other = await as(browser, "rafael.integration");
  await other.goto("/audit");
  await expect(other.getByRole("button", { name: "Export audit" })).toHaveCount(0);
});

test("the inventory coordinator annotates an item; the note is audited; sales cannot", async ({ browser }) => {
  const page = await as(browser, "sanna.inventory");
  await page.goto("/inventory");
  await page.getByRole("button", { name: "Annotate NVB-6205-2RS" }).click({ timeout: 30_000 });
  await page.getByLabel("Note").fill("Counted in GBG");
  await page.getByRole("button", { name: "Record note" }).click();
  await expect(page.getByRole("status")).toContainText("Note recorded for NVB-6205-2RS");
  const helena = await as(browser, "helena.compliance");
  await helena.goto("/audit");
  await helena.getByPlaceholder("Event type, actor, sequence or reference").fill("INVENTORY_ANNOTATED");
  await expect(helena.locator("ol.chain li").first()).toContainText("sanna.inventory");
  const daniel = await as(browser, "daniel.sales");
  await daniel.goto("/inventory");
  await expect(daniel.getByRole("button", { name: "Raw rows" }).first()).toBeVisible({ timeout: 30_000 });
  await expect(daniel.getByRole("button", { name: /^Annotate/ })).toHaveCount(0);
});

test("the administrator creates a user and changes roles; own roles are refused by the server", async ({ browser }) => {
  const page = await as(browser, "tomas.admin");
  const name = `e2e.${Date.now()}`;
  await page.goto("/admin");
  await page.getByRole("button", { name: "New user" }).click();
  await page.getByLabel("Username").fill(name);
  await page.getByLabel("Display name").fill("E2E fictional user");
  await page.getByLabel("Initial password").fill(`pw-${crypto.randomUUID()}`);
  await page.getByRole("checkbox", { name: "sales_ops" }).check();
  await page.getByRole("button", { name: "Create user" }).click();
  await expect(page.getByRole("status")).toContainText(`${name} created`);
  const row = page.getByRole("row").filter({ hasText: name });
  await page.getByRole("button", { name: `Change roles of ${name}` }).click();
  await page.getByLabel("Role to grant").selectOption("inventory_coordinator");
  await page.getByRole("button", { name: "Grant", exact: true }).click();
  await expect(row).toContainText("inventory_coordinator");
  await page.getByRole("button", { name: `Change roles of ${name}` }).click();
  await page.getByRole("button", { name: "Revoke sales_ops" }).click();
  await expect(row).not.toContainText("sales_ops");
  await page.getByRole("button", { name: "Change roles of tomas.admin" }).click();
  await page.getByRole("button", { name: "Grant", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(/own roles|SOD_VIOLATION|FORBIDDEN/i);
});

test("draft preview, version comparison and order filters", async ({ browser }) => {
  const page = await as(browser, "daniel.sales");
  await page.goto("/operations/new?backend=halden&customer=00417");
  await page.getByLabel("Group SKU").fill("BW-1002");
  await page.getByRole("button", { name: "Preview checks" }).click();
  const p = page.getByRole("region", { name: "Preview" });
  await expect(p).toContainText("0 available, 1 requested", { timeout: 30_000 });
  await expect(p).toContainText("Credit hold: CLEAR");
  const id = await createDraft(page, "00417", "BW-1004", "1");
  const s = (await (await page.request.get("/api/v1/auth/session")).json()) as { csrf_token: string };
  const r = await page.request.post(`/api/v1/operations/${id}/revisions`, { headers: { Origin: new URL(page.url()).origin, "X-CSRF-Token": s.csrf_token },
    data: { expected_version: 1, backend_id: "halden", native_customer_id: "00417", lines: [{ canonical_sku: "BW-1004", quantity: 3 }] } });
  expect(r.status(), await r.text()).toBe(200);
  await page.goto(`/operations/${id}`);
  const t = page.getByRole("table", { name: "Changes from v1 to v2" });
  await expect(t).toContainText("1 EA");
  await expect(t).toContainText("3 EA");
  await page.goto("/operations");
  await page.getByLabel("Prepared by").selectOption("daniel.sales");
  await page.getByLabel("Attempt outcome").selectOption("none");
  await expect(page.locator(`a[href="/operations/${id}"]`).first()).toBeVisible();
});
