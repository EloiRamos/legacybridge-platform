import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, call } from "../api/client";
import { Icon } from "../components/Icon";
import { checksOf, currentVersion, FourEyes, useOperationActions } from "../components/OperationActions";
import { PageHead } from "../components/Page";
import { ScrollRegion } from "../components/ScrollRegion";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { StatusBadge, Sys } from "../components/StatusBadge";
import { ago, eur, iso, money } from "../format";
import { useSession } from "../session";
import { BACKEND_FULL, backendName } from "../status";

/** The approval queue is computed by the server (GET /api/v1/approvals: READY orders this viewer may decide, own
 * orders excluded). Approve/decline buttons appear only when the operation's permitted_actions contain them. */
export function Approvals() {
  const { session } = useSession();
  const q = useQuery({ queryKey: ["approvals"], queryFn: () => call(api.GET("/api/v1/approvals")), refetchInterval: 5000 });
  const ready = useQuery({ queryKey: ["operations", "READY"], refetchInterval: 15000,
    queryFn: () => call(api.GET("/api/v1/operations", { params: { query: { state: ["READY"] } } })) });
  const [pick, setPick] = useState<string | null>(null);
  const items = [...(q.data?.items ?? [])].sort((a, b) => a.updated_at.localeCompare(b.updated_at));
  const mineIds = new Set(items.map((x) => x.operation_id));
  const others = (ready.data?.items ?? []).filter((o) => !mineIds.has(o.operation_id));
  const sel = items.find((x) => x.operation_id === pick) ?? items[0];
  const head = <PageHead eyebrow="Approvals" title="Review exactly what will be sent"
    lede="Each decision binds one version and its content hash. You never see your own orders here, and overrides need an override approver." />;
  const othersCard = others.length ? (
    <section className="card card-p" aria-labelledby="oth-h">
      <h2 className="h3" id="oth-h">Waiting for someone else · {others.length}</h2>
      {others.map((o) => (
        <div className="acc-row" key={o.operation_id}>
          <span><Link className="ref" to={`/operations/${o.operation_id}`}>{o.correlation_value}</Link> <span className="muted">· {backendName(o.backend_id)} {o.native_customer_id} · {eur(o.estimated_total)}</span></span>
          <span className="muted small">{o.created_by === session?.username ? "You prepared it — another person must approve (four-eyes)." : "Not in your queue — the server’s approval policy decides who may approve it."}</span>
        </div>
      ))}
    </section>) : null;
  if (q.isLoading) return <>{head}<Loading what="the approval queue" /></>;
  if (q.error) return <>{head}<ErrorBox error={q.error} what="the approval queue" /></>;
  if (!sel) return <>{head}<div className="card"><EmptyState title="You’re all caught up" icon="check">Nothing awaits your approval.</EmptyState></div>{othersCard}</>;
  return (
    <>
      {head}
      <div className="rv">
        <nav aria-label="Approval queue" className="stack g6" style={{ gap: 4 }}>
          <div className="grp"><h2 className="h3">{items.length} waiting for you</h2></div>
          {items.map((x) => (
            <button key={x.operation_id} className="rq" type="button" aria-current={x.operation_id === sel.operation_id} onClick={() => setPick(x.operation_id)}>
              <span className="l1"><span className="ref">{x.correlation_value}</span><b className="tnum">{eur(x.estimated_total)}</b></span>
              <span className="l2">{backendName(x.backend_id)} · account {x.native_customer_id}</span>
              <span className="l2">{x.created_by} · <time dateTime={iso(x.updated_at)}>{ago(x.updated_at)}</time></span>
            </button>
          ))}
        </nav>
        <Review id={sel.operation_id} />
      </div>
      {othersCard}
    </>
  );
}

function Review({ id }: { id: string }) {
  const { session } = useSession();
  const q = useQuery({ queryKey: ["operation", id], refetchInterval: 5000,
    queryFn: () => call(api.GET("/api/v1/operations/{operation_id}", { params: { path: { operation_id: id } } })) });
  const acts = useOperationActions(q.data);
  if (q.isLoading) return <Loading what="the order under review" />;
  if (q.error) return <ErrorBox error={q.error} what="the order under review" />;
  const d = q.data!;
  const op = d.operation;
  const v = currentVersion(d);
  const lines = v.content?.lines ?? [];
  const checks = checksOf(d);
  const overrides = checks.filter((c) => c.result === "OVERRIDE_REQUIRED");
  // Order: Decline… then the accent Approve… — each only when the server's permitted_actions contain it
  const decide = (["decline", "approve"] as const).filter((a) => acts.permitted.includes(a)).map((a) => (
    <button key={a} type="button" className={`btn${a === "approve" ? " acc" : ""}`} onClick={() => acts.start(a)} disabled={acts.run.isPending}>
      {a === "approve" ? <><Icon name="shield" /> Approve v{op.current_version}…</> : "Decline…"}
    </button>));
  return (
    <article className="card card-p stack g18" aria-label={`Review ${op.correlation_value}`}>
      <div className="rowx">
        <h2 className="d">Account <span className="mono" style={{ fontSize: 19 }}>{op.native_customer_id}</span></h2>
        <StatusBadge state={op.state} evidence={op.last_evidence} backend={op.backend_id} />
        <Link className="btn ghost sm" to={`/operations/${op.operation_id}`} style={{ marginLeft: "auto" }}>Open order <Icon name="ext" /></Link>
      </div>
      {acts.actionError ? <ErrorBox error={acts.actionError} /> : null}
      <FourEyes preparer={op.created_by} approver={session?.username ?? "you"} />
      <div className="pin">
        <div className="spread"><b>You’re approving exactly this</b><span className="muted">v{op.current_version} · {BACKEND_FULL[op.backend_id] ?? op.backend_id} · <span className="mono">{op.native_customer_id}</span> · <Sys backend={op.backend_id} /></span></div>
        <span className="hx">Content hash {String(v.content_hash ?? "—")}</span>
      </div>
      <ScrollRegion label="Lines under review">
        <table className="lt">
          <caption className="vh">Lines in v{op.current_version}</caption>
          <thead><tr><th scope="col">Group SKU</th><th scope="col">Native</th><th scope="col" className="num">Qty</th><th scope="col" className="num">Estimate</th></tr></thead>
          <tbody>
            {lines.map((l) => <tr key={l.line_no}><td className="mono" style={{ fontSize: 12.5 }}>{l.canonical_sku ?? "—"}</td><td className="mono" style={{ fontSize: 12.5 }}>{l.native_item}</td><td className="num">{l.quantity_native} {l.native_uom}</td><td className="num">{eur(l.line_estimate)}</td></tr>)}
            <tr><td colSpan={3} style={{ textAlign: "right", fontWeight: 600 }}>Total</td><td className="num" style={{ fontWeight: 700 }}>{op.estimated_total ? `€${money(op.estimated_total)}` : "unknown"}</td></tr>
          </tbody>
        </table>
      </ScrollRegion>
      <div className="pills" aria-label="Validation">
        {checks.map((c, i) => <span key={i} className={`bd ${c.result === "PASS" ? "t-ok" : c.result === "BLOCKING" ? "t-bad" : c.result === "WARNING" ? "t-info" : "t-warn"}`} title={c.detail ?? ""}>{c.check}{c.line_no ? ` · ${c.line_no}` : ""} · {c.result}</span>)}
      </div>
      {overrides.length ? <div className="banner"><Icon name="alert" /><div><b>An override reason is required.</b>{overrides.map((c) => c.detail || c.check).join("; ")}</div></div> : null}
      <div className="sticky-act">
        <span className="muted" style={{ fontSize: 13, flex: 1, minWidth: 200 }}>{decide.length ? "One hand-off follows approval. It is never repeated automatically." : "The server offers no decision to you on this version."}</span>
        {decide}
      </div>
      {acts.sheet}
    </article>
  );
}
