import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, call, type Schemas } from "../api/client";
import { Icon } from "../components/Icon";
import { OrdersTabs, PageHead } from "../components/Page";
import { ScrollRegion } from "../components/ScrollRegion";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { Sys } from "../components/StatusBadge";
import { backendName } from "../status";

type Account = Schemas["AccountOut"];

export function Hold({ a, prefix = true }: { a: Pick<Account, "backend_id" | "credit_hold">; prefix?: boolean }) {
  const tone = a.credit_hold === "HOLD" ? "t-bad" : a.credit_hold === "UNKNOWN" ? "t-warn" : "t-ok";
  return <span className={`bd ${tone}`}>{prefix ? `${backendName(a.backend_id)} hold: ` : ""}{a.credit_hold}</span>;
}

export function Customers() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [input, setInput] = useState(q);
  const result = useQuery({
    queryKey: ["customers", q], enabled: q.length >= 2,
    queryFn: () => call(api.GET("/api/v1/customers", { params: { query: { q } } })),
  });
  const go = (v: string) => { const t = v.trim(); setInput(t); setParams(t ? { q: t } : {}, { replace: true }); };
  const submit = (e: FormEvent) => { e.preventDefault(); go(input); };
  const ambiguous = new Set((result.data?.ambiguity_groups ?? []).flat());
  return (
    <>
      <PageHead eyebrow="Orders" title="Customers" tabs={<OrdersTabs />}
        lede="Search both ERPs. Accounts are shown per backend; parties are linked only by curated master data — nothing is merged automatically." />
      <form className="pills" role="search" aria-label="Customer accounts" onSubmit={submit}>
        <label className="srch" style={{ flex: "1 1 360px" }}><Icon name="search" /><span className="vh">Search both ERPs</span>
          <input id="q" value={input} onChange={(e) => setInput(e.target.value)} minLength={2} maxLength={60} placeholder="Name, account number or city (min. 2 characters)" /></label>
        <button className="btn pri" type="submit">Search</button>
      </form>
      {q.length < 2 ? <div className="card"><EmptyState title="Search to begin" icon="user">Results include accounts from HALDEN and SKARVIK, with credit-hold state per backend.</EmptyState></div>
        : result.isLoading ? <Loading what="customers" /> : result.error ? <ErrorBox error={result.error} what="customers" /> : result.data ? (
          <>
            {Object.entries(result.data.backend_errors ?? {}).map(([b, m]) => <div key={b} className="banner sm" role="alert"><Icon name="alert" /><div><b>{backendName(b)} could not be searched.</b>{m} Results from the other backend are shown.</div></div>)}
            {result.data.parties.map((p) => (
              <section key={p.party_id} className="card card-p party" aria-label={`Party ${p.display_name}`}>
                <div className="rowx" style={{ alignItems: "baseline" }}><h2 className="d s20">{p.display_name}</h2><span className="muted small">linked by curated master data</span></div>
                {p.conflicts.map((c, i) => <div key={i} className="banner sm"><Icon name="alert" /><span>{String(c.message)}</span></div>)}
                {p.accounts.map((a) => (
                  <div className="acc-row" key={a.backend_id + a.native_id}>
                    <span><Link className="mono" to={`/customers/${a.backend_id}/${a.native_id}`}>{backendName(a.backend_id)} {a.native_id}</Link> — {a.name_display} <span className="muted">· {a.city ?? "—"} {a.country_code ?? ""}</span></span>
                    <Hold a={a} />
                  </div>))}
              </section>
            ))}
            {result.data.ambiguity_groups.length ? <div className="banner sm"><Icon name="alert" /><div><b>Similar names found in the same system.</b>These are separate accounts — choose explicitly; nothing has been merged.</div></div> : null}
            {result.data.accounts.length === 0 ? <div className="card"><EmptyState title={`No accounts match “${q}”`}>Check the spelling or search by account number.</EmptyState></div> : (
              <section className="card clip">
                <ScrollRegion label="Accounts found">
                  <table className="dt">
                    <caption className="vh">Matching accounts</caption>
                    <thead><tr><th scope="col">Backend</th><th scope="col">Account</th><th scope="col">Name</th><th scope="col">City</th><th scope="col">Credit hold</th><th scope="col">Flags</th></tr></thead>
                    <tbody>{result.data.accounts.map((a) => (
                      <tr key={a.backend_id + a.native_id}>
                        <td><Sys backend={a.backend_id} /></td>
                        <td><Link to={`/customers/${a.backend_id}/${a.native_id}`} className="mono">{a.native_id}</Link></td>
                        <td>{a.name_display}</td><td>{a.city ?? "—"} {a.country_code ?? ""}</td><td><Hold a={a} /></td>
                        <td>{ambiguous.has(`${a.backend_id}:${a.native_id}`) ? <span className="bd t-warn">ambiguous name</span> : null}</td>
                      </tr>))}</tbody>
                  </table>
                </ScrollRegion>
              </section>)}
          </>
        ) : null}
    </>
  );
}
