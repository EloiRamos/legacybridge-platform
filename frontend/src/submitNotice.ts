/** A submit that the server answers with HTTP 200 and "still a draft" (blocking policy checks, e.g. a credit hold) is
 * not an error, but the console must not look as if nothing happened: say why the order was not submitted. */
export function blockedSubmitNotice(result: unknown): string | null {
  if (!result || typeof result !== "object") return null;
  const r = result as { state?: unknown; checks?: unknown };
  if (r.state !== "DRAFT" || !Array.isArray(r.checks)) return null;
  const blocking = (r.checks as { check?: unknown; result?: unknown; detail?: unknown }[])
    .filter((c) => c && c.result === "BLOCKING")
    .map((c) => String(c.detail || c.check || "blocking check"));
  if (!blocking.length) return null;
  return `Not submitted — the order is still a draft. Blocking policy check: ${blocking.join("; ")} Revise or withdraw the draft.`;
}
