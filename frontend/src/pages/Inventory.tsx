import { useMutation, useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";

import { api, call, type Schemas } from "../api/client";
import { Icon } from "../components/Icon";
import { OrdersTabs, PageHead } from "../components/Page";
import { ScrollRegion } from "../components/ScrollRegion";
import { Sheet } from "../components/Sheet";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { Sys } from "../components/StatusBadge";
import { dateTime, iso } from "../format";
import { useSession } from "../session";
import { backendName } from "../status";

type Avail = Schemas["AvailabilityOut"];
const TONE: Record<string, string> = { IN_STOCK: "t-ok", OUT_OF_STOCK: "t-neutral", UNDETERMINED: "t-warn", STALE: "t-warn-outline" };
const Chip = ({ a }: { a: Avail }) => <span className={`bd ${TONE[a.status] ?? "t-neutral"}`}>{a.status.replace("_", " ")}</span>;
const age = (s: number | null | undefined) => (s == null ? "snapshot, age unknown" : s < 60 ? `snapshot ${s} s old` : s < 3600 ? `snapshot ${Math.floor(s / 60)} min old` : `snapshot ${Math.floor(s / 3600)} h ${Math.floor(s / 60) % 60} min old`);

export function Inventory() {
  const q = useQuery({ queryKey: ["inventory"], queryFn: () => call(api.GET("/api/v1/inventory")) });
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const { can } = useSession();
  const [note, setNote] = useState<Avail | null>(null);
  const add = useMutation({
    mutationFn: ({ a, body }: { a: Avail; body: string }) => call(api.POST("/api/v1/inventory/annotations", { body: { canonical_sku: a.canonical_sku, backend_id: a.backend_id as "halden" | "skarvik", native_item: a.native_item, body } })).then(() => a),
    onSettled: () => setNote(null),
  });
  const head = <PageHead eyebrow="Orders" title="Inventory" tabs={<OrdersTabs />}
    lede="Each backend is shown separately; stock is never summed across legal entities (rule I-3). Snapshot figures show their age; uncertain figures are marked, not guessed." />;
  if (q.isLoading) return <>{head}<Loading what="inventory" /></>;
  if (q.error) return <>{head}<ErrorBox error={q.error} what="inventory" /></>;
  const d = q.data!;
  return (
    <>
      {head}
      {add.error ? <ErrorBox error={add.error} /> : null}
      {add.data && !add.error && !add.isPending ? <div className="banner sm ok" role="status"><span>Note recorded for {add.data.native_item} ({backendName(add.data.backend_id)}). Notes are kept on the server; this console does not list them.</span></div> : null}
      {Object.entries(d.backend_errors ?? {}).map(([b, m]) => <div key={b} className="errbox" role="alert"><b>{backendName(b)}: no current figures.</b>{m} Rows from {backendName(b)} are not shown; the other backend remains available.</div>)}
      {!d.items.length ? <div className="card"><EmptyState title="No inventory rows" icon="box">No items were returned by either backend.</EmptyState></div> : (
        <section className="card clip">
          <ScrollRegion label="Availability">
            <table className="dt inv" role="table">
              <caption className="vh">Availability per canonical item and backend</caption>
              <thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">Group SKU</th><th role="columnheader" scope="col">Name</th><th role="columnheader" scope="col">Backend</th><th role="columnheader" scope="col">Native item</th><th role="columnheader" scope="col">Status</th><th role="columnheader" scope="col" className="num">Available (base)</th><th role="columnheader" scope="col">Freshness</th><th role="columnheader" scope="col">Explanation</th></tr></thead>
              <tbody role="rowgroup">
                {d.items.map((item) => {
                  const span = item.per_backend.length + item.per_backend.filter((a) => open[item.canonical_sku + a.backend_id + a.native_item]).length;
                  return item.per_backend.map((a, i) => {
                    const k = item.canonical_sku + a.backend_id + a.native_item;
                    return (
                      <Fragment key={k}>
                        <tr role="row">
                          {i === 0 ? <td role="cell" rowSpan={span} className="grp mono">{item.canonical_sku}</td> : null}
                          {i === 0 ? <td role="cell" rowSpan={span} className="grp">{item.name}</td> : null}
                          <td role="cell" data-label="Backend"><Sys backend={a.backend_id} /></td>
                          <td role="cell" data-label="Native item" className="mono nowrap" style={{ fontSize: 12.5 }}>{a.native_item}</td>
                          <td role="cell" data-label="Status"><Chip a={a} /></td>
                          <td role="cell" data-label="Available (base)" className="num">{a.available_base == null ? "—" : a.available_base.toLocaleString("en-GB")}</td>
                          <td role="cell" data-label="Freshness" className="nowrap"><time dateTime={iso(a.as_of)} title={`as of ${dateTime(a.as_of)}`}>{a.freshness === "LIVE" ? "live read" : age(a.snapshot_age_seconds)}</time></td>
                          <td role="cell" className="why">{a.explanation}{" "}
                            <button type="button" className="tech" aria-expanded={!!open[k]} onClick={() => setOpen({ ...open, [k]: !open[k] })}><Icon name="chev" className="chev" /> Raw rows</button>
                            {can("inventory.annotate") ? <button type="button" className="tech" aria-label={`Annotate ${a.native_item}`} onClick={() => setNote(a)}><Icon name="file" /> Annotate</button> : null}
                          </td>
                        </tr>
                        {open[k] ? <tr role="row" className="inv-x"><td role="cell" colSpan={6}><pre tabIndex={0} className="js" style={{ margin: 0 }}>{JSON.stringify({ warehouses: a.warehouses, issues: a.issues, legacy_available_flag: a.legacy_available_flag }, null, 2)}</pre></td></tr> : null}
                      </Fragment>);
                  });
                })}
              </tbody>
            </table>
          </ScrollRegion>
        </section>)}
      {d.unmapped.length ? (
        <section className="card card-p" aria-labelledby="um-h">
          <h2 id="um-h">Items without a crosswalk mapping</h2>
          <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>{d.unmapped.map((a) => <li key={a.backend_id + a.native_item} style={{ marginBottom: 6 }}>{backendName(a.backend_id)} <span className="mono">{a.native_item}</span>: <Chip a={a} /> {a.explanation}</li>)}</ul>
        </section>) : null}
      {note ? <NoteSheet a={note} busy={add.isPending} onClose={() => setNote(null)} onSubmit={(body) => add.mutate({ a: note, body })} /> : null}
    </>
  );
}

/** A comment on a discrepancy (inventory.annotate). It changes no stock figure in either system. */
function NoteSheet({ a, busy, onClose, onSubmit }: { a: Avail; busy: boolean; onClose: () => void; onSubmit: (body: string) => void }) {
  const [v, setV] = useState("");
  const ok = v.trim() !== "";
  return (
    <Sheet title={`Annotate ${a.native_item}`} eyebrow={backendName(a.backend_id)} onClose={onClose} busy={busy} locked={busy} onSubmit={() => { if (ok && !busy) onSubmit(v.trim()); }}
      footer={<><button className="btn" type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="btn pri" type="submit" disabled={!ok || busy}>Record note</button></>}>
      <p className="muted small" style={{ margin: 0 }}>The note is stored on the server with your name and time; this console does not show it again. It changes nothing in {backendName(a.backend_id)}.</p>
      <div className="field"><label htmlFor="an-b">Note</label><textarea className="inp" id="an-b" rows={4} maxLength={500} value={v} onChange={(e) => setV(e.target.value)} data-autofocus="" /></div>
    </Sheet>
  );
}
