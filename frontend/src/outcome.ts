import { ApiError } from "./api/client";

/** What a failed command proves (RR-1). "refused": the server answered with its own structured 4xx problem. Anything
 * else — 5xx (an uncertain COMMIT included), a timeout, a gateway page, a lost or unreadable response — leaves the
 * outcome unknown, so the console must not claim that nothing happened. */
export function outcomeOf(error: unknown): "refused" | "unknown" {
  if (!(error instanceof ApiError)) return "unknown";
  const { status, code } = error.problem;
  return status >= 400 && status < 500 && status !== 408 && !code.startsWith("HTTP_") ? "refused" : "unknown";
}

export const OUTCOME_UNKNOWN = "The outcome of this request is not known. Check the current state before repeating it.";
