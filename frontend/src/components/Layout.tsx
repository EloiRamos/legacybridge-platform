import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { api, call, setCsrf } from "../api/client";
import { initials } from "../format";
import { useSession } from "../session";
import { AssistantDrawer, AssistantProvider } from "./AssistantPanel";
import { Icon, type IconName } from "./Icon";
import { homePath } from "./States";

/* Single primary navigation: Overview · Orders · Approvals · Evidence · Assistant, filtered by the
 * session's permissions. Desktop renders it in the top bar; phones (container ≤ 720 px) render the same items as a
 * bottom bar — never both. Administration lives in the account menu. */
type Section = "overview" | "orders" | "approvals" | "evidence" | "assistant";
const NAV: { key: Section; label: string; to: string; icon: IconName; permission: string }[] = [
  { key: "overview", label: "Overview", to: "/", icon: "grid", permission: "operation.read" },
  { key: "orders", label: "Orders", to: "/operations", icon: "list", permission: "operation.read" },
  { key: "approvals", label: "Approvals", to: "/approvals", icon: "shield", permission: "approval.grant.standard" },
  { key: "evidence", label: "Evidence", to: "/audit", icon: "chain", permission: "audit.read" },
  { key: "assistant", label: "Assistant", to: "/assistant", icon: "spark", permission: "assistant.use" },
];

export function sectionOf(pathname: string): Section | null {
  if (pathname === "/") return "overview";
  if (/^\/(operations|reconciliation|customers|inventory)(\/|$)/.test(pathname)) return "orders";
  if (pathname.startsWith("/approvals")) return "approvals";
  if (pathname.startsWith("/audit")) return "evidence";
  if (pathname.startsWith("/assistant")) return "assistant";
  return null;
}

export const canAdmin = (can: (p: string) => boolean) => can("admin.users") || can("ackqueue.read");

function useCounts(enabled: boolean, approver: boolean) {
  // database-only read models (the dashboard endpoint also reads backend health live, so it is not polled here)
  const recon = useQuery({ queryKey: ["reconciliation"], enabled, refetchInterval: 15000, queryFn: () => call(api.GET("/api/v1/reconciliation")) });
  const queue = useQuery({ queryKey: ["approvals"], enabled: enabled && approver, refetchInterval: 15000, queryFn: () => call(api.GET("/api/v1/approvals")) });
  const attention = (recon.data?.items ?? []).filter((o) => o.state === "UNKNOWN" || o.state === "ESCALATED").length;
  return { attention, approvals: queue.data?.items.length ?? 0 };
}

export function Layout() {
  const { session, can } = useSession();
  const { pathname, search: qs } = useLocation();
  const navigate = useNavigate();
  const section = sectionOf(pathname);
  const items = NAV.filter((n) => can(n.permission));
  const counts = useCounts(can("operation.read"), can("approval.grant.standard") || can("approval.grant.any"));
  const [q, setQ] = useState("");
  const badge = (k: Section): ReactNode =>
    k === "orders" && counts.attention ? <span className="ct">{counts.attention}<span className="vh"> need attention</span></span>
      : k === "approvals" && counts.approvals ? <span className="ct n">{counts.approvals}<span className="vh"> waiting for you</span></span> : null;
  const go = (e: FormEvent) => { e.preventDefault(); navigate(`/operations?q=${encodeURIComponent(q.trim())}`); };
  const wide = pathname === "/operations" && new URLSearchParams(qs).get("view") === "table";

  return (
    <AssistantProvider>
    <div className="app">
      <a className="skip" href="#main">Skip to content</a>
      <header className="top">
        <Link className="logo" to={homePath(can)} aria-label="LegacyBridge home"><span className="m"><Icon name="chain" /></span>LegacyBridge</Link>
        {items.length ? (
          <nav className="nav" aria-label="Main">
            {items.map((n) => <Link key={n.key} to={n.to} aria-current={section === n.key ? "page" : undefined}>{n.label}{badge(n.key)}</Link>)}
          </nav>
        ) : null}
        <div className="r">
          {can("operation.read") ? (
            <>
              <form className="cmd" role="search" onSubmit={go}>
                <Icon name="search" />
                <label className="vh" htmlFor="cmd-q">Search orders</label>
                <input id="cmd-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search orders, accounts…" maxLength={60} />
              </form>
              <Link className="iconbtn" to="/operations" aria-label="Search orders"><Icon name="search" /></Link>
            </>
          ) : null}
          {can("assistant.use") ? <AssistantDrawer /> : null}
          {session ? <AccountMenu /> : null}
        </div>
      </header>
      <main id="main" className={`main${wide ? " wide" : ""}`}>
        <Outlet />
      </main>
      <Footer />
      {items.length ? (
        <nav className="mnav" aria-label="Main (mobile)">
          {items.map((n) => (
            <Link key={n.key} to={n.to} aria-current={section === n.key ? "page" : undefined}>
              <Icon name={n.icon} /><span>{n.label}</span>{badge(n.key)}
            </Link>
          ))}
        </nav>
      ) : null}
    </div>
    </AssistantProvider>
  );
}

function AccountMenu() {
  const { session, can } = useSession();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); button.current?.focus(); } };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);
  if (!session) return null;
  const signOut = async () => {
    await call(api.POST("/api/v1/auth/logout"));
    setCsrf(null);
    qc.clear();
    navigate("/login");
  };
  return (
    <div className="acct" ref={box}>
      <button ref={button} className="av" type="button" aria-expanded={open} aria-controls="acct-menu" onClick={() => setOpen(!open)}
        aria-label={`${initials(session.username)} — account menu for ${session.username} (${session.roles.join(", ")})`}>{initials(session.username)}</button>
      {open ? (
        <div className="menu" id="acct-menu">
          <div className="who"><b>{session.username}</b><small>{session.roles.join(", ")} · {session.profile} profile</small></div>
          <ul>
            {canAdmin(can) ? <li><Link to="/admin" onClick={() => setOpen(false)}><Icon name="server" /> Administration</Link></li> : null}
            <li><button className="mi" type="button" onClick={signOut}><Icon name="lock" /> Sign out</button></li>
          </ul>
        </div>
      ) : null}
    </div>
  );
}

export function Footer() {
  return (
    <footer className="foot" role="contentinfo">
      Fictional engagement for Brightwater Industrial Supply Group (fictional) · HALDEN ERP 3.1 and SKARVIK OES 4 are
      simulated legacy systems · Not a production system
    </footer>
  );
}
