import { expect, test } from "@playwright/test";

import { approve, as, ctl, detail, evidenceShot, heading, operatorCommand, oracle, readyOrder, transitions, waitStatus } from "../helpers";

// UI-04: the three signature scenarios, driven through the console against the test profile. Faults, the
// process kill and the oracle reads happen outside the application (acceptance/e2e_ctl.py).
test.describe.configure({ mode: "serial" });

test("1 · HALDEN late commit: UNKNOWN, scheduled re-check, CONFIRMED/RECONCILED, one E8", async ({ browser }) => {
  test.setTimeout(180_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [id, cv] = await readyOrder(daniel, "halden", "00417");
  ctl("halden-fault", "POST", "/halden/orders", "delay", "--delay-ms", "2500");
  const priya = await as(browser, "priya.sales");
  await approve(priya, id);
  await waitStatus(priya, id, "Confirmed in HALDEN", 120_000);
  const t = await transitions(priya);
  expect(t).toContain("SUBMITTING → UNKNOWN");
  expect(t).toContain("RECONCILING → CONFIRMED");
  await evidenceShot(priya, "signature-1-halden-late-commit");
  const o = oracle(cv);
  expect([o.wire_e8, o.halden_orders]).toEqual([1, 1]); // wire-tap: exactly one E8; HALDEN: one order
});

test("2 · SKARVIK lost acknowledgement: ACK_OVERDUE, extract read-back confirms", async ({ browser }) => {
  test.setTimeout(180_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [id, cv] = await readyOrder(daniel, "skarvik", "KND-0081");
  ctl("skarvik-fault", "drop_ack");
  const priya = await as(browser, "priya.sales");
  await approve(priya, id);
  await waitStatus(priya, id, /Confirmed in SKARVIK/, 120_000);
  const t = await transitions(priya);
  expect(t).toContain("SUBMITTING → AWAITING_ACK");
  expect(t).toContain("AWAITING_ACK → UNKNOWN");
  await evidenceShot(priya, "signature-2-skarvik-lost-ack-read-back");
  const o = oracle(cv);
  expect([o.skarvik_claims, o.skarvik_orders]).toEqual([1, 1]);
});

test("2b · SKARVIK lost acknowledgement: authorized redelivery inside the window ⇒ DEDUP_CONFIRMED", async ({ browser }) => {
  test.setTimeout(180_000);
  ctl("reset");
  const daniel = await as(browser, "daniel.sales");
  const [id, cv] = await readyOrder(daniel, "skarvik", "KND-0081");
  ctl("skarvik-fault", "drop_ack");
  ctl("skarvik-fault", "stale_extracts", "--value", "true"); // no read-back before the human decides
  const priya = await as(browser, "priya.sales");
  await approve(priya, id);
  const marta = await as(browser, "marta.ops");
  await waitStatus(marta, id, "Outcome unknown", 90_000);
  await expect(marta.getByText("Redelivery window closes")).toBeVisible();
  // the command's own response and the authorization it created, before anything else is awaited (T-G2-01)
  const res = await operatorCommand(marta, id, "redelivery", "Authorize redelivery…", "Authorize");
  expect(res.status).toBe(202);
  expect(res.body.detail).toBe("redelivery authorized; the engine hands off the stored bytes once");
  const authorized = await detail(marta, id);
  const redeliver = authorized.authorizations.filter((a) => a.kind === "REDELIVER");
  expect(redeliver).toHaveLength(1);
  expect(["PENDING", "CLAIMED", "CONSUMED"]).toContain(redeliver[0]!.state);
  const created = authorized.timeline.find((e) => e.event_type === "AUTHORIZATION_CREATED" && e.payload.kind === "REDELIVER")!;
  expect([created.actor_username, created.correlation.authorization_id]).toEqual(["marta.ops", redeliver[0]!.id]);
  // the DUPLICATE acknowledgement confirms first as acknowledged-only; the verification read then upgrades it
  await waitStatus(marta, id, "Acknowledged — not yet read back", 90_000);
  const handed = await detail(marta, id);
  expect(handed.authorizations.filter((a) => a.kind === "REDELIVER").map((a) => a.state)).toEqual(["CONSUMED"]);
  expect(handed.transmissions.map((x) => x.kind)).toEqual(["SUBMIT", "REDELIVER"]); // exactly one redelivery
  expect(handed.transmissions[1]!.authorization_id).toBe(redeliver[0]!.id);
  expect(handed.timeline.filter((e) => e.event_type === "AUTHORIZATION_EXPIRED")).toEqual([]);
  await expect(marta.getByRole("table", { name: "Acknowledgements" })).toContainText("DUPLICATE");
  ctl("skarvik-fault", "stale_extracts", "--value", "false");
  await waitStatus(marta, id, "Confirmed in SKARVIK", 90_000);
  expect(await transitions(marta)).toContain("UNKNOWN → SUBMITTING"); // T27: the authorized redelivery
  await expect(marta.getByText(/EVIDENCE_UPGRADED/)).toBeVisible();
  const ev = await (await marta.request.get(`/api/v1/operations/${id}`)).json() as { operation: { last_evidence: string } };
  expect(ev.operation.last_evidence).toBe("DEDUP_CONFIRMED");
  await evidenceShot(marta, "signature-2b-skarvik-redelivery-dedup");
  const o = oracle(cv);
  expect([o.skarvik_claims, o.skarvik_orders]).toEqual([2, 1]); // two deliveries, one order (dedup)
});

test("3 · crash during submission: SKARVIK proven NOT_APPLIED; HALDEN UNKNOWN → escalation → attested", async ({ browser }) => {
  test.setTimeout(420_000);
  ctl("reset");
  ctl("restart-app", "before_handoff");
  try {
    const daniel = await as(browser, "daniel.sales");
    const priya = await as(browser, "priya.sales");
    const [sid, scv] = await readyOrder(daniel, "skarvik", "KND-0081");
    await approve(priya, sid);
    expect(ctl("reached", "before_handoff").reached).toBe(true);
    ctl("kill");
    ctl("restart-app", "before_handoff");
    await waitStatus(priya, sid, "Not applied (proven)", 120_000);
    await evidenceShot(priya, "signature-3a-skarvik-never-delivered");
    expect(oracle(scv).skarvik_claims).toBe(0);

    const [hid] = await readyOrder(daniel, "halden", "00417");
    await approve(priya, hid);
    expect(ctl("reached", "before_handoff").reached).toBe(true);
    ctl("kill");
  } finally {
    ctl("restart-app");
  }
  const marta = await as(browser, "marta.ops");
  const ids = await marta.request.get("/api/v1/operations?state=ESCALATED");
  expect(ids.ok()).toBeTruthy();
  // the HALDEN order: UNKNOWN after the restart (never NOT_APPLIED), escalated when the re-check limit is reached
  let hid = "";
  for (let i = 0; i < 120 && !hid; i++) {
    const list = await (await marta.request.get("/api/v1/operations?state=ESCALATED")).json() as { items: { operation_id: string; backend_id: string }[] };
    hid = list.items.find((x) => x.backend_id === "halden")?.operation_id ?? "";
    if (!hid) await marta.waitForTimeout(1_000);
  }
  expect(hid).not.toBe("");
  await waitStatus(marta, hid, "Needs decision", 10_000);
  const t = await transitions(marta);
  expect(t).toContain("SUBMITTING → UNKNOWN");
  expect(t).not.toContain("NOT_APPLIED");
  await marta.getByRole("button", { name: "Resolve — attest…" }).click();
  await marta.getByLabel("External reference").fill("PHONE-2026-0042");
  await marta.getByLabel("Note (≥ 20 characters)").fill("Customer service confirmed by phone: no order was booked.");
  await marta.getByRole("button", { name: "Close by attestation" }).click();
  await waitStatus(marta, hid, "Closed by attestation (not verified)", 30_000);
  await expect(marta.getByRole("note").filter({ hasText: "not a verified backend read" })).toBeVisible();
  await evidenceShot(marta, "signature-3b-halden-attested-not-verified");
  const cv = (await heading(marta).locator(".mono").first().innerText()).trim();
  expect(oracle(cv).wire_e8).toBe(0); // nothing reached the wire before the kill
});
