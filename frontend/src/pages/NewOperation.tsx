import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, call, type Schemas } from "../api/client";
import { Icon } from "../components/Icon";
import { PageHead } from "../components/Page";
import { ErrorBox } from "../components/States";
import { previewLine, type PreviewLine } from "../preview";

interface Preview { key: string; account: Schemas["AccountOut"] | null; accountError: unknown; lines: PreviewLine[]; stockError: string | null }
interface Line { sku: string; qty: number }

/** Draft editor. Submission contract unchanged: POST /api/v1/operations creates a DRAFT; validation against live
 * backend data happens when the preparer submits it for approval. */
export function NewOperation() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [backend, setBackend] = useState<"halden" | "skarvik">(params.get("backend") === "skarvik" ? "skarvik" : "halden");
  const [customer, setCustomer] = useState(params.get("customer") ?? "");
  const [lines, setLines] = useState<Line[]>([{ sku: "", qty: 1 }]);
  const [po, setPo] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const r = await call(api.POST("/api/v1/operations", { body: {
        backend_id: backend, native_customer_id: customer.trim(),
        lines: lines.filter((l) => l.sku.trim()).map((l) => ({ canonical_sku: l.sku.trim(), quantity: l.qty })),
        customer_po: backend === "skarvik" && po ? po : null, delivery_note: backend === "skarvik" && note ? note : null,
      } }));
      navigate(`/operations/${r.operation_id}`);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const key = JSON.stringify([backend, customer.trim(), lines]);
  const [pv, setPv] = useState<Preview | null>(null);
  const [checking, setChecking] = useState(false);
  const preview = async () => {
    setChecking(true);
    const [acc, inv] = await Promise.allSettled([
      call(api.GET("/api/v1/customers/{backend_id}/{native_id}", { params: { path: { backend_id: backend, native_id: customer.trim() } } })),
      call(api.GET("/api/v1/inventory"))]);
    const bErr = inv.status === "fulfilled" ? inv.value.backend_errors?.[backend] : "The stock could not be read.";
    const items = inv.status === "fulfilled" ? inv.value.items : [];
    setPv({ key, account: acc.status === "fulfilled" ? acc.value.account : null, accountError: acc.status === "rejected" ? acc.reason : null, stockError: bErr ?? null,
      lines: bErr ? [] : lines.filter((l) => l.sku.trim()).map((l) => previewLine(l.sku.trim(), l.qty, items.filter((i) => i.canonical_sku === l.sku.trim()).flatMap((i) => i.per_backend.filter((a) => a.backend_id === backend)))) });
    setChecking(false);
  };
  const upd = (i: number, patch: Partial<Line>) => setLines(lines.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  return (
    <>
      <PageHead eyebrow={<><Link to="/operations">Orders</Link><span aria-hidden="true">/</span><span>New order</span></>} title="New order draft"
        lede="Quantities are in group base units. The draft is validated against live backend data when you submit it for approval; nothing is sent to a backend until a different person approves the exact version." />
      <form className="col" onSubmit={submit} aria-label="New order draft" aria-busy={busy}>
        {error ? <ErrorBox error={error} /> : null}
        <section className="card card-p form">
          <div className="field"><label htmlFor="backend">Backend</label>
            <select className="inp" id="backend" value={backend} onChange={(e) => setBackend(e.target.value as "halden" | "skarvik")}>
              <option value="halden">HALDEN ERP 3.1 (BU Central Europe, UK &amp; Ireland)</option>
              <option value="skarvik">SKARVIK OES 4 (BU Nordic)</option>
            </select></div>
          <div className="field"><label htmlFor="customer">Account number</label>
            <input className="inp mono" id="customer" required value={customer} onChange={(e) => setCustomer(e.target.value)} maxLength={16} autoComplete="off" placeholder={backend === "halden" ? "00417" : "KND-0081"} aria-describedby="customer-h" />
            <span className="hint" id="customer-h">The account is resolved in {backend === "halden" ? "HALDEN" : "SKARVIK"} when the draft is created. <Link to="/customers">Find an account</Link></span></div>
        </section>
        <section className="card card-p" role="group" aria-labelledby="no-lines">
          <h2 className="h3" id="no-lines" style={{ marginBottom: 12 }}>Lines</h2>
          <div className="lines">
            {lines.map((l, i) => (
              <div className="line" key={i}>
                <div className="field"><label htmlFor={`sku-${i}`}>Group SKU</label><input className="inp mono" id={`sku-${i}`} value={l.sku} onChange={(e) => upd(i, { sku: e.target.value })} maxLength={16} placeholder="BW-1001" autoComplete="off" /></div>
                <div className="field"><label htmlFor={`qty-${i}`}>Quantity</label><input className="inp tnum" id={`qty-${i}`} type="number" min={1} max={999} value={l.qty} onChange={(e) => upd(i, { qty: Number(e.target.value) })} /></div>
                <div className="field hintc"><span className="lbl" aria-hidden="true">&nbsp;</span><span className="hint" style={{ minHeight: 42, display: "flex", alignItems: "center" }}>Group SKU, e.g. BW-1004 · 1–999 base units. Stock and price are checked at submission.</span></div>
                {lines.length > 1 ? <button className="btn sm" type="button" onClick={() => setLines(lines.filter((_, j) => j !== i))} aria-label={`Remove line ${i + 1}`}><Icon name="x" /></button> : <span />}
              </div>
            ))}
          </div>
          <button className="btn sm" type="button" style={{ marginTop: 12 }} onClick={() => setLines([...lines, { sku: "", qty: 1 }])}><Icon name="file" /> Add line</button>
        </section>
        {backend === "skarvik" ? (
          <section className="card card-p form">
            <div className="field"><label htmlFor="po">Customer PO (optional)</label><input className="inp" id="po" maxLength={20} value={po} onChange={(e) => setPo(e.target.value)} /></div>
            <div className="field"><label htmlFor="note">Delivery note (≤ 60, Latin-1)</label><input className="inp" id="note" maxLength={60} value={note} onChange={(e) => setNote(e.target.value)} /></div>
          </section>) : null}
        {pv && pv.key === key ? (
          <section className="card card-p stack" aria-labelledby="pv-h" style={{ scrollMarginBottom: 110 }} ref={(el) => el?.scrollIntoView({ block: "nearest" })}>
            <h2 className="h3" id="pv-h">Preview</h2>
            {pv.account ? <span>{pv.account.name_display} · Credit hold: {pv.account.credit_hold}</span> : <ErrorBox error={pv.accountError} what="the account" />}
            {pv.stockError ? <span className="muted">{pv.stockError}</span> : null}
            <ul style={{ margin: 0, paddingLeft: 18 }}>{pv.lines.map((l, i) => <li key={i}><span className="mono">{l.sku}</span> <span className={`bd ${l.tone === "ok" ? "t-ok" : l.tone === "bad" ? "t-bad" : "t-warn"}`}>{l.text}</span></li>)}</ul>
            <span className="muted small">What {backend === "halden" ? "HALDEN" : "SKARVIK"} shows right now — not a decision. The server validates again when you submit, and another person approves.</span>
          </section>) : null}
        <div className="sticky-act">
          <span className="muted" style={{ flex: 1, minWidth: 200, fontSize: 13 }}>Creates a draft only. You submit it; another person approves it.</span>
          <button className="btn" type="button" onClick={preview} disabled={checking || !customer.trim()}>{checking ? "Checking…" : "Preview checks"}</button>
          <Link className="btn" to="/operations">Cancel</Link>
          <button className="btn pri" type="submit" disabled={busy}>{busy ? "Creating…" : "Create draft"}</button>
        </div>
      </form>
    </>
  );
}
