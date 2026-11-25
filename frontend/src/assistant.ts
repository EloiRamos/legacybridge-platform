/* Reading an assistant turn: what was attempted, what ran, what changed, and which provider answered. */
export interface TraceCall { name?: unknown; permitted?: unknown; outcome?: unknown; error_code?: unknown; refused_reason?: unknown }
export type TraceTone = "ok" | "fail" | "refused" | "unknown";
export interface TraceRow { name: string; tone: TraceTone; label: string }

const REASONS: Record<string, string> = {
  UNKNOWN_TOOL: "no such tool exists", MALFORMED_ARGUMENTS: "invalid arguments", FORBIDDEN: "your role does not permit it",
  INTENT_INVALID_QUANTITY: "the requested quantity is not valid", INTENT_UNCONFIRMED: "needs your confirmation",
  INTENT_ALREADY_SERVED: "this request already stored a draft",
};

/** One row per attempted call. A refused call appears once, and only an executed call that succeeded is marked so. */
export function traceRows(calls: readonly TraceCall[], refused: readonly { name?: unknown; reason?: unknown }[]): TraceRow[] {
  const rows = calls.map((c): TraceRow => {
    const name = String(c.name ?? "call");
    const reason = String(c.refused_reason ?? refused.find((r) => r.name === c.name)?.reason ?? "");
    if (c.permitted === false) return { name, tone: "refused", label: `refused, not executed${reason ? `: ${REASONS[reason] ?? reason}` : ""}` };
    if (c.outcome === "FAILED") return { name, tone: "fail", label: `executed, returned an error${c.error_code ? ` (${String(c.error_code)})` : ""}` };
    if (c.outcome === "UNKNOWN") return { name, tone: "unknown", label: "executed, outcome unknown" };
    if (c.outcome === "SUCCEEDED") return { name, tone: "ok", label: "executed successfully" };
    return { name, tone: "unknown", label: "authorized, outcome not reported" };
  });
  // a refusal the server reported without a call entry is still shown, once
  const named = new Set(calls.filter((c) => c.permitted === false).map((c) => String(c.name)));
  for (const r of refused) if (!named.has(String(r.name ?? "call"))) rows.push({ name: String(r.name ?? "call"), tone: "refused", label: `refused, not executed${r.reason ? `: ${REASONS[String(r.reason)] ?? String(r.reason)}` : ""}` });
  return rows;
}

export const ACTION_WORDS: Record<string, string> = {
  open_operation: "Open the order", submit_for_approval: "Review the draft and submit it for approval", approve_in_ui: "A second person approves it on the order page",
  recheck_in_ui: "Request a read-only re-check on the order page", escalate_in_ui: "Escalate it on the order page", resolve_in_ui: "Resolve it on the order page",
  withdraw_unclaimed_in_ui: "Authorize withdrawal of the unclaimed message on the order page", authorize_redelivery_in_ui: "Authorize redelivery on the order page",
  search_customers: "Look up the customer account", check_inventory: "Check the inventory page",
};
export const nextActions = (a: readonly string[] | undefined): string[] => (a ?? []).filter((x) => x !== "none").map((x) => ACTION_WORDS[x] ?? x);

export const MODE_WORDS: Record<string, string> = {
  application: "application (no model)", disabled: "deterministic commands (no model)", scripted: "scripted replies (no model)", "openai live": "OpenAI model (live)", "openai mock": "simulated OpenAI transport (test, not live)",
};
export const modeWords = (m: string | undefined): string => (m ? MODE_WORDS[m] ?? m : "…");

/** What may be said about the facts of an answer: a live or mocked model supplies a field and the application renders its value; a scripted provider writes its own sentences. */
export const factsLabel = (t: { mode: string; fallback_used: boolean }): string =>
  t.mode === "scripted" && !t.fallback_used ? "Scripted statements with cited fields" : t.mode.startsWith("openai") && !t.fallback_used ? "Exact values from system records" : "From system records";

/** Whether the answer is a checked model answer, a fallback, or a rule-based reply. */
export function answerKind(t: { mode: string; fallback_used: boolean; fallback_reason?: string | null }): string {
  if (t.fallback_used) return t.fallback_reason === "DISABLED" ? "Rule-based reply from system records" : `Fallback: the model answer was not used (${t.fallback_reason ?? "unspecified"})`;
  if (t.mode === "application") return "Carried out by the application; no model was used";
  return "Model answer: cited values checked against system records, wording only in part";
}
