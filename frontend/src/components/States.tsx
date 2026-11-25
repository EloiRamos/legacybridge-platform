import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";
import { OUTCOME_UNKNOWN, outcomeOf } from "../outcome";
import { useSession } from "../session";
import { Icon, type IconName } from "./Icon";

/** Skeleton while a server response is pending. Announced once through role=status. */
export function Loading({ what }: { what: string }) {
  return (
    <div className="card card-p stack" aria-busy="true">
      <span role="status" className="vh">Loading {what}…</span>
      {[92, 76, 84, 60, 70].map((w) => <div key={w} className="skel" style={{ width: `${w}%` }} />)}
    </div>
  );
}

/** RFC 9457 problem from the API. A read refused with 403 is shown as access denied. With ``what`` the failure is a
 * read, which changes nothing. Without it, it is a command: only the server may say what a command did (RR-1). */
export function ErrorBox({ error, what }: { error: unknown; what?: string }) {
  const p = error instanceof ApiError ? error.problem : null;
  if (p?.status === 403 && what) return <Forbidden detail={p.detail} />; // a refused command keeps its own reason (e.g. four-eyes)
  return (
    <div className="errbox" role="alert">
      <b>{what ? `Couldn’t load ${what}.` : p ? p.title || p.code : "Request failed"}</b>
      {p ? <>{p.title && what ? `${p.title}. ` : ""}{p.detail}{p.detail ? " " : ""}</> : null}
      {p ? <>Code <span className="mono">{p.code}</span>. </> : what ? <>The server could not be reached. </> : <>No usable answer was received. </>}
      {what ? "Nothing was changed by this view." : outcomeOf(error) === "unknown" ? OUTCOME_UNKNOWN : null}
      {p?.request_id ? <> Request id <span className="mono">{p.request_id}</span>.</> : null}
    </div>
  );
}

export function EmptyState({ title, children, icon = "search", action }: { title: string; children?: ReactNode; icon?: IconName; action?: ReactNode }) {
  return (
    <div className="empty">
      <span className="em"><Icon name={icon} /></span>
      <b>{title}</b>
      {children ? <span>{children}</span> : null}
      {action}
    </div>
  );
}

/** Kept for callers that only need a short empty line inside a card. */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted" style={{ margin: 0 }}>{children}</p>;
}

export function homePath(can: (p: string) => boolean): string {
  return can("operation.read") ? "/" : "/admin";
}

export function Forbidden({ detail }: { detail?: string }) {
  const { session, can } = useSession();
  return (
    <div className="forbid">
      <EmptyState title="Not available for your role" icon="lock"
        action={<Link className="btn" to={homePath(can)}>Go to your start page</Link>}>
        {session ? `${session.username} (${session.roles.join(", ")}) does not hold the permission this page needs.` : null}
        {" "}The server enforces the same rule{detail ? `: ${detail}` : "."}
      </EmptyState>
    </div>
  );
}

export function NotFound() {
  const { can } = useSession();
  return (
    <div className="forbid">
      <h1 className="vh">Page not found</h1>
      <EmptyState title="Page not found" icon="help" action={<Link className="btn" to={homePath(can)}>Go home</Link>}>
        This address doesn’t match a page in the console.
      </EmptyState>
    </div>
  );
}
