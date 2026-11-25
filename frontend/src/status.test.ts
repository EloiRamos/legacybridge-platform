import { describe, expect, it } from "vitest";

import { STATUS, statusLabel } from "./status";

// UI-05: copy and colour for UNKNOWN (the permitted actions are worded per backend in unresolved.ts); distinct labels for acknowledged-only and attested outcomes.
describe("status semantics", () => {
  it("UNKNOWN is amber with the mandatory copy", () => {
    expect(STATUS.UNKNOWN.label).toBe("Outcome unknown");
    expect(STATUS.UNKNOWN.tone).toBe("warn");
    expect(STATUS.UNKNOWN.copy).toBe(
      "The backend may or may not have booked this order. Do not re-enter it.",
    );
  });
  it("never colours UNKNOWN red", () => {
    expect(STATUS.UNKNOWN.tone).not.toBe("bad");
  });
  it("distinguishes acknowledged-only from verified confirmations", () => {
    expect(statusLabel("CONFIRMED", "ACKNOWLEDGED_ONLY", "skarvik").label).toBe("Acknowledged — not yet read back");
    expect(statusLabel("CONFIRMED", "READ_BACK", "halden").label).toBe("Confirmed in HALDEN");
    expect(statusLabel("CONFIRMED", "DEDUP_CONFIRMED", "skarvik").label).toBe("Confirmed in SKARVIK");
  });
  it("labels attested closure as not verified and NOT_APPLIED as proven", () => {
    expect(STATUS.CLOSED_ATTESTED.label).toBe("Closed by attestation (not verified)");
    expect(STATUS.NOT_APPLIED.label).toBe("Not applied (proven)");
    expect(STATUS.ESCALATED.label).toBe("Needs decision");
    expect(STATUS.AWAITING_ACK.label).toBe("Delivered to SKARVIK — awaiting acknowledgement");
  });
});
