# Case study — one controlled order workflow over two incompatible legacy systems

> **Fictional and simulated.** The client, its people and history are invented; HALDEN ERP 3.1 and SKARVIK OES 4 are
> simulators written for this project. [Who executed which check](../evidence/VERIFICATION.md#who-executed-what).

## Problem (fictional)

Brightwater Industrial Supply Group's order desk serves three business units on two systems it cannot change. HALDEN
takes orders over HTTP, has no idempotency, and sometimes commits an order after the caller has given up. SKARVIK has
no API: orders are files on a share, picked up in batches, acknowledged later or not at all, and deduplicated only
within a time window. Staff re-keyed orders, re-sent them when unsure, and could not reconstruct who authorized what.

## From a workshop sentence to a tested control

| Discovery | Requirement | Control | How it was shown |
|---|---|---|---|
| D-3 "we don't know whether the order went in" | Outcomes are never guessed | Transmission recorded first, one hand-off, `UNKNOWN`, read-only reconciliation | HALDEN late commit: one hand-off, one order in HALDEN's records |
| D-4 "the Nordic desk re-sends" | A duplicate needs a decision | One authorized redelivery of the same bytes inside the window | Two claims, one order in SKARVIK's records |
| D-5 "on hold here, not there" | Conflicts stay visible | Accounts never merged; linked holds enter the approval policy | Approval needs an audited override |
| D-6 "nobody approves their own order" | Four eyes on the exact content | Approval binds version and hash; segregation of duties in the transaction | Own approval 403, wrong hash 409, 50-round races: one approval |
| D-8 "who authorized what" | Evidence without trusting the application | Hash chain, export, standalone verifier | Edited row located, truncated export rejected |

## Constraints (simulator behaviour)

Every constraint in the story is implemented by a simulator and exercised by a test: HALDEN's `delay`,
`commit_then_500`, ticket expiry (440) and rate limit (429); SKARVIK's late, lost, malformed and contradicting
acknowledgements, paused pickup, stale extracts and the dedup window. The SKARVIK contract was frozen
before its adapter was written.

## Decisions

| Decision | Why | Record |
|---|---|---|
| At most one handoff per authorization; never resend | A non-idempotent write cannot be repeated safely | G1/G2, ADR-F05 |
| Record the transmission before the handoff | A crash must leave something to reconcile | G4, ADR-F04 |
| `NOT_APPLIED` only with positive evidence | A missing HALDEN order is not proof | G5 |
| One port with declared capabilities, no backend branching in the workflow | Two styles, one workflow | ADR-F06, boundary check |
| Approvals bound to version and content hash | Approve exactly what was reviewed | G3, ADR-F07 |
| One process, singleton engine lease | HALDEN allows one service ticket | ADR-F04 |
| Hash-chained audit + standalone verifier | Compliance needs an independent check | G6, ADR-F11 |
| Optional assistant with read/draft tools only | Help without authority | G7, ADR-F08, ADR-F10 |

## Failure modes and what the tests showed

- **Late commit, lost request, corrupted response, 500 after commit** (R02–R05): one request on the wire, outcomes
  reconciled by reading, no resend. R01–R18 passed twice with byte-identical normalized outcomes.
- **Process kill around the handoff** (R11, R12): `UNKNOWN`, never lost, never resent; SKARVIK's unpublished staging
  file proves non-delivery.
- **Races** (R07, C-01…C-10): 50 rounds of simultaneous approvals in threads and in two separate processes — one
  approval and one authorization per round.
- **Injection through backend data** (R17): the scripted model followed the injected instruction; every attempted
  non-allowlisted tool call was refused and audited; no state changed.
- **Audit tampering** (R18): a superuser's edit of one row was located at its sequence number; a truncated export was
  rejected; an audit write failure rolled back the approval.

## What the tests and reviews found

Each was fixed with a failing check first.

### 1. "Minus three" became a draft of three

- **Observed.** In three of four live trials, a request for *minus three units* produced a valid draft of three.
- **Why it mattered.** The argument schema (1 to 999) was the control, and it passed: the model had silently corrected the user.
- **Investigation.** Request, tool arguments and stored draft were compared side by side; every layer had behaved as specified. The check was in the wrong place: it validated the *call*, not the *intent*.
- **Decision.** A prompt is not an enforcement point, and a list of forbidden words fails open. The application now accepts a small explicit grammar and treats everything else as unconfirmed; invalid quantities are refused outright. Confirming every draft was rejected as friction for requests that are already explicit.
- **What the first fix missed.** An independent live audit asked to have a *quoted* training example explained. Every argument was in the text, so the gate called them established and a draft was stored. It had checked where the values came from, and never whether anyone was giving an order.
- **Second correction.** The whole message must now be a drafting command, with every word accounted for; a stated version or a line that would disappear counts too. Order notation without a verb, accepted before, now needs a confirmation: less convenient, and deliberate.
- **Evidence.** [First](../../backend/tests/assistant/test_intent_grounding_reproduction.py) and [second reproduction](../../backend/tests/assistant/test_write_integrity_reproduction.py) (each fails on the code before it) and the [boundary tests](../../backend/tests/assistant/test_write_integrity.py), all asserting database state; 68 live invalid-quantity trials on an earlier build with `gpt-5.4-mini-2026-03-17`, 0 drafts. On the build of 2026-11-24 with `gpt-6-luna`, 4 live trials of the quoted example and 20 further trials that must not write: 0 drafts. **Remaining:** the grammar is English and narrow, and the live sample is small.

### 2. A draft was stored and the screen did not say so

- **Observed.** When the model's final answer failed its checks, the fallback dropped the link to a draft that was already committed.
- **Why it mattered.** An operator who sees nothing asks again, which is how duplicates start.
- **Root cause.** The only record of the write on screen was a field of the model's answer.
- **Correction.** The application builds a receipt itself and reads it back from the database: version, content hash, audit event. An outcome it cannot observe is shown as unknown, never as success or nothing.
- **What the first fix missed.** The receipt looked up the *newest* event of the draft. With a revision committed in between, version 1 was shown with the event of version 2; and a write that could not be read back still got the green banner. Confirmed line changes also silently cleared the customer PO.
- **Second correction.** The transaction hands over the identity of the event it appended; operation, version, hash and event must all agree, or the receipt says *not verified*. A line change keeps every other field, and the proposal lists the complete change.
- **Evidence.** The [receipt tests](../../backend/tests/assistant/test_receipt_integrity.py) follow a write with nine kinds of failure; the [binding tests](../../backend/tests/assistant/test_write_integrity.py) force the interleaving, race concurrent writers and mismatch each part in turn.

### 3. A citation was mistaken for proof

- **Observed.** "Quantity is 10" passed against a field holding 100; "not on hold" passed against `HOLD`. At the same time correct answers were rejected because an order number's digits were not in the cited field.
- **Root cause.** Numbers were compared as substrings, and a path that resolved was enough.
- **Decision.** Stop parsing prose for critical values: the model names a field and copies its value, the application compares and renders.
- **Evidence.** 43 of 53 reproduction cases failed before the change, none after; strict live tasks (`gpt-5.4-mini-2026-03-17`) rose from 70 to 99 of 132 without changing the rubric. **Remaining:** free text is checked only in part, and labelled; a later audit still found a number attached to the wrong account in a summary, which is now rejected when the account, item or unit is named.

### 4. A scheduled read could overtake an authorized action

- **Observed.** On SKARVIK, a timed re-check could slip between an operator's authorized withdrawal or redelivery and its execution.
- **Root cause.** The reconciler looked at the order's state but not at open authorizations.
- **Correction.** Authorized work takes precedence; re-checks and timed escalation wait, and a stale withdrawal claim is not projected.
- **Evidence.** The contested order of events is [forced in tests](../../backend/tests/workflow/test_withdraw_reconcile_interleavings.py); the demonstration redelivers with two claims and one order. **Remaining:** a command that arrives during a re-check is refused and has to be repeated.

### 5. The demonstration only worked by luck

- **Observed.** The documented walkthrough promised an unknown outcome; replayed as written, the order was simply confirmed.
- **Root cause.** The fault committed after 2.5 seconds, but the documented profile waited five.
- **Correction.** The [demonstration stack](../../deploy/compose.isolated.yaml) fixes every timing the story depends on, and [`demo-check`](../../tools/demo_check.py) asserts each step with the simulators' own records. It held in 5 consecutive runs.

### 6. The last check found what the earlier ones had skipped

- **Observed.** The final end-to-end runs failed in four places that every unit suite had passed: the public demonstration crashed, a paid run was labelled *simulated* on screen, the application restarted hundreds of times after a Docker restart, and the fault campaign reported a disagreement with the independent ledgers.
- **Why it mattered.** An earlier change had left empty fields out of an API response and was tested against the console, not against the project's own demonstration script. A change is not verified until everything that consumes it has run.
- **Investigation.** Each was traced before anything was changed: the script indexed a field that is now optional; a wrapper hid the provider's own transport label; the database was the only service without a restart policy; and the campaign's comparison read another stack's directory (the product's account was right).
- **Correction.** All four, each with a test or a check that fails without it. The fault campaign, the browser suite, backup and restore can now run on a disposable stack that proves it owns what it stops and deletes.
- **Evidence.** [`demo-check`](../../tools/demo_check.py) holds again in three consecutive runs; [deployment](../../backend/tests/unit/test_deployment_hygiene.py), [ownership](../../backend/tests/security/test_docker_ownership.py) and [campaign](../../backend/tests/assistant/test_live_campaign.py) tests; 18 of 18 fault scenarios with outcomes byte-identical to the earlier accepted run.

### 7. A stop that a restart could undo

- **Observed.** An independent review of the previous version found it by reading: after a model answered under an unexpected name, the paid test run stopped, but the stop was one row in a ledger. If the process died before that row was written, or the write failed, the same authorization could be started again and send more requests.
- **Why it mattered.** "Ends the authorization" was a promise about money and trust that held only when nothing else went wrong.
- **Investigation.** Every path was replayed offline with a transport that counts requests: killed processes, refused writes, interrupted requests, two processes starting at once. An existing test even *asserted* that a crashed run could continue. The same check-then-start race was found in the three campaign runners.
- **Decision.** Stop inferring from what was recorded and require proof instead: a run continues only from a *resolved end*. Reserving nothing more after any unexplained gap costs an authorization now and then; the owner can issue a new one.
- **Correction.** Admission is one transaction. An unfinished invocation, or a request without a recorded outcome, refuses the next start, and an end is written only when every request is accounted for. Ordinary completion still leaves the rest of an authorization usable.
- **Evidence.** [26 cases](../../backend/tests/assistant/test_authorization_restart.py): 22 failed before, none after. **Remaining:** the ledger is a local file; whoever can replace it can defeat it.

### 8. "Not tested in other browsers" was hiding a defect

- **Observed.** The console's browser checks had only ever run in Chromium; Firefox and WebKit did not start, and the limitation list said so. With current builds and an isolated profile directory both did, and WebKit failed 13 of 124 checks.
- **Root cause.** Safari does not focus a button that is clicked, so closing a dialog lost the place of a keyboard user; it also ignores a margin that keeps a focused field clear of the phone's action bar.
- **Correction.** The console remembers the control that was used, asks for the margin explicitly and scrolls wide tables by key in every browser.
- **Evidence.** 124 of 124 in Chromium and WebKit. **Remaining:** in Firefox 122 of 124; in two pages the browser's own process crashes (a segmentation fault in this build) while the accessibility checker runs.

### 9. A better answer broke the older client

- **Observed.** A strict client of the original assistant response could not decode a turn once receipts and proposals were added.
- **Root cause.** The fields were considered additive because the project's own console accepted them.
- **Correction.** The original eight fields are the default and the notice tells the person what was stored or proposed; a header opts in to the typed fields.
- **Evidence.** [10 cases](../../backend/tests/assistant/test_wire_versions.py): all failed before, none now.

### 10. Validation filled the disk and stopped Docker

- **Observed.** During a heavy validation run the host disk filled. Docker stopped, interrupting a protected HALDEN installation on the same machine.
- **Root cause.** Confirmed: no effective storage watchdog, and a thin virtual disk that reports space the host does not have. Unknown: what used the last 2.5 GiB.
- **Correction.** HALDEN's database was saved first, then HALDEN was recovered under its original container identity. Later runs needed 10 GiB free and a watchdog that stops its own work below 6 GiB.
- **Evidence.** Integrity check and five of five tables equal to the saved copy; the watchdog's stop path was tested. **Remaining:** other project volumes were checked for presence, not for content; a watchdog cannot rule out every storage incident.

### Others, in brief

| Found | Cause | Change | Regression |
|---|---|---|---|
| A corrupted backend response could lose an observation | Text containing NUL cannot be stored in PostgreSQL | Control characters are escaped before storage | [test_untrusted_text.py](../../backend/tests/unit/test_untrusted_text.py) |
| Concurrent creates with one key returned 500 to the loser | The expected unique violation was not recovered | The losing request returns the winning operation | [test_create_races_and_outage.py](../../backend/tests/api/test_create_races_and_outage.py) |
| Losing the database was a generic 500 | Connection loss was not classified | 503 whose text never says whether an uncertain COMMIT was applied | the same file |
| The console said "Nothing was changed" after a command whose answer was lost | One error text served reads and commands | Only a structured refusal counts as refused; otherwise the outcome is unknown | [outcome.test.ts](../../frontend/src/outcome.test.ts) |
| A refused role change showed a missing-page message | One error view treated every 403 alike; mocks had missed it, a live test had not | Refused commands keep the server's reason | [console_functions.spec.ts](../../frontend/tests/e2e/journeys/console_functions.spec.ts) |
| A HALDEN response could be read without limit | No size cap | 1 MiB; an oversized answer after a hand-off is *uncertain* | [test_halden_transport_size.py](../../backend/tests/unit/test_halden_transport_size.py) |
| The first live assistant smoke failed and hid why | A rejected answer was reported as a request limit | The rejection reason is kept; results carry the id to cite | [test_smoke_rejection_regression.py](../../backend/tests/assistant/test_smoke_rejection_regression.py) |
| The unknown-order banner said "only read-only checks" next to a redelivery button | One sentence served two systems | Guidance by backend capability and by the actions permitted to this person | [unresolved.test.ts](../../frontend/src/unresolved.test.ts) |
| The published API contract omitted authentication and every error but two | Generator defaults | Declared from the routes' own dependencies; every response in the API suites is checked against it | [test_published_contract.py](../../backend/tests/api/test_published_contract.py) |
| The trace marked a failed tool call as successful | Permitted and succeeded were one flag | Separate outcomes: refused, failed, unknown, succeeded | [assistant.test.ts](../../frontend/src/assistant.test.ts) |

## The assistant: the trade-off

The tempting fix for "minus three" was a better prompt, and for the citations a second model as judge. Neither is a control.
The chosen design moves three decisions out of the model: *whether* to write (a command that the whole message must be, or the
person's confirmation), *what was written* (read back from the database with its own audit event) and *what a fact says* (rendered from the record). The price is a narrower
assistant: it asks more often, and its free text is openly labelled as its own wording. Live results, including the
trials that failed: [verification](../evidence/VERIFICATION.md#assistant).

## Review

Independent review found most of what is above, and each time the author's own checks had passed. Earlier versions were sent back; the last
one for a single defect, the restartable stop. Its correction, and the compatibility correction, were reproduced independently before
release. [What was and was not reproduced](../evidence/VERIFICATION.md#who-executed-what).

## Limitations

Fixed assistant scenarios, one reference platform, no TLS, simulators that approximate real systems:
[KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md).
