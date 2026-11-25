# Training guide

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people, accounts, figures
> and pre-2026-11 dates are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. No real customer, deployment
> or outcome exists.

One task-based module per persona, each ending with an exercise in the `demo` profile
(`LBF_PROFILE=demo LBF_SIM_CONTROL=true LBF_TRANSPORT_TIMEOUT_SEC=1 docker compose up -d --build --wait`). Faults are armed by the system
administrator on `/admin` (demo controls). Accounts: `deploy/secrets/demo-credentials.txt`.

| Module | Persona | Learn | Exercise |
|---|---|---|---|
| 1 Order preparation | Sales operations (`daniel.sales`) | Search both ERPs without merging accounts; read availability with its freshness; prepare and submit a draft | Draft 10 × BW-1001 for Nordkap (HALDEN 00417), submit, and observe that you cannot approve it yourself |
| 2 Four-eyes approval | Sales operations (`priya.sales`), operations manager (`marta.ops`) | Approve exactly the version and hash shown; value threshold €2,500 | Approve Daniel's draft as Priya; prepare one above €2,500 and see that only the operations manager may approve it |
| 3 Working uncertainty | Operations manager | Never re-enter an `UNKNOWN` order; read-only re-check; escalation; verified vs attested resolution | Sysadmin arms `halden_commit_then_500` + `halden_listing_500x3`; approve an order; watch it become *Outcome unknown*, then resolve it by a verified read of the HALDEN order number |
| 4 SKARVIK acknowledgements | Operations manager | Deadline, withdraw before pickup, one redelivery inside the dedup window | Sysadmin arms `skarvik_pause_pickup`; approve a SKARVIK order; after the deadline withdraw the unclaimed file and see *Not applied (proven)* |
| 5 Inventory discrepancies | Inventory coordinator (`sanna.inventory`) | Per-backend figures, snapshot age, raw rows; annotations | Open the raw rows of BW-1001 in both systems and record the discrepancy with *Annotate* |
| 6 Integration health | Integration engineer (`rafael.integration`) | `/readyz`, backend health, unmatched acknowledgements, `/metrics` | Sysadmin arms `skarvik_malformed_ack`; find the entry in the unmatched-acknowledgement queue |
| 7 Audit | Compliance reviewer (`helena.compliance`) | Verify the chain; export; standalone verification | Export the audit on `/audit` (*Export audit*, both files) and run `python3 oracles/audit_verify.py` on it |
| 8 Administration | System administrator (`tomas.admin`) | Users and roles (never your own), backups, demo controls | Create a user and grant a role on `/admin`, try to grant yourself a role (refused), run `make backup` |
