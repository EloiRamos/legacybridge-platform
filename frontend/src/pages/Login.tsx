import { type FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, call } from "../api/client";
import { Icon } from "../components/Icon";
import { Footer } from "../components/Layout";
import { ErrorBox } from "../components/States";
import { useSession } from "../session";

function safeNext(raw: string | null): string {
  return raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
}

export function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const { signIn } = useSession();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const s = await call(api.POST("/api/v1/auth/login", { body: { username, password } }));
      signIn(s);
      const next = safeNext(params.get("next"));
      // land on a page the role may read (the sysadmin has no operation.read)
      navigate(next === "/" && !s.permissions.includes("operation.read") ? "/admin" : next);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="signin">
      <main id="main">
        <form onSubmit={submit} className="card card-p stack g16" aria-labelledby="login-title">
          <div className="logo"><span className="m"><Icon name="chain" /></span>LegacyBridge</div>
          <div>
            <h1 id="login-title" className="d" style={{ fontSize: 27 }}>Sign in</h1>
            <p className="muted small" style={{ margin: "4px 0 0" }}>Order operations console · fictional engagement, simulated legacy systems</p>
          </div>
          {error ? <ErrorBox error={error} /> : null}
          <div className="field"><label htmlFor="username">Username</label>
            <input className="inp" id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></div>
          <div className="field"><label htmlFor="password">Password</label>
            <input className="inp" id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></div>
          <button className="btn pri" type="submit" disabled={busy}>Sign in</button>
        </form>
      </main>
      <Footer />
    </div>
  );
}

export function Reauth() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const { signIn } = useSession();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      signIn(await call(api.POST("/api/v1/auth/reauth", { body: { password } })));
      navigate(safeNext(params.get("next")));
    } catch (err) {
      setError(err);
    }
  };
  return (
    <form onSubmit={submit} className="card card-p stack g16" style={{ maxWidth: 460, width: "100%", margin: "24px auto" }} aria-labelledby="reauth-title">
      <div className="eyebrow"><Icon name="lock" /> Step-up</div>
      <h1 id="reauth-title" className="d" style={{ fontSize: 27 }}>Confirm your identity</h1>
      <p className="muted" style={{ margin: 0 }}>Approvals, declines, redelivery and withdrawal authorizations, resolutions and role changes require a password confirmation within the last 15 minutes.</p>
      {error ? <ErrorBox error={error} /> : null}
      <div className="field"><label htmlFor="reauth-password">Password</label>
        <input className="inp" id="reauth-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></div>
      <button className="btn pri" type="submit">Confirm</button>
    </form>
  );
}
