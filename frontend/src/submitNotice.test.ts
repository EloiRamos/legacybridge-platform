import { describe, expect, it } from "vitest";

import { blockedSubmitNotice } from "./submitNotice";

// Submit answered "still a draft" with blocking checks (observed live: HALDEN 04450, credit hold) must be explained.
describe("blocked submit feedback", () => {
  const blocked = {
    operation_id: "96e25540-e0e4-406e-9a97-a8c91debb2e7", state: "DRAFT",
    detail: "blocking checks: validation recorded, still a draft",
    checks: [
      { check: "target_hold", result: "BLOCKING", detail: "Target account is on credit hold.", line_no: null },
      { check: "line_stock", result: "PASS", detail: "", line_no: 1 },
    ],
  };
  it("names the blocking check", () => {
    expect(blockedSubmitNotice(blocked)).toBe(
      "Not submitted — the order is still a draft. Blocking policy check: Target account is on credit hold. Revise or withdraw the draft.",
    );
  });
  it("stays silent for a successful submit and for other actions", () => {
    expect(blockedSubmitNotice({ ...blocked, state: "READY", checks: [] })).toBeNull();
    expect(blockedSubmitNotice({ operation_id: "x", state: "WITHDRAWN", detail: "" })).toBeNull();
    expect(blockedSubmitNotice({ ...blocked, checks: [{ check: "line_stock", result: "PASS" }] })).toBeNull();
    expect(blockedSubmitNotice(undefined)).toBeNull();
  });
});
