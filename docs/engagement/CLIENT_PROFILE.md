# Client profile — Brightwater Industrial Supply Group

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people, accounts, figures
> and pre-2026-11 dates are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. No real customer, deployment
> or outcome exists.

| Attribute | Value |
|---|---|
| Company | Brightwater Industrial Supply Group — industrial MRO distribution (bearings, hydraulics, valves, PPE, power tools, lubricants) |
| Headquarters | Düsseldorf (DE) |
| Business units | BU Central Europe (DC Düsseldorf `DUS`), BU UK & Ireland (DC Manchester `MAN`), BU Nordic (DC Gothenburg `GBG`), the last formed by acquiring Norrvik Industrial AB in 2023 |
| Order desk | One group desk in Düsseldorf for all three units; a Nordic satellite desk in Gothenburg |
| Systems of record | **HALDEN ERP 3.1** for Central Europe and UK & Ireland (customers, items, stock `DUS`/`MAN`, orders); **SKARVIK OES 4** for Nordic (customers `KND-nnnn`, stock `GBG`, orders `NV-nnnnnn`). Group finance is out of scope. |
| Integration constraints | HALDEN: HTTP only, frozen contract, no idempotency, one service login whose new ticket revokes the previous one, 12-character free-text reference. SKARVIK: no API; files on a share (`inbox/`, `outbox/acks/`, `outbox/extracts/`); batch pickup; ISO-8859-1 semicolon files; deduplication by message id for 72 h; stock and order status as periodic snapshots. Neither vendor can change its system. |

Every constraint above is implemented by a simulator and exercised by tests (`legacy-systems/`,
`backend/tests/contract/`).

## Personas

| Persona | Role | Responsibilities |
|---|---|---|
| Marta Kowalczyk, Operations Manager | `ops_manager` | order integrity; high-value and override approvals; escalations; SKARVIK redelivery/withdrawal |
| Daniel Okoye, Sales Operations Specialist | `sales_ops` | prepares orders; approves colleagues' standard orders |
| Sanna Lindqvist, Inventory Coordinator | `inventory_coordinator` | stock discrepancies across `DUS`/`MAN`/`GBG`; annotations |
| Rafael Duarte, IT Integration Engineer | `integration_engineer` | adapters, backend health, unmatched acknowledgements, read-only re-checks |
| Helena Brandt, Security & Compliance Reviewer | `compliance_reviewer` | audit export and verification, segregation of duties |
| Tomás Ibarra, System Administrator | `sysadmin` | users and roles (never own), deployment, backups; demo controls in `demo` only |

(A second sales operations account, `priya.sales`, exists so that four-eyes approvals can be demonstrated.)
