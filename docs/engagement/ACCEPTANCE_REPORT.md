# Acceptance report

**Status: accepted by the project owner for release as a simulated portfolio engagement, with the limitations below.** The offline part was
reproduced in an independent review; the stack evidence was executed by the author and its records verified.

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people, accounts, figures
> and pre-2026-11 dates are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. No real customer, deployment
> or outcome exists.

| Area | Result (2026-11-25) | Executed by |
|---|---|---|
| Workflow, approvals, hand-off | Backend suite 1,578 passed, 3 opt-in skips | author; independently reproduced |
| Both simulators | Live conformance 32 of 32; end-to-end 23 of 23 in Chromium and in WebKit | author |
| Faults and recovery | 18 of 18 scenarios, two runs with identical normalized outcomes | author; exports replayed independently |
| API | 41 of 41 operations, no undocumented status, no schema error | author; contract equality reproduced |
| Assistant | Scripted safety 30 of 30; 13 live requests, 0 authority effects | author; scripted part reproduced |
| Operations | Backup and restore, outage, process loss, default-profile controls (isolated variant) | author |
| Demonstration and clean clone | 8 steps in three runs; clone ready in 19 s with cached layers | author |

Accepted as documented: Chromium and WebKit only; no screen-reader session; loopback only; residual image advisories; no genuine live model
refusal observed; the default three-command deployment not independently reproduced. Details: [VERIFICATION](../evidence/VERIFICATION.md),
[KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md). Earlier figures in older material belong to earlier versions. Sign-off by the fictional
customer is not part of this repository.
