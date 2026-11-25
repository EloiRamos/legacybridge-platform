"""Outcomes and evidence classes — closed sets and verification levels."""

from __future__ import annotations

from lbf.domain.enums import EvidenceClass, OperationState, Outcome, VerificationLevel

E = EvidenceClass

OUTCOME_EVIDENCE: dict[Outcome, frozenset[EvidenceClass]] = {
    Outcome.CONFIRMED: frozenset(
        {E.READ_BACK, E.ACKNOWLEDGED_ONLY, E.RECONCILED, E.DEDUP_CONFIRMED, E.MANUAL_VERIFIED}
    ),
    Outcome.REJECTED: frozenset({E.BACKEND_REJECTION}),
    Outcome.NOT_APPLIED: frozenset(
        {E.NOT_SENT, E.PRE_PROCESSING_REFUSAL, E.NEVER_DELIVERED, E.WITHDRAWN_BEFORE_PICKUP, E.APPROVAL_EXPIRED}
    ),
    Outcome.UNKNOWN: frozenset(
        {
            E.NO_MATCH_AFTER_UNCERTAIN,
            E.READBACK_MISMATCH,
            E.RECONCILIATION_FAILED,
            E.ACK_OVERDUE,
            E.INTERRUPTED,
            E.SOURCE_STALE,
        }
    ),
    Outcome.ESCALATED: frozenset({E.AMBIGUOUS_MATCH, E.CONTRADICTION, E.RECHECK_LIMIT, E.MANUAL}),
    Outcome.CLOSED_ATTESTED: frozenset({E.ATTESTED_BOOKED, E.ATTESTED_NOT_BOOKED}),
}

# Each evidence class belongs to exactly one outcome.
EVIDENCE_OUTCOME: dict[EvidenceClass, Outcome] = {
    ev: outcome for outcome, classes in OUTCOME_EVIDENCE.items() for ev in classes
}

NOT_APPLIED_EVIDENCE = OUTCOME_EVIDENCE[Outcome.NOT_APPLIED]

VERIFIED_BY_READ = frozenset({E.READ_BACK, E.RECONCILED, E.DEDUP_CONFIRMED, E.MANUAL_VERIFIED})

OUTCOME_STATE: dict[Outcome, OperationState] = {
    Outcome.CONFIRMED: OperationState.CONFIRMED,
    Outcome.REJECTED: OperationState.REJECTED,
    Outcome.NOT_APPLIED: OperationState.NOT_APPLIED,
    Outcome.UNKNOWN: OperationState.UNKNOWN,
    Outcome.ESCALATED: OperationState.ESCALATED,
    Outcome.CLOSED_ATTESTED: OperationState.CLOSED_ATTESTED,
}


def is_allowed(outcome: Outcome, evidence: EvidenceClass) -> bool:
    return evidence in OUTCOME_EVIDENCE[outcome]


def require_allowed(outcome: Outcome, evidence: EvidenceClass) -> None:
    if not is_allowed(outcome, evidence):
        raise ValueError(f"evidence class {evidence} is not allowed for outcome {outcome}")


def verification_level(outcome: Outcome, evidence: EvidenceClass) -> VerificationLevel | None:
    """Verification level of a recorded outcome. ``None`` for REJECTED/NOT_APPLIED, whose
    evidence class itself states the positive evidence (no "verified booking" level applies)."""
    require_allowed(outcome, evidence)
    if outcome is Outcome.CONFIRMED:
        return (
            VerificationLevel.VERIFIED_BY_READ if evidence in VERIFIED_BY_READ else VerificationLevel.ACKNOWLEDGED_ONLY
        )
    if outcome is Outcome.CLOSED_ATTESTED:
        return VerificationLevel.ATTESTED
    if outcome in (Outcome.UNKNOWN, Outcome.ESCALATED):
        return VerificationLevel.UNRESOLVED
    return None
