import { expect, type Page, type Request } from "@playwright/test";

import type { Schemas } from "../../src/api/client";

/** Fictional data typed against the frozen API schema. Nothing here reaches a server. */
export const ORIGIN = "http://127.0.0.1:5212";
export const OP = "f537ecff-3bfc-4db1-a20e-c07c544c4e6a";
export const CV = "LB22JZJ5YA56";
export const HASH = "804e5e0c2ac52d25ca7ca60047b5391df10c1477e5422847037c904f3ddea52a";
export const CSRF = "mocked-csrf";
const AT = "2026-11-23T18:02:00.124233Z";
type Summary = Schemas["OperationSummaryOut"];
type Detail = Schemas["OperationDetailOut"];
type Avail = Schemas["AvailabilityOut"];

const PERMISSIONS: Schemas["Permission"][] = ["customer.read", "inventory.read", "operation.read", "draft.create", "draft.edit",
  "draft.submit", "draft.withdraw", "approval.grant.standard", "approval.grant.any", "approval.override", "reconcile.recheck",
  "escalation.open", "escalation.resolve", "audit.read", "assistant.use", "admin.users", "ackqueue.read"];
const session: Schemas["SessionOut"] = { authenticated_at: AT, csrf_token: CSRF, permissions: PERMISSIONS, profile: "test",
  roles: ["ops_manager"], user_id: "00000000-0000-4000-8000-000000000001", username: "marta.ops" };

export function summary(state: Summary["state"], patch: Partial<Summary> = {}): Summary {
  return { operation_id: OP, correlation_value: CV, backend_id: "skarvik", native_customer_id: "KND-0081", state,
    last_outcome: state === "UNKNOWN" ? "UNKNOWN" : null, last_evidence: state === "UNKNOWN" ? "ACK_OVERDUE" : null,
    verification_level: state === "UNKNOWN" ? "UNRESOLVED" : null, current_version: 1, attempt_count: state === "UNKNOWN" ? 1 : 0,
    origin: "UI", created_by: "daniel.sales", created_at: AT, updated_at: AT, estimated_total: "1204.75", anchor_order_ref: null, ...patch };
}

/** A data-rich order: every record table populated with realistic widths. */
export function detail(state: Summary["state"], permitted: Schemas["Action"][], rich = true): Detail {
  const line = (n: number) => ({ line_no: n, native_uom: "EA", native_item: `NVB-6205-2RS-${n}`, canonical_sku: `BW-100${n}`, line_estimate: "240.95", quantity_base: 5, quantity_native: 5 });
  const check = (c: string, line_no: number | null = null) => ({ check: c, detail: "", result: "PASS", line_no });
  const sha = "81b512f1e5ce225c62c7939264dd30313a562d279229c2fd387a5e0550d4e76e";
  return {
    operation: summary(state), permitted_actions: permitted, capabilities: {}, schedule: {}, timeline: [],
    versions: [{ operation_id: OP, version: 1, content_hash: HASH, created_by: "daniel.sales", created_at: AT, origin: "UI",
      content: { backend_id: "skarvik", native_customer_id: "KND-0081", lines: [1, 2, 3, 4, 5].map(line) } }],
    validation: { version: 1, blocking: false, estimated_total: "1204.75", checks: [check("target_hold"), check("line_stock", 1), check("value_threshold")] },
    approvals: rich ? [{ attempt_no: 1, decision: "APPROVED", decided_by: "priya.sales", version: 1, content_hash: HASH, decided_at: AT,
      override_reason: "Customer confirmed the split delivery in writing, see ticket", reason: null }] : [],
    authorizations: rich ? [{ kind: "SUBMIT", attempt_no: 1, state: "CONSUMED", granted_by: "priya.sales", granted_at: AT, expires_at: AT, consumed_at: AT }] : [],
    transmissions: rich ? [{ attempt_no: 1, transmission_no: 1, kind: "SUBMIT", state: "OBSERVED", message_id: `${CV}-01`, payload_sha256: sha, started_at: AT, observed_at: AT }] : [],
    acknowledgements: [],
    reconciliation_runs: rich ? [1, 2, 3].map(() => ({ trigger: "AUTO", verdict: "NO_MATCH", started_at: AT, finished_at: AT })) : [],
    escalations: rich ? [{ reason: "RECHECK_LIMIT", opened_at: AT, resolution: "ATTESTED_NOT_BOOKED", external_reference: "SKV-DESK-2026-118204", resolved_at: AT }] : [],
  };
}

const avail = (backend: string, sku: string, native: string, status: Avail["status"], n: number | null, freshness: string, age: number | null, explanation: string): Avail => ({
  as_of: AT, available_base: n, backend_id: backend, canonical_sku: sku, explanation, freshness, issues: [], legacy_available_flag: null,
  native_item: native, snapshot_age_seconds: age, status, warehouses: [{ warehouse: "GBG", on_hand: n }] });
export function inventory(degraded = false): Schemas["InventoryOut"] {
  const halden = (a: Avail) => (degraded ? [] : [a]);
  return {
    items: [
      { canonical_sku: "BW-1001", name: "Deep groove ball bearing 6205-2RS", per_backend: [
        ...halden(avail("halden", "BW-1001", "HX-6205-2RS", "IN_STOCK", 1340, "LIVE", null, "1,340 EA free across 2 warehouses.")),
        avail("skarvik", "BW-1001", "NVB-6205-2RS", "IN_STOCK", 120, "SNAPSHOT", 42, "120 EA free in GBG (snapshot 42 s old).")] },
      { canonical_sku: "BW-1004", name: "Hydraulic hose 2SN DN12, per metre", per_backend: [
        ...halden(avail("halden", "BW-1004", "HX-HOSE-2SN12", "OUT_OF_STOCK", 0, "LIVE", null, "No free stock.")),
        avail("skarvik", "BW-1004", "NVB-HYD-2SN-12", "UNDETERMINED", null, "SNAPSHOT", 42, "Stored quantity is not a number; availability cannot be determined.")] },
      { canonical_sku: "BW-1007", name: "Safety relay 24 V DC", per_backend: [
        avail("skarvik", "BW-1007", "NVB-PNOZ-24", "STALE", 8, "SNAPSHOT", 9200, "8 EA in an extract older than the freshness limit.")] },
    ],
    unmapped: [], backend_errors: degraded ? { halden: "HALDEN could not be read (timeout)." } : {},
  };
}

export interface Mock { mutations: Request[]; unexpected: string[]; live: { detail: Detail } }
export interface MockOptions {
  detail?: Detail; inventory?: Schemas["InventoryOut"] | "503"; queue?: Summary[];
  /** Answer to any POST on the operation; called once per request. */
  onPost?: (r: Request, live: { detail: Detail }) => Promise<{ status: number; body: unknown }> | { status: number; body: unknown };
}

/** Answers every API call from memory. Anything unmocked, and anything for another origin, is aborted and recorded,
 * so a test can prove no request left for a real backend. */
export async function mock(page: Page, o: MockOptions = {}): Promise<Mock> {
  const m: Mock = { mutations: [], unexpected: [], live: { detail: o.detail ?? detail("UNKNOWN", ["recheck", "escalate"]) } };
  const orders = (n: number) => Array.from({ length: n }, (_, i) => ({ order_ref: `SO-24010${i}`, reference: `${CV}-REFERENCE-LONG-${i}`, status: "PARTIALLY_DELIVERED", order_date: "2026-11-20" }));
  const account = { backend_id: "skarvik", native_id: "KND-0081", name_display: "Nordkap Marine Services AS", name_raw: "NORDKAP", credit_hold: "CLEAR", city: "Bergen", country_code: "NO" };
  const get: Record<string, () => unknown> = {
    "/api/v1/auth/session": () => session,
    "/api/v1/reconciliation": () => ({ items: [] }),
    "/api/v1/approvals": () => ({ items: o.queue ?? [] }),
    "/api/v1/operations": () => ({ items: o.queue ?? [], total: (o.queue ?? []).length }),
    "/api/v1/inventory": () => o.inventory ?? inventory(),
    [`/api/v1/operations/${OP}`]: () => m.live.detail,
    "/api/v1/customers": () => ({ parties: [], ambiguity_groups: [], backend_errors: {}, accounts: [account, { ...account, native_id: "KND-0082", name_display: "Nordkap Maritime Logistics and Services AS" }] }),
    "/api/v1/customers/skarvik/KND-0081": () => ({ account, linked_accounts: [], open_operations: [], orders: orders(3), backend_errors: {} }),
    "/api/v1/admin/users": () => [{ user_id: "u1", username: "rafael.integration", display_name: "Rafael Integration", roles: ["integration_operator", "ops_manager"], is_active: true, locked_until: AT }],
    "/api/v1/admin/ack-queue": () => [{ file_name: `ACK_${CV}-01_20261123.csv`, status: "REJECTED", order_ref: "SKV-OES-000118204", code: "E1042", recorded_at: AT, sha256: HASH, reason: "no operation" }],
  };
  await page.route("**/*", async (route) => {
    const r = route.request();
    const u = new URL(r.url());
    if (u.origin !== ORIGIN) { m.unexpected.push(r.url()); return route.abort(); }
    if (!u.pathname.startsWith("/api")) return route.continue();
    if (r.method() === "GET") {
      if (u.pathname === "/api/v1/inventory" && o.inventory === "503") return route.fulfill({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ status: 503, code: "BACKEND_UNAVAILABLE", title: "Backend unavailable", detail: "Neither backend answered." }) });
      const f = get[u.pathname];
      if (f) return route.fulfill({ json: f() });
    } else if (o.onPost) {
      m.mutations.push(r);
      const a = await o.onPost(r, m.live);
      return route.fulfill({ status: a.status, contentType: a.status >= 400 ? "application/problem+json" : "application/json", body: JSON.stringify(a.body) });
    } else m.mutations.push(r);
    m.unexpected.push(`${r.method()} ${r.url()}`);
    return route.abort();
  });
  return m;
}
export const clean = (m: Mock) => expect(m.unexpected, "no unmocked or external request").toEqual([]);
