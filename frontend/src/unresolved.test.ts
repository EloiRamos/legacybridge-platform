import { describe, expect, it } from "vitest";

import { unresolvedGuidance } from "./unresolved";

// The banner said "Only read-only checks are allowed" while the same page offered redelivery and withdrawal.
describe("unresolvedGuidance", () => {
  it("SKARVIK inside the window names every permitted action and claims no read-only restriction", () => {
    const g = unresolvedGuidance("UNKNOWN", "skarvik", ["authorize_redelivery", "authorize_withdraw_unclaimed", "escalate", "recheck"]);
    expect(g.yours).toBe("Available to you now: authorize redelivery of the same message; authorize withdrawal of the unclaimed message; escalate for a decision; re-check (read-only).");
    expect(g.rule).toContain("deduplication window");
    expect(g.rule + g.yours).not.toMatch(/only read-only/i);
  });
  it("SKARVIK after the window offers no redelivery to the person", () => {
    const g = unresolvedGuidance("UNKNOWN", "skarvik", ["recheck", "escalate"]);
    expect(g.yours).not.toContain("redelivery");
  });
  it("HALDEN never mentions redelivery or withdrawal", () => {
    const g = unresolvedGuidance("UNKNOWN", "halden", ["recheck", "escalate"]);
    expect(g.rule).toContain("never sent again");
    expect(g.rule + g.yours).not.toMatch(/redeliver|withdraw/i);
  });
  it("distinguishes verified and attested resolution, and says when nothing is available", () => {
    expect(unresolvedGuidance("ESCALATED", "halden", ["resolve_verified", "resolve_attested"]).yours).toBe(
      "Available to you now: resolve by a verified read; resolve by attestation.");
    expect(unresolvedGuidance("ESCALATED", "skarvik", []).yours).toMatch(/authorized person/);
    expect(unresolvedGuidance("UNKNOWN", "skarvik", []).yours).toMatch(/Nothing is available to your role/);
  });
});
