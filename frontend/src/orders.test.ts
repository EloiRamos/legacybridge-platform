import { describe, expect, it } from "vitest";

import { countByStage, matches, type Op, sortOps } from "./orders";

const mk = (ref: string, state: Op["state"], extra: Partial<Op> = {}): Op => ({
  operation_id: ref, correlation_value: ref, backend_id: "halden", native_customer_id: "00417", state, attempt_count: 0,
  current_version: 1, created_by: "daniel.sales", origin: "CONSOLE", created_at: "2026-11-22T10:00:00Z",
  updated_at: "2026-11-22T10:00:00Z", estimated_total: null, last_evidence: null, last_outcome: null, verification_level: null, ...extra,
});
const rows = [
  mk("LBA", "CONFIRMED", { estimated_total: "50.00", updated_at: "2026-11-22T10:03:00Z" }),
  mk("LBB", "UNKNOWN", { backend_id: "skarvik", native_customer_id: "KND-0081", estimated_total: "900.00", updated_at: "2026-11-22T10:01:00Z" }),
  mk("LBC", "ESCALATED", { updated_at: "2026-11-22T10:02:00Z" }),
  mk("LBD", "READY", { created_by: "priya.sales", estimated_total: "10.00", updated_at: "2026-11-22T10:04:00Z" }),
];

describe("orders workspace (grouped and table share one filter/sort)", () => {
  it("filters by stage, system and free text", () => {
    expect(rows.filter((o) => matches(o, { q: "", stage: "attention", backend: "all" })).map((o) => o.correlation_value)).toEqual(["LBB", "LBC"]);
    expect(rows.filter((o) => matches(o, { q: "", stage: "all", backend: "skarvik" })).map((o) => o.correlation_value)).toEqual(["LBB"]);
    expect(rows.filter((o) => matches(o, { q: "priya", stage: "all", backend: "all" })).map((o) => o.correlation_value)).toEqual(["LBD"]);
    expect(rows.filter((o) => matches(o, { q: "outcome unknown", stage: "all", backend: "all" })).map((o) => o.correlation_value)).toEqual(["LBB"]);
  });
  it("sorts by status with Needs decision first, then by update time", () => {
    expect(sortOps(rows, "status", "asc").map((o) => o.correlation_value)).toEqual(["LBC", "LBB", "LBD", "LBA"]);
  });
  it("sorts totals numerically with unknown totals last when descending", () => {
    const r = [...rows, mk("LBE", "DRAFT")];
    expect(sortOps(r, "total", "desc").map((o) => o.correlation_value)).toEqual(["LBB", "LBA", "LBD", "LBC", "LBE"]);
  });
  it("defaults to newest first", () => {
    expect(sortOps(rows, "updated", "desc").map((o) => o.correlation_value)).toEqual(["LBD", "LBA", "LBC", "LBB"]);
  });
  it("counts per stage", () => {
    expect(countByStage(rows)).toEqual({ all: 4, attention: 2, approval: 1, flight: 0, draft: 0, closed: 1 });
  });
});
