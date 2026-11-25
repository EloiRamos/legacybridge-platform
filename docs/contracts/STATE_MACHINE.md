# Operation state machine

Source of truth: `backend/src/lbf/domain/state_machine.py` (T01–T32 + T18b).
Anything absent from the table is invalid. The PostgreSQL transition-check trigger is generated from
`allowed_state_changes()` at migration time and compared with it by test (`tests/security/test_transition_trigger.py`).

## States (closed)

`DRAFT`, `READY`, `DECLINED`, `APPROVED`, `SUBMITTING`, `AWAITING_ACK`, `UNKNOWN`, `RECONCILING`,
`ESCALATED`, `NOT_APPLIED`, `CONFIRMED`*, `REJECTED`*, `WITHDRAWN`*, `CLOSED_ATTESTED`* (* terminal, absorbing).

## Transitions

| Id | From → To | Actor | Trigger |
|---|---|---|---|
| T01 | ∅ → DRAFT | user | create_draft |
| T02 / T04 / T07 / T32 | DRAFT / READY / DECLINED / NOT_APPLIED → DRAFT | user | revise_draft |
| T03 | DRAFT → READY | user | submit_for_approval (fresh non-blocking validation) |
| T05 | READY → APPROVED | user | approve (version + content hash, SoD-1, step-up, policy, overlap I5) |
| T06 | READY → DECLINED | user | decline (reason ≥ 10 chars) |
| T08 | DRAFT/READY/DECLINED/NOT_APPLIED → WITHDRAWN | user | withdraw (no PENDING/CLAIMED authorization) |
| T09 | APPROVED → SUBMITTING | system | begin_transmission (transmission row committed first) |
| T10 | APPROVED → NOT_APPLIED | system | preflight failed (`NOT_SENT`) or authorization expired (`APPROVAL_EXPIRED`) |
| T11 / T12 / T13 / T14 / T15 / T16 | SUBMITTING → CONFIRMED / REJECTED / NOT_APPLIED / UNKNOWN / AWAITING_ACK / ESCALATED | system | phase-1/phase-2 classification |
| T17 | AWAITING_ACK → CONFIRMED | system | ack ACCEPTED (or DUPLICATE for a redelivery) |
| T18 | AWAITING_ACK → REJECTED | system | ack REJECTED **and** a confirming ORDERS extract read |
| T18b | AWAITING_ACK → ESCALATED | system | ack REJECTED contradicted by an extract (`CONTRADICTION`) |
| T19 | AWAITING_ACK → UNKNOWN | system | ack deadline passed (`ACK_OVERDUE`) |
| T20 | AWAITING_ACK/UNKNOWN → NOT_APPLIED | user (command handler) | withdraw_unclaimed succeeded (`WITHDRAWN_BEFORE_PICKUP`) |
| T21 | UNKNOWN/ESCALATED → RECONCILING | system | reconciliation run started (auto, manual request, recovery, late ack) |
| T22 / T23 / T24 / T25 / T26 | RECONCILING → CONFIRMED / UNKNOWN / ESCALATED / NOT_APPLIED / REJECTED | system | verdict |
| T27 | UNKNOWN → SUBMITTING | system | redelivery of the stored payload (REDELIVER authorization) |
| T28 / T28s | UNKNOWN → ESCALATED | user / system | manual escalate / UNKNOWN older than 30 min (scaled in test) |
| T29 | ESCALATED → CONFIRMED | user | resolution VERIFIED by a backend read (`MANUAL_VERIFIED`) |
| T30 | ESCALATED → CLOSED_ATTESTED | user | attestation with external reference (not verified) |
| T31 | NOT_APPLIED → READY | user | submit_for_approval for attempt n+1 |

Representation notes: row T28 carries both a user trigger and a system trigger; the table holds
them as `T28` (user) and `T28s` (system). A user's re-check is a *request*; T21 is executed by the
reconciler. The same applies to T27: the user authorizes a redelivery; the executor performs it.

## Explicitly invalid (negative tests ST-NEG)

`UNKNOWN→READY`, `UNKNOWN→APPROVED`, `UNKNOWN→DRAFT`, `ESCALATED→NOT_APPLIED` (direct), any transition out of a
terminal state, `SUBMITTING→SUBMITTING`, `APPROVED→APPROVED`, `NOT_APPLIED→SUBMITTING`, `AWAITING_ACK→SUBMITTING`,
a second `REDELIVER` for the same attempt (database partial unique index), any user-initiated
transition into `SUBMITTING`, `AWAITING_ACK`, `RECONCILING` or `CONFIRMED` except T29.

## Phase-1 / phase-2 classification

| Phase-1 | Phase-2 | Result |
|---|---|---|
| NOT_DELIVERED | — | NOT_APPLIED / NEVER_DELIVERED (async) or NOT_SENT (sync connect failure) |
| REFUSED_PRE_PROCESSING | — | NOT_APPLIED / PRE_PROCESSING_REFUSAL |
| REJECTED | — | REJECTED / BACKEND_REJECTION |
| ACCEPTED (sync) | anchored read-back | MATCHED ⇒ CONFIRMED/READ_BACK; READ_FAILED ⇒ CONFIRMED/ACKNOWLEDGED_ONLY; MISMATCH ⇒ UNKNOWN/READBACK_MISMATCH |
| DELIVERED_PENDING_ACK | wait for ack | AWAITING_ACK |
| UNCERTAIN | correlation reconcile once | MATCHED ⇒ CONFIRMED/RECONCILED; NO_MATCH ⇒ UNKNOWN/NO_MATCH_AFTER_UNCERTAIN; AMBIGUOUS/CONTRADICTION ⇒ ESCALATED; READ_FAILED ⇒ UNKNOWN/RECONCILIATION_FAILED |
| INTERRUPTED | scheduled reconcile | as UNCERTAIN; async temp-file evidence ⇒ NOT_APPLIED/NEVER_DELIVERED |

Re-check schedule after entering UNKNOWN: +5 s, +15 s, +60 s, +300 s, +900 s, then ESCALATED/RECHECK_LIMIT.
`LBF_TIME_SCALE` (test profile only, 0.02) scales only this schedule and the 30-minute escalation timer.
