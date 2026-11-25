/** Display helpers. Times are shown in the viewer's locale; the UTC ISO value is kept for hover. */

export function money(v: string | number | null | undefined): string {
  if (v == null || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function eur(v: string | number | null | undefined): string {
  const m = money(v);
  return m === "—" ? "—" : `€${m}`;
}

const asDate = (v: unknown): Date | null => {
  if (typeof v !== "string" && !(v instanceof Date)) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
};

export function hm(v: unknown): string {
  const d = asDate(v);
  return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
}

export function hms(v: unknown): string {
  const d = asDate(v);
  return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
}

export function dateTime(v: unknown): string {
  const d = asDate(v);
  return d ? d.toLocaleString([], { dateStyle: "medium", timeStyle: "medium" }) : v == null ? "—" : String(v);
}

export function iso(v: unknown): string | undefined {
  const d = asDate(v);
  return d ? d.toISOString() : undefined;
}

/** "4 min ago", "2 h 5 min ago" relative to ``now`` (defaults to the viewer's clock). */
export function ago(v: unknown, now: Date = new Date()): string {
  const d = asDate(v);
  if (!d) return "—";
  const m = Math.max(0, Math.round((now.getTime() - d.getTime()) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 60 * 24) return `${Math.floor(m / 60)} h ${m % 60} min ago`;
  return `${Math.floor(m / 1440)} d ago`;
}

/** Humanised event type: "RECONCILIATION_FINISHED" → "Reconciliation finished". */
export const evLabel = (t: string): string => t.toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export const initials = (username: string): string => {
  const parts = username.split(/[._-]/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
};

export const shortHash = (h: unknown, n = 12): string => (typeof h === "string" && h ? `${h.slice(0, n)}…` : "—");
