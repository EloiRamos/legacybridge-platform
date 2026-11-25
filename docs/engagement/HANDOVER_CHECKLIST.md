# Handover checklist

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people, accounts, figures
> and pre-2026-11 dates are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. No real customer, deployment
> or outcome exists.

| # | Item | Where | Done in this repository |
|---|---|---|---|
| 1 | Accounts and roles reviewed with the customer's administrator | `/admin`; [CLIENT_PROFILE](CLIENT_PROFILE.md) | personas seeded by `make demo-users`; review is a customer activity |
| 2 | Secrets generated on the target host, never copied | `make secrets`, `deploy/secrets/` (git-ignored) | yes (script) |
| 3 | Backup schedule agreed; restore rehearsed | [RECOVERY](../operations/RECOVERY.md); `make backup` / `make restore` | restore rehearsed ([verification](../evidence/VERIFICATION.md)); schedule is a customer decision |
| 4 | Audit head-hash retention procedure agreed with compliance | [RUNBOOK § Audit](../operations/RUNBOOK.md) | procedure written |
| 5 | Runbook walkthrough per persona | [TRAINING_GUIDE](TRAINING_GUIDE.md) | modules written |
| 6 | Acceptance report reviewed | [ACCEPTANCE_REPORT](ACCEPTANCE_REPORT.md) | accepted for release with limitations |
| 7 | Known limitations acknowledged | [KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md) | listed |
| 8 | Support boundary stated: the simulators are not the real HALDEN/SKARVIK | README, this document | stated |
| 9 | Open optional items listed | live behaviour beyond fixed scenarios, base-image advisories | listed (L-01, L-02) |
| 10 | Demonstration rehearsed on the target host | `make demo-up`, `make demo-check`, `make demo-down` | yes (isolated stack) |
