import type { Schemas } from "./api/client";

export type OperationState = Schemas["OperationState"];
export type Tone = "neutral" | "info" | "ok" | "ok-outline" | "warn" | "warn-outline" | "muted" | "attested" | "bad";

interface StatusSpec {
  label: string;
  tone: Tone;
  copy?: string;
}

// Single source of status semantics. The key type is generated from the API enum, so a new
// or renamed state is a compile error here.
export const STATUS: Record<OperationState, StatusSpec> = {
  DRAFT: { label: "Draft", tone: "neutral" },
  READY: { label: "Awaiting approval", tone: "info" },
  DECLINED: { label: "Declined", tone: "neutral" },
  APPROVED: { label: "Approved — queued for hand-off", tone: "info" },
  SUBMITTING: { label: "Handing off", tone: "info" },
  AWAITING_ACK: {
    label: "Delivered to SKARVIK — awaiting acknowledgement",
    tone: "info",
    copy: "The order file is in the SKARVIK inbox; SKARVIK has not acknowledged it yet.",
  },
  UNKNOWN: {
    label: "Outcome unknown",
    tone: "warn",
    copy: "The backend may or may not have booked this order. Do not re-enter it.",
  },
  RECONCILING: { label: "Checking the backend", tone: "info" },
  ESCALATED: { label: "Needs decision", tone: "warn-outline" },
  NOT_APPLIED: { label: "Not applied (proven)", tone: "muted" },
  CONFIRMED: { label: "Confirmed", tone: "ok" },
  REJECTED: { label: "Rejected by the backend", tone: "bad" },
  WITHDRAWN: { label: "Withdrawn", tone: "neutral" },
  CLOSED_ATTESTED: { label: "Closed by attestation (not verified)", tone: "attested" },
};

const BACKEND_NAMES: Record<string, string> = { halden: "HALDEN", skarvik: "SKARVIK" };

export const EVIDENCE_WORDS: Record<string, string> = {
  READ_BACK: "verified by reading the order back",
  ACKNOWLEDGED_ONLY: "acknowledged by the backend, not yet read back",
  RECONCILED: "verified by reconciliation",
  DEDUP_CONFIRMED: "duplicate acknowledged and read back",
  MANUAL_VERIFIED: "verified by a manual read of the supplied order",
  BACKEND_REJECTION: "the backend rejected it; nothing was booked",
  NOT_SENT: "no request was sent",
  PRE_PROCESSING_REFUSAL: "refused before any order logic ran",
  NEVER_DELIVERED: "the order file was never published to the inbox",
  WITHDRAWN_BEFORE_PICKUP: "withdrawn from the inbox before pickup",
  APPROVAL_EXPIRED: "the approval expired before any hand-off",
  NO_MATCH_AFTER_UNCERTAIN: "no matching order visible after an uncertain hand-off",
  READBACK_MISMATCH: "the order read back differs from the approval",
  RECONCILIATION_FAILED: "the backend could not be read",
  ACK_OVERDUE: "no acknowledgement within the deadline",
  INTERRUPTED: "the application stopped during the hand-off",
  SOURCE_STALE: "only snapshots older than the hand-off are available",
  AMBIGUOUS_MATCH: "several orders carry the reference",
  CONTRADICTION: "acknowledgement and order data contradict each other",
  RECHECK_LIMIT: "automatic re-checks exhausted",
  MANUAL: "escalated by a person",
  ATTESTED_BOOKED: "attested as booked (not verified)",
  ATTESTED_NOT_BOOKED: "attested as not booked (not verified)",
};

/** Label for a state, distinguishing acknowledged-only confirmations from verified ones. */
export function statusLabel(state: OperationState, evidence?: string | null, backend?: string): StatusSpec {
  if (state === "CONFIRMED") {
    if (evidence === "ACKNOWLEDGED_ONLY") return { label: "Acknowledged — not yet read back", tone: "ok-outline" };
    return { label: `Confirmed in ${BACKEND_NAMES[backend ?? ""] ?? "backend"}`, tone: "ok" };
  }
  return STATUS[state];
}

export const BACKEND_SHORT: Record<string, string> = BACKEND_NAMES;
export const BACKEND_FULL: Record<string, string> = { halden: "HALDEN ERP 3.1", skarvik: "SKARVIK OES 4" };
export const backendName = (b: string): string => BACKEND_NAMES[b] ?? b.toUpperCase();

/** Shorter label for dense rows (tables, grouped list); the full label is used everywhere else. */
const SHORT: Partial<Record<OperationState, string>> = {
  AWAITING_ACK: "Awaiting SKARVIK ack",
  REJECTED: "Rejected by backend",
};
export function shortLabel(state: OperationState, evidence?: string | null, backend?: string): string {
  if (state === "CONFIRMED" && evidence === "ACKNOWLEDGED_ONLY") return "Acknowledged only";
  return SHORT[state] ?? statusLabel(state, evidence, backend).label;
}

/** Operational stage used to group and filter orders (grouped view and compact table share it). */
export type Stage = "attention" | "approval" | "flight" | "draft" | "closed";
export const STAGE_OF: Record<OperationState, Stage> = {
  UNKNOWN: "attention", ESCALATED: "attention",
  READY: "approval",
  APPROVED: "flight", SUBMITTING: "flight", AWAITING_ACK: "flight", RECONCILING: "flight",
  DRAFT: "draft", DECLINED: "draft",
  CONFIRMED: "closed", REJECTED: "closed", WITHDRAWN: "closed", CLOSED_ATTESTED: "closed", NOT_APPLIED: "closed",
};
export const STAGES: { key: Stage; label: string }[] = [
  { key: "attention", label: "Needs attention" }, { key: "approval", label: "Awaiting approval" },
  { key: "flight", label: "In flight" }, { key: "draft", label: "Drafts" }, { key: "closed", label: "Closed" },
];
export const STAGE_RANK: Record<Stage, number> = { attention: 0, approval: 1, flight: 2, draft: 3, closed: 4 };

/** States in which the outcome of a hand-off is not established (Observed / Not known panel; never "failed"). */
export const UNRESOLVED: readonly OperationState[] = ["UNKNOWN", "ESCALATED"];
export const isUnresolved = (s: OperationState): boolean => UNRESOLVED.includes(s);
