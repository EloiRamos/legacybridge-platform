import { Link } from "react-router-dom";

import type { Schemas } from "../api/client";
import { answerKind, factsLabel, modeWords, nextActions, traceRows } from "../assistant";
import { shortHash } from "../format";
import { Icon } from "./Icon";

export type Turn = Schemas["TurnOut"];
interface Answer {
  summary?: string;
  facts?: { claim: string; record?: string | null; source?: { tool_call_id?: string; field_path?: string } }[];
  draft_operation_id?: string | null;
  warnings?: string[];
  next_human_actions?: string[];
}
type Receipt = Schemas["ReceiptOut"];

/** What the application itself stored for a drafting call. Shown whatever the model answered, including in a fallback. */
function ReceiptCard({ r }: { r: Receipt }) {
  if (r.status === "UNKNOWN") return (
    <div className="banner sm" role="note" aria-label="Draft receipt"><Icon name="help" /><div><b>Draft outcome unknown</b>
      It is not known whether this draft was stored. <Link to="/operations?stage=draft">Check Orders</Link> before preparing it again.</div></div>);
  if (r.status !== "STORED") return (
    <div className="banner sm" role="note" aria-label="Draft receipt" data-status={r.status}><Icon name="alert" /><div><b>Draft not verified</b>
      This draft was not read back: the application does not confirm what was stored. Do not prepare it again before you have checked{" "}
      {r.operation_id ? <><Link to={`/operations/${r.operation_id}`}>this order</Link> or </> : null}<Link to="/operations?stage=draft">Orders</Link>.</div></div>);
  const lines = (r.lines as { quantity_base?: number; canonical_sku?: string; native_item?: string }[]).map((l) => `${l.quantity_base} × ${l.canonical_sku ?? l.native_item}`).join(", ");
  return (
    <div className="banner sm ok" role="note" aria-label="Draft receipt"><Icon name="check" /><div>
      <b>{r.tool === "revise_draft" ? "Draft revised" : "Draft stored"}</b>
      {r.correlation_value ? <><span className="mono">{r.correlation_value}</span> · </> : null}version {r.version}{lines ? ` · ${lines}` : ""} · content <span className="mono">{shortHash(r.content_hash)}</span>
      {r.audit_seq != null ? <> · audit event <span className="mono">#{r.audit_seq}</span></> : null}
      <div style={{ marginTop: 4 }}><Link to={`/operations/${r.operation_id}`}>Open the assistant-prepared draft</Link> — you submit it; another person approves it.</div></div></div>);
}

/** An assistant turn: the application's own receipts and proposals first, then values read from system records, then the
 * model's wording, then what was attempted. The assistant reads and drafts only. */
export function AssistantAnswer({ t, compact, onConfirm }: { t: Turn; compact?: boolean; onConfirm?: (text: string) => void }) {
  const a = t.answer as Answer;
  const receipts = t.receipts ?? [];
  const rows = traceRows(t.tool_calls, t.refused_calls);
  const next = nextActions(a.next_human_actions);
  const model = !t.fallback_used && t.mode !== "disabled" && t.mode !== "application";
  return (
    <div className="ans">
      {receipts.map((r) => <ReceiptCard key={r.call_id} r={r} />)}
      {t.proposal ? (
        <div className="banner sm info" role="note" aria-label="Draft proposal"><Icon name="shield" /><div>
          <b>Nothing was written — confirm the exact draft</b>{t.proposal.description}
          <div className="muted small">Why it was not written directly: {t.proposal.reasons.join("; ")}.</div>
          {onConfirm ? <button type="button" className="btn sm" style={{ marginTop: 6 }} onClick={() => onConfirm(`confirm ${t.proposal!.code}`)}>Confirm this draft</button>
            : <div className="small">Reply <span className="mono">confirm {t.proposal.code}</span> to the assistant.</div>}</div></div>) : null}
      {t.notice ? <div className="banner sm info"><Icon name="alert" /><span>{t.notice}</span></div> : null}
      {a.summary && a.summary !== t.notice ? <div>{model ? <span className="lbl">Assistant’s wording</span> : null}{a.summary}</div> : t.notice ? null : <div>—</div>}
      {a.facts?.length ? (
        <div><span className="lbl">{factsLabel(t)}</span>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: compact ? 13 : 13.5 }}>
            {a.facts.map((f, i) => <li key={i}>{f.record ? <b>{f.record} · </b> : null}{f.claim}{f.source ? <span className="cite"><Icon name="chain" />{f.source.tool_call_id} · {f.source.field_path}</span> : null}</li>)}
          </ul></div>) : null}
      {(a.warnings ?? []).map((w, i) => <div key={i} className="banner sm"><Icon name="alert" /><span>{w}</span></div>)}
      {!receipts.length && a.draft_operation_id ? <p style={{ margin: 0 }}><Link to={`/operations/${a.draft_operation_id}`}>Open the assistant-prepared draft</Link> — you submit it; another person approves it.</p> : null}
      {!compact && next.length ? (
        <div><b style={{ fontSize: 13 }}>What a person does next</b>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13.5 }}>{next.map((n, i) => <li key={i}>{n}</li>)}</ul></div>) : null}
      {rows.length ? (
        <div className="trace" role="group" aria-label="Tool calls">
          {rows.map((r, i) => <span key={i} className={r.tone === "ok" ? undefined : r.tone === "refused" ? "no" : "wa"}><span aria-hidden="true">{r.tone === "ok" ? "✓" : r.tone === "refused" ? "✕" : r.tone === "fail" ? "!" : "?"}</span> {r.name} — {r.label}</span>)}
        </div>) : null}
      <span className="muted" style={{ fontSize: 12 }}>Mode: {modeWords(t.mode)} · {answerKind(t)}</span>
    </div>
  );
}
