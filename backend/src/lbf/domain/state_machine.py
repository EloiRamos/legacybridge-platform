"""Operation state machine — the normative transition table (T01–T32 + T18b).

Anything absent from :data:`TRANSITIONS` is invalid. The SQL transition-check trigger is generated
from :func:`allowed_state_changes` at migration time and compared with it by test.

Actor semantics: ``USER`` transitions are executed by a command on behalf of an authenticated human;
``SYSTEM`` transitions are executed by engines (executor, ack poller, reconciler, sweeper). A human
*request* for a re-check is a request; the transition into ``RECONCILING`` (T21) is executed by the
reconciler. No user-initiated transition enters ``SUBMITTING``, ``AWAITING_ACK``, ``RECONCILING`` or
``CONFIRMED`` except T29 (verified resolution backed by a backend read).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lbf.domain.enums import TERMINAL_STATES
from lbf.domain.enums import OperationState as S
from lbf.domain.enums import Permission as P

Actor = Literal["USER", "SYSTEM"]


@dataclass(frozen=True)
class Transition:
    id: str
    sources: frozenset[S | None]  # None = ∅ (creation)
    target: S
    actor: Actor
    trigger: str
    permissions: tuple[P, ...] = ()


def _t(tid: str, sources: set[S | None], target: S, actor: Actor, trigger: str, *perms: P) -> Transition:
    return Transition(tid, frozenset(sources), target, actor, trigger, tuple(perms))


TRANSITIONS: tuple[Transition, ...] = (
    _t("T01", {None}, S.DRAFT, "USER", "create_draft", P.DRAFT_CREATE),
    _t("T02", {S.DRAFT}, S.DRAFT, "USER", "revise_draft", P.DRAFT_EDIT),
    _t("T03", {S.DRAFT}, S.READY, "USER", "submit_for_approval", P.DRAFT_SUBMIT),
    _t("T04", {S.READY}, S.DRAFT, "USER", "revise_draft", P.DRAFT_EDIT),
    _t("T05", {S.READY}, S.APPROVED, "USER", "approve", P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY),
    _t("T06", {S.READY}, S.DECLINED, "USER", "decline", P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY),
    _t("T07", {S.DECLINED}, S.DRAFT, "USER", "revise_draft", P.DRAFT_EDIT),
    _t(
        "T08",
        {S.DRAFT, S.READY, S.DECLINED, S.NOT_APPLIED},
        S.WITHDRAWN,
        "USER",
        "withdraw",
        P.DRAFT_WITHDRAW,
        P.APPROVAL_GRANT_ANY,
    ),
    _t("T09", {S.APPROVED}, S.SUBMITTING, "SYSTEM", "begin_transmission"),
    _t("T10", {S.APPROVED}, S.NOT_APPLIED, "SYSTEM", "preflight_failed_or_authorization_expired"),
    _t("T11", {S.SUBMITTING}, S.CONFIRMED, "SYSTEM", "accepted_and_read_back"),
    _t("T12", {S.SUBMITTING}, S.REJECTED, "SYSTEM", "backend_rejected"),
    _t("T13", {S.SUBMITTING}, S.NOT_APPLIED, "SYSTEM", "not_delivered_or_refused"),
    _t("T14", {S.SUBMITTING}, S.UNKNOWN, "SYSTEM", "uncertain_or_mismatch_or_interrupted"),
    _t("T15", {S.SUBMITTING}, S.AWAITING_ACK, "SYSTEM", "delivered_pending_ack"),
    _t("T16", {S.SUBMITTING}, S.ESCALATED, "SYSTEM", "phase2_ambiguous_or_contradiction"),
    _t("T17", {S.AWAITING_ACK}, S.CONFIRMED, "SYSTEM", "ack_accepted"),
    _t("T18", {S.AWAITING_ACK}, S.REJECTED, "SYSTEM", "ack_rejected_and_extract_confirms"),
    _t("T18b", {S.AWAITING_ACK}, S.ESCALATED, "SYSTEM", "ack_rejected_contradicted_by_extract"),
    _t("T19", {S.AWAITING_ACK}, S.UNKNOWN, "SYSTEM", "ack_overdue"),
    _t(
        "T20",
        {S.AWAITING_ACK, S.UNKNOWN},
        S.NOT_APPLIED,
        "USER",
        "withdraw_unclaimed",
        P.SKARVIK_WITHDRAW_AUTHORIZE,
    ),
    _t("T21", {S.UNKNOWN, S.ESCALATED}, S.RECONCILING, "SYSTEM", "reconciliation_started"),
    _t("T22", {S.RECONCILING}, S.CONFIRMED, "SYSTEM", "verdict_matched"),
    _t("T23", {S.RECONCILING}, S.UNKNOWN, "SYSTEM", "verdict_inconclusive"),
    _t("T24", {S.RECONCILING}, S.ESCALATED, "SYSTEM", "verdict_ambiguous_or_limit"),
    _t("T25", {S.RECONCILING}, S.NOT_APPLIED, "SYSTEM", "absence_proven"),
    _t("T26", {S.RECONCILING}, S.REJECTED, "SYSTEM", "late_ack_rejected_and_extract_confirms"),
    _t("T27", {S.UNKNOWN}, S.SUBMITTING, "SYSTEM", "redelivery_begin"),
    _t("T28", {S.UNKNOWN}, S.ESCALATED, "USER", "escalate", P.ESCALATION_OPEN),
    _t("T28s", {S.UNKNOWN}, S.ESCALATED, "SYSTEM", "unknown_age_limit"),
    _t("T29", {S.ESCALATED}, S.CONFIRMED, "USER", "resolve_verified", P.ESCALATION_RESOLVE),
    _t("T30", {S.ESCALATED}, S.CLOSED_ATTESTED, "USER", "resolve_attested", P.ESCALATION_RESOLVE),
    _t("T31", {S.NOT_APPLIED}, S.READY, "USER", "submit_for_approval", P.DRAFT_SUBMIT),
    _t("T32", {S.NOT_APPLIED}, S.DRAFT, "USER", "revise_draft", P.DRAFT_EDIT),
)

BY_ID: dict[str, Transition] = {t.id: t for t in TRANSITIONS}
# T28 is one row with a USER trigger (manual escalate) and a SYSTEM trigger (age limit);
# it is represented as two entries ("T28" and "T28s") so the actor is unambiguous.
PLAN_TRANSITION_IDS = (*(f"T{n:02d}" for n in range(1, 33)), "T18b")

# Invalid transitions (each has a negative test ST-NEG-xx).
EXPLICITLY_INVALID: tuple[tuple[S, S], ...] = (
    (S.UNKNOWN, S.READY),
    (S.UNKNOWN, S.APPROVED),
    (S.UNKNOWN, S.DRAFT),
    (S.SUBMITTING, S.SUBMITTING),
    (S.APPROVED, S.APPROVED),
    (S.NOT_APPLIED, S.SUBMITTING),
    (S.AWAITING_ACK, S.SUBMITTING),
    (S.ESCALATED, S.NOT_APPLIED),
)


class InvalidTransition(ValueError):
    pass


def find(source: S | None, target: S, actor: Actor | None = None) -> list[Transition]:
    return [
        t for t in TRANSITIONS if source in t.sources and t.target == target and (actor is None or t.actor == actor)
    ]


def is_valid(source: S | None, target: S, actor: Actor | None = None) -> bool:
    return bool(find(source, target, actor))


def require(source: S | None, target: S, transition_id: str) -> Transition:
    """Assert that ``transition_id`` permits ``source → target``; returns the transition."""
    t = BY_ID.get(transition_id)
    if t is None or source not in t.sources or t.target != target:
        raise InvalidTransition(f"{transition_id}: {source} -> {target} is not a permitted transition")
    if source in TERMINAL_STATES:
        raise InvalidTransition(f"{source} is terminal")
    return t


def allowed_state_changes() -> frozenset[tuple[S, S]]:
    """(from, to) pairs with from != to that the SQL trigger accepts on ``operations.state`` updates."""
    pairs = set()
    for t in TRANSITIONS:
        for s in t.sources:
            if s is not None and s != t.target:
                pairs.add((s, t.target))
    return frozenset(pairs)


def user_reachable_targets() -> frozenset[S]:
    return frozenset(t.target for t in TRANSITIONS if t.actor == "USER")
