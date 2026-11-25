"""The same workflow on both backends (BR-08): transitions whose preconditions do not depend on a capability are
exercised on HALDEN and SKARVIK; capability-specific transitions are covered in test_transitions.py with the
capability that enables them (T15/T17–T20/T25–T27 async file drop; T11 read-back and T16 sync)."""

from __future__ import annotations

import pytest
from workflow_support import WF

from lbf.domain.enums import EvidenceClass as E
from lbf.domain.enums import OperationState as S
from lbf.domain.enums import ResolutionKind
from lbf.workflow import commands

pytestmark = pytest.mark.db
BOTH = ["halden", "skarvik"]


@pytest.fixture
def wf(app_engine, tmp_path):
    return WF(app_engine, tmp_path)


def make_unknown(wf: WF, backend: str):
    """Order committed in the backend, outcome not established in the application."""
    op = wf.approved(backend)
    if wf.ports[backend].capabilities().is_async:
        wf.skarvik.faults.counted["drop_ack"] = 1
        wf.step()
        wf.skarvik.pickup()  # processed, ack lost; no snapshot newer than handoff + lag exists yet
        wf.skarvik.flush_acks()
        wf.clock.advance(21)
        wf.engines.reconciler.ack_overdue()
    else:
        wf.halden.next_e8_modes.append("commit_then_uncertain")
        wf.hook = lambda n: setattr(wf.halden, "read_failures", 1000) if n == "after_handoff_before_observe" else None
        wf.step()
        wf.hook = None
        wf.halden.read_failures = 0
    assert wf.state(op)[0] == S.UNKNOWN
    return op


@pytest.mark.parametrize("backend", BOTH)
def test_draft_lifecycle_t01_t02_t03_t06_t07_t08(wf, backend):
    op = wf.draft(backend)
    commands.revise_draft(wf.ctx, wf.p("daniel"), op, 1, wf.request(backend, qty=6))
    wf.submit(op)
    row = wf.row(op)
    from sqlalchemy import select

    from lbf.persistence import tables as t

    with wf.engine.connect() as conn:
        h = conn.execute(select(t.draft_versions.c.content_hash).where(
            t.draft_versions.c.operation_id == op, t.draft_versions.c.version == row.current_version)).scalar()
    commands.decline(wf.ctx, wf.p("marta"), op, version=row.current_version, content_hash=h,
                     reason="quantity must be confirmed by the site")
    commands.revise_draft(wf.ctx, wf.p("daniel"), op, 2, wf.request(backend, qty=7))
    wf.submit(op)
    commands.withdraw(wf.ctx, wf.p("daniel"), op)
    assert wf.state(op)[0] == S.WITHDRAWN and wf.transmissions(op) == []


@pytest.mark.parametrize("backend", BOTH)
def test_t21_t22_recheck_confirms_a_committed_order(wf, backend):
    op = make_unknown(wf, backend)
    wf.clock.advance(6)
    if wf.ports[backend].capabilities().is_async:
        wf.skarvik.extracts()
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.CONFIRMED, E.RECONCILED)
    assert wf.handoffs(op) == 1 and len(wf.backend_orders(op)) == 1


@pytest.mark.parametrize("backend", BOTH)
def test_t28_t21_t24_t29_escalate_recheck_then_verified_resolution(wf, backend):
    op = make_unknown(wf, backend)
    commands.escalate(wf.ctx, wf.p("rafael"), op)  # T28
    assert wf.state(op) == (S.ESCALATED, E.MANUAL)
    wf.halden.read_failures = 1000  # HALDEN reads fail; SKARVIK snapshot is not yet fresh enough
    commands.request_recheck(wf.ctx, wf.p("rafael"), op)
    wf.engines.reconciler.run_once()  # T21 → T24 (stays escalated, reason kept)
    wf.halden.read_failures = 0
    assert wf.state(op) == (S.ESCALATED, E.MANUAL)
    [order] = wf.backend_orders(op)
    if wf.ports[backend].capabilities().is_async:
        wf.clock.advance(7)
        wf.skarvik.extracts()
    assert commands.resolve(wf.ctx, wf.p("marta"), op, kind=ResolutionKind.VERIFIED,
                            external_reference=order) is S.CONFIRMED  # T29
    assert wf.state(op) == (S.CONFIRMED, E.MANUAL_VERIFIED)


@pytest.mark.parametrize("backend", BOTH)
def test_t30_attested_closure_is_distinct_from_verified(wf, backend):
    op = make_unknown(wf, backend)
    commands.escalate(wf.ctx, wf.p("marta"), op)
    commands.resolve(wf.ctx, wf.p("marta"), op, kind=ResolutionKind.ATTESTED_BOOKED, external_reference="CALL-881",
                     note="backend desk confirmed booking by phone; reference CALL-881")
    assert wf.state(op) == (S.CLOSED_ATTESTED, E.ATTESTED_BOOKED)
    assert "ESCALATION_RESOLVED" in wf.events(op)
