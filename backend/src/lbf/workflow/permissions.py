"""``permitted_actions`` — a pure function.

The UI renders only what the API returns; the API re-evaluates on every command. Inputs are the
operation's state and evidence facts, the backend's capability declaration, the principal and the
current time. No branch on backend identity: capability predicates only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from lbf.auth.policy import (
    Principal,
    Requirements,
    has_permission,
    sod1_violated,
    sod4_violated,
    sod5_violated,
)
from lbf.domain.enums import Action, EvidenceClass, OperationState
from lbf.domain.enums import Permission as P
from lbf.domain.evidence import NOT_APPLIED_EVIDENCE
from lbf.ports.capabilities import BackendCapabilities

S = OperationState
EDITABLE = frozenset({S.DRAFT, S.READY, S.DECLINED, S.NOT_APPLIED})
MANUAL_RECHECK_INTERVAL = timedelta(seconds=10)


@dataclass(frozen=True)
class OperationFacts:
    state: OperationState
    created_by: UUID
    version_authors: frozenset[UUID]
    last_evidence: EvidenceClass | None = None
    has_open_authorization: bool = False  # a PENDING/CLAIMED authorization exists
    approver_of_current_attempt: UUID | None = None
    first_handoff_at: datetime | None = None
    message_id: str | None = None
    redeliveries_in_attempt: int = 0
    last_manual_recheck_at: datetime | None = None
    approval_requirements: Requirements | None = None  # from the current validation snapshot


def redelivery_window_open(facts: OperationFacts, caps: BackendCapabilities, now: datetime) -> bool:
    if not caps.supports_redelivery or facts.first_handoff_at is None:
        return False
    if caps.dedup_window_seconds is None or caps.dedup_safety_margin_seconds is None:
        return False
    deadline = facts.first_handoff_at + timedelta(seconds=caps.dedup_window_seconds - caps.dedup_safety_margin_seconds)
    return now < deadline


def permitted_actions(
    facts: OperationFacts, caps: BackendCapabilities, principal: Principal, now: datetime
) -> frozenset[Action]:
    def can(p: P) -> bool:
        return has_permission(principal, p)

    actions: set[Action] = set()
    state = facts.state
    me = principal.user_id

    if state in EDITABLE and can(P.DRAFT_EDIT):
        actions.add(Action.REVISE)

    if can(P.DRAFT_SUBMIT) and (
        state is S.DRAFT or (state is S.NOT_APPLIED and facts.last_evidence in NOT_APPLIED_EVIDENCE)
    ):
        actions.add(Action.SUBMIT_FOR_APPROVAL)

    if state in EDITABLE and not facts.has_open_authorization:
        if can(P.APPROVAL_GRANT_ANY) or (can(P.DRAFT_WITHDRAW) and facts.created_by == me):
            actions.add(Action.WITHDRAW)

    if state is S.READY and not sod1_violated(me, facts.created_by, facts.version_authors):
        may_decide = can(P.APPROVAL_GRANT_STANDARD) or can(P.APPROVAL_GRANT_ANY)
        if may_decide:
            actions.add(Action.DECLINE)
            req = facts.approval_requirements
            approve_ok = req is not None and not req.blocking
            if approve_ok and req is not None:
                if req.needs_grant_any and not can(P.APPROVAL_GRANT_ANY):
                    approve_ok = False
                if req.needs_override and not can(P.APPROVAL_OVERRIDE):
                    approve_ok = False
            if approve_ok:
                actions.add(Action.APPROVE)

    if state in (S.UNKNOWN, S.ESCALATED) and can(P.RECONCILE_RECHECK):
        last = facts.last_manual_recheck_at
        if last is None or now - last >= MANUAL_RECHECK_INTERVAL:
            actions.add(Action.RECHECK)

    if state is S.UNKNOWN and can(P.ESCALATION_OPEN):
        actions.add(Action.ESCALATE)

    if state is S.ESCALATED and can(P.ESCALATION_RESOLVE) and not sod5_violated(me, facts.approver_of_current_attempt):
        actions.add(Action.RESOLVE_VERIFIED)
        actions.add(Action.RESOLVE_ATTESTED)

    if (
        state in (S.AWAITING_ACK, S.UNKNOWN)
        and caps.can_withdraw_before_pickup
        and facts.message_id is not None
        and not facts.has_open_authorization
        and can(P.SKARVIK_WITHDRAW_AUTHORIZE)
    ):
        actions.add(Action.AUTHORIZE_WITHDRAW_UNCLAIMED)

    if (
        state is S.UNKNOWN
        and facts.redeliveries_in_attempt == 0
        and not facts.has_open_authorization
        and redelivery_window_open(facts, caps, now)
        and can(P.SKARVIK_REDELIVER_AUTHORIZE)
        and not sod4_violated(me, facts.version_authors, facts.created_by)
    ):
        actions.add(Action.AUTHORIZE_REDELIVERY)

    return frozenset(actions)


def action_names(actions: Iterable[Action]) -> list[str]:
    return sorted(a.value for a in actions)
