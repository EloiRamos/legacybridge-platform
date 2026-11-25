import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, type Page, test } from "@playwright/test";

import { approve, as, ctl, detail, evidenceShot, heading, operatorCommand, oracle, readyOrder, transitions, USERS, waitStatus } from "../helpers";

// Role journeys against the test profile: BR-01 customers, BR-05 reconciliation queue, F2-09 verified
// resolution, OPS-05 runbook procedures (withdraw unclaimed, unmatched acknowledgement), UI-03 buttons = permitted
// actions for every role.
test.describe.configure({ mode: "serial" });

const FIXTURE = fileURLToPath(new URL("../../../../acceptance/fixtures/customer_expectations.yaml", import.meta.url));

test("BR-01 · customer search shows exactly the expected accounts, groups and links; nothing is merged", async ({ browser }) => {
  const page = await as(browser, "daniel.sales");
  const fixture = readFileSync(FIXTURE, "utf-8");
  // nordkap: one curated party linking both ERPs
  await page.goto("/customers");
  await page.getByLabel("Search both ERPs").fill("nordkap");
  await page.getByRole("button", { name: "Search" }).click();
  const party = page.getByRole("region", { name: "Party Nordkap Marine Services AS" });
  await expect(party).toContainText("HALDEN 00417");
  await expect(party).toContainText("SKARVIK KND-0081");
  const rows = page.getByRole("table", { name: "Matching accounts" }).locator("tbody tr");
  await expect(rows).toHaveCount(2);
  expect(fixture).toContain('[[halden, "00417"], [skarvik, KND-0081]]');
  // alder: two similar HALDEN accounts stay separate and must be chosen explicitly
  await page.getByLabel("Search both ERPs").fill("alder");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByText("These are separate accounts — choose explicitly; nothing has been merged.")).toBeVisible();
  await expect(rows).toHaveCount(2);
  await expect(page.getByRole("table", { name: "Matching accounts" })).toContainText("10233");
  await expect(page.getByRole("table", { name: "Matching accounts" })).toContainText("10871");
  await expect(page.getByText("ambiguous name")).toHaveCount(2);
  await evidenceShot(page, "br01-customers-alder-ambiguity");
});

test("BR-05 · an UNKNOWN order appears in the reconciliation queue within 5 s, with read-only actions only", async ({ browser }) => {
  test.setTimeout(120_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [id, cv] = await readyOrder(daniel, "halden", "00417");
  ctl("wiretap-arm", "blackhole", "--hold", "20");
  const priya = await as(browser, "priya.sales");
  await approve(priya, id);
  await waitStatus(priya, id, "Outcome unknown", 30_000);
  const t0 = Date.now();
  const rafael = await as(browser, "rafael.integration");
  await rafael.goto("/reconciliation");
  await expect(rafael.getByRole("region", { name: "Reconciliation queue" })).toContainText(cv, { timeout: 5_000 });
  expect(Date.now() - t0).toBeLessThanOrEqual(5_000);
  await rafael.goto(`/operations/${id}`);
  await expect(rafael.getByRole("note").first()).toContainText("Do not re-enter it");
  const buttons = await actionLabels(rafael);
  expect(buttons.every((b) => ["Re-check (read-only)", "Escalate"].includes(b))).toBe(true);
  await evidenceShot(rafael, "br05-reconciliation-queue-unknown");
  expect(oracle(cv).halden_orders).toBe(0);
});

test("F2-09 · verified resolution: a wrong reference is refused; the matching order confirms MANUAL_VERIFIED", async ({ browser }) => {
  test.setTimeout(240_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [id, cv] = await readyOrder(daniel, "halden", "00417");
  // the order commits but every read fails for a while: the operation stays UNKNOWN and is escalated by hand
  ctl("halden-fault", "POST", "/halden/orders", "commit_then_500");
  ctl("halden-fault", "GET", "/halden/orders", "error_500", "--count", "50");
  const priya = await as(browser, "priya.sales");
  await approve(priya, id);
  const marta = await as(browser, "marta.ops");
  await waitStatus(marta, id, "Outcome unknown", 60_000);
  await marta.getByRole("button", { name: "Escalate" }).click();
  // The click only opens the confirmation; the order escalates on the confirmed command
  await expect(marta.getByRole("dialog")).toContainText("not known");
  expect((await detail(marta, id)).operation.state).not.toBe("ESCALATED");
  await marta.getByRole("button", { name: "Escalate order", exact: true }).click();
  await waitStatus(marta, id, "Needs decision", 30_000);
  ctl("halden-clear-faults"); // reads work again for the human's verification
  const orderNo = oracle(cv).halden_order_nos[0]!;
  await marta.getByRole("button", { name: "Resolve — verify order…" }).click();
  await marta.getByLabel("Backend order reference").fill("SO-240101"); // an existing order of the same account, other lines
  await marta.getByRole("button", { name: "Read and verify" }).click();
  await expect(marta.getByRole("alert")).toBeVisible({ timeout: 30_000 });
  await waitStatus(marta, id, "Needs decision", 10_000);
  await marta.getByRole("button", { name: "Resolve — verify order…" }).click();
  await marta.getByLabel("Backend order reference").fill(orderNo);
  await marta.getByRole("button", { name: "Read and verify" }).click();
  await waitStatus(marta, id, "Confirmed in HALDEN", 60_000);
  const ev = await (await marta.request.get(`/api/v1/operations/${id}`)).json() as { operation: { last_evidence: string } };
  expect(ev.operation.last_evidence).toBe("MANUAL_VERIFIED");
  expect(await transitions(marta)).toContain("ESCALATED → CONFIRMED");
  await evidenceShot(marta, "f2-09-verified-resolution");
});

test("OPS-05 · runbook: withdraw an unclaimed SKARVIK message ⇒ NOT_APPLIED (proven)", async ({ browser }) => {
  test.setTimeout(180_000);
  ctl("reset");
  ctl("skarvik-fault", "pause_pickup", "--value", "true");
  try {
    const daniel = await as(browser, "daniel.sales");
    const [id, cv] = await readyOrder(daniel, "skarvik", "KND-0081");
    const priya = await as(browser, "priya.sales");
    await approve(priya, id);
    const marta = await as(browser, "marta.ops");
    await waitStatus(marta, id, "Outcome unknown", 90_000);
    // the command's own response, then the persisted state and audit chain, then the page (T-G2-01)
    const res = await operatorCommand(marta, id, "withdraw-unclaimed", "Withdraw unclaimed message…", "Withdraw");
    expect(res.status).toBe(200);
    expect(res.body.state).toBe("NOT_APPLIED");
    expect(res.body.detail).toMatch(/moved to withdrawn\/ before pickup$/);
    const d = await detail(marta, id);
    expect([d.operation.state, d.operation.last_evidence]).toEqual(["NOT_APPLIED", "WITHDRAWN_BEFORE_PICKUP"]);
    const withdraws = d.authorizations.filter((a) => a.kind === "WITHDRAW");
    expect(withdraws.map((a) => a.state)).toEqual(["CONSUMED"]);
    const created = d.timeline.find((e) => e.event_type === "AUTHORIZATION_CREATED" && e.payload.kind === "WITHDRAW")!;
    expect(created.actor_username).toBe("marta.ops");
    expect(created.correlation.authorization_id).toBe(withdraws[0]!.id);
    const after = d.timeline.filter((e) => e.seq > created.seq);
    expect(after.map((e) => e.event_type)).toEqual(["WITHDRAW_RESULT", "OUTCOME_RECORDED"]);
    expect(after[0]!.correlation.authorization_id).toBe(withdraws[0]!.id);
    expect(after[0]!.payload.withdrawn).toBe(true);
    expect(after[1]!.state).toMatchObject({ from: "UNKNOWN", to: "NOT_APPLIED" });
    expect(after[1]!.payload.evidence_class).toBe("WITHDRAWN_BEFORE_PICKUP");
    await waitStatus(marta, id, "Not applied (proven)", 30_000);
    await evidenceShot(marta, "ops05-withdraw-unclaimed");
    ctl("skarvik-fault", "pause_pickup", "--value", "false");
    await marta.waitForTimeout(3_000); // SKARVIK resumes pickup: the withdrawn file is not in the inbox any more
    expect(oracle(cv).skarvik_claims).toBe(0);
  } finally {
    ctl("skarvik-fault", "pause_pickup", "--value", "false");
  }
});

test("OPS-05 · runbook: a malformed acknowledgement lands in the unmatched-ack queue", async ({ browser }) => {
  test.setTimeout(180_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [id] = await readyOrder(daniel, "skarvik", "KND-0081");
  ctl("skarvik-fault", "malformed_ack");
  const priya = await as(browser, "priya.sales");
  await approve(priya, id);
  const marta = await as(browser, "marta.ops");
  await waitStatus(marta, id, /Confirmed in SKARVIK|Outcome unknown/, 90_000);
  await marta.goto("/admin");
  const queue = marta.getByRole("region", { name: "Unmatched acknowledgements" });
  await expect(queue).toContainText("MALFORMED", { timeout: 30_000 });
  await evidenceShot(marta, "ops05-unmatched-ack-queue");
  await waitStatus(marta, id, "Confirmed in SKARVIK", 90_000); // the read-back still confirms the order
});

// ----------------------------------------------------------------------------------------------- UI-03

const LABELS: Record<string, string> = {
  revise: "Revise", submit_for_approval: "Submit for approval", approve: "Approve…", decline: "Decline…",
  withdraw: "Withdraw draft", recheck: "Re-check (read-only)", escalate: "Escalate",
  resolve_verified: "Resolve — verify order…", resolve_attested: "Resolve — attest…",
  authorize_withdraw_unclaimed: "Withdraw unclaimed message…", authorize_redelivery: "Authorize redelivery…",
};

async function actionLabels(page: Page): Promise<string[]> {
  const section = page.getByRole("region", { name: "Actions" });
  await expect(section).toBeVisible();
  return (await section.getByRole("button").allInnerTexts()).map((x) => x.trim()).sort();
}

test("UI-03 · for every role the rendered buttons equal the server's permitted actions", async ({ browser }) => {
  test.setTimeout(240_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [ready] = await readyOrder(daniel, "halden", "00417", "BW-1001", "3");
  const [draftDanielOnly] = await (async () => {
    await daniel.goto("/operations/new");
    await daniel.getByLabel("Account number").fill("00417");
    await daniel.getByLabel("Group SKU").fill("BW-1004");
    await daniel.getByLabel("Quantity").fill("1");
    await daniel.getByRole("button", { name: "Create draft" }).click();
    await expect(daniel).toHaveURL(/\/operations\/[0-9a-f-]{36}$/, { timeout: 30_000 });
    return [daniel.url().split("/").pop()!];
  })();
  const [unknown] = await readyOrder(daniel, "skarvik", "KND-0081");
  ctl("skarvik-fault", "pause_pickup", "--value", "true");
  try {
    await approve(await as(browser, "priya.sales"), unknown);
    await waitStatus(await as(browser, "marta.ops"), unknown, "Outcome unknown", 90_000);
    const cells: string[] = [];
    for (const user of USERS) {
      const page = await as(browser, user);
      for (const id of [ready, draftDanielOnly, unknown]) {
        const r = await page.request.get(`/api/v1/operations/${id}`);
        await page.goto(`/operations/${id}`);
        if (r.status() !== 200) {
          await expect(page.getByRole("region", { name: "Actions" })).toHaveCount(0);
          cells.push(`${user} ${id.slice(0, 8)} no read access (${r.status()})`);
          continue;
        }
        const expected = ((await r.json()) as { permitted_actions: string[] }).permitted_actions.map((a) => LABELS[a] ?? a).sort();
        await expect(heading(page)).toContainText("Order");
        expect(await actionLabels(page), `${user} on ${id}`).toEqual(expected);
        cells.push(`${user} ${id.slice(0, 8)} [${expected.join(", ")}]`);
      }
    }
    test.info().annotations.push({ type: "cells", description: cells.join("\n") });
    expect(cells).toHaveLength(USERS.length * 3);
  } finally {
    ctl("skarvik-fault", "pause_pickup", "--value", "false");
  }
});
