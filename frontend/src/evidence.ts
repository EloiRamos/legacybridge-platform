/* Evidence presentation derived only from the operation read model (GET /api/v1/operations/{id}).
 *
 * Nothing here infers an outcome. Semantic invariants kept by construction and checked in evidence.test.ts:
 *   ATTEMPTED ACTION != OBSERVED SIDE EFFECT · UNKNOWN != FAILED
 *   MISSING ACKNOWLEDGEMENT != CONFIRMED NON-EXECUTION · DECLARED STATE != OBSERVED STATE · DETECTED != PREVENTED
 * "Observed" lists recorded events only; classifications and schedules are labelled as rules, never as observations. */
import type { Schemas } from "./api/client";
import { hm, shortHash } from "./format";
import { backendName, EVIDENCE_WORDS, isUnresolved, type OperationState } from "./status";

export type Summary = Schemas["OperationSummaryOut"];
export type Detail = Schemas["OperationDetailOut"];
type Rec = Record<string, unknown>;

export interface TimelineEvent {
  seq: number;
  event_type: string;
  occurred_at: string;
  actor_type: string;
  actor_username: string | null;
  on_behalf_of: string | null;
  state: { from?: string | null; to?: string | null } | null;
  correlation: Rec;
  payload: Rec;
  event_hash: string;
}

export const timelineOf = (d: Pick<Detail, "timeline">): TimelineEvent[] => d.timeline as unknown as TimelineEvent[];
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : v == null ? null : String(v));

/** Evidence classes that rest on a read of the backend (or a manual verified read) — "verified". */
export const VERIFIED_EVIDENCE = new Set(["READ_BACK", "RECONCILED", "DEDUP_CONFIRMED", "MANUAL_VERIFIED"]);

// ------------------------------------------------------------------------------------------------ journey
export type StageClass = "done" | "cur" | "unk" | "bad" | "todo";
export interface JourneyStage { t: string; s: string; c: StageClass }

const PRE_APPROVAL: readonly OperationState[] = ["DRAFT", "READY", "DECLINED"];
const HANDED_OFF: readonly OperationState[] = ["SUBMITTING", "AWAITING_ACK", "UNKNOWN", "RECONCILING", "ESCALATED", "CONFIRMED", "REJECTED", "CLOSED_ATTESTED"];

/** Five-step journey. With ``detail`` the steps cite the recorded approval and transmissions; without it
 * (list preview) they are derived from the state only and say less. */
export function journey(op: Summary, detail?: Pick<Detail, "approvals" | "transmissions">): JourneyStage[] {
  const st = op.state;
  const B = backendName(op.backend_id);
  const attempt = op.attempt_count;
  const approval = detail?.approvals.find((a) => a.decision === "APPROVED" && a.attempt_no === attempt);
  const xs = detail ? detail.transmissions.filter((x) => x.attempt_no === attempt) : [];
  const handed = detail ? xs.length > 0 : HANDED_OFF.includes(st);
  const unresolved = isUnresolved(st) || (st === "RECONCILING");
  const ev = op.last_evidence ?? null;

  const approved: JourneyStage = PRE_APPROVAL.includes(st) || (st === "WITHDRAWN" && attempt === 0)
    ? { t: "Approved", s: st === "READY" ? "awaiting another person" : st === "DECLINED" ? "declined" : "not submitted", c: st === "READY" ? "cur" : "todo" }
    : { t: "Approved", s: approval ? `${str(approval.decided_by)} · ${shortHash(approval.content_hash, 7)}` : `attempt ${attempt}`, c: "done" };

  let handoff: JourneyStage;
  if (!handed) handoff = { t: "Handed off", s: st === "APPROVED" ? "queued — once" : PRE_APPROVAL.includes(st) ? "never before approval" : "no hand-off recorded", c: st === "APPROVED" ? "cur" : "todo" };
  else if (st === "REJECTED") handoff = { t: "Handed off", s: `rejected by ${B}`, c: "bad" };
  else if (unresolved) handoff = { t: "Handed off", s: "effect not observed", c: "unk" };
  else if (st === "SUBMITTING") handoff = { t: "Handed off", s: `in progress · ${B}`, c: "cur" };
  else handoff = { t: "Handed off", s: xs.length > 1 ? `${xs.length} transmissions · ${B}` : `once · ${B}`, c: "done" };

  let evidence: JourneyStage;
  if (unresolved) evidence = { t: "Outcome unknown", s: st === "ESCALATED" ? "needs a decision" : st === "RECONCILING" ? "re-checking (read-only)" : "read-only re-checks", c: "unk" };
  else if (st === "CONFIRMED") evidence = { t: "Evidence", s: ev && VERIFIED_EVIDENCE.has(ev) ? "read back" : "acknowledgement only", c: "done" };
  else if (st === "AWAITING_ACK") evidence = { t: "Evidence", s: "awaiting acknowledgement", c: "cur" };
  else if (st === "NOT_APPLIED") evidence = { t: "Evidence", s: ev ? EVIDENCE_WORDS[ev] ?? ev : "proven not applied", c: "done" };
  else if (st === "REJECTED") evidence = { t: "Evidence", s: "nothing booked", c: "bad" };
  else if (st === "CLOSED_ATTESTED") evidence = { t: "Evidence", s: "human attestation", c: "done" };
  else evidence = { t: "Evidence", s: "—", c: "todo" };

  let last: JourneyStage;
  if (st === "CONFIRMED") last = ev && VERIFIED_EVIDENCE.has(ev) ? { t: "Verified", s: "matches approval", c: "done" } : { t: "Verified", s: "read-back pending", c: "todo" };
  else if (st === "NOT_APPLIED") last = { t: "Proven not applied", s: "no order exists", c: "done" };
  else if (st === "REJECTED") last = { t: "Closed", s: "rejected", c: "bad" };
  else if (st === "WITHDRAWN") last = { t: "Withdrawn", s: "closed before any effect", c: "done" };
  else if (st === "CLOSED_ATTESTED") last = { t: "Closed", s: "attested — not verified", c: "todo" };
  else last = { t: "Verified", s: st === "ESCALATED" ? "awaiting a decision" : "—", c: "todo" };

  return [{ t: "Drafted", s: `${op.created_by} · v${op.current_version}`, c: "done" }, approved, handoff, evidence, last];
}

// ------------------------------------------------------------------------------------------------ evidence facets
export type FacetTone = "ok" | "warn" | "bad" | "none";
export interface Facet { key: string; t: string; tone: FacetTone; s: string; d: string }

export function facets(d: Detail): Facet[] {
  const op = d.operation;
  const B = backendName(op.backend_id);
  const xs = d.transmissions.filter((x) => x.attempt_no === op.attempt_count);
  const last = xs[xs.length - 1];
  const obs = (last?.observation ?? null) as Rec | null;
  const kind = str(obs?.kind);
  const out: Facet[] = [];
  if (!last) {
    out.push({ key: "handoff", t: "Hand-off", tone: "none", s: "Not sent",
      d: PRE_APPROVAL.includes(op.state) ? "No request is sent before an approved version exists." : "No transmission is recorded for the current attempt." });
  } else {
    const tone: FacetTone = last.state === "INTERRUPTED" || kind === "UNCERTAIN" ? "warn" : kind === "REJECTED" ? "bad"
      : kind === "NOT_DELIVERED" || kind === "REFUSED_PRE_PROCESSING" ? "none" : last.state === "SENDING" ? "none" : "ok";
    const s = last.state === "SENDING" ? "In progress" : last.state === "INTERRUPTED" ? "No observation recorded"
      : kind === "UNCERTAIN" ? "Not confirmed" : kind === "REJECTED" ? "Rejected" : kind === "NOT_DELIVERED" ? "Not delivered"
      : kind === "REFUSED_PRE_PROCESSING" ? "Refused before processing" : kind === "DELIVERED_PENDING_ACK" ? "Delivered to inbox" : kind === "ACCEPTED" ? "Accepted" : (kind ?? String(last.state));
    out.push({ key: "handoff", t: "Hand-off", tone, s,
      d: `${xs.length} transmission${xs.length > 1 ? "s" : ""} to ${B} · message ${str(last.message_id) ?? "—"} · payload ${shortHash(last.payload_sha256)}` });
  }
  if (op.backend_id === "skarvik") {
    const acks = d.acknowledgements;
    const a = acks[acks.length - 1];
    out.push(a
      ? { key: "ack", t: "Acknowledgement", tone: a.status === "REJECTED" ? "bad" : a.status === "MALFORMED" ? "warn" : "ok", s: `Recorded · ${str(a.status)}`, d: `${str(a.file_name) ?? "ack file"}${a.order_ref ? ` · order ${str(a.order_ref)}` : ""}` }
      : { key: "ack", t: "Acknowledgement", tone: last && op.state !== "AWAITING_ACK" ? "warn" : "none", s: op.state === "AWAITING_ACK" ? "Awaiting" : last ? "None recorded" : "—",
          d: op.state === "AWAITING_ACK" ? "The acknowledgement window is open." : last ? "No acknowledgement file has been recorded. This does not prove the order was not booked." : "No acknowledgement is expected before a hand-off." });
  }
  const ev = op.last_evidence ?? null;
  out.push(op.state === "CONFIRMED" && ev && VERIFIED_EVIDENCE.has(ev)
    ? { key: "readback", t: "Read-back", tone: "ok", s: "Matches approval", d: EVIDENCE_WORDS[ev] ?? ev }
    : op.state === "CONFIRMED"
      ? { key: "readback", t: "Read-back", tone: "none", s: "Not yet read back", d: "Acknowledged by the backend; the order has not been matched by a read." }
      : isUnresolved(op.state) || op.state === "RECONCILING"
        ? { key: "readback", t: "Read-back", tone: "warn", s: "Not established", d: "No read has matched the approved version yet." }
        : { key: "readback", t: "Read-back", tone: "none", s: "—", d: "No order has been read back." });
  const runs = d.reconciliation_runs;
  const lr = runs[runs.length - 1];
  out.push(!lr
    ? { key: "recon", t: "Reconciliation", tone: "none", s: op.state === "CONFIRMED" ? "Not needed" : "—", d: "Reconciliation runs only after an uncertain or unconfirmed hand-off." }
    : { key: "recon", t: "Reconciliation", tone: lr.verdict === "MATCHED" ? "ok" : "warn", s: `${runs.length} run${runs.length > 1 ? "s" : ""} · ${str(lr.verdict) ?? "running"}`,
        d: `Latest: ${str(lr.trigger) ?? "—"} run${lr.finished_at ? `, finished ${hm(lr.finished_at)}` : ", in progress"} (read-only).` });
  return out;
}

// ------------------------------------------------------------------------------------------------ Observed / Not known
export interface ObservedItem { seq: number; at: string; text: string }
export interface Knowns { observed: ObservedItem[]; notKnown: string[]; rules: string[] }

/** Concept C borrowing, shown only for UNKNOWN and ESCALATED ("Needs decision"). Returns null otherwise. */
export function knowns(d: Detail, now: Date = new Date()): Knowns | null {
  const op = d.operation;
  if (!isUnresolved(op.state)) return null;
  const B = backendName(op.backend_id);
  const attempt = op.attempt_count;
  const events = timelineOf(d).filter((e) => {
    const a = e.correlation?.attempt_no;
    return a == null || a === attempt;
  });
  const observed: ObservedItem[] = [];
  let orderRef: string | null = null;
  let ackSeen = false;
  let recheckNo = 0;
  for (const e of events) {
    const p = e.payload ?? {};
    const c = e.correlation ?? {};
    const push = (text: string) => observed.push({ seq: e.seq, at: e.occurred_at, text });
    switch (e.event_type) {
      case "APPROVAL_GRANTED":
        push(`${e.actor_username ?? "A person"} approved v${str(c.draft_version) ?? "?"}, bound to content hash ${shortHash(c.content_hash)}`);
        break;
      case "TRANSMISSION_STARTED":
        push(`Hand-off attempt recorded: transmission ${str(c.transmission_no) ?? "1"} to ${B}${p.kind === "REDELIVER" ? " (redelivery of the stored bytes)" : ""} · message ${str(c.message_id) ?? "—"}`);
        break;
      case "TRANSMISSION_OBSERVED":
      case "LATE_OBSERVATION": {
        const status = p.http_status != null ? ` · HTTP ${str(p.http_status)}` : "";
        if (p.backend_order_ref) orderRef = str(p.backend_order_ref);
        push(`${e.event_type === "LATE_OBSERVATION" ? "Late transport observation" : "Transport observation"}: ${str(p.observation) ?? "—"}${status}${p.backend_order_ref ? ` · order ref ${str(p.backend_order_ref)}` : ""}`);
        break;
      }
      case "TRANSMISSION_INTERRUPTED":
        push("No transport observation was recorded before the transmission lease expired");
        break;
      case "ACK_RECORDED":
        ackSeen = true;
        if (p.order_ref) orderRef = str(p.order_ref);
        push(`Acknowledgement recorded: ${str(p.status) ?? "—"}${p.order_ref ? ` · order ${str(p.order_ref)}` : ""}`);
        break;
      case "RECONCILIATION_FINISHED":
        recheckNo += 1;
        push(`Read-only re-check ${recheckNo}${p.phase === 2 ? " (immediate)" : p.trigger ? ` (${str(p.trigger)?.toLowerCase()})` : ""}: verdict ${str(p.verdict) ?? "—"}`);
        break;
      case "ESCALATION_OPENED":
        push(`Escalated for a person to decide (${str(p.reason) ?? "—"})`);
        break;
      case "WITHDRAW_RESULT":
        push(`Inbox withdrawal attempted: ${str(p.result) ?? str(p.outcome) ?? "result recorded"}`);
        break;
      default:
        break;
    }
  }
  const notKnown: string[] = [`Whether ${B} committed the order`];
  notKnown.push(orderRef ? `Whether ${B} order ${orderRef} carries exactly the approved lines — it has not been read back` : `No ${B} order reference has been observed`);
  if (op.backend_id === "skarvik" && !ackSeen) notKnown.push("No acknowledgement file has been recorded — its absence does not prove the order was not booked");
  if (!orderRef) notKnown.push("No read-back exists, so booked lines (if any) cannot be compared with the approved version");

  const outcome = [...events].reverse().find((e) => e.event_type === "OUTCOME_RECORDED" && e.payload?.evidence_class);
  const cls = outcome ? str(outcome.payload.evidence_class) : op.last_evidence;
  const rules: string[] = [];
  rules.push(op.state === "ESCALATED"
    ? `Classification rule, not an observation: ${cls ? `recorded as ${cls} (${EVIDENCE_WORDS[cls] ?? "unresolved"}). ` : ""}An unresolved hand-off is unknown — not failed. Only a verified read, an attestation (recorded as not verified) or proof of non-delivery closes it.`
    : `Classification rule, not an observation: ${cls ? `recorded as ${cls} (${EVIDENCE_WORDS[cls] ?? "unresolved"}). ` : ""}An attempted hand-off without a confirming read stays unknown — not failed. Do not re-enter the order.`);
  const s = d.schedule as Rec;
  const sched: string[] = [];
  if (op.state === "UNKNOWN" && s.next_recheck_at) sched.push(`next automatic read-only re-check ${hm(s.next_recheck_at)}`);
  if (op.state === "UNKNOWN" && s.redelivery_deadline && new Date(String(s.redelivery_deadline)) > now) sched.push(`redelivery under the same message id possible until ${hm(s.redelivery_deadline)}`);
  if (op.state === "UNKNOWN" && s.escalation_at) sched.push(`escalation to a person at ${hm(s.escalation_at)} if still unresolved`);
  if (sched.length) rules.push(`Scheduled, not observed: ${sched.join(" · ")}.`);
  return { observed, notKnown, rules };
}

/** One-line reason for an order that needs attention (grouped view / preview). Words come from the evidence class. */
export function reasonFor(op: Summary): string | null {
  if (op.state === "UNKNOWN") return op.last_evidence ? `${EVIDENCE_WORDS[op.last_evidence] ?? op.last_evidence} — do not re-enter` : "Effect not observed — do not re-enter";
  if (op.state === "ESCALATED") return op.last_evidence ? `${EVIDENCE_WORDS[op.last_evidence] ?? op.last_evidence} — a person decides` : "A person decides";
  if (op.state === "DECLINED") return "Declined — revise or withdraw";
  return null;
}
