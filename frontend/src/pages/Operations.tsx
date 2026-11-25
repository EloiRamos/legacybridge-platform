import { useQuery } from "@tanstack/react-query";
import { type KeyboardEvent, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, call, type Schemas } from "../api/client";
import { Icon } from "../components/Icon";
import { MiniJourney } from "../components/Journey";
import { OrdersTabs, PageHead } from "../components/Page";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { StatusBadge, Sys } from "../components/StatusBadge";
import { journey, reasonFor } from "../evidence";
import { ago, eur, hm, iso, money, shortHash } from "../format";
import { countByStage, LIST_LIMIT, matches, type Op, type OrderFilter, type SortDir, type SortKey, sortOps } from "../orders";
import { useSession } from "../session";
import { BACKEND_FULL, backendName, EVIDENCE_WORDS, isUnresolved, type Stage, STAGE_OF, STAGES } from "../status";

const SORT_LABEL: Record<SortKey, string> = { ref: "reference", account: "account", status: "status", total: "estimated total", by: "prepared by", updated: "last update" };

const OUTCOMES: Schemas["Outcome"][] = ["CONFIRMED", "REJECTED", "NOT_APPLIED", "UNKNOWN", "ESCALATED", "CLOSED_ATTESTED"];

export function Operations() {
  const { can } = useSession();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "table" ? "table" : "grouped";
  const f: OrderFilter = {
    q: params.get("q") ?? "",
    stage: (STAGES.some((s) => s.key === params.get("stage")) ? params.get("stage") : "all") as Stage | "all",
    by: params.get("by") ?? "all", outcome: params.get("outcome") ?? "all",
    backend: (["halden", "skarvik"].includes(params.get("backend") ?? "") ? params.get("backend") : "all") as OrderFilter["backend"],
  };
  const sortKey = (Object.keys(SORT_LABEL).includes(params.get("sort") ?? "") ? params.get("sort") : "updated") as SortKey;
  const sortDir: SortDir = params.get("dir") === "asc" ? "asc" : "desc";
  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) { if (v == null || v === "" || v === "all") next.delete(k); else next.set(k, v); }
    setParams(next, { replace: true });
  };
  const q = useQuery({ queryKey: ["operations"], refetchInterval: 5000, queryFn: () => call(api.GET("/api/v1/operations")) });
  const all = useMemo(() => q.data?.items ?? [], [q.data]);
  const rows = useMemo(() => sortOps(all.filter((o) => matches(o, f)), sortKey, sortDir), [all, f.q, f.stage, f.backend, f.by, f.outcome, sortKey, sortDir]); // eslint-disable-line react-hooks/exhaustive-deps
  const counts = countByStage(all.filter((o) => matches(o, { ...f, stage: "all" })));
  const filtered = !!(f.q || f.stage !== "all" || f.backend !== "all" || f.by !== "all" || f.outcome !== "all");
  const clear = () => set({ q: null, stage: null, backend: null, by: null, outcome: null });
  const people = [...new Set([...all.map((o) => o.created_by), ...(f.by && f.by !== "all" ? [f.by] : [])])].sort();
  const attention = all.filter((o) => isUnresolved(o.state)).length;

  const actions = (
    <>
      <div className="seg" role="group" aria-label="View">
        <button type="button" aria-pressed={view === "grouped"} onClick={() => set({ view: null })}><Icon name="grid" /> Grouped</button>
        <button type="button" aria-pressed={view === "table"} onClick={() => set({ view: "table" })}><Icon name="list" /> Table</button>
      </div>
      {can("draft.create") ? <Link className="btn pri" to="/operations/new"><Icon name="file" /> New order</Link> : null}
    </>
  );
  return (
    <>
      <PageHead eyebrow="Orders" title="Every order, with its proof" actions={actions} tabs={<OrdersTabs attention={attention} />} />
      <div className="tool">
        <label className="srch">
          <Icon name="search" /><span className="vh">Search orders</span>
          <input value={f.q} onChange={(e) => set({ q: e.target.value })} placeholder="Reference, account, person, status…" maxLength={60} />
        </label>
        <div className="pills flt" role="group" aria-label="Filters">
          {[{ key: "all" as const, label: "All" }, ...STAGES].map((s) => (
            <button key={s.key} className="pill" type="button" aria-pressed={f.stage === s.key} onClick={() => set({ stage: s.key })}>
              {s.label} <span className="c">{counts[s.key]}</span>
            </button>
          ))}
          <span className="sep" aria-hidden="true" />
          {(["all", "halden", "skarvik"] as const).map((b) => (
            <button key={b} className="pill" type="button" aria-pressed={f.backend === b} onClick={() => set({ backend: b })}>{b === "all" ? "Both systems" : backendName(b)}</button>
          ))}
          <label className="flt-l">Prepared by <select className="inp" value={f.by} onChange={(e) => set({ by: e.target.value })}><option value="all">Anyone</option>{people.map((p) => <option key={p} value={p}>{p}</option>)}</select></label>
          <label className="flt-l">Attempt outcome <select className="inp" value={f.outcome} onChange={(e) => set({ outcome: e.target.value })}><option value="all">Any</option><option value="none">No attempt outcome</option>{OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}</select></label>
          {filtered ? <button className="btn ghost sm" type="button" onClick={clear}>Clear</button> : null}
        </div>
      </div>
      {all.length >= LIST_LIMIT ? (
        <div className="banner sm info" role="note"><Icon name="alert" /><div><b>Showing the {LIST_LIMIT} most recently updated orders.</b>The server returns at most {LIST_LIMIT}; search, filters and sorting apply to these only. Use the customer card or the reconciliation queue for older orders.</div></div>
      ) : null}
      {q.isLoading ? <Loading what="orders" /> : q.error ? <ErrorBox error={q.error} what="orders" /> : !rows.length ? (
        <div className="card">
          {all.length ? (
            <EmptyState title="No orders match" action={<button className="btn sm" type="button" onClick={clear}>Clear filters</button>}>Try another filter, or clear them.</EmptyState>
          ) : (
            <EmptyState title="No orders yet" icon="file" action={can("draft.create") ? <Link className="btn sm" to="/operations/new">New order</Link> : undefined}>Orders appear here once a draft is created.</EmptyState>
          )}
        </div>
      ) : view === "table" ? (
        <OrdersTable rows={rows} total={all.length} sortKey={sortKey} sortDir={sortDir}
          onSort={(k) => set({ sort: k, dir: k === sortKey ? (sortDir === "asc" ? "desc" : "asc") : k === "updated" || k === "total" ? "desc" : "asc" })} />
      ) : <Grouped rows={rows} />}
    </>
  );
}

function Grouped({ rows }: { rows: Op[] }) {
  return (
    <>
      {STAGES.map((s) => [s, rows.filter((o) => STAGE_OF[o.state] === s.key)] as const).filter(([, g]) => g.length).map(([s, g]) => (
        <section key={s.key} className="grp" aria-label={s.label}>
          <h2 className="h3">{s.label} · {g.length}</h2>
          <div className="ol">{g.map((o) => <OrderRow key={o.operation_id} o={o} />)}</div>
        </section>
      ))}
    </>
  );
}

export function OrderRow({ o, flat }: { o: Op; flat?: boolean }) {
  const reason = reasonFor(o);
  return (
    <Link className="orow" to={`/operations/${o.operation_id}`} style={flat ? { padding: "12px 0" } : undefined}>
      <span><span className="ref">{o.correlation_value}</span><br /><span className="sm"><Sys backend={o.backend_id} /> <span className="mono">{o.native_customer_id}</span></span></span>
      <span className="c2 hm"><b>Account <span className="mono">{o.native_customer_id}</span></b><span>v{o.current_version} · prepared by {o.created_by}{o.origin === "ASSISTANT" ? " · assistant-prepared" : ""}</span></span>
      <span className="c3"><StatusBadge state={o.state} evidence={o.last_evidence} backend={o.backend_id} short />{reason ? <span className="reason">{reason}</span> : null}</span>
      <span className="num tnum" style={{ fontWeight: 600 }}>{eur(o.estimated_total)}</span>
      <span className="sm hm"><time dateTime={iso(o.updated_at)} title={iso(o.updated_at)}>{ago(o.updated_at)}</time></span>
      <span className="hm" aria-hidden="true"><Icon name="chev" /></span>
    </Link>
  );
}

function OrdersTable({ rows, total, sortKey, sortDir, onSort }: { rows: Op[]; total: number; sortKey: SortKey; sortDir: SortDir; onSort: (k: SortKey) => void }) {
  const navigate = useNavigate();
  const [sel, setSel] = useState<string | null>(null);
  const body = useRef<HTMLTableSectionElement>(null);
  const selected = rows.find((r) => r.operation_id === sel) ?? rows[0]!;
  const th = (k: SortKey, label: string, cls = "") => (
    <th scope="col" className={cls} aria-sort={sortKey === k ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => onSort(k)}>{label}<span className="ar" aria-hidden="true">{sortKey === k ? (sortDir === "asc" ? "▲" : "▼") : "↕"}</span></button>
    </th>
  );
  const onKey = (e: KeyboardEvent<HTMLTableRowElement>, o: Op, i: number) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter") { e.preventDefault(); navigate(`/operations/${o.operation_id}`); }
    else if (e.key === " ") { e.preventDefault(); setSel(o.operation_id); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = body.current?.querySelectorAll<HTMLTableRowElement>("tr")[i + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) { next.focus(); setSel(rows[i + (e.key === "ArrowDown" ? 1 : -1)]!.operation_id); }
    }
  };
  return (
    <div className="ows pv">
      <div className="ot-wrap">
        <div className="tblw">
          <table className="ot" role="grid" aria-rowcount={rows.length + 1}>
            <caption className="vh">Orders, {rows.length} rows. Select a row to preview; Enter opens the order.</caption>
            <thead><tr>
              {th("ref", "Reference")}{th("account", "Account · system", "c-cust")}{th("status", "Status", "c-st")}
              <th scope="col" className="c-ev">Evidence</th>{th("total", "Est. EUR", "num")}{th("by", "Prepared by", "c-by")}{th("updated", "Updated", "num c-upd")}
            </tr></thead>
            <tbody ref={body}>
              {rows.map((o, i) => (
                <tr key={o.operation_id} tabIndex={0} aria-selected={selected.operation_id === o.operation_id}
                  onClick={() => setSel(o.operation_id)} onDoubleClick={() => navigate(`/operations/${o.operation_id}`)} onKeyDown={(e) => onKey(e, o, i)}>
                  <td>
                    <Link className="ref" to={`/operations/${o.operation_id}`} onClick={(e) => e.stopPropagation()}>{o.correlation_value}</Link>
                    <span className="mx">Account {o.native_customer_id} · {backendName(o.backend_id)}</span>
                    <span className="mx mx-st"><StatusBadge state={o.state} evidence={o.last_evidence} backend={o.backend_id} short /></span>
                  </td>
                  <td className="cust c-cust"><b>{o.native_customer_id}</b><span><Sys backend={o.backend_id} /> {o.origin === "ASSISTANT" ? "assistant-prepared" : ""}</span></td>
                  <td className="c-st"><StatusBadge state={o.state} evidence={o.last_evidence} backend={o.backend_id} short /></td>
                  <td className="ev c-ev">{o.last_evidence ?? "—"}</td>
                  <td className="num">{money(o.estimated_total)}</td>
                  <td className="c-by">{o.created_by}</td>
                  <td className="num muted c-upd"><time dateTime={iso(o.updated_at)} title={iso(o.updated_at)}>{hm(o.updated_at)}</time></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="tfoot">
          <span>{rows.length} of {total} orders · sorted by {SORT_LABEL[sortKey]} {sortDir === "asc" ? "ascending" : "descending"}{total >= LIST_LIMIT ? ` · latest ${LIST_LIMIT} only` : ""}</span>
          <span>Local time · Enter opens · Space selects · ↑↓ move</span>
        </div>
      </div>
      <aside className="card preview" aria-label={`Preview of ${selected.correlation_value}`}><Preview o={selected} /></aside>
    </div>
  );
}

function Preview({ o }: { o: Op }) {
  const reason = reasonFor(o);
  return (
    <>
      <div className="spread" style={{ alignItems: "center" }}><span className="ref">{o.correlation_value}</span><StatusBadge state={o.state} evidence={o.last_evidence} backend={o.backend_id} short /></div>
      <h2 className="d" style={{ fontSize: 19 }}>Account <span className="mono" style={{ fontSize: 17 }}>{o.native_customer_id}</span></h2>
      <MiniJourney stages={journey(o)} />
      {isUnresolved(o.state) ? <div className="banner sm"><Icon name="alert" /><div><b>{reason}</b>Do not re-enter this order.</div></div> : null}
      <div className="kv">
        <div><span>System</span><span>{BACKEND_FULL[o.backend_id] ?? o.backend_id}</span></div>
        <div><span>Version</span><span>v{o.current_version} · attempt {o.attempt_count}</span></div>
        <div><span>Evidence</span><span>{o.last_evidence ? <span title={EVIDENCE_WORDS[o.last_evidence]} className="mono" style={{ fontSize: 12 }}>{o.last_evidence}</span> : "—"}</span></div>
        <div><span>Backend order</span><span className="mono">{o.anchor_order_ref ?? "—"}</span></div>
        <div><span>Est. total</span><span className="tnum">{o.estimated_total ? `EUR ${money(o.estimated_total)}` : "unknown"}</span></div>
        <div><span>Prepared by</span><span>{o.created_by}</span></div>
        <div><span>Operation id</span><span className="mono" style={{ fontSize: 12 }} title={o.operation_id}>{shortHash(o.operation_id, 8)}</span></div>
      </div>
      <div><b style={{ fontSize: 12.5 }}>Latest update</b><div className="muted" style={{ fontSize: 13 }}><time dateTime={iso(o.updated_at)} title={iso(o.updated_at)}>{ago(o.updated_at)}</time></div></div>
      <Link className="btn pri" to={`/operations/${o.operation_id}`}>Open order <Icon name="arrow" /></Link>
    </>
  );
}
