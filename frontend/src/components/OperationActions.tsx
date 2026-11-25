import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, call, type Schemas } from "../api/client";
import type { Detail } from "../evidence";
import { dateTime, eur, initials } from "../format";
import { useSession } from "../session";
import { backendName, BACKEND_FULL, STATUS } from "../status";
import { blockedSubmitNotice } from "../submitNotice";
import { Icon } from "./Icon";
import { Sheet } from "./Sheet";

export type Action = Schemas["Action"];

/** Button labels for the server's permitted_actions (identical strings are asserted by e2e UI-03). */
export const ACTION_LABEL: Record<Action, string> = {
  revise: "Revise", submit_for_approval: "Submit for approval", withdraw: "Withdraw draft", approve: "Approve…",
  decline: "Decline…", recheck: "Re-check (read-only)", escalate: "Escalate", resolve_verified: "Resolve — verify order…",
  resolve_attested: "Resolve — attest…", authorize_withdraw_unclaimed: "Withdraw unclaimed message…",
  authorize_redelivery: "Authorize redelivery…",
};
const PRIMARY = new Set<Action>(["approve", "submit_for_approval", "resolve_verified"]);
type Dialog = null | "approve" | "decline" | "resolve_verified" | "resolve_attested" | "withdraw_unclaimed" | "redelivery" | "revise" | "withdraw" | "escalate";

export interface VersionContent {
  content_hash?: string;
  content?: { lines?: { line_no: number; canonical_sku: string | null; native_item: string; quantity_base: number; quantity_native: number; native_uom: string; line_estimate: string | null }[] };
  created_by?: string;
  version?: number;
}
export const currentVersion = (d: Detail): VersionContent => (d.versions[d.versions.length - 1] ?? {}) as VersionContent;
export const checksOf = (d: Detail) => (d.validation?.checks ?? []) as { check: string; result: string; detail?: string; line_no?: number | null }[];

/** State-changing commands for one operation. Every button is rendered from the server's permitted_actions; the
 * server re-checks permission, four-eyes and the exact version on every command. */
export function useOperationActions(d: Detail | undefined) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const id = d?.operation.operation_id ?? "";
  const confirming = useRef(false); // set synchronously: two submits in one tick must not send two requests
  const reread = () => Promise.all([qc.invalidateQueries({ queryKey: ["operation", id] }), qc.invalidateQueries({ queryKey: ["approvals"] }),
    qc.invalidateQueries({ queryKey: ["operations"] }), qc.invalidateQueries({ queryKey: ["dashboard"] })]);
  const run = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSettled: () => { confirming.current = false; },
    onSuccess: async (result) => {
      setDialog(null);
      setActionError(null);
      setActionNotice(blockedSubmitNotice(result));
      await reread();
    },
    onError: (e) => {
      setActionNotice(null);
      setDialog(null); // the refusal is shown on the page (role=alert), next to the order it concerns
      if (e instanceof ApiError && e.problem.code === "REAUTH_REQUIRED") navigate(`/reauth?next=${encodeURIComponent(window.location.pathname)}`);
      else {
        setActionError(e);
        void reread(); // the command may have been applied although its answer was lost (RR-1): show the server's state
      }
    },
  });
  const path = { params: { path: { operation_id: id } } };
  const start = (a: Action) => {
    if (!d) return;
    setActionError(null);
    const op = d.operation;
    switch (a) {
      case "submit_for_approval": run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/submit", { ...path, body: { expected_version: op.current_version } }))); break;
      case "recheck": run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/recheck", path))); break;
      case "authorize_withdraw_unclaimed": setDialog("withdraw_unclaimed"); break;
      case "authorize_redelivery": setDialog("redelivery"); break;
      default: setDialog(a);
    }
  };
  const confirmOnce = (fn: () => Promise<unknown>) => {
    if (confirming.current || run.isPending) return;
    confirming.current = true;
    run.mutate(fn);
  };
  const permitted = (d?.permitted_actions ?? []) as Action[];
  const buttons = (filter?: (a: Action) => boolean) => permitted.filter((a) => !filter || filter(a)).map((a) => (
    <button key={a} type="button" className={`btn${PRIMARY.has(a) ? " pri" : ""}`} onClick={() => start(a)} disabled={run.isPending}>{ACTION_LABEL[a]}</button>
  ));
  const close = () => setDialog(null);
  let sheet: ReactNode = null;
  if (d && dialog) {
    const op = d.operation;
    const v = currentVersion(d);
    const hash = String(v.content_hash ?? "");
    const checks = checksOf(d);
    const overrides = checks.filter((c) => c.result === "OVERRIDE_REQUIRED");
    const eyebrow = <><span className="mono">{op.correlation_value}</span> · v{op.current_version} · {backendName(op.backend_id)}</>;
    const common = { onClose: close, busy: run.isPending, eyebrow };
    if (dialog === "approve") sheet = <ApproveSheet d={d} hash={hash} overrides={overrides.map((c) => c.detail || c.check)} {...common}
      onApprove={(reason) => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/approve", { ...path, body: { version: op.current_version, content_hash: hash, override_reason: reason || null } })))} />;
    if (dialog === "decline") sheet = <ReasonSheet title={`Decline v${op.current_version}`} submitLabel="Decline" label="Reason" min={10} {...common}
      intro={<p style={{ margin: 0 }}>Version v{op.current_version}, content hash <span className="mono">{hash.slice(0, 12)}…</span>. The preparer can revise or withdraw it.</p>}
      onSubmit={(reason) => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/decline", { ...path, body: { version: op.current_version, content_hash: hash, reason } })))} />;
    if (dialog === "resolve_verified") sheet = <ReasonSheet title="Resolve by verified read" submitLabel="Read and verify" label="Backend order reference" min={1} input {...common}
      intro={<p style={{ margin: 0 }}>LegacyBridge reads the order you name from {backendName(op.backend_id)} and confirms only if account, reference and lines match the approved version. If they don’t match, nothing is confirmed.</p>}
      onSubmit={(ref) => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/resolve", { ...path, body: { kind: "VERIFIED", external_reference: ref, note: "" } })))} />;
    if (dialog === "resolve_attested") sheet = <AttestSheet {...common}
      onSubmit={(kind, ref, note) => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/resolve", { ...path, body: { kind, external_reference: ref, note } })))} />;
    if (dialog === "withdraw") sheet = <ConfirmSheet title="Withdraw this order" submitLabel="Withdraw order" guarded {...common}
      onSubmit={() => confirmOnce(() => call(api.POST("/api/v1/operations/{operation_id}/withdraw", path)))}>
      <p style={{ margin: 0 }}>The order becomes Withdrawn. This is final: it can no longer be revised, submitted or approved, and a new draft would be needed. Nothing is sent to {backendName(op.backend_id)}.</p>
    </ConfirmSheet>;
    if (dialog === "escalate") sheet = <ConfirmSheet title="Escalate for a decision" submitLabel="Escalate order" guarded {...common}
      onSubmit={() => confirmOnce(() => call(api.POST("/api/v1/operations/{operation_id}/escalate", { ...path, body: { note: "" } })))}>
      <p style={{ margin: 0 }}>The order moves from {STATUS.UNKNOWN.label} to {STATUS.ESCALATED.label}, for a person to resolve by a verified read or an attestation. Whether {backendName(op.backend_id)} booked it stays not known: escalating proves nothing and re-sends nothing. Do not re-enter the order.</p>
    </ConfirmSheet>;
    if (dialog === "withdraw_unclaimed") sheet = <ConfirmSheet title="Withdraw unclaimed message" submitLabel="Withdraw" {...common}
      onSubmit={() => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/withdraw-unclaimed", path)))}>
      <p style={{ margin: 0 }}>Moves the order file from the SKARVIK inbox to <span className="mono">withdrawn/</span>. If SKARVIK already claimed it, nothing is proven and the order stays unresolved.</p>
    </ConfirmSheet>;
    if (dialog === "redelivery") sheet = <ConfirmSheet title="Authorize one redelivery" submitLabel="Authorize" {...common}
      onSubmit={() => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/redelivery", path)))}>
      <p style={{ margin: 0 }}>The engine hands the stored, byte-identical message over once more under the same message id. SKARVIK deduplicates it within its window (until {dateTime((d.schedule as Record<string, unknown>).redelivery_deadline)}). At most one redelivery per attempt.</p>
    </ConfirmSheet>;
    if (dialog === "revise") sheet = <ReviseSheet d={d} {...common}
      onSubmit={(lines) => run.mutate(() => call(api.POST("/api/v1/operations/{operation_id}/revisions", { ...path, body: { expected_version: op.current_version, backend_id: op.backend_id as "halden" | "skarvik", native_customer_id: op.native_customer_id, lines } })))} />;
  }
  return { start, buttons, permitted, sheet, run, actionError, actionNotice, setDialog };
}

interface Common { onClose: () => void; busy?: boolean; eyebrow?: ReactNode }
const Footer = ({ onClose, label, ok, busy, accent, guarded }: { onClose: () => void; label: string; ok: boolean; busy?: boolean; accent?: boolean; guarded?: boolean }) => (
  <>
    <button type="button" className="btn" onClick={onClose} disabled={guarded && busy} data-autofocus={guarded || undefined}>Cancel</button>
    <button type="submit" className={`btn ${accent ? "acc" : "pri"}`} disabled={!ok || busy}>{busy ? "Working…" : label}</button>
  </>
);

export function FourEyes({ preparer, approver, tight }: { preparer: string; approver: string; tight?: boolean }) {
  return (
    <div className={`eyes${tight ? " tight" : ""}`} aria-label="Four-eyes check">
      <div className="person"><span className="a" aria-hidden="true">{initials(preparer)}</span><div><b>{preparer}</b><small>Prepared</small></div></div>
      <span className="neq" aria-label="different persons" title="Different persons">≠</span>
      <div className="person"><span className="a me" aria-hidden="true">{initials(approver)}</span><div><b>{approver}</b><small>Deciding</small></div></div>
    </div>
  );
}

function ApproveSheet({ d, hash, overrides, onApprove, ...c }: Common & { d: Detail; hash: string; overrides: string[]; onApprove: (reason: string) => void }) {
  const { session } = useSession();
  const [reason, setReason] = useState("");
  const op = d.operation;
  const v = currentVersion(d);
  const lines = v.content?.lines ?? [];
  const need = overrides.length ? 20 : 0;
  const ok = reason.trim().length >= need;
  return (
    <Sheet title={`Approve v${op.current_version}`} onClose={c.onClose} eyebrow={c.eyebrow} busy={c.busy} onSubmit={() => onApprove(reason.trim())}
      footer={<Footer onClose={c.onClose} label="Approve" ok={ok} busy={c.busy} accent />}>
      <FourEyes preparer={op.created_by} approver={session?.username ?? "you"} tight />
      <div className="pin">
        <b><span className="mono">{op.correlation_value}</span> · account <span className="mono">{op.native_customer_id}</span></b>
        <span className="muted">{lines.length} line{lines.length === 1 ? "" : "s"} · EUR {eur(op.estimated_total).replace("€", "")} · {BACKEND_FULL[op.backend_id] ?? op.backend_id}</span>
        <span className="hx">Content hash {hash}</span>
      </div>
      <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>The approval binds this version and its content hash; any change to the draft invalidates it. One hand-off follows; it is never repeated automatically. You may be asked to re-authenticate.</p>
      {overrides.length ? (
        <div className="field">
          <div className="banner sm"><Icon name="alert" /><div><b>An override reason is required.</b>{overrides.join("; ")}</div></div>
          <label htmlFor="sh-t">Override reason <span className="muted" style={{ fontWeight: 400 }}>— recorded with the approval</span></label>
          <textarea className="inp" id="sh-t" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} autoFocus aria-describedby="sh-t-c" />
          <span id="sh-t-c" className={`counter${ok ? " ok" : ""}`} aria-live="polite">{reason.trim().length} / {need} characters minimum</span>
        </div>
      ) : null}
    </Sheet>
  );
}

function ReasonSheet({ title, submitLabel, label, min, input, intro, onSubmit, ...c }: Common & { title: string; submitLabel: string; label: string; min: number; input?: boolean; intro: ReactNode; onSubmit: (v: string) => void }) {
  const [v, setV] = useState("");
  const ok = v.trim().length >= min;
  return (
    <Sheet title={title} onClose={c.onClose} eyebrow={c.eyebrow} busy={c.busy} onSubmit={() => onSubmit(v.trim())} footer={<Footer onClose={c.onClose} label={submitLabel} ok={ok} busy={c.busy} />}>
      {intro}
      <div className="field">
        <label htmlFor="sh-v">{label}</label>
        {input ? <input className="inp mono" id="sh-v" value={v} onChange={(e) => setV(e.target.value)} maxLength={40} autoFocus autoComplete="off" />
          : <textarea className="inp" id="sh-v" rows={3} value={v} onChange={(e) => setV(e.target.value)} maxLength={500} autoFocus aria-describedby="sh-v-c" />}
        {min > 1 ? <span id="sh-v-c" className={`counter${ok ? " ok" : ""}`} aria-live="polite">{v.trim().length} / {min} characters minimum</span> : null}
      </div>
    </Sheet>
  );
}

/** ``guarded``: opens on Cancel, and cannot be dismissed while its request is in flight. */
function ConfirmSheet({ title, submitLabel, onSubmit, children, guarded, ...c }: Common & { title: string; submitLabel: string; onSubmit: () => void; children: ReactNode; guarded?: boolean }) {
  return (
    <Sheet title={title} onClose={c.onClose} eyebrow={c.eyebrow} busy={c.busy} locked={guarded && c.busy} onSubmit={onSubmit} footer={<Footer onClose={c.onClose} label={submitLabel} ok busy={c.busy} guarded={guarded} />}>
      {children}
    </Sheet>
  );
}

function AttestSheet({ onSubmit, ...c }: Common & { onSubmit: (kind: "ATTESTED_BOOKED" | "ATTESTED_NOT_BOOKED", ref: string, note: string) => void }) {
  const [kind, setKind] = useState<"ATTESTED_BOOKED" | "ATTESTED_NOT_BOOKED">("ATTESTED_NOT_BOOKED");
  const [ref, setRef] = useState("");
  const [note, setNote] = useState("");
  const ok = ref.trim().length > 0 && note.trim().length >= 20;
  return (
    <Sheet title="Close by attestation" onClose={c.onClose} eyebrow={c.eyebrow} busy={c.busy} onSubmit={() => onSubmit(kind, ref.trim(), note.trim())}
      footer={<Footer onClose={c.onClose} label="Close by attestation" ok={ok} busy={c.busy} />}>
      <div className="banner sm att"><Icon name="alert" /><div><b>Recorded as not verified.</b>An attestation closes the order on a person’s statement and an external reference. It is never shown as a verified backend read.</div></div>
      <fieldset className="optgroup">
        <legend className="lbl" style={{ marginBottom: 8 }}>Attestation</legend>
        <label className="opt"><input type="radio" name="kind" checked={kind === "ATTESTED_BOOKED"} onChange={() => setKind("ATTESTED_BOOKED")} /> <span>Booked in the backend</span></label>
        <label className="opt"><input type="radio" name="kind" checked={kind === "ATTESTED_NOT_BOOKED"} onChange={() => setKind("ATTESTED_NOT_BOOKED")} /> <span>Not booked</span></label>
      </fieldset>
      <div className="field"><label htmlFor="att-ref">External reference</label><input className="inp mono" id="att-ref" value={ref} onChange={(e) => setRef(e.target.value)} maxLength={40} autoFocus autoComplete="off" /></div>
      <div className="field"><label htmlFor="att-note">Note (≥ 20 characters)</label><textarea className="inp" id="att-note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={500} aria-describedby="att-note-c" />
        <span id="att-note-c" className={`counter${note.trim().length >= 20 ? " ok" : ""}`} aria-live="polite">{note.trim().length} / 20 characters minimum</span></div>
    </Sheet>
  );
}

function ReviseSheet({ d, onSubmit, ...c }: Common & { d: Detail; onSubmit: (lines: { canonical_sku: string | null; native_item: string | null; quantity: number }[]) => void }) {
  const v = currentVersion(d);
  const [lines, setLines] = useState((v.content?.lines ?? []).map((l) => ({ item: l.native_item, qty: l.quantity_base })));
  const ok = lines.length > 0 && lines.every((l) => l.item.trim() && l.qty >= 1 && l.qty <= 999);
  return (
    <Sheet title="Revise draft" onClose={c.onClose} eyebrow={c.eyebrow} busy={c.busy} onSubmit={() => onSubmit(lines.map((l) => ({ canonical_sku: null, native_item: l.item.trim(), quantity: l.qty })))}
      footer={<Footer onClose={c.onClose} label="Save new version" ok={ok} busy={c.busy} />}>
      <p className="muted" style={{ margin: 0 }}>Saving creates a new immutable version. Any earlier approval of this order no longer applies.</p>
      {lines.map((l, i) => (
        <div className="line" key={i} style={{ gridTemplateColumns: "minmax(0,1fr) 110px" }}>
          <div className="field"><label htmlFor={`rv-item-${i}`}>Native item</label><input className="inp mono" id={`rv-item-${i}`} value={l.item} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, item: e.target.value } : x)))} /></div>
          <div className="field"><label htmlFor={`rv-qty-${i}`}>Quantity (base)</label><input className="inp tnum" id={`rv-qty-${i}`} type="number" min={1} max={999} value={l.qty} onChange={(e) => setLines(lines.map((x, j) => (j === i ? { ...x, qty: Number(e.target.value) } : x)))} /></div>
        </div>
      ))}
    </Sheet>
  );
}
