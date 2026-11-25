/* What changed between two stored draft versions. Display only: the approval binds the server's hash. */
interface Line { canonical_sku: string | null; native_item: string; quantity_native: number; native_uom: string }
export interface Content { native_customer_id?: string; customer_po?: string | null; requested_date?: string | null; delivery_note?: string | null; lines?: Line[] }
export interface Change { what: string; from: string; to: string }

const HEADER: [keyof Content, string][] = [["native_customer_id", "Account"], ["customer_po", "Customer PO"], ["requested_date", "Requested date"], ["delivery_note", "Delivery note"]];
const show = (v: unknown) => (v == null || v === "" ? "—" : String(v));

function byItem(c: Content): Map<string, string> {
  const m = new Map<string, string>(), seen = new Map<string, number>();
  for (const l of c.lines ?? []) {
    const k = l.canonical_sku ?? l.native_item, n = (seen.get(k) ?? 0) + 1;
    seen.set(k, n);
    m.set(n > 1 ? `${k} (${n})` : k, `${l.quantity_native} ${l.native_uom}`);
  }
  return m;
}

export function diffVersions(a: Content, b: Content): Change[] {
  const out: Change[] = HEADER.map(([k, what]) => ({ what, from: show(a[k]), to: show(b[k]) })).filter((c) => c.from !== c.to);
  const x = byItem(a), y = byItem(b);
  for (const what of [...new Set([...x.keys(), ...y.keys()])].sort()) {
    const from = x.get(what) ?? "not present", to = y.get(what) ?? "removed";
    if (from !== to) out.push({ what, from, to });
  }
  return out;
}
