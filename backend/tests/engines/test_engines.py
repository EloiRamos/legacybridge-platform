"""Engine tests: F2-01 order of effects, crash simulation at every pause point (G1, G4), F2-07 expiry,
schedule with ``LBF_TIME_SCALE``, restart resumption, SEC-17 pause-point inertness.

A "kill" is modelled in-process by raising a ``BaseException`` at a pause point: the engine's work stops exactly
there, leaving the database and the backend as a real process death would; a *new* engine set (new engine id)
then performs recovery, as after ``docker kill`` + restart (the container-level variant is R11)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from workflow_support import WF

from lbf.domain.enums import EvidenceClass as E
from lbf.domain.enums import OperationState as S
from lbf.engines import executor as executor_module
from lbf.engines.pause import POINTS, PausePoints
from lbf.engines.runner import EngineSet
from lbf.persistence import tables as t

pytestmark = pytest.mark.db


class Kill(BaseException):
    pass


@pytest.fixture
def wf(app_engine, tmp_path):
    return WF(app_engine, tmp_path)


def kill_at(point):
    def hook(name):
        if name == point:
            raise Kill(point)
    return hook


def restart(wf: WF, after: float = 120) -> EngineSet:
    """New process: a fresh engine set; time passes beyond every lease."""
    wf.hook = None
    wf.clock.advance(after)
    fresh = EngineSet(wf.ctx, "engine-restarted", PausePoints(profile="test", hook=wf._hook))
    fresh.sweeper.run_once()
    return fresh


def per_authorization(wf: WF, op) -> list[int]:
    with wf.engine.connect() as conn:
        return list(conn.execute(select(func.count()).select_from(t.transmissions)
                                 .where(t.transmissions.c.operation_id == op)
                                 .group_by(t.transmissions.c.authorization_id)).scalars())


# --------------------------------------------------------------------------- F2-01


@pytest.mark.parametrize("backend", ["halden", "skarvik"])
def test_f2_01_transmission_row_is_committed_before_the_handoff(wf, db_settings, backend):
    op = wf.approved(backend)
    seen = {}
    other = create_engine(db_settings.url("audit_reader"), connect_args={"options": "-c search_path=lbf"})

    def probe(name):
        if name != "before_handoff":
            return
        with wf.engine.connect() as conn:  # a separate session sees only committed data
            x = conn.execute(select(t.transmissions).where(t.transmissions.c.operation_id == op)).one()
            a = conn.execute(select(t.authorizations).where(t.authorizations.c.id == x.authorization_id)).one()
            o = conn.execute(select(t.operations.c.state).where(t.operations.c.id == op)).scalar()
        with other.connect() as conn:
            started = conn.execute(select(t.audit_events.c.event_type).where(
                t.audit_events.c.operation_id == op, t.audit_events.c.event_type == "TRANSMISSION_STARTED")).all()
        seen.update(state=x.state, auth=a.state, op=o, started=len(started), handoffs=wf.handoffs(op))

    wf.hook = probe
    wf.step()
    other.dispose()
    assert seen == {"state": "SENDING", "auth": "CONSUMED", "op": "SUBMITTING", "started": 1, "handoffs": 0}
    assert wf.handoffs(op) == 1


# --------------------------------------------------------------------------- crash at each pause point (G1, G4)


def test_kill_after_claim_releases_the_claim_and_sends_exactly_once(wf):
    op = wf.approved("halden")
    wf.hook = kill_at("after_claim")
    with pytest.raises(Kill):
        wf.step()
    assert wf.transmissions(op) == [] and wf.handoffs(op) == 0
    fresh = restart(wf, after=40)  # claim lease 30 s, authorization still valid (15 min)
    fresh.step()
    assert wf.state(op) == (S.CONFIRMED, E.READ_BACK)
    assert per_authorization(wf, op) == [1] and wf.handoffs(op) == 1


def test_kill_before_handoff_halden_stays_unknown_never_not_applied_never_resent(wf):
    op = wf.approved("halden")
    wf.hook = kill_at("before_handoff")
    with pytest.raises(Kill):
        wf.step()
    fresh = restart(wf)
    assert wf.state(op) == (S.UNKNOWN, E.INTERRUPTED)
    for _ in range(3):
        fresh.step()
        wf.clock.advance(20)
    state, _evidence = wf.state(op)
    assert state in (S.UNKNOWN, S.ESCALATED) and state != S.NOT_APPLIED  # G4; HALDEN cannot prove absence
    assert wf.handoffs(op) == 0 and len(wf.transmissions(op)) == 1 and wf.halden_client.e8_request_count == 0


def test_kill_after_handoff_before_observe_halden_reconciles_without_resend(wf):
    op = wf.approved("halden")
    wf.hook = kill_at("after_handoff_before_observe")
    with pytest.raises(Kill):
        wf.step()
    assert wf.handoffs(op) == 1 and len(wf.backend_orders(op)) == 1
    fresh = restart(wf)
    assert wf.state(op) == (S.UNKNOWN, E.INTERRUPTED)
    fresh.step()
    assert wf.state(op) == (S.CONFIRMED, E.RECONCILED)
    assert wf.handoffs(op) == 1 and wf.halden_client.e8_request_count == 1
    assert per_authorization(wf, op) == [1]


def test_kill_before_handoff_skarvik_proves_never_delivered(wf):
    op = wf.approved("skarvik")
    wf.hook = kill_at("before_handoff")
    with pytest.raises(Kill):
        wf.step()
    fresh = restart(wf)
    wf.settle_skarvik()
    fresh.step()
    assert wf.state(op) == (S.NOT_APPLIED, E.NEVER_DELIVERED)
    assert wf.handoffs(op) == 0 and wf.backend_orders(op) == []


def test_kill_between_observe_and_phase2_reruns_phase2_read_only(wf, monkeypatch):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("commit_then_uncertain")

    def die(*_a, **_k):
        raise Kill("phase2")

    monkeypatch.setattr(executor_module, "run_phase2", die)
    with pytest.raises(Kill):
        wf.step()
    monkeypatch.undo()
    assert wf.state(op)[0] == S.SUBMITTING and wf.row(op).phase2_pending
    fresh = restart(wf)
    assert wf.state(op) == (S.CONFIRMED, E.RECONCILED)
    assert wf.halden_client.e8_request_count == 1
    fresh.step()
    assert wf.handoffs(op) == 1


def test_kill_mid_reconcile_lease_expires_to_read_failed(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    wf.clock.advance(5)
    wf.hook = kill_at("mid_reconcile")
    with pytest.raises(Kill):
        wf.engines.reconciler.run_once()
    assert wf.state(op)[0] == S.RECONCILING
    fresh = restart(wf, after=61)
    assert wf.state(op) == (S.UNKNOWN, E.RECONCILIATION_FAILED)
    with wf.engine.connect() as conn:
        runs = conn.execute(select(t.reconciliation_runs.c.verdict).where(
            t.reconciliation_runs.c.operation_id == op)).scalars().all()
    assert "READ_FAILED" in runs and None not in runs
    fresh.step()
    assert wf.halden_client.e8_request_count == 1


def test_kill_before_ack_record_records_the_ack_once_after_restart(wf):
    op = wf.approved("skarvik")
    wf.step()
    wf.settle_skarvik()
    wf.hook = kill_at("before_ack_record")
    with pytest.raises(Kill):
        wf.engines.ack_poller.run_once()
    fresh = restart(wf, after=1)
    fresh.ack_poller.run_once()
    fresh.ack_poller.run_once()
    assert wf.state(op)[0] == S.CONFIRMED
    with wf.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.acknowledgements)).scalar() == 1


def test_every_pause_point_is_exercised():
    assert set(POINTS) == {"after_claim", "before_handoff", "after_handoff_before_observe", "before_ack_record",
                           "mid_reconcile"}


# --------------------------------------------------------------------------- F2-07 expiry


def test_f2_07_expired_claim_after_authorization_expiry_is_not_applied_with_zero_handoffs(wf):
    op = wf.approved("halden")
    wf.hook = kill_at("after_claim")
    with pytest.raises(Kill):
        wf.step()
    fresh = restart(wf, after=16 * 60)
    fresh.step()
    assert wf.state(op) == (S.NOT_APPLIED, E.APPROVAL_EXPIRED)
    assert wf.transmissions(op) == [] and wf.handoffs(op) == 0
    assert "AUTHORIZATION_EXPIRED" in wf.events(op)


# --------------------------------------------------------------------------- schedule, restart


def test_time_scale_applies_only_to_schedule_and_escalation_in_test_profile(app_engine, tmp_path):
    wf = WF(app_engine, tmp_path, time_scale=0.02)
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    row = wf.row(op)
    assert row.next_recheck_at - row.unknown_since == timedelta(seconds=0.1)
    assert wf.settings.unknown_escalation_at(row.unknown_since) - row.unknown_since == timedelta(seconds=36)
    from lbf.workflow.context import WorkflowSettings

    assert WorkflowSettings(profile="default", time_scale=0.02).scale == 1.0
    offsets = []
    for _ in range(4):
        wf.clock.now = wf.row(op).next_recheck_at
        wf.engines.reconciler.due_rechecks()
        r = wf.row(op)
        offsets.append((r.next_recheck_at - r.unknown_since).total_seconds())
    assert offsets == [0.3, 1.2, 6.0, 18.0]


def test_restart_resumes_schedule_from_stored_timestamps(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    wf.clock.now = wf.row(op).next_recheck_at
    wf.engines.reconciler.due_rechecks()
    before = wf.row(op)
    fresh = restart(wf, after=0)
    fresh.step()
    after = wf.row(op)
    assert (after.state, after.auto_rechecks, after.next_recheck_at, after.unknown_since) == (
        before.state, before.auto_rechecks, before.next_recheck_at, before.unknown_since)


# --------------------------------------------------------------------------- SEC-17


def test_sec17_pause_points_are_inert_outside_the_test_profile(tmp_path):
    called = []
    for profile in ("default", "demo"):
        p = PausePoints(profile=profile, points=frozenset({"before_handoff"}), directory=tmp_path,
                        hook=called.append)
        p.reach("before_handoff")
        assert p.active is False and list(tmp_path.iterdir()) == [] and called == []
    (tmp_path / "before_handoff.release").write_text("go")
    active = PausePoints(profile="test", points=frozenset({"before_handoff"}), directory=tmp_path,
                         hook=called.append, max_wait_seconds=2)
    active.reach("before_handoff")
    assert called == ["before_handoff"] and not (tmp_path / "before_handoff.release").exists()
    with pytest.raises(ValueError):
        PausePoints(profile="test", points=frozenset({"nowhere"}))
