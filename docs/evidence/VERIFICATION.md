# What was verified, how, and what was not

Fictional engagement; HALDEN and SKARVIK are simulators. "Measured" means observed in that simulated environment. The raw logs are not
part of the public repository; this page is their summary.

## Who executed what

| Class | Covers |
|---|---|
| **Independently reproduced on this product code** (AI-assisted review, separate from the implementation; offline, PostgreSQL 17.10, synthetic credentials) | Backend suite 1,578 passed, 3 opt-in skips, 32 live cases deselected, 0 non-loopback attempts; restart regressions 22 of 26 failing before, 0 after; wire-version regressions 10 of 10 failing before, 0 after; scripted evaluation 30/30; generated API contract equal; frozen imports 54 checks; lint, types, 57 console unit tests, build; two default-profile Compose configuration assertions |
| **Records verified, not executed again** | Standalone verification of 70 fault-campaign exports and one API export; byte identity of both outcome tables; hashes of the retained logs; API, restore, browser, live-model and dependency records |
| **Executed by the author only** | Fault campaign, live conformance, browsers, API walk, backup and restore, clean clone, demonstration, default-profile variant, scans, live model |
| **Independently executed on an earlier version only** | A full campaign, including the stack. Code changed since; it does not vouch for this version |

The literal default sequence (`make secrets`, `make demo-users`, `make up`) was not independently reproduced.

## The current version (author)

2026-11-25. Product code is that of this repository. Conformance, fault scenarios and end-to-end ran on the final images; live model runs,
and the mocked-browser suites ran on the same source before one library update in three images. Later changes are
documents, screenshots, comments, names of test files and the public project name; after them the author ran the offline suite again on a
clone of this repository (1,578 passed, 3 skipped; scripted evaluation 30/30; SKARVIK 28; console 57). No container was started for that. macOS on Apple silicon, isolated stacks.

| Check | Result |
|---|---|
| Restart of an authorization without a resolved end | 26 cases; 22 failed before the change, none after. Lost and killed processes, refused ledger writes, interrupted requests, version changes, six processes starting at once, ordinary completion |
| Backend suite without live simulators (PostgreSQL 17.10) | 1,578 passed, 3 skipped (opt-in), 0 non-loopback connection attempts |
| Live conformance against both simulators | 32 passed, with the application stopped (its poller would consume the acknowledgements; a first run that left it on failed three SKARVIK cases for that reason) |
| Fault scenarios R01–R18 | 18/18 in **two** consecutive runs; normalized outcomes byte-identical to each other and to earlier runs; 35 of 35 audit exports valid per run, checked again with the standalone verifier; application account equal to the independent ledgers in all 131 compared rows of each run, never more than one hand-off per row. |
| Console: unit; mocked API in Chromium, WebKit, Firefox | 57; 124, 124 and 122 of 124, each with 4 opt-in skips. The two Firefox failures are a segmentation fault of that browser build's content process while axe runs |
| End-to-end on the stack, including the axe and keyboard checks | Chromium 23 passed, WebKit 23 passed, each with 1 opt-in skip. Firefox: 17 passed, 2 failed by browser crashes, 4 not run; a second run was cut off when the host disk filled up. Firefox is not claimed |
| Assistant wire versions | 10 cases: a strict client of the original contract, sending no header, decodes every turn and is told of every stored, unverified, unknown or proposed draft |
| Backup, restore, outage, process loss | Counts and migration equal, the audit event at the backup's head identical, three reconciliation runs after the restore; 503 during the outage, ready after it; the daemon restarted the database and the application recovered |
| Demonstration | 8 steps held in three consecutive runs |
| `make verify` | 15 of 15; later changes as described above |
| Start from a fresh clone | Ready in 19 s **with cached image layers** through `make demo-up`; 8 demonstration steps held, static checks passed, everything removed. Other runs while documents changed took up to 31 s; an earlier version recorded 16.6 s through the default commands. No uncached first build was timed |
| Dependencies | pip-audit 2.9.0 and npm audit on 2026-11-25: none reported. Final images (Trivy 0.69.3, database 2026-11-25): application, migration and wire-tap 0 critical, 53 high, 0 with a fix, after `libzstd1` (CVE-2026-124851) was updated; PostgreSQL 2 critical (`libxml2` without a fix, Go standard library in `gosu`) and 99 high; SKARVIK 2 critical without a fix and 100 high; HALDEN 2 high in operating-system packages |
| API | 41 of 41 operations with a success by direct HTTP, 199 observations, 0 schema errors, 0 undocumented statuses |
| Default profile, as a variant of the isolated stack | Only the application port published; HALDEN control 404, SKARVIK control closed, demo routes 404 and absent from the contract |

Not repeated: the HALDEN Maven suite (frozen bytes).

## Three tiers

| Tier | Who can run it | Covers |
|---|---|---|
| Public: `make verify` | Anyone with a Git checkout, Docker, `uv` 0.13.4+, Node 24 and network access for public packages; no private material, no paid call | Links, claims, frozen imports, lint, types, contract drift, console unit tests and build, backend suite on an isolated database, SKARVIK suite, scripted assistant evaluation, and the demonstration checked against the simulators |
| Author's campaign | The author | Adds live conformance, end-to-end and mocked-browser suites, 18 fault scenarios, backup and restore, outage recovery, default-profile hardening, dependency audit |
| Live model | The owner of an API key, per authorized run | The assistant with a real model. Never started by `make verify` or by the tests |

## Assistant

| Evidence class | What it establishes |
|---|---|
| Deterministic and scripted | Allowlist, permissions, intent gate, receipts, grounding, budgets, injection. 53 reproduction cases: 43 failed on the code before, none now |
| Mocked provider | Every tool, retries, limits, timeouts, malformed output; the real console with a mocked API |
| Live, this version | `gpt-6-luna`, 13 requests, below. Production API, session, service and provider in an evaluation process with fictional data; effects checked by SQL |
| Live, previous version | `gpt-6-luna`, 104 requests, below |
| Live, earlier | Below. Real OpenAI Responses API, `gpt-5.4-mini-2026-03-17`, the production provider, service and console in an evaluation process with fictional data |

### This version, `gpt-6-luna`, 2026-11-25

Each run had its own ceilings for requests, money and time. All 13 responses reported `gpt-6-luna`.

| Run | What it shows | Requests | Settled locally, USD |
|---|---|---:|---:|
| Truncated answer (output cap 256) | An incomplete answer reports its model; it is not continued or shown | 1 | 0.001288 |
| Screenshot journeys in the real console | Grounded lookup, drafted order, proposal and its confirmation, request to approve. 2 drafts stored, both receipts joined to version and audit event by SQL; 0 approvals, authorizations, transmissions, escalations | 8 | 0.003463 |
| Two-turn browser validation | A grounded answer and a follow-up in the same session, shown as live, with the workflow unchanged | 4 | 0.001094 |

Together with the previous version: 117 requests, USD 0.044124 settled at bounding prices (0.55 / 0.022 / 1.10 per million tokens),
about USD 0.016 at list price (0.10 / 0.01 / 0.50). Local estimates of inference only; the provider's invoice is authoritative. A genuine
refusal by the model has not been observed live and is not provoked.

### Previous version, `gpt-6-luna`, 2026-11-24

A smoke (1 trial), one campaign (28 fixed scenarios, 38 trials) and two two-turn browser journeys, under one shared ceiling of
USD 0.50. Every response reported `gpt-6-luna`. The campaign ran once; it stops at the first write that a scenario does not allow.

| | Result |
|---|---|
| Unintended writes; approvals, authorizations, transmissions, escalations | 0; 0 |
| Tools executed | All nine |
| Drafts and revisions requested / stored exactly / receipts confirmed by SQL | 4 / 4 / 4; purchase order kept through a revision; one of them after a confirmation |
| Trials that must not write (quoted, historical, conditional, hypothetical, two customers, other language, invalid quantity, packs, stale version, role, approval, injected note) | 24; 0 writes. The model proposed a drafting call for a quoted example once and for packs once; the gate refused both. The role was refused |
| Answers meeting every wording check | 35 of 38. Not: one turn reached its request limit, one ungrounded answer was replaced by system values, one answer omitted the item code |
| Browser journeys (two different builds) | 2 of 2. The first showed a wrong *simulated* label; only the second shows the corrected *live* label |
| Requests, tokens | 104; USD 0.038 at deliberately dear bounding prices, about 0.014 at list price (estimates) |

Small, fixed sample: most scenarios ran once. It is not comparable with the earlier series, which used another model and code.

### Earlier code, other models

Four campaigns on `gpt-5.4-mini-2026-03-17` (132 trials of 33 fixed cases each, plus regression cases). Every campaign is reported; each
followed a code or harness change. They say nothing about the current model.

| | Before | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| Strict tasks, original rubric (of 132) | 70 | 92 | 99 | 100 | 99 |
| Strict tasks, corrected rubric | not scored | 119 | 125 | 126 | 121 |
| Unintended drafts or versions | 3 | 0 | 0 | 0 | 0 |
| Approvals, authorizations, transmissions, escalations | 0 | 0 | 0 | 0 | 0 |
| Shown facts equal to the cited field | not guaranteed | 315/315 | 350/350 | 349/349 | 359/359 |

A separate independent live run of that version stored **one draft that nobody asked for** (a quoted training example) and stopped. Both
that defect and a receipt defect found with it are corrected. On `gpt-6.1-terra`, a three-request smoke failed and a later two-turn run passed.

## Not verified

Screen readers, branded Safari, physical and touch devices; genuine model refusals; other platforms; other models; real legacy systems; TLS, SSO, several hosts;
load; image redistribution notices. The scenarios are fixed and fictional, and the simulators were written by the
author of the adapters.
