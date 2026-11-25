import { useMutation, useQuery } from "@tanstack/react-query";
import { createContext, type FormEvent, type KeyboardEvent, type ReactNode, useContext, useEffect, useId, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { api, call } from "../api/client";
import { useSession } from "../session";
import { modeWords } from "../assistant";
import { opener } from "../focus";
import { AssistantAnswer, type Turn } from "./AssistantAnswer";
import { Icon } from "./Icon";
import { EmptyState, ErrorBox } from "./States";

type Asked = { q: string; t: Turn };
const Ctx = createContext<{ turns: Asked[]; add: (a: Asked) => void } | null>(null);

/** The turns shown to this person, shared by the drawer, the page and the order card. Display only: the conversation the
 * model sees is held by the server per session, so a re-authenticated session starts empty here too. */
export function AssistantProvider({ children }: { children: ReactNode }) {
  const { session } = useSession();
  const key = `${session?.user_id}|${session?.authenticated_at}`;
  const [state, setState] = useState<{ key: string; turns: Asked[] }>({ key, turns: [] });
  const add = (a: Asked) => setState((s) => ({ key, turns: [a, ...(s.key === key ? s.turns : [])].slice(0, 10) }));
  return <Ctx.Provider value={{ turns: state.key === key ? state.turns : [], add }}>{children}</Ctx.Provider>;
}

export function useAssistant() {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAssistant outside AssistantProvider");
  const ask = useMutation({
    mutationFn: (q: string) => call(api.POST("/api/v1/assistant/turns", { body: { text: q }, params: { header: { "LBF-Assistant-Version": "2" } } })),
    onSuccess: (t, q) => c.add({ q, t }),
  });
  return { turns: c.turns, ask };
}

/** Read-and-draft assistant. It calls only POST /api/v1/assistant/turns; the server's tool
 * registry decides what runs with the invoking person's permissions. It cannot approve, submit, re-check,
 * withdraw, redeliver or resolve — any such request is refused server-side and shown as refused. */
export function AssistantPanel({ drawer = false }: { drawer?: boolean }) {
  const id = useId();
  const status = useQuery({ queryKey: ["assistant-status"], queryFn: () => call(api.GET("/api/v1/assistant/status")) });
  const [text, setText] = useState("");
  const { turns, ask } = useAssistant();
  const send = (q: string) => { if (q.trim() && !ask.isPending) ask.mutate(q.trim(), { onSuccess: () => setText("") }); };
  const submit = (e: FormEvent) => { e.preventDefault(); send(text); };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text); } };
  const mode = status.data?.mode;
  const deterministic = mode === "disabled" || mode === "scripted";
  const suggestions = deterministic
    ? ["unresolved", "stock BW-1004", "find customer nordkap", "approve the oldest order"]
    : ["Which orders are unresolved, and why?", "Availability of BW-1004 in both backends", "Approve the oldest order"];
  return (
    <div className="asst">
      <div style={{ textAlign: "center" }}>
        <div className="eyebrow" style={{ justifyContent: "center" }}>Assistant · {modeWords(mode)}</div>
        {drawer ? null : <h1 className="d" tabIndex={-1} style={{ marginTop: 6 }}>Ask, don’t act</h1>}
        <p className="lede" style={{ margin: "6px auto 0" }}>{mode === "disabled"
          ? "Fixed commands answer from system records; no model is used and earlier questions are not remembered. It reads and drafts — people submit and approve."
          : "Values from system records are shown with the field they come from; the explanation is the assistant’s wording. It reads and drafts — people submit and approve. Follow-up questions use this session’s recent questions; signing out or confirming your identity starts a new conversation."}</p>
      </div>
      {drawer && turns.length ? null : (
        <div className="can">
          <div className="card card-p"><b><Icon name="eye" /> It can</b><ul><li>Look up customers, items and availability</li><li>Explain an order’s timeline and status</li><li>Prepare a draft badged <i>assistant-prepared</i>, which you review and submit</li></ul></div>
          <div className="card card-p"><b><Icon name="lock" /> It can’t</b><ul><li>Approve</li><li>Submit or re-send</li><li>Re-check or reconcile</li><li>Withdraw or redeliver</li><li>Resolve an unknown outcome</li></ul></div>
        </div>)}
      {status.data && deterministic && status.data.grammar.length ? (
        <p className="muted small" style={{ margin: 0, textAlign: "center" }}>Commands: {status.data.grammar.join(" · ")}</p>) : null}
      {status.error ? <ErrorBox error={status.error} what="the assistant status" /> : null}
      <div className="chips" style={{ justifyContent: "center" }}>
        {suggestions.map((s) => <button key={s} type="button" onClick={() => send(s)} disabled={ask.isPending}>{s}</button>)}
      </div>
      <form className="cmpz" onSubmit={submit}>
        <label className="vh" htmlFor={id}>Ask the assistant</label>
        <textarea id={id} data-autofocus={drawer ? "" : undefined} rows={1} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} maxLength={2000} placeholder="Ask about an order, customer or item…" />
        <button className="btn pri" type="submit" disabled={ask.isPending || !text.trim()}><Icon name="send" /> {ask.isPending ? "Working…" : "Ask"}</button>
      </form>
      {ask.error ? <><ErrorBox error={ask.error} /><p className="muted small" style={{ margin: 0 }}>If you asked for a draft, check <Link to="/operations?stage=draft">your drafts</Link> before asking again.</p></> : null}
      {!turns.length ? <EmptyState title="Start with a question" icon="spark">Try one of the suggestions above.</EmptyState> : turns.map(({ q, t }) => (
        <section key={t.turn_id} className="card card-p stack g8" aria-label={`Answer to: ${q}`}>
          <div className="bub me">{q}</div>
          <AssistantAnswer t={t} onConfirm={turns[0]?.t.turn_id === t.turn_id && !ask.isPending ? send : undefined} />
        </section>
      ))}
    </div>
  );
}

/** Drawer on every page: a native modal <dialog>, so focus is trapped, Escape closes it and focus returns. */
function Drawer({ onClose }: { onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    const from = opener();
    if (d && !d.open) d.showModal();
    d?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    return () => { d?.close(); from?.focus?.(); };
  }, []);
  return (
    <dialog ref={ref} className="sheet" aria-labelledby="asst-title" onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <div className="sheet-h">
        <h2 id="asst-title" tabIndex={-1}>Assistant</h2>
        <button type="button" className="btn x" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
      </div>
      <div className="sheet-b"><AssistantPanel drawer /></div>
    </dialog>
  );
}

export function AssistantDrawer() {
  const { pathname } = useLocation();
  const [openOn, setOpenOn] = useState<string | null>(null); // the page it was opened on: following a link closes it
  if (pathname.startsWith("/assistant")) return null; // the page itself is the panel
  return (
    <>
      <button type="button" className="iconbtn asst-open" aria-haspopup="dialog" aria-label="Open assistant" onClick={() => setOpenOn(pathname)}><Icon name="spark" /></button>
      {openOn === pathname ? <Drawer onClose={() => setOpenOn(null)} /> : null}
    </>
  );
}
