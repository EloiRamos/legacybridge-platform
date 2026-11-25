# ADR-F11 — Evidence architecture: independent oracles, wire-tap and a determinism gate

- Status: Accepted
- Context: the application's own logs and audit trail are its *account* of what happened. A claim such as "at most one
  order request per approval" cannot be proven by the system that makes it.
- Decision: every acceptance claim about side effects is checked against sources **outside** the application:
  - a recording proxy between the app and HALDEN (`tools/wiretap/`, test profile only) that writes every request to a
    ledger **on receipt, before forwarding**, and can black-hole, corrupt or drop traffic deterministically;
  - an independent HALDEN client that reads orders directly (never through the wire-tap) at quiescence;
  - SKARVIK's own `received` and `orders` ledgers and the share listing (`inbox/`, `withdrawn/`);
  - a standalone audit verifier (`oracles/audit_verify.py`, standard library only, never imports `lbf`) that checks
    exports against their manifest, recomputes the hash chain, reports the first broken sequence number and
    reconstructs each operation's evidence chain (rules (a)–(d));
  - a comparison per correlation value of the application's `TRANSMISSION_STARTED` events with the wire-tap/SKARVIK
    counts (E-04).
  Expected outcomes for R01–R18 were written (`resilience/expected/`) before the behaviour, and are never
  edited to fit results. The runner normalizes ids and timestamps into `outcomes.tsv`; two consecutive full runs must
  be byte-identical.
- Consequences: test-only infrastructure (wire-tap, pause points, sim control) exists and must be absent from the
  default profile (SEC-14/15/17); every scenario starts from reset initial conditions; a harness defect can masquerade
  as a product defect (several were found and fixed and recorded with the failing check). The oracles
  are written by the same implementer as the system: independent review remains necessary.
