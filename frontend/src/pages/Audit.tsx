import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, call, type Schemas } from "../api/client";
import { Icon } from "../components/Icon";
import { PageHead } from "../components/Page";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { dateTime, evLabel, hms, iso } from "../format";
import { useSession } from "../session";

const PAGE = 50;

/** Chain of custody from GET /api/v1/audit/events (append-only, hash-linked). Verification is GET /audit/verify:
 * the server recomputes the chain; the console only reports that result and never claims its own check. */
export function Audit() {
  const { can } = useSession();
  const [params, setParams] = useSearchParams();
  const operation = params.get("operation") ?? "";
  const [text, setText] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const head = useQuery({ queryKey: ["audit-head"], refetchInterval: 10000,
    queryFn: () => call(api.GET("/api/v1/audit/events", { params: { query: { from_seq: 1, limit: 1 } } })) });
  const headSeq = head.data?.head_seq ?? 0;
  const from = operation ? 1 : Math.max(1, headSeq - limit + 1);
  const events = useQuery({
    queryKey: ["audit", operation, from, operation ? 1000 : limit], enabled: !!operation || head.isSuccess,
    queryFn: () => call(api.GET("/api/v1/audit/events", { params: { query: { operation_id: operation || undefined, from_seq: from, limit: operation ? 1000 : limit } } })),
  });
  const ops = useQuery({ queryKey: ["operations"], enabled: can("operation.read"), queryFn: () => call(api.GET("/api/v1/operations")) });
  const verify = useQuery({ queryKey: ["audit-verify"], enabled: false, retry: false, queryFn: () => call(api.GET("/api/v1/audit/verify")) });
  const exp = useMutation({ mutationFn: () => call(api.POST("/api/v1/audit/exports")).then((e) => ({ e, files: [
    URL.createObjectURL(new Blob([e.jsonl], { type: "application/jsonl" })),
    URL.createObjectURL(new Blob([JSON.stringify(e.manifest, null, 2) + "\n"], { type: "application/json" }))] as const })) });
  const t = text.trim().toLowerCase();
  const list = [...(events.data?.events ?? [])].reverse().filter((e) => !t || `${e.event_type} ${e.actor_username ?? e.actor_type} ${e.seq} ${String(e.correlation?.correlation_value ?? "")}`.toLowerCase().includes(t));
  const corr = new Map((ops.data?.items ?? []).map((o) => [o.operation_id, o.correlation_value]));
  const v = verify.data;

  return (
    <>
      <PageHead eyebrow="Evidence" title="The chain of custody"
        lede="Every event links to the one before it by hash. Change a past event and every later link breaks. Chain integrity detects modification relative to retained head hashes — it is not completeness and not authenticity."
        actions={<>{can("audit.export") ? <button className="btn" type="button" onClick={() => exp.mutate()} disabled={exp.isPending}><Icon name="file" /> {exp.isPending ? "Exporting…" : "Export audit"}</button> : null}{can("audit.verify")
          ? <button className="btn pri" type="button" onClick={() => verify.refetch()} disabled={verify.isFetching}><Icon name="shield" /> {verify.isFetching ? "Verifying…" : "Verify the chain"}</button>
          : <span className="muted small" style={{ maxWidth: 280 }}>Chain verification is reserved for the compliance reviewer (audit.verify).</span>}</>} />
      {verify.error ? <ErrorBox error={verify.error} what="the verification result" /> : null}
      {exp.error ? <ErrorBox error={exp.error} /> : null}
      {exp.data ? <ExportResult e={exp.data.e} files={exp.data.files} /> : null}
      {v ? (
        <div className={`verify${v.valid ? "" : " bad"}`} role="status">
          <Icon name={v.valid ? "check" : "x"} />
          {v.valid
            ? <>Server verification: chain VALID — {v.count} events, head #{v.head_seq} · <span className="mono">{v.head_hash.slice(0, 16)}</span></>
            : <>Server verification: chain INVALID at #{String(v.first_broken_seq)} — {v.reason}</>}
          <span style={{ fontWeight: 400, color: "var(--tx3)" }}>· recomputed by the server ({verify.dataUpdatedAt ? dateTime(new Date(verify.dataUpdatedAt).toISOString()) : ""}); this page did not re-hash the events</span>
        </div>) : null}
      <div className="pills" role="group" aria-label="Filter events">
        <label className="srch"><Icon name="search" /><span className="vh">Filter events</span>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Event type, actor, sequence or reference" maxLength={60} /></label>
        <label className="vh" htmlFor="e-op">Order</label>
        <select className="inp" id="e-op" style={{ width: "auto", height: 40 }} value={operation}
          onChange={(e) => { const n = new URLSearchParams(params); if (e.target.value) n.set("operation", e.target.value); else n.delete("operation"); setParams(n, { replace: true }); }}>
          <option value="">All orders</option>
          {operation && !corr.has(operation) ? <option value={operation}>{operation}</option> : null}
          {(ops.data?.items ?? []).map((o) => <option key={o.operation_id} value={o.operation_id}>{o.correlation_value}</option>)}
        </select>
      </div>
      {head.error ? <ErrorBox error={head.error} what="audit events" /> : events.isLoading || head.isLoading ? <Loading what="audit events" /> : events.error ? <ErrorBox error={events.error} what="audit events" /> : !list.length ? (
        <div className="card"><EmptyState title="No events" icon="chain">Nothing matches this filter.</EmptyState></div>
      ) : (
        <>
          <ol className="chain" aria-label="Hash-linked events, newest first">
            {list.map((e) => {
              const isOpen = !!open[e.seq];
              const cv = (e.operation_id && corr.get(e.operation_id)) || (e.correlation?.correlation_value as string | undefined);
              return (
                <li className="blk" key={e.seq}>
                  <span className="lk" aria-hidden="true" />
                  <button className="blkc" type="button" aria-expanded={isOpen} onClick={() => setOpen({ ...open, [e.seq]: !isOpen })}>
                    <span className="r1">
                      <span className="mono muted">#{e.seq}</span><b>{evLabel(e.event_type)}</b>
                      <span className="muted small">{e.actor_username ?? (e.actor_type.toLowerCase() === "system" ? "system · workflow engine" : `system · ${e.actor_type.toLowerCase()}`)}{e.on_behalf_of ? " (via assistant)" : ""}</span>
                      {cv ? <span className="mono" style={{ fontSize: 12 }}>{cv}</span> : null}
                      <time className="muted tnum" style={{ marginLeft: "auto", fontSize: 12.5 }} dateTime={iso(e.occurred_at)} title={iso(e.occurred_at)}>{hms(e.occurred_at)}</time>
                    </span>
                    <span className="r2"><Icon name="chain" /> prev {e.prev_hash.slice(0, 10)}… → <b style={{ color: "var(--ac)" }}>{e.event_hash.slice(0, 10)}…</b>{e.state ? ` · ${String(e.state.from ?? "∅")} → ${String(e.state.to ?? "—")}` : ""}</span>
                  </button>
                  {isOpen ? (
                    <div className="blkx">
                      <div>
                        <b>Event</b><p className="mono" style={{ margin: "4px 0 8px", fontSize: 12 }}>{e.event_type}</p>
                        <b>Provenance</b><p className="muted" style={{ margin: "4px 0 8px" }}>{e.actor_username ? `User session · ${e.actor_username}` : `System actor (${e.actor_type})`}{e.on_behalf_of ? ` on behalf of ${e.on_behalf_of}` : "; no on-behalf-of"}. Recorded {dateTime(e.occurred_at)}.</p>
                        {e.operation_id ? <Link to={`/operations/${e.operation_id}`}>Open {cv ?? "the order"}</Link> : null}
                      </div>
                      <div>
                        <b>Hashes (as stored)</b>
                        <p className="mono" style={{ margin: "4px 0", fontSize: 11.5, overflowWrap: "anywhere" }}>prev {e.prev_hash}<br />this {e.event_hash}</p>
                        <pre tabIndex={0} className="js">{JSON.stringify({ correlation: e.correlation, payload: e.payload }, null, 2)}</pre>
                      </div>
                    </div>) : null}
                </li>);
            })}
          </ol>
          {!operation && from > 1
            ? <button className="btn" type="button" style={{ alignSelf: "center" }} onClick={() => setLimit(Math.min(1000, limit + PAGE))} disabled={limit >= 1000}>Show {PAGE} older events ({from - 1} before #{from})</button>
            : <p className="muted small" style={{ margin: 0, textAlign: "center" }}>{operation ? `All ${events.data?.events.length ?? 0} events of this order` : `Start of the chain · ${events.data?.events.length ?? 0} events`}{t ? " (text filter applied)" : ""}</p>}
        </>
      )}
    </>
  );
}

/** The export exactly as the server returned it: JSON Lines plus manifest. Nothing is recomputed here; the standalone
 * verifier (oracles/audit_verify.py) checks the files, and the head hash is retained outside the system. */
function ExportResult({ e, files }: { e: Schemas["ExportOut"]; files: readonly [string, string] }) {
  const m = e.manifest as Record<string, unknown>;
  const name = `audit-${String(m.from_seq)}-${String(m.to_seq)}`;
  return (
    <div className="banner info" role="status"><Icon name="file" /><div><b>Export ready: {String(m.count)} events.</b>Head hash <span className="mono" style={{ overflowWrap: "anywhere" }}>{String(m.head_hash)}</span>. Keep it outside this system and check the files with the standalone verifier.
      <div className="pills" style={{ marginTop: 8 }}><a className="btn sm" href={files[0]} download={`${name}.jsonl`}>Download events</a><a className="btn sm" href={files[1]} download={`${name}.manifest.json`}>Download manifest</a></div></div></div>
  );
}
