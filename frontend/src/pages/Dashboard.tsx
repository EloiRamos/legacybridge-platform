import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { api, call } from "../api/client";
import { Icon, type IconName } from "../components/Icon";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { hm, iso } from "../format";
import { useSession } from "../session";
import { backendName, EVIDENCE_WORDS } from "../status";

type Rec = Record<string, unknown>;
const IN_FLIGHT = ["APPROVED", "SUBMITTING", "AWAITING_ACK", "RECONCILING"];

/** Overview: counts from GET /api/v1/dashboard (and the viewer's approval queue). No figure is computed from data
 * the API does not return. */
export function Dashboard() {
  const { can } = useSession();
  const approver = can("approval.grant.standard") || can("approval.grant.any");
  const q = useQuery({ queryKey: ["dashboard"], queryFn: () => call(api.GET("/api/v1/dashboard")), refetchInterval: 15000 });
  const queue = useQuery({ queryKey: ["approvals"], enabled: approver, queryFn: () => call(api.GET("/api/v1/approvals")), refetchInterval: 15000 });
  const d = q.data;
  const mine = queue.data?.items.length ?? 0;
  const nAttn = d ? (approver ? (mine ? 1 : 0) : 0) + (d.unknown ? 1 : 0) + (d.escalated ? 1 : 0) : 0;
  const title = !d ? "Overview" : nAttn === 0 ? "Nothing needs a person right now" : `${["", "One queue needs", "Two queues need", "Three queues need"][nAttn]} a person`;
  const now = new Date();
  const c = d?.counts_by_state ?? {};
  const inFlight = IN_FLIGHT.reduce((n, s) => n + (c[s] ?? 0), 0);

  return (
    <>
      <div>
        <div className="eyebrow">Overview · {now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" })}{q.dataUpdatedAt ? ` · updated ${hm(new Date(q.dataUpdatedAt).toISOString())}` : ""}</div>
        <h1 className="d" tabIndex={-1} style={{ marginTop: 6 }}>{title}</h1>
        <p className="lede">LegacyBridge orchestrates Brightwater orders across two legacy systems. Nothing reaches HALDEN or SKARVIK without an approved, hash-bound version — and nothing uncertain is retried blindly.</p>
        {can("customer.read") || can("inventory.read") ? (
          <p className="muted small" style={{ margin: "6px 0 0" }}>Look up: {can("customer.read") ? <Link to="/customers">customers</Link> : null}{can("customer.read") && can("inventory.read") ? " · " : null}{can("inventory.read") ? <Link to="/inventory">inventory</Link> : null}</p>) : null}
      </div>
      {q.isLoading ? <Loading what="the overview" /> : q.error ? <ErrorBox error={q.error} what="the overview" /> : d ? (
        <>
          <section className="needs" aria-label="Needs a person">
            {approver ? (
              <Need to="/approvals" n={queue.isLoading ? "…" : queue.error ? "—" : mine} title="Approvals waiting for you" go="Review"
                sub={<>{d.pending_approvals - mine > 0 ? `${d.pending_approvals - mine} more await${d.pending_approvals - mine > 1 ? "" : "s"} someone else. ` : ""}You never approve an order you prepared.</>} />
            ) : (
              <Need to="/operations?stage=flight" n={c.AWAITING_ACK ?? 0} title="Awaiting SKARVIK acknowledgement" go="View"
                sub={<>Delivered to the inbox; {d.overdue_acks ? `${d.overdue_acks} past the acknowledgement deadline.` : "none past the acknowledgement deadline."}</>} />
            )}
            <Need to="/reconciliation" n={d.unknown} warn={d.unknown > 0} title="Outcome unknown" go="Inspect"
              sub="The backend may or may not have booked it. Read-only re-checks continue — never re-enter." />
            <Need to="/reconciliation" n={d.escalated} warn={d.escalated > 0} title="Needs a decision" go="Decide"
              sub="Re-checks ended or a person escalated it. It is resolved by a verified read or an attestation." />
          </section>
          <Bridge backends={d.backends} issues={d.data_quality_issues} inFlight={inFlight} uncertain={d.unknown + d.escalated} confirmed={c.CONFIRMED ?? 0} overdue={d.overdue_acks} />
          <section className="card card-p" aria-labelledby="fd-h">
            <div className="spread">
              <h2 className="d" id="fd-h">Recent outcomes</h2>
              {can("audit.read") ? <Link className="btn ghost sm" to="/audit">All evidence <Icon name="arrow" /></Link> : <Link className="btn ghost sm" to="/operations?stage=closed">Closed orders <Icon name="arrow" /></Link>}
            </div>
            {!d.recent_outcomes.length ? <EmptyState title="No outcomes yet" icon="clock">Confirmed, rejected, withdrawn and proven-not-applied orders appear here.</EmptyState> : (
              <ul className="feed">
                {d.recent_outcomes.map((o) => {
                  const [tone, icon]: [string, IconName] = o.state === "CONFIRMED" ? ["ok", "check"] : o.state === "REJECTED" ? ["bad", "x"] : o.state === "CLOSED_ATTESTED" ? ["att", "alert"] : ["", "clock"];
                  return (
                    <li key={o.operation_id}>
                      <span className={`ic36 ${tone}`}><Icon name={icon} /></span>
                      <span>
                        <Link className="ref" to={`/operations/${o.operation_id}`}>{o.correlation_value}</Link>{" "}
                        <StatusBadge state={o.state} evidence={o.last_evidence} backend={o.backend_id} short />
                        <span className="muted small" style={{ display: "block", marginTop: 2 }}>{backendName(o.backend_id)} · account {o.native_customer_id}{o.last_evidence ? ` · ${EVIDENCE_WORDS[o.last_evidence] ?? o.last_evidence}` : ""}</span>
                      </span>
                      <time className="when" dateTime={iso(o.updated_at)} title={iso(o.updated_at)}>{hm(o.updated_at)}</time>
                    </li>);
                })}
              </ul>)}
          </section>
        </>
      ) : null}
    </>
  );
}

function Need({ to, n, title, sub, go, warn }: { to: string; n: number | string; title: string; sub: ReactNode; go: string; warn?: boolean }) {
  return (
    <Link className={`need${warn ? " warn" : ""}`} to={to}>
      <span className="n">{n}</span>
      <span><span className="t">{title}</span><br /><span className="s">{sub}</span></span>
      <span className="go">{go} <Icon name="arrow" /></span>
    </Link>
  );
}

function Bridge({ backends, issues, inFlight, uncertain, confirmed, overdue }: { backends: Rec[]; issues: Record<string, number>; inFlight: number; uncertain: number; confirmed: number; overdue: number }) {
  const node = (id: string, name: string, unit: string) => {
    const b = backends.find((x) => x.backend_id === id);
    const ok = b ? !!b.reachable : false;
    const fresh = !b ? "No health report" : b.snapshot_age_seconds == null ? (ok ? "Reachable · live reads" : "Not answering") : `${ok ? "Reachable" : "Not answering"} · snapshot ${String(b.snapshot_age_seconds)} s old`;
    return (
      <div className="node">
        <span className="nm">{name}</span>
        <span className="h"><span className={`dot${ok ? "" : " bad"}`} aria-hidden="true" />{fresh}</span>
        <span className="muted" style={{ fontSize: 12.5 }}>{unit} · {issues[id] == null || issues[id] < 0 ? "data-quality count unavailable" : `${issues[id]} data-quality issue${issues[id] === 1 ? "" : "s"}`}{b?.detail ? ` · ${String(b.detail)}` : ""}</span>
      </div>
    );
  };
  return (
    <section className="card" aria-labelledby="br-h">
      <div className="card-p spread" style={{ paddingBottom: 0 }}><h2 className="d" id="br-h">The bridge</h2><span className="muted small">Simulated systems · health and counts from the API</span></div>
      <div className="bridge">
        {node("halden", "HALDEN ERP 3.1", "BU Central Europe, UK & Ireland")}
        <div className="link" aria-hidden="true"><span>HTTP form posts · single hand-off</span><span className="ln rev" /><span>read-back</span></div>
        <div className="node core">
          <span className="nm">LegacyBridge</span>
          <span className="muted" style={{ fontSize: 12.5 }}>Approve the exact version → hand off once → prove the outcome</span>
          <span className="h" style={{ marginTop: 4 }}>{inFlight} in flight · {uncertain} uncertain · {confirmed} confirmed{overdue ? ` · ${overdue} ack overdue` : ""}</span>
        </div>
        <div className="link" aria-hidden="true"><span>file inbox · ack</span><span className="ln" /><span>extract read-back</span></div>
        {node("skarvik", "SKARVIK OES 4", "BU Nordic")}
      </div>
    </section>
  );
}
