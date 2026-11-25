import type { Schemas } from "./api/client";
import { backendName } from "./status";

type Action = Schemas["Action"];

/** What a person may do about an UNKNOWN order, by backend capability and by the actions the server permits them now.
 * HALDEN has no duplicate protection: nothing is ever re-sent. SKARVIK allows an authorized withdrawal of an unclaimed
 * message and, inside its deduplication window, an authorized redelivery of the same message. */
const WORDS: Partial<Record<Action, string>> = {
  recheck: "re-check (read-only)", authorize_withdraw_unclaimed: "authorize withdrawal of the unclaimed message",
  authorize_redelivery: "authorize redelivery of the same message", escalate: "escalate for a decision",
  resolve_verified: "resolve by a verified read", resolve_attested: "resolve by attestation",
};

export function unresolvedGuidance(state: string, backend: string, permitted: readonly Action[]): { rule: string; yours: string } {
  const B = backendName(backend);
  const rule = backend === "skarvik"
    ? `Do not re-enter it. Automatic re-checks of ${B} only read. ${B} also allows, when separately authorized, withdrawing the message while it is still unclaimed, or redelivering the same message inside its deduplication window.`
    : `Do not re-enter it. Automatic re-checks of ${B} only read. ${B} cannot detect duplicates, so the order is never sent again.`;
  const mine = permitted.map((a) => WORDS[a]).filter((x): x is string => !!x);
  const yours = mine.length ? `Available to you now: ${mine.join("; ")}.`
    : state === "ESCALATED" ? "Nothing is available to you; an authorized person resolves it." : "Nothing is available to your role right now; it can be escalated by an authorized person.";
  return { rule, yours };
}
