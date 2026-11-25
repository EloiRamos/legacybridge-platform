import type { Knowns } from "../evidence";
import { hms, iso } from "../format";
import { Icon } from "./Icon";

/** OBSERVED / NOT KNOWN (Concept C borrowing) — rendered only for UNKNOWN and Needs decision, directly below the
 * journey. OBSERVED lists recorded events with their audit sequence; NOT KNOWN lists what no evidence establishes;
 * rules and schedules are labelled as such, never as observations. */
export function KnownPanel({ k, latest, tight }: { k: Knowns; latest?: number; tight?: boolean }) {
  const obs = latest ? k.observed.slice(-latest) : k.observed;
  const nk = latest ? k.notKnown.slice(0, 1) : k.notKnown;
  return (
    <section className="known" aria-label="What is observed and what is not known">
      <div className={`kn obs${tight ? " tight" : ""}`}>
        <div className="hd"><b>OBSERVED</b><span>{latest ? "latest recorded events" : "recorded events and system responses"}</span></div>
        {obs.length ? (
          <ul>{obs.map((x) => (
            <li key={x.seq}><Icon name="eye" /><span>{x.text} <time className="src" dateTime={iso(x.at)} title={iso(x.at)}>{hms(x.at)} · #{x.seq}</time></span></li>
          ))}</ul>
        ) : <p className="muted small" style={{ margin: 0 }}>No event of this attempt has been recorded yet.</p>}
      </div>
      <div className={`kn nk${tight ? " tight" : ""}`}>
        <div className="hd"><b>NOT KNOWN</b>{latest ? null : <span>cannot be established yet</span>}</div>
        <ul>{nk.map((x) => <li key={x}><Icon name="help" /><span>{x}</span></li>)}</ul>
        {latest ? null : k.rules.map((r) => <div className="rule" key={r}>{r}</div>)}
      </div>
    </section>
  );
}
