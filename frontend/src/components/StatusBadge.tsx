import { type OperationState, shortLabel, statusLabel, type Tone } from "../status";
import { Icon } from "./Icon";

/** Operational status badge. Labels and tones come only from status.ts. UNKNOWN is rust, never red. */
export function StatusBadge({ state, evidence, backend, short }: { state: OperationState; evidence?: string | null; backend?: string; short?: boolean }) {
  const spec = statusLabel(state, evidence, backend);
  const label = short ? shortLabel(state, evidence, backend) : spec.label;
  const icon = spec.tone === "warn" ? "help" : spec.tone === "warn-outline" ? "alert" : null;
  return (
    <span className={`bd t-${spec.tone}${icon ? " has-i" : ""}`}>
      {icon ? <Icon name={icon} /> : null}{label}
    </span>
  );
}

export function ToneBadge({ tone, children, plain }: { tone: Tone; children: React.ReactNode; plain?: boolean }) {
  return <span className={`bd t-${tone}${plain ? " plain" : ""}`}>{children}</span>;
}

export const Sys = ({ backend }: { backend: string }) => <span className="sys">{backend.toUpperCase()}</span>;
