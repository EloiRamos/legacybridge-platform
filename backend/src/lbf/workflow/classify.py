"""Phase-1 / phase-2 classification (normative) and reconciliation verdict mapping (T21–T26).

Pure functions over observations, reconciliation results and capability predicates — never backend identity.
Refactored from v1 ``service._classify`` / ``_read_back`` / ``_reconcile`` (traceability:
``docs/contracts/HALDEN_ADAPTER.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

from lbf.domain.enums import EvidenceClass as E
from lbf.domain.enums import OperationState as S
from lbf.domain.enums import Outcome
from lbf.domain.enums import ReconciliationVerdict as V
from lbf.domain.enums import TransportObservationKind as K
from lbf.domain.evidence import EVIDENCE_OUTCOME, NOT_APPLIED_EVIDENCE
from lbf.domain.models import ReconciliationResult, TransportObservation
from lbf.ports.capabilities import BackendCapabilities


@dataclass(frozen=True)
class Decision:
    target: S
    transition: str
    evidence: E | None = None
    order_ref: str | None = None
    detail: str = ""

    @property
    def outcome(self) -> Outcome | None:
        return EVIDENCE_OUTCOME[self.evidence] if self.evidence is not None else None


@dataclass(frozen=True)
class Phase1:
    decision: Decision | None  # final for this observation, or None when phase 2 must run
    phase2: str | None = None  # "READ_BACK" (anchored) or "CORRELATION"
    anchor: str | None = None


def phase1(obs: TransportObservation, caps: BackendCapabilities) -> Phase1:
    k = obs.kind
    if k is K.NOT_DELIVERED:
        # positive evidence the payload never reached the backend's processing: a file that was never published
        # (async share) or a connection that was never established (sync; v1 ADR-4 rule ``NOT_SENT``)
        ev = E.NEVER_DELIVERED if caps.is_async else E.NOT_SENT
        return Phase1(Decision(S.NOT_APPLIED, "T13", ev, detail=obs.detail))
    if k is K.REFUSED_PRE_PROCESSING:
        return Phase1(Decision(S.NOT_APPLIED, "T13", E.PRE_PROCESSING_REFUSAL, detail=obs.detail))
    if k is K.REJECTED:
        return Phase1(Decision(S.REJECTED, "T12", E.BACKEND_REJECTION, detail=obs.detail))
    if k is K.DELIVERED_PENDING_ACK:
        return Phase1(Decision(S.AWAITING_ACK, "T15", None, detail=obs.detail))
    if k is K.ACCEPTED and obs.backend_order_ref:
        return Phase1(None, "READ_BACK", obs.backend_order_ref)
    return Phase1(None, "CORRELATION")  # UNCERTAIN, INTERRUPTED, or ACCEPTED without a reference


def phase2_read_back(result: ReconciliationResult, anchor: str) -> Decision:
    """ACCEPTED (sync) + anchored read-back (v1 ``_read_back``)."""
    if result.verdict is V.MATCHED:
        return Decision(S.CONFIRMED, "T11", E.READ_BACK, anchor, result.detail)
    if result.verdict is V.READ_FAILED:
        return Decision(S.CONFIRMED, "T11", E.ACKNOWLEDGED_ONLY, anchor,
                        "acknowledged by the backend; read-back failed: " + result.detail)
    if result.verdict is V.SOURCE_STALE:
        return Decision(S.UNKNOWN, "T14", E.SOURCE_STALE, detail=result.detail)
    return Decision(S.UNKNOWN, "T14", E.READBACK_MISMATCH, detail=result.detail)


def phase2_correlation(result: ReconciliationResult) -> Decision:
    """UNCERTAIN + one immediate correlation reconcile (v1 ``_reconcile``)."""
    v = result.verdict
    if v is V.MATCHED:
        return Decision(S.CONFIRMED, "T11", E.RECONCILED, result.matched_order_ref, result.detail)
    if v is V.AMBIGUOUS:
        return Decision(S.ESCALATED, "T16", E.AMBIGUOUS_MATCH, detail=result.detail)
    if v is V.CONTRADICTION:
        return Decision(S.ESCALATED, "T16", E.CONTRADICTION, detail=result.detail)
    if v is V.READ_FAILED:
        return Decision(S.UNKNOWN, "T14", E.RECONCILIATION_FAILED, detail=result.detail)
    if v is V.SOURCE_STALE:
        return Decision(S.UNKNOWN, "T14", E.SOURCE_STALE, detail=result.detail)
    if v is V.MISMATCH:
        return Decision(S.UNKNOWN, "T14", E.READBACK_MISMATCH, detail=result.detail)
    return Decision(S.UNKNOWN, "T14", E.NO_MATCH_AFTER_UNCERTAIN, detail=result.detail)


@dataclass(frozen=True)
class AckFacts:
    """Acknowledgements recorded for the operation's current attempt (most recent last)."""

    accepted_ref: str | None = None  # ACCEPTED or DUPLICATE ack with an order reference
    duplicate: bool = False
    rejected: bool = False


def reconciliation_outcome(result: ReconciliationResult, *, reconciling_from: S, acks: AckFacts,
                           auto_limit_reached: bool) -> Decision:
    """RECONCILING → next state (T22–T26). Anchors are never erased; absence only from NOT_APPLIED evidence."""
    v = result.verdict
    if result.absence_proven and result.absence_evidence in NOT_APPLIED_EVIDENCE:
        return Decision(S.NOT_APPLIED, "T25", result.absence_evidence, detail=result.detail)
    if v is V.MATCHED:
        ev = E.DEDUP_CONFIRMED if acks.duplicate else E.RECONCILED
        return Decision(S.CONFIRMED, "T22", ev, result.matched_order_ref, result.detail)
    if acks.accepted_ref and v is not V.MISMATCH and v is not V.CONTRADICTION:
        # a late positive acknowledgement without a confirming read: acknowledged, not yet read back (R14)
        return Decision(S.CONFIRMED, "T22", E.ACKNOWLEDGED_ONLY, acks.accepted_ref,
                        "acknowledged by the backend; not yet read back")
    if acks.rejected and v is V.NO_MATCH:
        # a late REJECTED acknowledgement confirmed by a fresh extract without the order (T26, as T18)
        return Decision(S.REJECTED, "T26", E.BACKEND_REJECTION, detail=result.detail)
    if reconciling_from is S.ESCALATED:
        return Decision(S.ESCALATED, "T24", None, detail=result.detail)  # keeps its open escalation
    if v in (V.AMBIGUOUS, V.CONTRADICTION):
        ev = E.AMBIGUOUS_MATCH if v is V.AMBIGUOUS else E.CONTRADICTION
        return Decision(S.ESCALATED, "T24", ev, detail=result.detail)
    if auto_limit_reached:
        return Decision(S.ESCALATED, "T24", E.RECHECK_LIMIT, detail="automatic re-check limit reached")
    ev = {V.READ_FAILED: E.RECONCILIATION_FAILED, V.SOURCE_STALE: E.SOURCE_STALE,
          V.MISMATCH: E.READBACK_MISMATCH}.get(v, E.NO_MATCH_AFTER_UNCERTAIN)
    return Decision(S.UNKNOWN, "T23", ev, detail=result.detail)


def rejected_ack_verification(result: ReconciliationResult) -> Decision | None:
    """T18 / T18b: an ``AWAITING_ACK`` operation with a recorded REJECTED ack, after a verification read."""
    if result.verdict is V.NO_MATCH and not result.absence_proven:
        return Decision(S.REJECTED, "T18", E.BACKEND_REJECTION, detail="REJECTED acknowledgement confirmed by a "
                        "snapshot taken after the acknowledgement: " + result.detail)
    if result.verdict in (V.CONTRADICTION, V.MATCHED, V.AMBIGUOUS, V.MISMATCH) and result.candidates:
        return Decision(S.ESCALATED, "T18b", E.CONTRADICTION, detail=result.detail)
    return None  # SOURCE_STALE / READ_FAILED: keep waiting in AWAITING_ACK
