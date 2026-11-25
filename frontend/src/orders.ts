/* Orders workspace logic shared by the grouped view and the compact table (same data, filters and semantics). */
import type { Schemas } from "./api/client";
import { backendName, type Stage, STAGE_OF, STAGE_RANK, statusLabel } from "./status";

export type Op = Schemas["OperationSummaryOut"];
export type SortKey = "ref" | "account" | "status" | "total" | "by" | "updated";
export type SortDir = "asc" | "desc";
export interface OrderFilter { q: string; stage: Stage | "all"; backend: "all" | "halden" | "skarvik"; by?: string; outcome?: string }

/** Server limit of GET /api/v1/operations (readmodels.list_operations, newest first). */
export const LIST_LIMIT = 200;

export function matches(op: Op, f: OrderFilter): boolean {
  if (f.backend !== "all" && op.backend_id !== f.backend) return false;
  if (f.stage !== "all" && STAGE_OF[op.state] !== f.stage) return false;
  if (f.by && f.by !== "all" && op.created_by !== f.by) return false;
  if (f.outcome && f.outcome !== "all" && (op.last_outcome ?? "none") !== f.outcome) return false;
  const q = f.q.trim().toLowerCase();
  if (!q) return true;
  const hay = [op.correlation_value, op.native_customer_id, op.created_by, backendName(op.backend_id), op.state,
    statusLabel(op.state, op.last_evidence, op.backend_id).label, op.anchor_order_ref ?? "", op.operation_id].join(" ").toLowerCase();
  return hay.includes(q);
}

const num = (v: string | null | undefined): number => (v == null || v === "" ? Number.NEGATIVE_INFINITY : Number(v));

const KEY: Record<SortKey, (o: Op) => string | number> = {
  ref: (o) => o.correlation_value,
  account: (o) => `${o.backend_id}:${o.native_customer_id}`,
  status: (o) => STAGE_RANK[STAGE_OF[o.state]] * 100 + (o.state === "ESCALATED" ? 0 : 1),
  total: (o) => num(o.estimated_total),
  by: (o) => o.created_by,
  updated: (o) => new Date(o.updated_at).getTime(),
};

export function sortOps(rows: Op[], key: SortKey, dir: SortDir): Op[] {
  const m = dir === "asc" ? 1 : -1;
  const k = KEY[key];
  return rows.slice().sort((a, b) => {
    const x = k(a), y = k(b);
    const c = x < y ? -1 : x > y ? 1 : 0;
    return c * m || new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });
}

export function countByStage(rows: Op[]): Record<Stage | "all", number> {
  const c: Record<Stage | "all", number> = { all: rows.length, attention: 0, approval: 0, flight: 0, draft: 0, closed: 0 };
  for (const r of rows) c[STAGE_OF[r.state]] += 1;
  return c;
}
