import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, call } from "../api/client";
import { Icon } from "../components/Icon";
import { PageHead } from "../components/Page";
import { ScrollRegion } from "../components/ScrollRegion";
import { ErrorBox, Loading } from "../components/States";
import { useSession } from "../session";
import { BACKEND_FULL, backendName } from "../status";
import { Hold } from "./Customers";
import { OrderRow } from "./Operations";

export function CustomerCard() {
  const { backend = "", nativeId = "" } = useParams();
  const { can } = useSession();
  const q = useQuery({ queryKey: ["customer", backend, nativeId], queryFn: () => call(api.GET("/api/v1/customers/{backend_id}/{native_id}", { params: { path: { backend_id: backend, native_id: nativeId } } })) });
  const eyebrow = <><Link to="/customers">Customers</Link><span aria-hidden="true">/</span><span>{backendName(backend)}</span></>;
  if (q.isLoading) return <><PageHead eyebrow={eyebrow} title={<>Account <span className="mono">{nativeId}</span></>} /><Loading what="the customer" /></>;
  if (q.error) return <><PageHead eyebrow={eyebrow} title={<>Account <span className="mono">{nativeId}</span></>} /><ErrorBox error={q.error} what="the customer" /></>;
  const c = q.data!;
  const a = c.account;
  return (
    <>
      <PageHead eyebrow={eyebrow}
        title={<>{a.name_display} <span className="mono muted" style={{ fontSize: 18 }}>{backendName(backend)} {a.native_id}</span></>}
        actions={can("draft.create") ? <Link className="btn pri" to={`/operations/new?backend=${backend}&customer=${encodeURIComponent(a.native_id)}`}><Icon name="file" /> Prepare an order</Link> : undefined} />
      {Object.entries(c.backend_errors ?? {}).map(([b, m]) => <div key={b} className="banner sm" role="alert"><Icon name="alert" /><div><b>{backendName(b)}: part of this card could not be read.</b>{m}</div></div>)}
      <div className="det">
        <div className="col">
          <section className="card card-p" aria-label="Account">
            <div className="kv">
              <div><span>Credit hold</span><span><Hold a={a} prefix={false} /></span></div>
              <div><span>City / country</span><span>{a.city ?? "—"} {a.country_code ?? ""}</span></div>
              <div><span>Contact</span><span>{[a.contact_name, a.email, a.phone].filter(Boolean).join(" · ") || "—"}</span></div>
              {a.customer_since ? <div><span>Customer since</span><span>{a.customer_since}</span></div> : null}
              <div><span>System</span><span>{BACKEND_FULL[backend] ?? backend} (simulated)</span></div>
            </div>
            {Object.entries(a.untrusted_text ?? {}).map(([k, v]) => (
              <div className="untrusted" key={k} style={{ marginTop: 12 }}>
                <b>{k.replace(/_/g, " ")}</b> <span className="muted">— free text from the backend; untrusted, shown verbatim and never acted on</span>
                <q>{v}</q>
              </div>))}
          </section>
          <section className="card card-p" aria-labelledby="oo-h">
            <h2 className="h3" id="oo-h" style={{ marginBottom: 10 }}>Open operations</h2>
            {c.open_operations.length ? <div className="ol flat">{c.open_operations.map((o) => <OrderRow key={o.operation_id} o={o} flat />)}</div>
              : <p className="muted" style={{ margin: 0 }}>No open operations for this account.</p>}
          </section>
          <section className="card clip" aria-labelledby="bo-h">
            <div className="card-p" style={{ paddingBottom: 8 }}><h2 className="h3" id="bo-h">Recent backend orders</h2><span className="muted small">Read from {backendName(backend)}</span></div>
            {c.orders.length ? (
              <ScrollRegion label="Backend orders">
                <table className="dt">
                  <caption className="vh">Recent backend orders</caption>
                  <thead><tr><th scope="col">Order</th><th scope="col">Reference</th><th scope="col">Status</th><th scope="col">Date</th></tr></thead>
                  <tbody>{c.orders.map((o) => <tr key={String(o.order_ref)}><td className="mono">{String(o.order_ref)}</td><td className="mono">{String(o.reference ?? "—")}</td><td>{String(o.status)}</td><td className="tnum">{String(o.order_date ?? "—")}</td></tr>)}</tbody>
                </table>
              </ScrollRegion>) : <p className="muted card-p" style={{ margin: 0, paddingTop: 0 }}>No backend orders returned.</p>}
          </section>
        </div>
        <aside className="ctx" aria-label="Linked accounts">
          <section className="card card-p stack g8">
            <h2 className="h3">Linked accounts</h2><span className="muted" style={{ fontSize: 12.5 }}>Curated crosswalk</span>
            {c.linked_accounts.length ? c.linked_accounts.map((x) => (
              <div key={x.backend_id + x.native_id} className="stack g6" style={{ gap: 2 }}>
                <Link to={`/customers/${x.backend_id}/${x.native_id}`} className="mono">{backendName(x.backend_id)} {x.native_id}</Link>
                <span className="muted small">{x.name_display} · hold {x.credit_hold}</span>
              </div>)) : <span className="muted">None.</span>}
          </section>
        </aside>
      </div>
    </>
  );
}
