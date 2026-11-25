import { describe, expect, it } from "vitest";

import { answerKind, modeWords, nextActions, traceRows } from "./assistant";

describe("traceRows (L-29)", () => {
  it("never marks a failed call as successful and lists a refused call once", () => {
    const rows = traceRows([
      { name: "search_customers", permitted: true, outcome: "SUCCEEDED" },
      { name: "get_operation_timeline", permitted: true, outcome: "FAILED", error_code: "NOT_FOUND" },
      { name: "prepare_draft", permitted: true, outcome: "UNKNOWN" },
      { name: "approve_operation", permitted: false, outcome: "REFUSED", refused_reason: "UNKNOWN_TOOL" },
    ], [{ name: "approve_operation", reason: "UNKNOWN_TOOL" }]);
    expect(rows.map((r) => r.tone)).toEqual(["ok", "fail", "unknown", "refused"]);
    expect(rows[1]!.label).toBe("executed, returned an error (NOT_FOUND)");
    expect(rows[3]!.label).toBe("refused, not executed: no such tool exists");
  });
  it("does not claim success when the server reported no outcome, and keeps an orphan refusal", () => {
    const rows = traceRows([{ name: "x", permitted: true }], [{ name: "submit_order", reason: "UNKNOWN_TOOL" }]);
    expect(rows.map((r) => r.tone)).toEqual(["unknown", "refused"]);
  });
});

it("labels modes, next steps and answer kinds in words", () => {
  expect(modeWords("openai mock")).toContain("not live");
  expect(answerKind({ mode: "application", fallback_used: false })).toContain("no model");
  expect(modeWords("openai live")).toContain("live");
  expect(nextActions(["none"])).toEqual([]);
  expect(nextActions(["recheck_in_ui"])[0]).toContain("read-only re-check");
  expect(answerKind({ mode: "openai live", fallback_used: true, fallback_reason: "TIMEOUT" })).toContain("not used");
  expect(answerKind({ mode: "disabled", fallback_used: true, fallback_reason: "DISABLED" })).toContain("Rule-based");
});
