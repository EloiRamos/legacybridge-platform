import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, call } from "../api/client";
import { AdminUsers } from "../components/AdminUsers";
import { canAdmin } from "../components/Layout";
import { PageHead } from "../components/Page";
import { ScrollRegion } from "../components/ScrollRegion";
import { EmptyState, ErrorBox, Forbidden, Loading } from "../components/States";
import { useSession } from "../session";

const PRESETS = ["halden_commit_then_500", "halden_delay_2500ms", "halden_error_500", "halden_listing_500x3", "skarvik_drop_ack",
  "skarvik_contradict_ack", "skarvik_malformed_ack", "skarvik_ack_delay_30s", "skarvik_pause_pickup"] as const;
type Rec = Record<string, unknown>;

/** Administration (account menu). Each section is shown only for the permission that the server requires for
 * its endpoint. Users, roles and (demo/test profile) the demo controls can be changed; the server audits each change. */
export function Admin() {
  const { can, session } = useSession();
  const config = useQuery({ queryKey: ["config"], enabled: can("admin.config.read"), queryFn: () => call(api.GET("/api/v1/admin/config")) });
  const acks = useQuery({ queryKey: ["acks"], enabled: can("ackqueue.read"), queryFn: () => call(api.GET("/api/v1/admin/ack-queue")) });
  const engine = useQuery({ queryKey: ["engine"], enabled: can("backend.health.read"), queryFn: () => call(api.GET("/api/v1/admin/engine")) });
  const [preset, setPreset] = useState<(typeof PRESETS)[number]>(PRESETS[0]);
  const [done, setDone] = useState<string | null>(null);
  const demo = useMutation({ mutationFn: (x: { label: string; fn: () => Promise<unknown> }) => x.fn().then(() => x.label), onSuccess: (l) => setDone(l) });
  if (!canAdmin(can)) return <><h1 className="vh">Not available for your role</h1><Forbidden /></>;
  const ackRows = (acks.data ?? []) as Rec[];
  const ackCols = ackRows.length ? Object.keys(ackRows[0]!).slice(0, 7) : [];
  return (
    <>
      <PageHead eyebrow="Account · Administration" title="Administration" lede={`Visible sections follow your permissions (${session?.roles.join(", ")}).`} />
      {can("admin.users") ? <AdminUsers /> : null}
      {can("admin.config.read") ? (
        <section className="card card-p" aria-labelledby="ad-c">
          <h2 className="d s20" id="ad-c">Configuration</h2>
          <p className="muted small" style={{ margin: "4px 0 0" }}>Read-only · secrets redacted by the server</p>
          {config.isLoading ? <Loading what="configuration" /> : config.error ? <ErrorBox error={config.error} what="configuration" /> : <pre tabIndex={0} className="js">{JSON.stringify(config.data, null, 2)}</pre>}
        </section>) : null}
      {can("backend.health.read") ? (
        <section className="card card-p" aria-labelledby="ad-e">
          <h2 className="d s20" id="ad-e">Engine</h2>
          {engine.isLoading ? <Loading what="engine status" /> : engine.error ? <ErrorBox error={engine.error} what="engine status" /> : (
            <div className="kv" style={{ marginTop: 8 }}>
              {Object.entries((engine.data ?? {}) as Rec).map(([k, v]) => <div key={k}><span>{k.replace(/_/g, " ")}</span><span className="mono" style={{ fontSize: 12.5 }}>{typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}</span></div>)}
            </div>)}
        </section>) : null}
      {can("ackqueue.read") ? (
        <section className="card clip" aria-labelledby="ad-a">
          <div className="card-p" style={{ paddingBottom: 8 }}>
            <h2 className="d s20" id="ad-a">Unmatched acknowledgements</h2>
            <p className="muted small" style={{ margin: "4px 0 0" }}>ACK files from SKARVIK that match no operation. They are never applied automatically.</p>
          </div>
          {acks.isLoading ? <div className="card-p"><Loading what="the acknowledgement queue" /></div> : acks.error ? <div className="card-p"><ErrorBox error={acks.error} what="the acknowledgement queue" /></div> : !ackRows.length ? <EmptyState title="None" icon="check">No unmatched acknowledgement files.</EmptyState> : (
            <ScrollRegion label="ACK files">
              <table className="dt"><caption className="vh">Unmatched acknowledgements</caption>
                <thead><tr>{ackCols.map((c) => <th key={c} scope="col">{c.replace(/_/g, " ")}</th>)}</tr></thead>
                <tbody>{ackRows.map((r, i) => <tr key={i}>{ackCols.map((c) => <td key={c} className={/file|ref|id|sha/.test(c) ? "mono" : undefined} style={{ fontSize: 12.5 }}>{r[c] == null ? "—" : typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c])}</td>)}</tr>)}</tbody>
              </table>
            </ScrollRegion>)}
        </section>) : null}
      {can("demo.control") && session?.profile !== "default" ? (
        <section className="card card-p stack" aria-labelledby="ad-d">
          <h2 className="d s20" id="ad-d">Demo controls <span className="bd plain t-warn" style={{ fontSize: 12, verticalAlign: 4 }}>{session?.profile} profile only</span></h2>
          <p className="muted small" style={{ margin: 0 }}>These change the simulators, never a real system. Every demo action is audited; reset is refused while any order is in flight.</p>
          {demo.error ? <ErrorBox error={demo.error} /> : null}
          {done && !demo.error ? <div className="banner sm ok" role="status"><span>{done}: accepted by the server.</span></div> : null}
          <div className="pills">
            <button className="btn" type="button" disabled={demo.isPending} onClick={() => demo.mutate({ label: "Reset simulators", fn: () => call(api.POST("/api/v1/demo/reset")) })}>Reset simulators</button>
            <div className="field" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
              <label htmlFor="preset">Fault preset</label>
              <select className="inp" id="preset" style={{ width: "auto" }} value={preset} onChange={(e) => setPreset(e.target.value as (typeof PRESETS)[number])}>{PRESETS.map((p) => <option key={p} value={p}>{p}</option>)}</select>
            </div>
            <button className="btn pri" type="button" disabled={demo.isPending} onClick={() => demo.mutate({ label: `Arm fault ${preset}`, fn: () => call(api.POST("/api/v1/demo/faults", { body: { preset, count: 1 } })) })}>Arm fault</button>
          </div>
        </section>) : null}
    </>
  );
}
