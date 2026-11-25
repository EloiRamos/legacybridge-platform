import { expect, test } from "@playwright/test";

import { as, createDraft } from "../helpers";

// UI-01: every route renders for a permitted role, with the fictional/simulated footer on every page.
test("every route renders for the operations manager", async ({ browser }) => {
  const page = await as(browser, "marta.ops");
  // Page titles (overview title depends on the live queues)
  const routes: [string, string | RegExp][] = [
    ["/", /needs? a person|Nothing needs a person/], ["/customers", "Customers"], ["/inventory", "Inventory"],
    ["/operations", "Every order, with its proof"], ["/operations/new", "New order draft"],
    ["/approvals", /Review exactly what will be sent|Not available/], ["/reconciliation", "Reconciliation"],
    ["/audit", "The chain of custody"], ["/assistant", "Ask, don’t act"], ["/reauth", "Confirm your identity"],
    ["/customers/halden/00417", "Nordkap"],
  ];
  for (const [path, heading] of routes) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(heading);
    await expect(page.getByRole("contentinfo")).toContainText("simulated legacy systems");
    await expect(page.getByRole("alert")).toHaveCount(0);
  }
});

test("admin route renders for the system administrator", async ({ browser }) => {
  const page = await as(browser, "tomas.admin");
  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Administration");
  await expect(page.getByRole("heading", { name: "Users and roles" })).toBeVisible();
});

test("draft → detail with only the server-permitted actions; no token in browser storage", async ({ browser }) => {
  const page = await as(browser, "daniel.sales");
  const id = await createDraft(page);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Draft");
  await expect(page.getByRole("button", { name: "Submit for approval" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Approve/ })).toHaveCount(0); // own draft: never offered (SoD-1)
  await page.getByRole("button", { name: "Submit for approval" }).click();
  // validation reads HALDEN live; its frozen rate limit (30 req/10 s) can make the v1 client back off for seconds
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Awaiting approval", { timeout: 30_000 });
  const storage = await page.evaluate(() => ({ local: window.localStorage.length, session: window.sessionStorage.length }));
  expect(storage).toEqual({ local: 0, session: 0 }); // SEC-08
  expect(id).toMatch(/[0-9a-f-]{36}/);
});

test("customer search shows ambiguity and the curated party without merging", async ({ browser }) => {
  const page = await as(browser, "sanna.inventory");
  await page.goto("/customers");
  await page.getByLabel("Search both ERPs").fill("alder");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByText("Similar names found in the same system")).toBeVisible();
  await expect(page.getByRole("link", { name: "10233" })).toBeVisible();
  await expect(page.getByRole("link", { name: "10871" })).toBeVisible();
});

test("login form signs in and lands on a page the role may read", async ({ page }) => {
  const { password } = await import("../helpers");
  await page.goto("/login");
  await page.getByLabel("Username").fill("tomas.admin");
  await page.getByLabel("Password").fill(password("tomas.admin"));
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/admin$/);
});
