import type { JourneyStage } from "../evidence";

const SR: Record<JourneyStage["c"], string> = { done: "complete", cur: "in progress", unk: "outcome unknown", bad: "rejected", todo: "not reached" };

/** Five-step order journey. Stage text comes from evidence.journey(); nothing is inferred here. */
export function JourneyBar({ stages }: { stages: JourneyStage[] }) {
  return (
    <ol className="journey">
      {stages.map((s) => (
        <li key={s.t + s.c} className={`stage ${s.c}`}>
          <span className="bar" aria-hidden="true" />
          <b>{s.t}</b>
          <span>{s.s}<span className="vh"> ({SR[s.c]})</span></span>
        </li>
      ))}
    </ol>
  );
}

/** Compact five-segment journey used in the order preview. */
export function MiniJourney({ stages }: { stages: JourneyStage[] }) {
  return (
    <div className="mj" role="img" aria-label={`Journey: ${stages.map((s) => `${s.t} ${SR[s.c]}`).join(", ")}`}>
      {stages.map((s) => <span key={s.t + s.c} className={s.c} />)}
    </div>
  );
}
