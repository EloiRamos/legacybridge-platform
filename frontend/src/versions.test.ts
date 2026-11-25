import { describe, expect, it } from "vitest";

import { matches, type Op } from "./orders";
import { previewLine } from "./preview";
import { diffVersions } from "./versions";

const line = (sku: string, q: number) => ({ line_no: 1, canonical_sku: sku, native_item: "N-" + sku, quantity_base: q, quantity_native: q, native_uom: "EA", line_estimate: null });
const content = (lines: ReturnType<typeof line>[], extra = {}) => ({ backend_id: "halden", native_customer_id: "00417", customer_po: null, requested_date: null, delivery_note: null, lines, ...extra });

describe("diffVersions", () => {
  it("reports changed, added and removed lines and header fields, and nothing else", () => {
    const d = diffVersions(content([line("A", 1), line("B", 2)]), content([line("A", 5), line("C", 1)], { delivery_note: "dock 2" }));
    expect(d).toEqual([
      { what: "Delivery note", from: "—", to: "dock 2" },
      { what: "A", from: "1 EA", to: "5 EA" }, { what: "B", from: "2 EA", to: "removed" }, { what: "C", from: "not present", to: "1 EA" }]);
    expect(diffVersions(content([line("A", 1)]), content([line("A", 1)]))).toEqual([]);
  });
  it("keeps repeated items apart", () => {
    expect(diffVersions(content([line("A", 1), line("A", 2)]), content([line("A", 1), line("A", 3)]))).toEqual([{ what: "A (2)", from: "2 EA", to: "3 EA" }]);
  });
});

describe("previewLine", () => {
  const a = (status: string, n: number | null) => ({ status, available_base: n, freshness: "LIVE", native_item: "x", backend_id: "halden", explanation: "why" }) as never;
  it.each([
    [a("IN_STOCK", 10), 10, "ok"], [a("IN_STOCK", 9), 10, "warn"], [a("OUT_OF_STOCK", 0), 1, "warn"],
    [a("UNDETERMINED", null), 1, "warn"], [a("STALE", null), 1, "warn"], [undefined, 1, "bad"],
  ])("%#", (av, qty, tone) => expect(previewLine("BW-1", qty, av ? [av] : []).tone).toBe(tone));
  it("never states a decision", () => {
    for (const s of ["IN_STOCK", "OUT_OF_STOCK", "UNDETERMINED"]) expect(previewLine("BW-1", 1, [a(s, 5)]).text).not.toMatch(/approved|will be accepted|guarantee/i);
  });
});

describe("order filters", () => {
  const op = { operation_id: "i", correlation_value: "LB", native_customer_id: "1", created_by: "daniel.sales", backend_id: "halden", state: "CONFIRMED", last_outcome: "CONFIRMED", last_evidence: "READ_BACK", anchor_order_ref: null } as unknown as Op;
  const f = { q: "", stage: "all", backend: "all" } as const;
  it("filters by creator and by attempt outcome", () => {
    expect(matches(op, { ...f, by: "daniel.sales", outcome: "CONFIRMED" })).toBe(true);
    expect(matches(op, { ...f, by: "marta.ops" })).toBe(false);
    expect(matches(op, { ...f, outcome: "UNKNOWN" })).toBe(false);
    expect(matches({ ...op, last_outcome: null }, { ...f, outcome: "none" })).toBe(true);
    expect(matches(op, { ...f, outcome: "none" })).toBe(false);
  });
});
