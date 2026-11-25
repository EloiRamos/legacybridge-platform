import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, ApiError, call, type Schemas } from "../api/client";
import { dateTime } from "../format";
import { useSession } from "../session";
import { Icon } from "./Icon";
import { ScrollRegion } from "./ScrollRegion";
import { Sheet } from "./Sheet";
import { ErrorBox, Loading } from "./States";

type Role = Schemas["Role"];
type User = Schemas["UserOut"];
const ROLES: Role[] = ["ops_manager", "sales_ops", "inventory_coordinator", "integration_engineer", "compliance_reviewer", "sysadmin"];

/** Users and roles. Every change goes to the admin API, which requires a recent sign-in and refuses changes to one's
 * own roles and to the last system administrator (SoD-3). */
export function AdminUsers() {
  const { can } = useSession();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const users = useQuery({ queryKey: ["users"], queryFn: () => call(api.GET("/api/v1/admin/users")) });
  const [edit, setEdit] = useState<User | "new" | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = useMutation({
    mutationFn: (x: { done: string; fn: () => Promise<unknown> }) => x.fn().then(() => x.done),
    onSuccess: (d) => { setDone(d); setError(null); },
    onError: (e) => {
      setDone(null);
      if (e instanceof ApiError && e.problem.code === "REAUTH_REQUIRED") navigate(`/reauth?next=${encodeURIComponent("/admin")}`);
      else setError(e);
    },
    onSettled: () => { setEdit(null); void qc.invalidateQueries({ queryKey: ["users"] }); },
  });
  return (
    <section className="card clip" aria-labelledby="ad-u">
      <div className="card-p spread" style={{ paddingBottom: 8, alignItems: "center" }}>
        <h2 className="d s20" id="ad-u">Users and roles</h2>
        {can("admin.users") ? <button className="btn sm" type="button" onClick={() => setEdit("new")}>New user</button> : null}
      </div>
      {error ? <div className="card-p"><ErrorBox error={error} /></div> : null}
      {done ? <div className="card-p"><div className="banner sm ok" role="status"><span>{done}.</span></div></div> : null}
      {users.isLoading ? <div className="card-p"><Loading what="users" /></div> : users.error ? <div className="card-p"><ErrorBox error={users.error} what="users" /></div> : (
        <ScrollRegion label="User list">
          <table className="dt"><caption className="vh">Users</caption>
            <thead><tr><th scope="col">Username</th><th scope="col">Name</th><th scope="col">Roles</th><th scope="col">Active</th><th scope="col">Locked until</th>{can("admin.roles") ? <th scope="col">Change</th> : null}</tr></thead>
            <tbody>{users.data!.map((u) => <tr key={u.user_id}><td className="mono">{u.username}</td><td>{u.display_name}</td><td>{u.roles.map((r) => <span key={r} className="bd plain t-neutral" style={{ marginRight: 4 }}>{r}</span>)}</td><td>{u.is_active ? "yes" : "no"}</td><td>{u.locked_until ? dateTime(u.locked_until) : "—"}</td>
              {can("admin.roles") ? <td><button className="btn sm" type="button" aria-label={`Change roles of ${u.username}`} onClick={() => setEdit(u)}>Roles…</button></td> : null}</tr>)}</tbody>
          </table>
        </ScrollRegion>)}
      {edit === "new" ? <NewUser busy={run.isPending} onClose={() => setEdit(null)}
        onSubmit={(body) => run.mutate({ done: `${body.username} created`, fn: () => call(api.POST("/api/v1/admin/users", { body })) })} />
        : edit ? <EditRoles user={edit} busy={run.isPending} onClose={() => setEdit(null)}
          onGrant={(role) => run.mutate({ done: `${role} granted to ${edit.username}`, fn: () => call(api.POST("/api/v1/admin/users/{user_id}/roles", { params: { path: { user_id: edit.user_id } }, body: { role } })) })}
          onRevoke={(role) => run.mutate({ done: `${role} revoked from ${edit.username}`, fn: () => call(api.DELETE("/api/v1/admin/users/{user_id}/roles/{role}", { params: { path: { user_id: edit.user_id, role } } })) })} /> : null}
    </section>
  );
}

function NewUser({ busy, onClose, onSubmit }: { busy: boolean; onClose: () => void; onSubmit: (b: Schemas["UserCreateIn"]) => void }) {
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [roles, setRoles] = useState<Role[]>([]);
  const ok = /^[a-z0-9._-]{3,60}$/.test(username) && name.trim() !== "" && password.length >= 12;
  return (
    <Sheet title="New user" onClose={onClose} busy={busy} locked={busy} onSubmit={() => { if (ok && !busy) onSubmit({ username, display_name: name.trim(), password, roles }); }}
      footer={<><button className="btn" type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="btn pri" type="submit" disabled={!ok || busy}>Create user</button></>}>
      <div className="field"><label htmlFor="nu-u">Username</label><input className="inp mono" id="nu-u" value={username} onChange={(e) => setUsername(e.target.value)} maxLength={60} autoComplete="off" data-autofocus="" /></div>
      <div className="field"><label htmlFor="nu-n">Display name</label><input className="inp" id="nu-n" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} autoComplete="off" /></div>
      <div className="field"><label htmlFor="nu-p">Initial password</label><input className="inp" id="nu-p" type="password" value={password} onChange={(e) => setPassword(e.target.value)} maxLength={200} autoComplete="new-password" aria-describedby="nu-h" /><span className="hint" id="nu-h">At least 12 characters. Sent once to the server; not shown or stored in the console.</span></div>
      <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}><legend className="lbl">Roles</legend>
        {ROLES.map((r) => <label key={r} style={{ display: "flex", gap: 8, fontWeight: 400 }}><input type="checkbox" checked={roles.includes(r)} onChange={(e) => setRoles(e.target.checked ? [...roles, r] : roles.filter((x) => x !== r))} /><span className="mono">{r}</span></label>)}</fieldset>
    </Sheet>
  );
}

function EditRoles({ user, busy, onClose, onGrant, onRevoke }: { user: User; busy: boolean; onClose: () => void; onGrant: (r: Role) => void; onRevoke: (r: Role) => void }) {
  const free = ROLES.filter((r) => !user.roles.includes(r));
  const [role, setRole] = useState<Role | undefined>(free[0]);
  return (
    <Sheet title={`Roles of ${user.username}`} onClose={onClose} busy={busy} locked={busy} onSubmit={() => { if (role && !busy) onGrant(role); }}
      footer={<><button className="btn" type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="btn pri" type="submit" disabled={!role || busy}>Grant</button></>}>
      <p className="muted small" style={{ margin: 0 }}>A role change signs this person out. The server refuses changes to your own roles and to the last system administrator.</p>
      <ul className="allowed">{user.roles.map((r) => <li key={r}><span className="mono" style={{ flex: 1 }}>{r}</span><button className="btn sm" type="button" disabled={busy} aria-label={`Revoke ${r}`} onClick={() => onRevoke(r)}><Icon name="x" /> Revoke</button></li>)}</ul>
      {free.length ? <div className="field"><label htmlFor="er-r">Role to grant</label><select className="inp" id="er-r" value={role} onChange={(e) => setRole(e.target.value as Role)}>{free.map((r) => <option key={r} value={r}>{r}</option>)}</select></div> : null}
    </Sheet>
  );
}
