# Discovery notes (fictional workshops)

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people, accounts, figures
> and pre-2026-11 dates are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. No real customer, deployment
> or outcome exists.

Each finding is tied to the simulator behaviour that reproduces it and to the requirement it produced
([REQUIREMENTS.md](REQUIREMENTS.md)).

| # | Finding at the workshop | Reproduced by | Became |
|---|---|---|---|
| D-1 | "Nordkap Marine is 00417 in HALDEN and KND-0081 in SKARVIK; we have two Alder & Finch accounts and one is on hold." | HALDEN seed, SKARVIK seed, curated crosswalk | P1; search never merges; ambiguity groups (BR-01) |
| D-2 | "HALDEN's available flag says yes while the balance is zero; SKARVIK counts cartons and its numbers are hours old." | HALDEN seed anomalies; SKARVIK fixed-width extract with UoM and timestamp | P2; per-backend availability with freshness, raw rows visible (BR-02) |
| D-3 | "When HALDEN times out we don't know whether the order went in; people re-key it and sometimes we ship twice." | HALDEN `delay` and `commit_then_500` faults | P7; `UNKNOWN` + read-only reconciliation, never resend (R03, R04) |
| D-4 | "SKARVIK sometimes never sends the acknowledgement file; the Nordic desk re-sends the order." | SKARVIK `drop_ack`, `ack_delay`; dedup window | P7; redelivery only of the identical message inside the window (signature 2b, CF-13) |
| D-5 | "Vantaa Cold Storage is on hold in HALDEN but not in SKARVIK." | Seeds (`04450` flag `Y`, `KND-0114` clear) | P5; cross-backend hold conflict warning and policy |
| D-6 | "Anything over €2,500 must be signed off by operations; nobody approves their own order." | — (policy) | P6; four-eyes and value threshold, server-enforced (SEC-07, SEC-09) |
| D-7 | "After the last server restart an order was half-sent and we lost track of it." | Pause points + process kill | P8; transmission row before handoff, sweeper (R11, R12) |
| D-8 | "Audit asks who authorized what and we can't answer." | — | P9; hash-chained audit, export, standalone verifier (E-01…E-04) |
| D-9 | "HALDEN throws us out every few minutes and SKARVIK rejects files that got cut off." | HALDEN 429/440 and ticket TTL; SKARVIK trailer checksum, `E-STRUCT` | P10; refusal classes, single ticket owner (R06, ADR-F04) |
| D-10 | "Could an assistant prepare the routine orders?" | — | Optional read/draft assistant; humans submit and approve (ADR-F08) |
