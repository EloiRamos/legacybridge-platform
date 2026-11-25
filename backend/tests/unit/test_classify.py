"""Phase-1/phase-2 classification and reconciliation outcome mapping (T22–T26) — pure tables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lbf.domain.enums import EvidenceClass as E
from lbf.domain.enums import OperationState as S
from lbf.domain.enums import ReconciliationVerdict as V
from lbf.domain.enums import TransportObservationKind as K
from lbf.domain.evidence import EVIDENCE_OUTCOME
from lbf.domain.models import ReconciliationResult, TransportObservation
from lbf.domain.state_machine import BY_ID
from lbf.ports.capabilities import HALDEN_CAPABILITIES, SKARVIK_CAPABILITIES
from lbf.workflow.classify import (
    AckFacts,
    phase1,
    phase2_correlation,
    phase2_read_back,
    reconciliation_outcome,
    rejected_ack_verification,
)

NOW = datetime(2026, 11, 21, tzinfo=UTC)


def obs(kind, ref=None):
    return TransportObservation(kind=kind, backend_order_ref=ref, observed_at=NOW)


def res(verdict, ref=None, absence=None, candidates=()):
    return ReconciliationResult(verdict=verdict, matched_order_ref=ref, absence_proven=absence is not None,
                                absence_evidence=absence, candidates=list(candidates))


@pytest.mark.parametrize(("kind", "caps", "target", "evidence"), [
    (K.NOT_DELIVERED, HALDEN_CAPABILITIES, S.NOT_APPLIED, E.NOT_SENT),
    (K.NOT_DELIVERED, SKARVIK_CAPABILITIES, S.NOT_APPLIED, E.NEVER_DELIVERED),
    (K.REFUSED_PRE_PROCESSING, HALDEN_CAPABILITIES, S.NOT_APPLIED, E.PRE_PROCESSING_REFUSAL),
    (K.REJECTED, HALDEN_CAPABILITIES, S.REJECTED, E.BACKEND_REJECTION),
    (K.DELIVERED_PENDING_ACK, SKARVIK_CAPABILITIES, S.AWAITING_ACK, None),
])
def test_phase1_final_rows(kind, caps, target, evidence):
    p = phase1(obs(kind), caps)
    assert p.decision is not None and (p.decision.target, p.decision.evidence) == (target, evidence)
    assert p.decision.target in {BY_ID[p.decision.transition].target}


@pytest.mark.parametrize(("kind", "ref", "phase2"), [
    (K.ACCEPTED, "SO-1", "READ_BACK"), (K.ACCEPTED, None, "CORRELATION"), (K.UNCERTAIN, None, "CORRELATION"),
    (K.INTERRUPTED, None, "CORRELATION")])
def test_phase1_defers_to_phase2(kind, ref, phase2):
    p = phase1(obs(kind, ref), HALDEN_CAPABILITIES)
    assert p.decision is None and p.phase2 == phase2 and p.anchor == (ref if phase2 == "READ_BACK" else None)


@pytest.mark.parametrize(("verdict", "target", "evidence"), [
    (V.MATCHED, S.CONFIRMED, E.READ_BACK), (V.READ_FAILED, S.CONFIRMED, E.ACKNOWLEDGED_ONLY),
    (V.MISMATCH, S.UNKNOWN, E.READBACK_MISMATCH), (V.NO_MATCH, S.UNKNOWN, E.READBACK_MISMATCH),
    (V.SOURCE_STALE, S.UNKNOWN, E.SOURCE_STALE)])
def test_phase2_read_back(verdict, target, evidence):
    d = phase2_read_back(res(verdict, "SO-1" if verdict is V.MATCHED else None), "SO-1")
    assert (d.target, d.evidence) == (target, evidence)


@pytest.mark.parametrize(("verdict", "target", "evidence"), [
    (V.MATCHED, S.CONFIRMED, E.RECONCILED), (V.NO_MATCH, S.UNKNOWN, E.NO_MATCH_AFTER_UNCERTAIN),
    (V.AMBIGUOUS, S.ESCALATED, E.AMBIGUOUS_MATCH), (V.CONTRADICTION, S.ESCALATED, E.CONTRADICTION),
    (V.READ_FAILED, S.UNKNOWN, E.RECONCILIATION_FAILED), (V.SOURCE_STALE, S.UNKNOWN, E.SOURCE_STALE),
    (V.MISMATCH, S.UNKNOWN, E.READBACK_MISMATCH)])
def test_phase2_correlation(verdict, target, evidence):
    d = phase2_correlation(res(verdict, "SO-1" if verdict is V.MATCHED else None))
    assert (d.target, d.evidence) == (target, evidence)
    assert d.target is not S.NOT_APPLIED  # an uncertain handoff never becomes NOT_APPLIED in phase 2


def test_reconciliation_outcome_rules():
    none = AckFacts()
    absent = res(V.NO_MATCH, absence=E.NEVER_DELIVERED)
    assert reconciliation_outcome(absent, reconciling_from=S.UNKNOWN, acks=none, auto_limit_reached=False).target \
        is S.NOT_APPLIED
    assert reconciliation_outcome(res(V.MATCHED, "NV-1"), reconciling_from=S.UNKNOWN,
                                  acks=AckFacts("NV-1", duplicate=True), auto_limit_reached=False).evidence \
        is E.DEDUP_CONFIRMED
    late = reconciliation_outcome(res(V.SOURCE_STALE), reconciling_from=S.UNKNOWN, acks=AckFacts("NV-2"),
                                  auto_limit_reached=False)
    assert (late.target, late.evidence, late.order_ref) == (S.CONFIRMED, E.ACKNOWLEDGED_ONLY, "NV-2")
    rej = reconciliation_outcome(res(V.NO_MATCH), reconciling_from=S.UNKNOWN, acks=AckFacts(rejected=True),
                                 auto_limit_reached=False)
    assert (rej.target, rej.transition) == (S.REJECTED, "T26")
    keep = reconciliation_outcome(res(V.AMBIGUOUS, candidates=["a", "b"]), reconciling_from=S.ESCALATED, acks=none,
                                  auto_limit_reached=False)
    assert (keep.target, keep.evidence) == (S.ESCALATED, None)
    limit = reconciliation_outcome(res(V.NO_MATCH), reconciling_from=S.UNKNOWN, acks=none, auto_limit_reached=True)
    assert (limit.target, limit.evidence) == (S.ESCALATED, E.RECHECK_LIMIT)
    again = reconciliation_outcome(res(V.NO_MATCH), reconciling_from=S.UNKNOWN, acks=none, auto_limit_reached=False)
    assert (again.target, again.transition) == (S.UNKNOWN, "T23")


def test_rejected_ack_verification():
    assert rejected_ack_verification(res(V.NO_MATCH)).target is S.REJECTED
    assert rejected_ack_verification(res(V.CONTRADICTION, candidates=["NV-9"])).target is S.ESCALATED
    assert rejected_ack_verification(res(V.SOURCE_STALE)) is None
    assert rejected_ack_verification(res(V.READ_FAILED)) is None


def test_every_decision_evidence_belongs_to_its_target_state():
    for d in (phase2_correlation(res(v, "X" if v is V.MATCHED else None)) for v in V):
        if d.evidence is not None:
            assert EVIDENCE_OUTCOME[d.evidence].value == d.target.value
