import { describe, expect, it } from "vitest";

import { type Detail, facets, journey, knowns, reasonFor, type Summary } from "./evidence";
import { STAGE_OF, STATUS, UNRESOLVED } from "./status";

// Fixtures shaped like GET /api/v1/operations/{id} (lbf.api.readmodels.detail); values are fictional.
const op = (state: Summary["state"], extra: Partial<Summary> = {}): Summary => ({
  operation_id: "00000000-0000-4000-8000-000000000001", correlation_value: "LBTEST000001", backend_id: "halden",
  native_customer_id: "00417", state, attempt_count: 1, current_version: 1, created_by: "daniel.sales", origin: "CONSOLE",
  created_at: "2026-11-22T10:00:00Z", updated_at: "2026-11-22T10:05:00Z", estimated_total: "120.00", last_evidence: null,
  last_outcome: null, verification_level: null, anchor_order_ref: null, ...extra,
});
const ev = (seq: number, event_type: string, payload: Record<string, unknown> = {}, correlation: Record<string, unknown> = { attempt_no: 1 }, state: Record<string, string> | null = null) =>
  ({ seq, event_type, occurred_at: `2026-11-22T10:0${seq % 10}:00Z`, actor_type: event_type === "APPROVAL_GRANTED" ? "USER" : "ENGINE",
    actor_username: event_type === "APPROVAL_GRANTED" ? "priya.sales" : null, on_behalf_of: null, state, correlation, payload, event_hash: "h".repeat(64) });
const detail = (o: Summary, timeline: ReturnType<typeof ev>[], extra: Partial<Detail> = {}): Detail => ({
  operation: o, versions: [{ version: 1, content_hash: "c".repeat(64), content: { lines: [] } }], validation: null,
  approvals: [{ attempt_no: 1, decision: "APPROVED", decided_by: "priya.sales", content_hash: "c".repeat(64) }], authorizations: [],
  transmissions: [{ attempt_no: 1, transmission_no: 1, state: "OBSERVED", message_id: "m-1", payload_sha256: "p".repeat(64), observation: { kind: "UNCERTAIN" } }],
  acknowledgements: [], reconciliation_runs: [], escalations: [], timeline, permitted_actions: [], capabilities: {},
  schedule: { next_recheck_at: "2026-11-22T10:10:00Z", redelivery_deadline: null, escalation_at: null }, ...extra,
} as unknown as Detail);

const unknownTimeline = [
  ev(1, "APPROVAL_GRANTED", {}, { attempt_no: 1, draft_version: 1, content_hash: "c".repeat(64) }),
  ev(2, "TRANSMISSION_STARTED", { kind: "SUBMIT" }, { attempt_no: 1, transmission_no: 1, message_id: "m-1" }, { from: "APPROVED", to: "SUBMITTING" }),
  ev(3, "TRANSMISSION_OBSERVED", { observation: "UNCERTAIN", http_status: null }),
  ev(4, "RECONCILIATION_FINISHED", { phase: 2, verdict: "NO_MATCH" }),
  ev(5, "OUTCOME_RECORDED", { evidence_class: "NO_MATCH_AFTER_UNCERTAIN" }, { attempt_no: 1 }, { from: "SUBMITTING", to: "UNKNOWN" }),
];

describe("Observed / Not known panel (Concept C borrowing)", () => {
  it("appears only for UNKNOWN and Needs decision", () => {
    for (const s of Object.keys(STATUS) as Summary["state"][]) {
      const k = knowns(detail(op(s), unknownTimeline));
      expect(k !== null, s).toBe(UNRESOLVED.includes(s));
    }
  });
  it("lists only recorded events as observed, each with its audit sequence", () => {
    const k = knowns(detail(op("UNKNOWN", { last_evidence: "NO_MATCH_AFTER_UNCERTAIN" }), unknownTimeline))!;
    expect(k.observed.map((o) => o.seq)).toEqual([1, 2, 3, 4]);
    // the classification (OUTCOME_RECORDED) is a rule, not an observation
    expect(k.observed.some((o) => /NO_MATCH_AFTER_UNCERTAIN/.test(o.text))).toBe(false);
    expect(k.rules[0]).toMatch(/^Classification rule, not an observation/);
    expect(k.rules[1]).toMatch(/^Scheduled, not observed/);
  });
  it("never calls an attempt a side effect, and never calls unknown failed", () => {
    const k = knowns(detail(op("UNKNOWN"), unknownTimeline))!;
    const all = [...k.observed.map((o) => o.text), ...k.notKnown, ...k.rules].join(" | ");
    expect(k.observed.find((o) => o.seq === 2)!.text).toMatch(/^Hand-off attempt recorded/);
    expect(all).not.toMatch(/(?<!not )\bfailed\b|\bwas not booked\b|\bbooked successfully\b/i);
    expect(all).toMatch(/not failed/);
    expect(k.notKnown[0]).toBe("Whether HALDEN committed the order");
  });
  it("a missing SKARVIK acknowledgement is not known — never proof of non-execution", () => {
    const k = knowns(detail(op("UNKNOWN", { backend_id: "skarvik" }), unknownTimeline))!;
    expect(k.notKnown.join(" ")).toMatch(/No acknowledgement file has been recorded — its absence does not prove the order was not booked/);
  });
  it("an observed order reference moves to 'not read back', not to 'confirmed'", () => {
    const tl = [...unknownTimeline, ev(6, "TRANSMISSION_OBSERVED", { observation: "ACCEPTED", backend_order_ref: "SO-1" })];
    const k = knowns(detail(op("ESCALATED"), tl))!;
    expect(k.notKnown.join(" ")).toMatch(/order SO-1 carries exactly the approved lines — it has not been read back/);
    expect(k.rules[0]).toMatch(/unknown — not failed/);
  });
  it("ignores events of earlier attempts", () => {
    const tl = [ev(9, "TRANSMISSION_STARTED", {}, { attempt_no: 0, transmission_no: 1, message_id: "old" }), ...unknownTimeline];
    expect(knowns(detail(op("UNKNOWN"), tl))!.observed.some((o) => o.seq === 9)).toBe(false);
  });
});

describe("journey and evidence facets", () => {
  it("marks an unresolved hand-off as unknown (rust), not as rejected (red)", () => {
    const j = journey(op("UNKNOWN"), detail(op("UNKNOWN"), []));
    expect(j.map((s) => s.c)).toEqual(["done", "done", "unk", "unk", "todo"]);
    expect(j.some((s) => s.c === "bad")).toBe(false);
  });
  it("only a verified evidence class completes the last step", () => {
    expect(journey(op("CONFIRMED", { last_evidence: "READ_BACK" }))[4]).toMatchObject({ t: "Verified", c: "done" });
    expect(journey(op("CONFIRMED", { last_evidence: "ACKNOWLEDGED_ONLY" }))[4]).toMatchObject({ c: "todo" });
    expect(journey(op("CLOSED_ATTESTED"))[4]).toMatchObject({ s: "attested — not verified", c: "todo" });
  });
  it("no hand-off before approval", () => {
    expect(journey(op("READY", { attempt_count: 0 }))[2]).toMatchObject({ s: "never before approval", c: "todo" });
  });
  it("an uncertain transport observation is 'not confirmed' (warn), never 'delivered'", () => {
    const f = facets(detail(op("UNKNOWN"), unknownTimeline));
    expect(f[0]).toMatchObject({ key: "handoff", tone: "warn", s: "Not confirmed" });
    expect(f.find((x) => x.key === "readback")).toMatchObject({ tone: "warn", s: "Not established" });
  });
  it("reason lines carry the evidence words and the do-not-re-enter rule", () => {
    expect(reasonFor(op("UNKNOWN", { last_evidence: "NO_MATCH_AFTER_UNCERTAIN" }))).toBe("no matching order visible after an uncertain hand-off — do not re-enter");
    expect(reasonFor(op("CONFIRMED"))).toBeNull();
  });
});

describe("stage mapping", () => {
  it("covers every state and keeps UNKNOWN/ESCALATED in Needs attention", () => {
    expect(Object.keys(STAGE_OF).sort()).toEqual(Object.keys(STATUS).sort());
    expect(STAGE_OF.UNKNOWN).toBe("attention");
    expect(STAGE_OF.ESCALATED).toBe("attention");
  });
});
