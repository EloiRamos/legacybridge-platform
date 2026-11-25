import AxeBuilder from "@axe-core/playwright";
import { expect, type Locator, type Page, test } from "@playwright/test";

import { clean, CSRF, detail, HASH, inventory, mock, type Mock, type MockOptions, OP, ORIGIN, summary } from "./fixtures";

const VIEWPORTS = [[1440, 900], [1024, 768], [390, 844], [360, 800]] as const;
const PHONES = VIEWPORTS.slice(2);
const h1 = (p: Page) => p.getByRole("heading", { level: 1 });

async function open(page: Page, path: string, o: MockOptions = {}, size: readonly [number, number] = VIEWPORTS[0]): Promise<Mock> {
  await page.setViewportSize({ width: size[0], height: size[1] });
  const m = await mock(page, o);
  await page.goto(path);
  await expect(h1(page)).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);
  return m;
}
async function axe(page: Page) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(r.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
}
const noDocOverflow = async (page: Page) =>
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
/** The element's centre is what the user would actually hit (nothing sticky is painted over it). */
const uncovered = (l: Locator) => l.evaluate((e) => {
  const r = e.getBoundingClientRect();
  const x = r.left + Math.min(r.width / 2, 40), y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  return { inViewport: r.top >= 0 && r.bottom <= window.innerHeight && r.left >= 0 && r.right <= window.innerWidth + 0.5, hit: !!hit && (e === hit || e.contains(hit)) };
});
const OK = { inViewport: true, hit: true };

test.describe("G11-M03 scrollable tables", () => {
  const PAGES: [string, string, MockOptions][] = [
    ["rich order", `/operations/${OP}`, { detail: detail("UNKNOWN", ["recheck", "escalate"]) }],
    ["administration", "/admin", {}],
    ["customer card", "/customers/skarvik/KND-0081", {}],
    ["customers", "/customers?q=nordkap", {}],
    ["inventory", "/inventory", {}],
    ["approvals", "/approvals", { queue: [summary("READY")], detail: detail("READY", ["approve", "decline"]) }],
  ];
  for (const [name, path, o] of PAGES) for (const size of VIEWPORTS) {
    test(`${name} ${size[0]}: focusable exactly while overflowing, named, axe clean`, async ({ page }) => {
      const m = await open(page, path, o, size);
      await expect(page.locator(".tblw").first()).toBeVisible();
      await expect.poll(() => page.locator(".tblw").evaluateAll((all) => all.filter((e) => {
        const over = e.scrollWidth > e.clientWidth;
        const ok = over ? (e as HTMLElement).tabIndex === 0 && e.getAttribute("role") === "region" && !!e.getAttribute("aria-label") : !e.hasAttribute("tabindex") && !e.hasAttribute("role");
        return !ok;
      }).length)).toBe(0);
      const names = await page.locator(".tblw[role=region]").evaluateAll((e) => e.map((x) => x.getAttribute("aria-label")));
      expect(new Set(names).size).toBe(names.length);
      await axe(page);
      await noDocOverflow(page);
      clean(m);
    });
  }
  for (const size of VIEWPORTS) test(`rich order ${size[0]}: Tab reaches the region and arrow keys scroll it`, async ({ page }) => {
    const m = await open(page, `/operations/${OP}`, { detail: detail("UNKNOWN", ["recheck", "escalate"]) }, size);
    const region = page.locator(".records .tblw[role=region]").first(); // the first record table that overflows at this width
    await expect(region).toBeVisible();
    for (let i = 0; i < 60 && !(await region.evaluate((e) => e === document.activeElement)); i++) await page.keyboard.press("Tab");
    await expect(region).toBeFocused();
    expect(await region.evaluate((e) => getComputedStyle(e).outlineStyle)).toBe("solid");
    expect(await region.evaluate((e) => e.scrollLeft)).toBe(0);
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => region.evaluate((e) => e.scrollLeft)).toBeGreaterThan(0);
    for (let i = 0; i < 60; i++) await page.keyboard.press("ArrowRight");
    await expect.poll(() => region.evaluate((e) => e.scrollWidth - e.clientWidth - e.scrollLeft)).toBeLessThanOrEqual(1);
    for (let i = 0; i < 60; i++) await page.keyboard.press("ArrowLeft");
    await expect.poll(() => region.evaluate((e) => e.scrollLeft)).toBe(0);
    await page.keyboard.press("Tab"); // not a trap
    await expect(region).not.toBeFocused();
    clean(m);
  });
  test("a resize adds and removes the tab stop", async ({ page }) => {
    await open(page, "/customers?q=nordkap");
    const w = page.locator(".tblw");
    await expect(w).not.toHaveAttribute("tabindex");
    await page.setViewportSize({ width: 360, height: 800 });
    await expect(w).toHaveAttribute("tabindex", "0");
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(w).not.toHaveAttribute("tabindex");
  });
  test("table semantics preserved", async ({ page }) => {
    await open(page, `/operations/${OP}`, { detail: detail("UNKNOWN", ["recheck"]) });
    const t = page.getByRole("table", { name: "Approvals" });
    await expect(t.getByRole("columnheader")).toHaveCount(8);
    await expect(t.getByRole("row")).toHaveCount(2);
  });
});

test.describe("G11-M04 confirmation", () => {
  const CASES = [
    { action: "withdraw", opener: "Withdraw draft", confirm: "Withdraw order", from: "DRAFT", to: "WITHDRAWN", shows: "Withdrawn", body: null },
    { action: "escalate", opener: "Escalate", confirm: "Escalate order", from: "UNKNOWN", to: "ESCALATED", shows: "Needs decision", body: { note: "" } },
  ] as const;
  for (const c of CASES) test.describe(c.action, () => {
    const start = () => detail(c.from, c.action === "withdraw" ? ["revise", "submit_for_approval", "withdraw"] : ["recheck", "escalate"], c.action === "escalate");
    const accept: MockOptions["onPost"] = (_r, live) => { live.detail = detail(c.to, [], c.action === "escalate"); return { status: 200, body: live.detail.operation }; };
    const opener = (p: Page) => p.getByRole("button", { name: c.opener, exact: true });
    const dialog = (p: Page) => p.getByRole("dialog");

    for (const [how, cancel] of [
      ["Cancel", (p: Page) => dialog(p).getByRole("button", { name: "Cancel" }).click()],
      ["Escape", (p: Page) => p.keyboard.press("Escape")],
      ["Close", (p: Page) => dialog(p).getByRole("button", { name: "Close" }).click()],
    ] as const) test(`${how}: zero mutation requests, focus returns to the opener`, async ({ page }) => {
      const m = await open(page, `/operations/${OP}`, { detail: start(), onPost: accept });
      await opener(page).focus();
      await page.keyboard.press("Enter");
      await expect(dialog(page)).toBeVisible();
      expect(m.mutations).toHaveLength(0); // opening never mutates
      await cancel(page);
      await expect(dialog(page)).toHaveCount(0);
      await expect(opener(page)).toBeFocused();
      await page.waitForTimeout(400);
      expect(m.mutations).toHaveLength(0);
      clean(m);
    });

    test("opens on the safe choice, named, focus contained, axe clean; Enter on it cancels", async ({ page }) => {
      const m = await open(page, `/operations/${OP}`, { detail: start(), onPost: accept });
      await opener(page).click();
      await expect(dialog(page)).toHaveAccessibleName(/.+/);
      await expect(dialog(page).getByRole("button", { name: "Cancel" })).toBeFocused();
      for (let i = 0; i < 8; i++) {
        await page.keyboard.press("Tab");
        expect(await page.evaluate(() => document.activeElement === document.body || !!document.activeElement?.closest("dialog"))).toBe(true);
      }
      await axe(page);
      await dialog(page).getByRole("button", { name: "Cancel" }).focus();
      await page.keyboard.press("Enter");
      await expect(dialog(page)).toHaveCount(0);
      expect(m.mutations).toHaveLength(0);
    });

    test("confirm by keyboard: exactly one authorized request, no optimistic claim, focus on the heading", async ({ page }) => {
      let release = () => {};
      const held = new Promise<void>((r) => { release = r; });
      const m = await open(page, `/operations/${OP}`, { detail: start(), onPost: async (r, live) => { await held; return accept!(r, live); } });
      await opener(page).focus();
      await page.keyboard.press("Enter");
      await dialog(page).getByRole("button", { name: c.confirm }).focus();
      await page.keyboard.press("Enter");
      await expect.poll(() => m.mutations.length).toBe(1);
      await expect(h1(page)).not.toContainText(c.shows); // nothing claimed before the server answers
      // while in flight the decision can be neither repeated nor seemingly cancelled
      await expect(dialog(page).getByRole("button", { name: "Working…" })).toBeDisabled();
      await expect(dialog(page).getByRole("button", { name: "Cancel" })).toBeDisabled();
      for (let i = 0; i < 4; i++) await page.keyboard.press("Escape");
      await page.keyboard.press("Enter");
      await expect(dialog(page)).toBeVisible();
      release();
      await expect(dialog(page)).toHaveCount(0);
      await expect(h1(page)).toContainText(c.shows);
      await expect(h1(page)).toBeFocused();
      await page.waitForTimeout(3500); // past one poll interval: nothing is retried
      expect(m.mutations).toHaveLength(1);
      const r = m.mutations[0]!;
      expect([r.method(), r.url()]).toEqual(["POST", `${ORIGIN}/api/v1/operations/${OP}/${c.action}`]);
      expect(r.headers()["x-csrf-token"]).toBe(CSRF);
      expect(r.postData() ? r.postDataJSON() : null).toEqual(c.body);
      clean(m);
    });

    test("two submits in the same tick still send one request", async ({ page }) => {
      const m = await open(page, `/operations/${OP}`, { detail: start(), onPost: accept });
      await opener(page).click();
      await dialog(page).locator("form").evaluate((f: HTMLFormElement) => { f.requestSubmit(); f.requestSubmit(); });
      await expect(dialog(page)).toHaveCount(0);
      await page.waitForTimeout(300);
      expect(m.mutations).toHaveLength(1);
    });

    test("409 refusal: shown, not retried, state unchanged, a new attempt is a new decision", async ({ page }) => {
      const m = await open(page, `/operations/${OP}`, { detail: start(), onPost: () => ({ status: 409, body: { status: 409, code: "CONFLICT_STATE", title: "Conflict", detail: "not permitted from state RECONCILING", request_id: "rq-1" } }) });
      const before = await h1(page).innerText();
      await opener(page).click();
      await dialog(page).getByRole("button", { name: c.confirm }).click();
      await expect(dialog(page)).toHaveCount(0);
      await expect(page.getByRole("alert")).toContainText("CONFLICT_STATE");
      await expect(opener(page)).toBeFocused(); // the action is still available, so focus returns to it
      await page.waitForTimeout(3500);
      expect(m.mutations).toHaveLength(1);
      expect(await h1(page).innerText()).toBe(before);
      await opener(page).click();
      await expect(dialog(page)).toBeVisible();
      expect(m.mutations).toHaveLength(1);
      clean(m);
    });

    for (const status of [403, 500]) test(`${status} refusal is not retried and claims nothing`, async ({ page }) => {
      const m = await open(page, `/operations/${OP}`, { detail: start(), onPost: () => ({ status, body: { status, code: status === 403 ? "FORBIDDEN" : "INTERNAL", title: "Refused", detail: "refused" } }) });
      await opener(page).click();
      await dialog(page).getByRole("button", { name: c.confirm }).click();
      await expect(dialog(page)).toHaveCount(0);
      await expect(h1(page)).not.toContainText(c.shows);
      await page.waitForTimeout(3500);
      expect(m.mutations).toHaveLength(1);
    });

    test("not rendered unless the server permits it", async ({ page }) => {
      await open(page, `/operations/${OP}`, { detail: detail(c.from, ["recheck"], false) });
      await expect(opener(page)).toHaveCount(0);
    });

    for (const size of VIEWPORTS) test(`dialog usable at ${size[0]}`, async ({ page }) => {
      await open(page, `/operations/${OP}`, { detail: start(), onPost: accept }, size);
      await opener(page).click();
      expect(await uncovered(dialog(page).getByRole("button", { name: c.confirm }))).toEqual(OK);
      expect(await uncovered(dialog(page).getByRole("button", { name: "Cancel" }))).toEqual(OK);
      await noDocOverflow(page);
    });
  });

  test("wording: escalation says not known and never failed; withdrawal says final", async ({ page }) => {
    const m = await open(page, `/operations/${OP}`, { detail: detail("UNKNOWN", ["recheck", "escalate"]) });
    await page.getByRole("button", { name: "Escalate", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText(/not known/i);
    expect(await page.getByRole("dialog").innerText()).not.toMatch(/(?<!not )\bfailed\b/i);
    await page.keyboard.press("Escape");
    m.live.detail = detail("DRAFT", ["withdraw"], false);
    await page.reload();
    await page.getByRole("button", { name: "Withdraw draft", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText(/final/i);
  });
  test("read-only re-check stays a single click", async ({ page }) => {
    const m = await open(page, `/operations/${OP}`, { detail: detail("UNKNOWN", ["recheck", "escalate"]), onPost: () => ({ status: 202, body: {} }) });
    await page.getByRole("button", { name: "Re-check (read-only)" }).click();
    await expect.poll(() => m.mutations.length).toBe(1);
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
});

test.describe("G11-M05 mobile inventory", () => {
  for (const size of PHONES) test(`${size[0]}: status, availability and evidence visible with no horizontal scroll`, async ({ page }) => {
    const m = await open(page, "/inventory", { inventory: inventory() }, size);
    const t = page.getByRole("table", { name: "Availability per canonical item and backend" });
    expect(await page.locator(".tblw").evaluate((e) => e.scrollWidth - e.clientWidth)).toBeLessThanOrEqual(0);
    const rows = t.locator("tbody tr");
    await expect(rows).toHaveCount(5);
    for (let i = 0; i < 5; i++) {
      for (const field of ["Status", "Available (base)", "Freshness"]) {
        const cell = rows.nth(i).locator(`td[data-label="${field}"]`);
        await cell.scrollIntoViewIfNeeded();
        await cell.evaluate((e) => e.scrollIntoView({ block: "center" }));
        expect(await uncovered(cell), `row ${i} ${field}`).toEqual(OK);
        expect(await cell.evaluate((e) => getComputedStyle(e, "::before").content)).toContain(field);
      }
    }
    await noDocOverflow(page);
    await axe(page);
    clean(m);
  });
  for (const size of PHONES) test(`${size[0]}: table semantics survive the stacked layout`, async ({ page }) => {
    await open(page, "/inventory", {}, size);
    const t = page.getByRole("table", { name: "Availability per canonical item and backend" });
    await expect(t.getByRole("columnheader")).toHaveCount(8);
    await expect(t.getByRole("row")).toHaveCount(6);
    await expect(t.getByRole("row").nth(1).getByRole("cell")).toHaveCount(8);
  });
  for (const size of VIEWPORTS) test(`${size[0]}: UNDETERMINED is neither a quantity nor out of stock`, async ({ page }) => {
    await open(page, "/inventory", {}, size);
    const row = page.locator("tbody tr").filter({ hasText: "NVB-HYD-2SN-12" });
    await expect(row.locator(".bd")).toHaveText("UNDETERMINED");
    await expect(row.locator(".bd")).toHaveClass(/t-warn/);
    await expect(row.getByText("—", { exact: true })).toBeVisible();
    await expect(page.locator("tbody tr").filter({ hasText: "HX-HOSE-2SN12" }).locator(".bd")).toHaveText(/^OUT OF[ _]STOCK$/);
    await expect(page.locator("tbody tr").filter({ hasText: "HX-HOSE-2SN12" }).locator("td.num")).toHaveText("0");
  });
  for (const size of PHONES) test(`${size[0]}: degraded backend and raw rows`, async ({ page }) => {
    await open(page, "/inventory", { inventory: inventory(true) }, size);
    await expect(page.getByRole("alert")).toContainText("HALDEN: no current figures.");
    await expect(page.locator("tbody tr")).toHaveCount(3);
    await expect(page.locator("tbody .sys, tbody td").filter({ hasText: /^HALDEN$/ })).toHaveCount(0);
    await page.getByRole("button", { name: "Raw rows" }).first().click();
    await expect(page.locator("tbody pre")).toBeVisible();
    await noDocOverflow(page);
    await axe(page);
  });
  test("360: whole endpoint failing stays an error, not an empty inventory", async ({ page }) => {
    await open(page, "/inventory", { inventory: "503" }, PHONES[1]!);
    await expect(page.getByRole("alert")).toContainText("BACKEND_UNAVAILABLE");
    await expect(page.locator("table")).toHaveCount(0);
  });
  for (const size of VIEWPORTS.slice(0, 2)) test(`${size[0]}: desktop stays a table`, async ({ page }) => {
    await open(page, "/inventory", {}, size);
    expect(await page.locator("tbody tr").first().evaluate((e) => getComputedStyle(e).display)).toBe("table-row");
    expect(await page.locator("tbody td").first().evaluate((e) => getComputedStyle(e, "::before").content)).toBe("none");
  });
});

test.describe("G11-M06 mobile action bars", () => {
  const review: MockOptions = { queue: [summary("READY")], detail: detail("READY", ["approve", "decline"]) };
  /** Scrolls through the page; in every position the bar must not overlap any of the given elements. */
  async function neverOverlaps(page: Page, selectors: string[]) {
    const hits = await page.evaluate((sels) => {
      const bar = document.querySelector(".sticky-act")!;
      const out: string[] = [];
      for (let y = 0; y <= document.documentElement.scrollHeight; y += 40) {
        window.scrollTo(0, y);
        const b = bar.getBoundingClientRect();
        for (const s of sels) for (const e of document.querySelectorAll(s)) {
          const r = e.getBoundingClientRect();
          if (r.height && r.top < b.bottom - 1 && r.bottom > b.top + 1) out.push(`${s}@${y}`);
        }
      }
      window.scrollTo(0, 0);
      return out;
    }, selectors);
    expect(hits).toEqual([]);
  }
  for (const size of PHONES) {
    test(`approvals ${size[0]}: bar never covers the evidence; hash, lines and decision all reachable`, async ({ page }) => {
      const m = await open(page, "/approvals", review, size);
      await neverOverlaps(page, [".eyes", ".pin", ".pin .hx", ".lt tbody tr", ".pills"]);
      const hash = page.locator(".pin .hx");
      await expect(hash).toContainText(HASH);
      for (const l of [hash, page.locator(".lt tbody tr").first().locator("td").first(), page.getByRole("button", { name: /^Approve v1/ }), page.getByRole("button", { name: "Decline…" })]) {
        await l.evaluate((e) => e.scrollIntoView({ block: "center" }));
        expect(await uncovered(l)).toEqual(OK);
      }
      await page.getByRole("button", { name: /^Approve v1/ }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: /^Approve v1/ })).toBeFocused();
      expect(m.mutations).toHaveLength(0);
      await axe(page);
    });
    test(`new order ${size[0]}: fields never covered, keyboard focus never hidden, draft can be filled`, async ({ page, browserName }) => {
      const next = browserName === "webkit" ? "Alt+Tab" : "Tab"; // Safari's own key for stepping through every control
      const m = await open(page, "/operations/new", {}, size);
      await neverOverlaps(page, ["#backend", "#customer", "#sku-0", "#qty-0"]);
      for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Add line" }).click();
      await neverOverlaps(page, ["#sku-0", "#qty-0", "#sku-3", "#qty-3"]);
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.locator("#backend").focus();
      for (let i = 0; i < 24; i++) {
        const f = page.locator(":focus");
        const id = await f.evaluate((e) => e.id || e.textContent);
        expect(await uncovered(f), `focused: ${id}`).toEqual(OK);
        if (id === "Create draft") break;
        await page.keyboard.press(next);
      }
      await expect(page.getByRole("button", { name: "Create draft" })).toBeFocused();
      await page.locator("#sku-3").fill("BW-1004");
      await page.locator("#qty-3").fill("7");
      await expect(page.locator("#qty-3")).toHaveValue("7");
      await noDocOverflow(page);
      await axe(page);
      clean(m);
    });
  }
  for (const size of VIEWPORTS.slice(0, 2)) test(`${size[0]}: desktop bar stays sticky`, async ({ page }) => {
    await open(page, "/approvals", review, size);
    expect(await page.locator(".sticky-act").evaluate((e) => [getComputedStyle(e).position, getComputedStyle(e).bottom])).toEqual(["sticky", "16px"]);
  });
});

// Evidence only: LBF_MOCKED_SHOTS=<dir> writes the bounded screenshot set from the bundle under test.
test.describe("evidence capture", () => {
  const dir = process.env.LBF_MOCKED_SHOTS;
  test.skip(!dir, "set LBF_MOCKED_SHOTS to capture");
  const review: MockOptions = { queue: [summary("READY")], detail: detail("READY", ["approve", "decline"]) };
  const conflict: MockOptions["onPost"] = () => ({ status: 409, body: { status: 409, code: "CONFLICT_STATE", title: "Conflict", detail: "not permitted from state RECONCILING" } });
  for (const size of VIEWPORTS) test(`shots ${size[0]}`, async ({ page }) => {
    const w = size[0], phone = w <= 390;
    const shot = (n: string) => page.screenshot({ path: `${dir}/${n}-${w}.png` });
    const fresh = async (path: string, o: MockOptions = {}) => { await page.unrouteAll(); await open(page, path, o, size); };
    await fresh(`/operations/${OP}`, { detail: detail("UNKNOWN", ["recheck", "escalate"]), onPost: conflict });
    const region = page.locator(".records .tblw[role=region]").first();
    await region.evaluate((e) => e.scrollIntoView({ block: "center" }));
    await page.keyboard.press("Tab");
    await region.focus();
    for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(300);
    await shot("m03-records-focused-scrolled");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.getByRole("button", { name: "Escalate", exact: true }).click();
    await shot("m04-escalate-confirm");
    if (w === 1440 || w === 390) {
      await page.getByRole("button", { name: "Escalate order" }).click();
      await expect(page.getByRole("alert")).toBeVisible();
      await shot("m04-escalate-409");
    }
    await fresh(`/operations/${OP}`, { detail: detail("DRAFT", ["revise", "submit_for_approval", "withdraw"], false) });
    await page.getByRole("button", { name: "Withdraw draft", exact: true }).click();
    await shot("m04-withdraw-confirm");
    await fresh("/inventory");
    if (phone) await page.locator("table.inv").evaluate((e) => e.scrollIntoView({ block: "start" }));
    await shot("m05-inventory");
    if (phone) {
      await fresh("/inventory", { inventory: inventory(true) });
      await page.getByRole("alert").evaluate((e) => window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 70));
      await shot("m05-inventory-degraded");
    }
    await fresh("/approvals", review);
    if (phone) await page.locator(".eyes").evaluate((e) => window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - 70));
    await shot("m06-approvals-evidence");
    if (phone) {
      await page.locator(".sticky-act").evaluate((e) => e.scrollIntoView({ block: "center" }));
      await shot("m06-approvals-decision");
    }
    await fresh("/operations/new");
    if (phone) await page.locator("#sku-0").evaluate((e) => e.scrollIntoView({ block: "center" }));
    await shot("m06-new-order");
  });
});
