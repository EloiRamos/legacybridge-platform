import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { clean, CSRF, detail, mock, type Mock, type MockOptions, OP, summary } from "./fixtures";

// Console functions that were API/CLI only (C3-F1). Mocked API; nothing leaves the page.
const ALL = ["inventory.annotate", "audit.export", "audit.verify", "admin.roles", "draft.create"];
async function go(page: Page, path: string, o: MockOptions = {}, drop: string[] = [], extra: string[] = ALL): Promise<Mock> {
  const m = await mock(page, o);
  await page.route("**/api/v1/auth/session", async (r) => {
    const base = ["customer.read", "inventory.read", "operation.read", "draft.create", "draft.edit", "draft.submit", "audit.read", "admin.users", "ackqueue.read"];
    await r.fulfill({ json: { authenticated_at: "2026-11-23T18:02:00Z", csrf_token: CSRF, permissions: [...base, ...extra].filter((p) => !drop.includes(p)), profile: "test",
      roles: ["ops_manager"], user_id: "00000000-0000-4000-8000-000000000001", username: "marta.ops" } });
  });
  await page.route("**/api/v1/audit/events**", (r) => r.fulfill({ json: { events: [], head_seq: 42 } }));
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  return m;
}
async function axe(page: Page) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(r.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id)).toEqual([]);
}

test.describe("inventory annotation", () => {
  test("records one note for exactly this item and says what it did", async ({ page }) => {
    const sent: unknown[] = [];
    const m = await go(page, "/inventory");
    await page.route("**/api/v1/inventory/annotations", (r) => { sent.push([r.request().postDataJSON(), r.request().headers()["x-csrf-token"]]); return r.fulfill({ status: 201, json: { annotation_id: "a1" } }); });
    await page.getByRole("button", { name: "Annotate HX-6205-2RS" }).click();
    await axe(page);
    await page.getByLabel("Note").fill("Counted 118 on the shelf");
    await page.getByRole("button", { name: "Record note" }).click();
    await expect(page.getByRole("status")).toContainText("Note recorded for HX-6205-2RS");
    expect(sent).toEqual([[{ canonical_sku: "BW-1001", backend_id: "halden", native_item: "HX-6205-2RS", body: "Counted 118 on the shelf" }, CSRF]]);
    clean(m);
  });
  test("a lost answer is reported as unknown; without the permission there is no button", async ({ page }) => {
    await go(page, "/inventory");
    await page.route("**/api/v1/inventory/annotations", (r) => r.abort("connectionreset"));
    await page.getByRole("button", { name: "Annotate HX-6205-2RS" }).click();
    await page.getByLabel("Note").fill("x");
    await page.getByRole("button", { name: "Record note" }).click();
    await expect(page.getByRole("alert")).toContainText("The outcome of this request is not known");
    await go(page, "/inventory", {}, ["inventory.annotate"]);
    await expect(page.getByRole("button", { name: /^Annotate/ })).toHaveCount(0);
  });
});

test.describe("audit export", () => {
  test("downloads the JSON Lines and the manifest exactly as the server sent them", async ({ page }) => {
    const jsonl = '{"seq":1,"name":"Å"}\n';
    await go(page, "/audit");
    const urls: string[] = [];
    await page.route("**/api/v1/audit/exports**", (r) => { urls.push(r.request().url()); return r.fulfill({ json: { jsonl, manifest: { from_seq: 1, to_seq: 41, count: 41, head_hash: "ab".repeat(32), jsonl_sha256: "cd".repeat(32) } } }); });
    await page.getByRole("button", { name: "Export audit" }).click();
    await expect(page.getByRole("status")).toContainText("41 events");
    await expect(page.getByRole("status")).toContainText("ab".repeat(32));
    const read = async (name: string) => { const [d] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name }).click()]); const c: Buffer[] = []; for await (const x of await d.createReadStream()) c.push(x as Buffer); return [d.suggestedFilename(), Buffer.concat(c).toString("utf8")]; };
    expect(await read("Download events")).toEqual(["audit-1-41.jsonl", jsonl]);
    const [n, body] = await read("Download manifest");
    expect(n).toBe("audit-1-41.manifest.json");
    expect(JSON.parse(body!)).toMatchObject({ count: 41, jsonl_sha256: "cd".repeat(32) });
    expect(urls).toHaveLength(1);
    await axe(page);
  });
  test("not offered without audit.export", async ({ page }) => {
    await go(page, "/audit", {}, ["audit.export"]);
    await expect(page.getByRole("button", { name: "Export audit" })).toHaveCount(0);
  });
});

test.describe("users and roles", () => {
  test("creates a user, grants and revokes a role through the admin API", async ({ page }) => {
    const sent: unknown[] = [];
    await go(page, "/admin");
    await page.route("**/api/v1/admin/users**", (r) => {
      if (r.request().method() === "GET") return r.fallback();
      sent.push([r.request().method(), new URL(r.request().url()).pathname, r.request().postDataJSON() as unknown]);
      return r.request().method() === "POST" && r.request().url().endsWith("/users") ? r.fulfill({ status: 201, json: { user_id: "u2", username: "nina.ops", display_name: "Nina", roles: ["sales_ops"], is_active: true, locked_until: null } }) : r.fulfill({ status: 204 });
    });
    await page.getByRole("button", { name: "New user" }).click();
    await page.getByLabel("Username").fill("nina.ops");
    await page.getByLabel("Display name").fill("Nina");
    await page.getByLabel("Initial password").fill("a long passphrase");
    await page.getByRole("checkbox", { name: "sales_ops" }).check();
    await axe(page);
    await page.getByRole("button", { name: "Create user" }).click();
    await expect(page.getByRole("status")).toContainText("nina.ops created");
    await page.getByRole("button", { name: "Change roles of rafael.integration" }).click();
    await page.getByLabel("Role to grant").selectOption("compliance_reviewer");
    await page.getByRole("button", { name: "Grant", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.getByRole("button", { name: "Change roles of rafael.integration" }).click();
    await page.getByRole("button", { name: "Revoke ops_manager" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(sent).toEqual([["POST", "/api/v1/admin/users", { username: "nina.ops", display_name: "Nina", password: "a long passphrase", roles: ["sales_ops"] }],
      ["POST", "/api/v1/admin/users/u1/roles", { role: "compliance_reviewer" }], ["DELETE", "/api/v1/admin/users/u1/roles/ops_manager", null]]);
    expect(await page.locator("body").innerText()).not.toContain("a long passphrase");
  });
  test("a refusal is shown; step-up leads to re-authentication; no buttons without the permissions", async ({ page }) => {
    await go(page, "/admin");
    let code = "SOD_VIOLATION";
    await page.route("**/api/v1/admin/users/**", (r) => r.fulfill({ status: code === "SOD_VIOLATION" ? 403 : 401, contentType: "application/problem+json", body: JSON.stringify({ status: 403, code, title: code, detail: "own roles cannot be changed" }) }));
    const revoke = async () => { await page.getByRole("button", { name: "Change roles of rafael.integration" }).click(); await page.getByRole("button", { name: "Revoke ops_manager" }).click(); };
    await revoke();
    await expect(page.getByRole("alert")).toContainText("own roles cannot be changed");
    code = "REAUTH_REQUIRED";
    await revoke();
    await expect(page).toHaveURL(/\/reauth\?next=%2Fadmin/);
    await go(page, "/admin", {}, ["admin.roles", "admin.users"]);
    await expect(page.getByRole("button", { name: /New user|Change roles/ })).toHaveCount(0);
  });
});

test("version comparison shows what changed between two versions of the order", async ({ page }) => {
  const d = detail("READY", ["approve"]);
  const v1 = structuredClone(d.versions[0]!) as { version: number; content: { lines: { quantity_base: number; quantity_native: number }[] } };
  v1.content.lines[0]!.quantity_base = 9; v1.content.lines[0]!.quantity_native = 9;
  const cur = { ...d.versions[0]!, version: 2 };
  await go(page, `/operations/${OP}`, { detail: { ...d, operation: summary("READY", { current_version: 2 }), versions: [v1, cur] } });
  const s = page.getByRole("region", { name: "Versions" });
  await expect(s.getByRole("table", { name: "Changes from v1 to v2" })).toContainText("9 EA");
  await expect(s.getByRole("table", { name: "Changes from v1 to v2" })).toContainText("5 EA");
  await axe(page);
});

test("draft preview reads live facts, states no decision and changes nothing", async ({ page }) => {
  const m = await go(page, "/operations/new?backend=skarvik&customer=KND-0081");
  await page.getByLabel("Group SKU").fill("BW-1001");
  await page.getByLabel("Quantity").fill("500");
  await page.getByRole("button", { name: "Preview checks" }).click();
  const p = page.getByRole("region", { name: "Preview" });
  await expect(p).toContainText("Nordkap Marine Services AS");
  await expect(p).toContainText("Credit hold: CLEAR");
  await expect(p).toContainText("120 available, 500 requested");
  await expect(p).toContainText("not a decision");
  await axe(page);
  await page.getByLabel("Quantity").fill("5"); // an edited draft has no preview any more
  await expect(p).toHaveCount(0);
  expect(m.mutations).toEqual([]);
  clean(m);
});

test("orders can be filtered by who prepared them and by attempt outcome", async ({ page }) => {
  const q = [summary("UNKNOWN"), { ...summary("CONFIRMED"), operation_id: "f537ecff-3bfc-4db1-a20e-c07c544c4e6b", correlation_value: "LBOTHER00001", created_by: "priya.sales", last_outcome: "CONFIRMED" as const }];
  await go(page, "/operations", { queue: q });
  await page.getByLabel("Prepared by").selectOption("priya.sales");
  await expect(page.getByText("LBOTHER00001")).toBeVisible();
  await expect(page.getByText("LB22JZJ5YA56")).toHaveCount(0);
  await page.getByLabel("Prepared by").selectOption("all");
  await page.getByLabel("Attempt outcome").selectOption("UNKNOWN");
  await expect(page.getByText("LB22JZJ5YA56")).toBeVisible();
  await expect(page.getByText("LBOTHER00001")).toHaveCount(0);
  await expect(page).toHaveURL(/outcome=UNKNOWN/);
  await page.getByRole("button", { name: "Clear", exact: true }).click();
  await expect(page.getByText("LBOTHER00001")).toBeVisible();
});

for (const [w, h] of [[1024, 768], [390, 844]] as const) test(`no page overflow at ${w}`, async ({ page }) => {
  await page.setViewportSize({ width: w, height: h });
  for (const path of ["/inventory", "/audit", "/admin", "/operations", "/operations/new", `/operations/${OP}`]) {
    await go(page, path, { queue: [summary("UNKNOWN")] });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), path).toBeLessThanOrEqual(0);
  }
});
