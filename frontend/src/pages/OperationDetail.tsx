import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, call, type Schemas } from "../api/client";
import { AssistantAnswer, type Turn } from "../components/AssistantAnswer";
import { useAssistant } from "../components/AssistantPanel";
import { Icon } from "../components/Icon";
import { JourneyBar } from "../components/Journey";
import { KnownPanel } from "../components/KnownPanel";
import { ACTION_LABEL, type Action, checksOf, currentVersion, useOperationActions } from "../components/OperationActions";
import { ScrollRegion } from "../components/ScrollRegion";
import { ErrorBox, Loading } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";
import { type Detail, facets, journey, knowns, type TimelineEvent, timelineOf } from "../evidence";
import { dateTime, eur, evLabel, hms, iso, money, shortHash } from "../format";
import { useSession } from "../session";
import { BACKEND_FULL, backendName, EVIDENCE_WORDS, isUnresolved, STATUS } from "../status";
import { unresolvedGuidance } from "../unresolved";
import { diffVersions } from "../versions";

type Rec = Record<string, unknown>;

export function OperationDetail() {
  const { id = "" } = useParams();
  const { can } = useSession();
  const q = useQuery({
    queryKey: ["operation", id], refetchInterval: 3000,
    queryFn: () => call(api.GET("/api/v1/operations/{operation_id}", { params: { path: { operation_id: id } } })),
  });
  const acts = useOperationActions(q.data);
  if (q.isLoading) return <><DetailHead /><Loading what="the order" /></>;
  if (q.error) return <><DetailHead /><ErrorBox error={q.error} what="this order" /></>;
  const d: Detail = q.data!;
  const op = d.operation;
  const B = backendName(op.backend_id);
  const K = knowns(d);
  const permitted = d.permitted_actions as Action[];

  return (
    <>
      <DetailHead op={op} banner={<StateBanner d={d} />}>
        <section aria-label="Actions" className="acts" aria-busy={acts.run.isPending}>
          {permitted.length ? acts.buttons() : <span className="muted small">No action is available to you for this order.</span>}
        </section>
      </DetailHead>
      {acts.run.isPending ? <p role="status" className="muted small" style={{ margin: 0 }}>Working — waiting for the server (live backend reads can take a few seconds)…</p> : null}
      {acts.actionError ? <ErrorBox error={acts.actionError} /> : null}
      {acts.actionNotice ? <div className="banner bad" role="alert"><Icon name="alert" /><div><b>Submit was not accepted.</b>{acts.actionNotice}</div></div> : null}
      <div className="det">
        <div className="col">
          <section className="card card-p stack g16" aria-labelledby="j-h">
            <h2 className="h3" id="j-h">Order journey</h2>
            <JourneyBar stages={journey(op, d)} />
          </section>
          {K ? <KnownPanel k={K} /> : null}
          <div className="two">
            <ApprovedVersion d={d} />
            <section className="card card-p stack" aria-labelledby="auth-h">
              <h2 className="h3" id="auth-h">Your authority here</h2>
              {permitted.length ? (
                <ul className="allowed">
                  {permitted.map((a) => <li key={a}><Icon name="check" />{ACTION_LABEL[a].replace("…", "")}</li>)}
                  {isUnresolved(op.state) ? <li className="no"><Icon name="x" />Re-enter or re-send the order — nobody</li> : null}
                </ul>
              ) : <p className="muted" style={{ margin: 0 }}>No action is available to you for this order{op.state === "READY" ? " — another person approves it (four-eyes), or your role cannot approve this value." : "."}</p>}
              <span className="muted" style={{ fontSize: 12.5 }}>Actions appear at the top of the page. The server re-checks each one against your role and this exact version.</span>
            </section>
          </div>
          <section aria-labelledby="ev-h">
            <h2 className="d s20" id="ev-h" style={{ marginBottom: 12 }}>Integration evidence</h2>
            <div className="evs">
              {facets(d).map((f) => (
                <div key={f.key} className={`evc ${f.tone}`}>
                  <span className="h"><Icon name={f.tone === "ok" ? "check" : f.tone === "warn" ? "help" : f.tone === "bad" ? "x" : "clock"} />{f.t}</span>
                  <span className="st">{f.s}</span>
                  <p>{f.d}</p>
                </div>
              ))}
            </div>
          </section>
          <Lines d={d} />
          <Versions d={d} />
          <Records d={d} />
          <Timeline events={timelineOf(d)} />
        </div>
        <aside className="ctx" aria-label="Context">
          {can("assistant.use") ? <AskAboutOrder correlation={op.correlation_value} unresolved={isUnresolved(op.state)} /> : null}
          {can("audit.read") ? (
            <section className="card card-p stack g6" aria-labelledby="aud-h">
              <h2 className="h3" id="aud-h">Audit</h2>
              <span className="muted small">{d.timeline.length} hash-chained events{d.timeline.length ? <> · head <span className="mono">#{String((d.timeline[d.timeline.length - 1] as Rec).seq)}</span></> : null}</span>
              <Link className="btn sm" to={`/audit?operation=${op.operation_id}`}>View evidence chain</Link>
            </section>
          ) : null}
          <section className="card card-p stack g6" aria-labelledby="cu-h">
            <h2 className="h3" id="cu-h">Customer</h2>
            {can("customer.read") ? <Link to={`/customers/${op.backend_id}/${op.native_customer_id}`}>Account {op.native_customer_id}</Link> : <span>Account {op.native_customer_id}</span>}
            <span className="muted small">{BACKEND_FULL[op.backend_id] ?? B} (simulated)</span>
          </section>
        </aside>
      </div>
      {acts.sheet}
    </>
  );
}

function DetailHead({ op, banner, children }: { op?: Schemas["OperationSummaryOut"]; banner?: ReactNode; children?: ReactNode }) {
  return (
    <div>
      <div className="eyebrow">
        <Link to="/operations"><Icon name="chev" className="flip" /> Orders</Link>
        {op ? <><span aria-hidden="true">/</span><span>{backendName(op.backend_id)}</span><span aria-hidden="true">/</span><span>Account <span className="mono">{op.native_customer_id}</span></span></> : null}
      </div>
      <div className="ph" style={{ marginTop: 6 }}>
        <h1 className="d oph" tabIndex={-1}>
          <span className="vh">Order </span>
          {op ? <><span className="mono cv">{op.correlation_value}</span><StatusBadge state={op.state} evidence={op.last_evidence} backend={op.backend_id} /></> : "Order"}
        </h1>
        {banner ? <div className="sb">{banner}</div> : null}
        {children}
      </div>
    </div>
  );
}

function StateBanner({ d }: { d: Detail }) {
  const op = d.operation;
  const s = d.schedule as Rec;
  const B = backendName(op.backend_id);
  const g = unresolvedGuidance(op.state, op.backend_id, d.permitted_actions as Action[]);
  const seen = knowns(d)?.observed ?? [];
  const latest = seen[seen.length - 1];
  switch (op.state) {
    case "UNKNOWN":
      return <div className="banner" role="note"><Icon name="help" /><div><b>We don’t know whether {B} booked this order.</b>{g.rule} {g.yours}{latest ? ` Latest observation: ${latest.text}.` : ""}{s.next_recheck_at ? ` Next automatic re-check ${dateTime(s.next_recheck_at)}.` : ""}</div></div>;
    case "ESCALATED":
      return <div className="banner" role="note"><Icon name="alert" /><div><b>{B} may or may not have booked this order.</b>{op.last_evidence ? `${EVIDENCE_WORDS[op.last_evidence] ?? op.last_evidence}. ` : ""}Do not re-enter it — a person resolves it by a verified read or an attestation. {g.yours}</div></div>;
    case "READY":
      return <div className="banner info"><Icon name="shield" /><div><b>Waiting for a second person.</b>{d.permitted_actions.includes("approve") ? `You may approve or decline exactly v${op.current_version}.` : "Another person approves the exact version (four-eyes); the server decides who may."}</div></div>;
    case "AWAITING_ACK":
      return <div className="banner info"><Icon name="clock" /><div><b>{STATUS.AWAITING_ACK.label}.</b>{STATUS.AWAITING_ACK.copy}{s.ack_deadline_at ? ` Acknowledgement deadline ${dateTime(s.ack_deadline_at)}.` : ""}</div></div>;
    case "CONFIRMED":
      return op.last_evidence === "ACKNOWLEDGED_ONLY"
        ? <div className="banner info"><Icon name="eye" /><div><b>Acknowledged, not yet read back.</b>{B} acknowledged the order; it has not yet been matched by a read.</div></div> : null;
    case "CLOSED_ATTESTED":
      return <div className="banner att" role="note"><Icon name="alert" /><div><b>Closed by attestation (not verified).</b>Closed on a human attestation with an external reference. This is not a verified backend read.</div></div>;
    case "NOT_APPLIED":
      return <div className="banner info"><Icon name="check" /><div><b>Not applied — proven.</b>{op.last_evidence ? `Evidence: ${EVIDENCE_WORDS[op.last_evidence] ?? op.last_evidence}.` : ""}</div></div>;
    default:
      return null;
  }
}

function ApprovedVersion({ d }: { d: Detail }) {
  const op = d.operation;
  const v = currentVersion(d);
  const hash = String(v.content_hash ?? "");
  const approval = d.approvals.find((a) => a.decision === "APPROVED" && a.attempt_no === op.attempt_count);
  const s = d.schedule as Rec;
  return (
    <section className="card card-p" aria-labelledby="ver-h">
      <h2 className="h3" id="ver-h">{approval ? "Approved version" : "Current version"}</h2>
      <div className="kv" style={{ marginTop: 8 }}>
        <div><span>Version</span><span>v{op.current_version} · {(v.content?.lines ?? []).length} line{(v.content?.lines ?? []).length === 1 ? "" : "s"} · {op.estimated_total ? `EUR ${money(op.estimated_total)}` : "total unknown"}</span></div>
        <div><span>Content hash</span><span className="mono" style={{ fontSize: 12.5 }} title={hash}>{hash ? `${hash.slice(0, 12)}…${hash.slice(-4)}` : "—"}</span></div>
        <div><span>Prepared → approved</span><span>{op.created_by}{op.origin === "ASSISTANT" ? " (assistant-prepared)" : ""} → {approval ? String(approval.decided_by) : <span className="muted">pending</span>}</span></div>
        <div><span>Backend order</span><span className="mono">{op.anchor_order_ref ?? "—"}</span></div>
        <div><span>Evidence</span><span>{op.last_evidence ? <><span className="mono" style={{ fontSize: 12 }}>{op.last_evidence}</span> — {EVIDENCE_WORDS[op.last_evidence] ?? ""}</> : "—"}</span></div>
        <div><span>Verification level</span><span>{op.verification_level ?? "—"}</span></div>
        <div><span>Attempt</span><span>{op.attempt_count}</span></div>
        {s.next_recheck_at ? <div><span>Next automatic re-check</span><span>{dateTime(s.next_recheck_at)} ({String(s.auto_rechecks ?? 0)} done)</span></div> : null}
        {s.ack_deadline_at && op.state === "AWAITING_ACK" ? <div><span>Acknowledgement deadline</span><span>{dateTime(s.ack_deadline_at)}</span></div> : null}
        {s.redelivery_deadline && op.state === "UNKNOWN" ? <div><span>Redelivery window closes</span><span>{dateTime(s.redelivery_deadline)}</span></div> : null}
      </div>
    </section>
  );
}

function Lines({ d }: { d: Detail }) {
  const v = currentVersion(d);
  const lines = v.content?.lines ?? [];
  const checks = checksOf(d);
  return (
    <section className="card card-p" aria-labelledby="ln-h">
      <h2 className="h3" id="ln-h" style={{ marginBottom: 12 }}>Lines · v{d.operation.current_version}</h2>
      <ScrollRegion label="Lines">
        <table className="lt">
          <caption className="vh">Draft lines, version {d.operation.current_version}</caption>
          <thead><tr><th scope="col">#</th><th scope="col">Group SKU</th><th scope="col">Native item</th><th scope="col" className="num">Qty (base)</th><th scope="col" className="num">Qty (native)</th><th scope="col" className="num">Estimate</th></tr></thead>
          <tbody>{lines.map((l) => (
            <tr key={l.line_no}><td className="tnum">{l.line_no}</td><td className="mono" style={{ fontSize: 12.5 }}>{l.canonical_sku ?? "—"}</td><td className="mono" style={{ fontSize: 12.5 }}>{l.native_item}</td>
              <td className="num">{l.quantity_base}</td><td className="num">{l.quantity_native} {l.native_uom}</td><td className="num">{eur(l.line_estimate)}</td></tr>))}</tbody>
        </table>
      </ScrollRegion>
      <div className="pills" style={{ marginTop: 12 }} aria-label="Validation">
        {d.validation ? checks.map((c, i) => (
          <span key={i} className={`bd ${c.result === "PASS" ? "t-ok" : c.result === "BLOCKING" ? "t-bad" : c.result === "WARNING" ? "t-info" : "t-warn"}`} title={c.detail ?? ""}>
            {c.check}{c.line_no ? ` · line ${c.line_no}` : ""} · {c.result}
          </span>)) : <span className="muted small">Not validated yet — validation runs when the draft is submitted for approval.</span>}
      </div>
      {checks.some((c) => c.detail && c.result !== "PASS") ? (
        <ul className="muted small" style={{ margin: "10px 0 0", paddingLeft: 18 }}>
          {checks.filter((c) => c.detail && c.result !== "PASS").map((c, i) => <li key={i}><span className="mono">{c.check}</span>: {c.detail}</li>)}
        </ul>
      ) : null}
    </section>
  );
}

/** Immutable versions and what changed between two of them. */
function Versions({ d }: { d: Detail }) {
  const vs = d.versions as (Rec & { version: number; content_hash?: string; created_by?: string; origin?: string; content?: object })[];
  const last = vs[vs.length - 1]?.version ?? 1;
  const [pick, setPick] = useState<[number, number] | null>(null);
  if (vs.length < 2) return null;
  const [x, y] = pick ?? [vs[vs.length - 2]!.version, last];
  const at = (n: number) => vs.find((v) => v.version === n);
  const changes = diffVersions(at(x)?.content ?? {}, at(y)?.content ?? {});
  const sel = (label: string, value: number, set: (n: number) => void) => (
    <label className="flt-l">{label} <select className="inp" value={value} onChange={(e) => set(Number(e.target.value))}>{vs.map((v) => <option key={v.version} value={v.version}>v{v.version} · {String(v.created_by ?? "")}{v.origin === "ASSISTANT" ? " (assistant)" : ""}</option>)}</select></label>);
  return (
    <section className="card card-p" aria-labelledby="vs-h">
      <h2 className="h3" id="vs-h" style={{ marginBottom: 12 }}>Versions</h2>
      <div className="pills" style={{ marginBottom: 12 }}>{sel("From", x, (n) => setPick([n, y]))}{sel("To", y, (n) => setPick([x, n]))}</div>
      {changes.length ? (
        <ScrollRegion label="Changes">
          <table className="lt"><caption className="vh">Changes from v{x} to v{y}</caption>
            <thead><tr><th scope="col">Field or item</th><th scope="col">v{x}</th><th scope="col">v{y}</th></tr></thead>
            <tbody>{changes.map((c) => <tr key={c.what}><td className="mono" style={{ fontSize: 12.5 }}>{c.what}</td><td>{c.from}</td><td>{c.to}</td></tr>)}</tbody></table>
        </ScrollRegion>) : <p className="muted small" style={{ margin: 0 }}>No difference in the order content.</p>}
      <p className="muted" style={{ fontSize: 12.5, margin: "10px 0 0" }}>Compared in the browser for reading; an approval binds the server’s content hash of one version.</p>
    </section>
  );
}

const COLS: [string, string, string[]][] = [
  ["Approvals", "approvals", ["attempt_no", "decision", "decided_by", "version", "content_hash", "decided_at", "override_reason", "reason"]],
  ["Authorizations", "authorizations", ["kind", "attempt_no", "state", "granted_by", "granted_at", "expires_at", "consumed_at"]],
  ["Transmissions", "transmissions", ["attempt_no", "transmission_no", "kind", "state", "message_id", "payload_sha256", "started_at", "observed_at"]],
  ["Acknowledgements", "acknowledgements", ["file_name", "status", "order_ref", "code", "recorded_at"]],
  ["Reconciliation runs", "reconciliation_runs", ["trigger", "verdict", "started_at", "finished_at"]],
  ["Escalations", "escalations", ["reason", "opened_at", "resolution", "external_reference", "resolved_at"]],
];
const cell = (k: string, v: unknown) => (k.includes("hash") || k.includes("sha") ? <span className="mono" title={String(v ?? "")}>{shortHash(v, 16)}</span>
  : k.endsWith("_at") ? <time dateTime={iso(v)} title={iso(v)}>{dateTime(v)}</time> : v == null || v === "" ? "—" : String(v));

/** Recorded approvals, authorizations, hand-offs, acknowledgements, re-checks and escalations (read model rows). */
function Records({ d }: { d: Detail }) {
  const present = COLS.filter(([, key]) => ((d as unknown as Record<string, Rec[]>)[key] ?? []).length);
  const absent = COLS.filter(([, key]) => !((d as unknown as Record<string, Rec[]>)[key] ?? []).length).map(([c]) => c.toLowerCase());
  return (
    <section className="card card-p records" aria-labelledby="rec-h">
      <div className="spread"><h2 className="h3" id="rec-h">Records</h2><span className="muted" style={{ fontSize: 12.5 }}>As stored · times in your locale, UTC on hover</span></div>
      {present.map(([caption, key, cols]) => {
        const rows = (d as unknown as Record<string, Rec[]>)[key] ?? [];
        return (
          <ScrollRegion key={key} label={caption}>
            <table className="dt">
              <caption className="cap">{caption}</caption>
              <thead><tr>{cols.map((c) => <th key={c} scope="col">{c.replaceAll("_", " ")}</th>)}</tr></thead>
              <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{cell(c, r[c])}</td>)}</tr>)}</tbody>
            </table>
          </ScrollRegion>
        );
      })}
      {absent.length ? <p className="muted small" style={{ margin: 0 }}>None recorded: {absent.join(", ")}.</p> : null}
    </section>
  );
}

const tone = (e: TimelineEvent) => {
  const to = e.state?.to;
  if (/ESCALATION_OPENED|INTERRUPTED|LATE_OBSERVATION|INTEGRITY_ALERT/.test(e.event_type) || to === "UNKNOWN" || to === "ESCALATED") return "warn";
  if (to === "REJECTED") return "bad";
  if (/OUTCOME_RECORDED|EVIDENCE_UPGRADED/.test(e.event_type) && (to === "CONFIRMED" || to === "NOT_APPLIED" || !to)) return "ok";
  if (/APPROVAL|SUBMITTED|AUTHORIZATION/.test(e.event_type)) return "ac";
  return "";
};

function Timeline({ events }: { events: TimelineEvent[] }) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const shown = all ? events : events.slice(-6);
  return (
    <section className="card card-p" aria-labelledby="tl-h">
      <div className="spread" style={{ marginBottom: 16 }}><h2 className="h3" id="tl-h">Audit timeline</h2><span className="muted" style={{ fontSize: 12.5 }}>{events.length} events · newest last</span></div>
      {events.length > 6 ? (
        <button className="tech vt-more" type="button" aria-expanded={all} onClick={() => setAll(!all)}>
          <Icon name="chev" className="chev" /> {all ? "Show the latest 6 only" : `Show ${events.length - 6} earlier events`}
        </button>) : null}
      <ol className="vt">
        {shown.map((e) => (
          <li key={e.seq}>
            <span className={`dt ${tone(e)}`} aria-hidden="true" />
            <div className="t1"><b>{evLabel(e.event_type)}</b><time className="t2 tnum" dateTime={iso(e.occurred_at)} title={iso(e.occurred_at)}>{hms(e.occurred_at)}</time></div>
            <div className="t2">
              <span className="code">#{e.seq} · {e.event_type}</span>
              {e.state ? <> · {String(e.state.from ?? "∅")} → {String(e.state.to ?? "—")}</> : null}
              {" · "}{e.actor_type === "USER" || e.actor_username ? (e.actor_username ?? e.actor_type) : (e.actor_type.toLowerCase() === "system" ? "system · workflow engine" : `system · ${e.actor_type.toLowerCase()}`)}
              {e.on_behalf_of ? " (via assistant)" : ""}
            </div>
            <button className="tech" type="button" aria-expanded={!!open[e.seq]} onClick={() => setOpen({ ...open, [e.seq]: !open[e.seq] })}>
              <Icon name="chev" className="chev" /> Technical detail
            </button>
            {open[e.seq] ? <pre tabIndex={0} className="js">{JSON.stringify({ seq: e.seq, type: e.event_type, state: e.state, correlation: e.correlation, payload: e.payload, event_hash: e.event_hash }, null, 2)}</pre> : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

function AskAboutOrder({ correlation, unresolved }: { correlation: string; unresolved: boolean }) {
  const [turn, setTurn] = useState<{ q: string; t: Turn } | null>(null);
  const q = `status ${correlation}`;
  const { ask } = useAssistant();
  return (
    <section className="card card-p stack" aria-labelledby="ask-h">
      <div className="spread" style={{ alignItems: "center" }}><h2 className="h3" id="ask-h"><Icon name="spark" /> Ask about this order</h2><span className="ro">Read-only</span></div>
      {turn ? <><div className="bub me">{turn.q}</div><AssistantAnswer t={turn.t} compact /></>
        : <div className="chips"><button type="button" onClick={() => ask.mutate(q, { onSuccess: (t) => setTurn({ q, t }) })} disabled={ask.isPending}>{ask.isPending ? "Asking…" : unresolved ? "Why does this order need attention?" : "Summarise this order’s status"}</button></div>}
      {ask.error ? <ErrorBox error={ask.error} /> : null}
      <div className="muted" style={{ fontSize: 12.5, borderTop: "1px solid var(--bd)", paddingTop: 10 }}>The assistant reads and drafts. It cannot approve, submit, re-check, withdraw, redeliver or resolve.</div>
    </section>
  );
}
