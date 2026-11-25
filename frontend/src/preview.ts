/* Reads shown while drafting (live validation preview). These are the same live facts the server reads; the
 * checks that decide are made by the server when the draft is submitted, so nothing here states a decision. */
import type { Schemas } from "./api/client";

type Avail = Schemas["AvailabilityOut"];
export interface PreviewLine { sku: string; tone: "ok" | "warn" | "bad"; text: string }

export function previewLine(sku: string, qty: number, found: Avail[]): PreviewLine {
  const a = found[0];
  if (!a) return { sku, tone: "bad", text: "not found in this system’s catalogue" };
  if (a.available_base == null) return { sku, tone: "warn", text: `${a.status.replace("_", " ").toLowerCase()} — ${a.explanation}` };
  const enough = a.status === "IN_STOCK" && a.available_base >= qty;
  return { sku, tone: enough ? "ok" : "warn", text: `${a.available_base.toLocaleString("en-GB")} available, ${qty} requested (${a.freshness === "LIVE" ? "live read" : "snapshot"})` };
}
