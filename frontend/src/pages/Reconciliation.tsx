import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api, call } from "../api/client";
import { Icon } from "../components/Icon";
import { KnownPanel } from "../components/KnownPanel";
import { OrdersTabs, PageHead } from "../components/Page";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { knowns, type Summary } from "../evidence";
import { ago, eur, iso } from "../format";
import { backendName, isUnresolved } from "../status";

/** Queue from GET /api/v1/reconciliation (UNKNOWN, RECONCILING, ESCALATED, overdue AWAITING_ACK). Actions are only
 * offered on the order page, from the server's permitted_actions — never from this list. */
export function Reconciliation() {
  const q = useQuery({ queryKey: ["reconciliation"], queryFn: () => call(api.GET("/api/v1/reconciliation")), refetchInterval: 3000 });
  const items = q.data?.items ?? [];
  return (
    <>
      <PageHead eyebrow="Orders" title="Reconciliation" tabs={<OrdersTabs attention={items.filter((o) => isUnresolved(o.state)).length} />} />
      <div className="banner" role="note"><Icon name="alert" /><div><b>These orders may or may not have been booked. Do not re-enter them.</b>Only read-only checks, an authorized SKARVIK withdrawal or redelivery, escalation and resolution are possible — open an order to see the actions available to you.</div></div>
      {q.isLoading ? <Loading what="the reconciliation queue" /> : q.error ? <ErrorBox error={q.error} what="the reconciliation queue" /> : !items.length ? (
        <div className="card"><EmptyState title="No unresolved orders" icon="check">Every hand-off has a recorded outcome.</EmptyState></div>
      ) : (
        <section className="col" aria-label="Reconciliation queue">
          {items.map((o) => <QueueItem key={o.operation_id} o={o} />)}
        </section>
      )}
    </>
  );
}

function QueueItem({ o }: { o: Summary }) {
  const unresolved = isUnresolved(o.state);
  const d = useQuery({ queryKey: ["operation", o.operation_id], enabled: unresolved, refetchInterval: 10000,
    queryFn: () => call(api.GET("/api/v1/operations/{operation_id}", { params: { path: { operation_id: o.operation_id } } })) });
  const K = d.data ? knowns(d.data) : null;
  return (
    <article className="card card-p stack" aria-label={o.correlation_value}>
      <div className="rowx">
        <Link className="ref" to={`/operations/${o.operation_id}`} style={{ fontSize: 14 }}>{o.correlation_value}</Link>
        <StatusBadge state={o.state} evidence={o.last_evidence} backend={o.backend_id} />
        <span className="muted">{backendName(o.backend_id)} · account {o.native_customer_id} · {eur(o.estimated_total)} · <time dateTime={iso(o.updated_at)} title={iso(o.updated_at)}>{ago(o.updated_at)}</time></span>
        <Link className="btn sm" to={`/operations/${o.operation_id}`} style={{ marginLeft: "auto" }}>Open <Icon name="arrow" /></Link>
      </div>
      {K ? <KnownPanel k={K} latest={2} tight /> : d.isLoading ? <div className="skel" style={{ width: "60%" }} /> : d.error ? <ErrorBox error={d.error} what="the evidence for this order" /> : null}
    </article>
  );
}
