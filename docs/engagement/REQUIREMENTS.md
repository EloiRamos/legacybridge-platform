# Requirements — Brightwater Industrial Supply Group order operations

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people and all
> business figures are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are purpose-built simulators. No real
> customer, deployment or outcome exists. Requirements were fixed before implementation.

## Business problems (P1–P10)

P1 fragmented customer information · P2 inventory visibility · P3 two incompatible legacy integration styles ·
P4 manual order re-keying · P5 inconsistent data semantics · P6 approval requirements (four-eyes, €2,500 threshold) ·
P7 uncertain transaction outcomes · P8 operational recovery after restart · P9 auditability · P10 system reliability
(throttling, ticket expiry, truncated files). Each maps to a simulator behaviour that the tests exercise.

## Business requirements [F]

| ID | Requirement | Measurable acceptance criterion |
|---|---|---|
| BR-01 | Unified customer lookup across both ERPs; never auto-merge ambiguous records | Search "alder" returns HALDEN `10233` and `10871` as separate accounts with an ambiguity flag and `10871` hold badge; search "nordkap" returns HALDEN `00417` and SKARVIK `KND-0081` grouped under one party **only** because the curated crosswalk links them; a party is never created from name similarity |
| BR-02 | Inventory visibility with explicit uncertainty | For every seeded anomaly (HALDEN flag mismatch, duplicate `BW-1009`, `?`, `1.200`, `-3`; SKARVIK stale snapshot, missing crosswalk factor, pack-unit conversion) the availability view shows the expected status (`IN_STOCK`/`OUT_OF_STOCK`/`UNDETERMINED`/`STALE`) and an explanation; expected table in `acceptance/fixtures/inventory_expectations.yaml` matches 100% |
| BR-03 | Controlled order entry with segregation of duties | 0 approvals where approver = preparer; 0 approvals of a version other than the current version; orders ≥ €2,500 approved only by `ops_manager` (negative tests) |
| BR-04 | No duplicate booking caused by the console under the defined fault model | Across R01–R18, oracle-observed handoffs per authorization ≤ 1; automatic resends = 0; HALDEN orders per correlation value ≤ 1 in every scenario not deliberately injecting an external duplicate |
| BR-05 | Uncertain outcomes are visible and worked, not hidden | Every operation in `UNKNOWN` appears in the reconciliation queue within 5 s; automatic read-only re-checks follow the schedule; an unresolved `UNKNOWN` becomes `ESCALATED` within 30 min (test profile: scaled by `LBF_TIME_SCALE` = 0.02, i.e. 36 s) |
| BR-06 | Recovery after application restart | After `docker kill -s KILL` of the app at each defined kill point, restart yields 0 lost operations, 0 additional handoffs, and every interrupted transmission in `UNKNOWN` or a positively evidenced state |
| BR-07 | Audit reconstruction | For any operation, the compliance export reconstructs request → normalized intent → versions → authorization → handoff(s) → observations → reconciliation reads → final outcome; the standalone verifier reports `VALID` for an untampered export and the exact first broken sequence number for a tampered one |
| BR-08 | One workflow for both ERPs | The same state machine and conformance suite pass for HALDEN and SKARVIK; differences are only those declared in capabilities and visible in the UI |
| BR-09 | Assistant helps but cannot act | 0 approvals, transmissions, redeliveries, withdrawals or resolutions attributable to the assistant across all tests and evaluations; prompt-injection fixtures produce 0 executed non-allowlisted tool calls |
| BR-10 | Operability | Clean checkout to running console in ≤ 3 documented commands; `/readyz` reflects DB and backend health; runbooks cover every escalation and recovery path |

## Functional technical requirements

| ID | Requirement |
|---|---|
| TR-01 | Framework-neutral canonical domain model (plain Pydantic models and enums in `lbf.domain`; no FastAPI, SQLAlchemy or SDK imports) |
| TR-02 | One `LegacyBackendPort` protocol; workflow code MUST NOT branch on backend identity (static check) |
| TR-03 | Capability declaration per backend drives permitted actions |
| TR-04 | HALDEN adapter reuses v1 `bridge/` modules byte-identically (import manifest) |
| TR-05 | SKARVIK simulator is an independent program with its own contract document and freeze manifest; no shared code with `lbf` |
| TR-06 | Durable state in PostgreSQL with Alembic migrations; append-only tables enforced by privileges and triggers |
| TR-07 | Server-side authentication (local accounts, Argon2id), opaque server-side sessions, CSRF protection, RBAC with a static versioned policy |
| TR-08 | Version-bound approvals with four-eyes and value thresholds |
| TR-09 | Transmission row committed before every transport handoff; `UNIQUE(authorization_id)` |
| TR-10 | Restart recovery sweeper; singleton engine lease |
| TR-11 | Capability-driven reconciliation, automatic read-only re-check schedule, escalation, manual resolution with verified/attested distinction |
| TR-12 | Hash-chained audit events written in the same transaction as state changes; JSONL export + manifest; standalone verifier |
| TR-13 | Bounded assistant: framework-neutral provider interface, OpenAI provider (official SDK, Responses API, exclusive live provider), scripted provider, deterministic fallback, permission-checked read/draft tools, structured answers, grounding check, inference cost governance and telemetry |
| TR-14 | Evaluation harness with deterministic fixtures and independent oracles; offline OpenAI adapter contract tests; live smoke and live runs gated by double opt-in, owner authorization file and deterministic budgets; no live dependency in standard validation |
| TR-15 | React + TypeScript console using a client generated from the OpenAPI document; no business state in the browser |
| TR-16 | Deterministic fault injection (HALDEN `/_sim`, SKARVIK control port, wire-tap proxy modes, process-kill harness, pause points in the `test` profile) |
| TR-17 | Independent oracles package that never imports `lbf` |

## Non-functional requirements

| ID | Requirement | Bound |
|---|---|---|
| NFR-01 | Reproducibility | Exact pins: `uv.lock`, `package-lock.json`, image digests recorded; `make up` from a clean clone |
| NFR-02 | Determinism | Resilience suite run twice with the same seed produces identical normalized outcome tables |
| NFR-03 | Local-only exposure | All published ports bind `127.0.0.1`; internal services not published except in the `test` profile |
| NFR-04 | Latency (indicative, not an SLA) | Read endpoints p95 < 1.5 s against simulators on the reference machine; recorded, not guaranteed |
| NFR-05 | Accessibility | No `serious`/`critical` axe-core violations on the 10 main routes; full keyboard path for approve/decline |
| NFR-06 | Supportability | Structured JSON logs with `operation_id`, `correlation_value`, `transmission_id`, `request_id` |
| NFR-07 | Security hygiene | No secrets in git; `.env` ignored; dependency audit (pip-audit, npm audit) with findings triaged |
| NFR-08 | Portability | Reference platform macOS arm64 + Docker; other platforms untested and stated as such |

Traceability from each requirement to tests and independent oracles: `docs/engagement/ACCEPTANCE_PLAN.md`
