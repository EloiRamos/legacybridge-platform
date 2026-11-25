"""C-04 claim race, C-05 two engine instances, C-06 overlap guard race, C-07 re-check serialization, C-09 late
observation vs sweeper, C-10 singleton lease — each 50 rounds with real threads against PostgreSQL.

C-05 runs two complete engine sets (distinct engine ids) against one database with the HALDEN fake, which does
not revoke tickets — correctness must not depend on the singleton lease."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from workflow_support import WF

from lbf.domain.enums import OperationState as S
from lbf.domain.enums import TransportObservationKind as K
from lbf.domain.errors import DomainError, ErrorCode
from lbf.domain.models import TransportObservation
from lbf.engines.executor import Executor
from lbf.engines.lease import EngineLease
from lbf.engines.pause import PausePoints
from lbf.engines.reconciler import Reconciler
from lbf.engines.runner import EngineSet
from lbf.engines.sweeper import Sweeper
from lbf.persistence import tables as t

pytestmark = pytest.mark.db
ROUNDS = 50


@pytest.fixture
def wf(app_engine, tmp_path):
    return WF(app_engine, tmp_path)


def race(*fns):
    barrier = threading.Barrier(len(fns))

    def wrap(fn):
        barrier.wait()
        try:
            return fn()
        except DomainError as exc:
            return exc.code
        except Exception as exc:  # recorded as an outcome of the race
            return type(exc).__name__

    with ThreadPoolExecutor(len(fns)) as pool:
        return [f.result() for f in [pool.submit(wrap, fn) for fn in fns]]


def count(wf, table, *where):
    with wf.engine.connect() as conn:
        q = select(func.count()).select_from(table)
        for w in where:
            q = q.where(w)
        return conn.execute(q).scalar()


def test_c04_claim_race_two_engines_one_claim(wf):
    a, b = Executor(wf.ctx, "engine-A"), Executor(wf.ctx, "engine-B")
    for _ in range(ROUNDS):
        op = wf.approved("halden")
        claims = race(a.claim, b.claim)
        assert sum(1 for c in claims if c is not None) == 1
        winner = a if claims[0] is not None else b
        winner.process(claims[0] if claims[0] is not None else claims[1])
        assert wf.state(op)[0] == S.CONFIRMED
    assert count(wf, t.transmissions) == ROUNDS == wf.halden_client.e8_request_count


def test_c05_two_engine_instances_never_duplicate_a_transmission(wf):
    second = EngineSet(wf.ctx, "engine-test-2", PausePoints(profile="test"))
    ops = []
    for _ in range(ROUNDS):
        ops.append(wf.approved("halden", qty=1 + len(ops) % 5))
        race(wf.engines.executor.run_once, second.executor.run_once)
    for _ in range(3):
        race(wf.engines.step, second.step)
    with wf.engine.connect() as conn:
        per_auth = conn.execute(select(func.count()).select_from(t.transmissions)
                                .group_by(t.transmissions.c.authorization_id)).scalars().all()
    assert len(per_auth) == ROUNDS and set(per_auth) == {1}
    assert wf.halden_client.e8_request_count == ROUNDS
    assert all(wf.state(op)[0] == S.CONFIRMED for op in ops)
    assert all(wf.handoffs(op) == 1 for op in ops)


def test_c06_overlap_guard_serializes_concurrent_approvals(wf):
    for _ in range(ROUNDS):
        a, b = wf.draft("halden", qty=3), wf.draft("halden", qty=4)
        wf.submit(a)
        wf.submit(b)
        outcomes = race(lambda a=a: wf.approve(a, by="priya"), lambda b=b: wf.approve(b, by="marta"))
        codes = [o for o in outcomes if isinstance(o, ErrorCode)]
        assert codes == [ErrorCode.CONFLICT_OVERLAP], outcomes
        approved = [op for op in (a, b) if wf.state(op)[0] == S.APPROVED]
        assert len(approved) == 1
        wf.step()  # the approved one completes, so it no longer blocks the next round
        assert wf.state(approved[0])[0] == S.CONFIRMED


def test_c07_one_reconciliation_run_per_operation(wf):
    r1, r2 = Reconciler(wf.ctx, "engine-A"), Reconciler(wf.ctx, "engine-B")
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    for _ in range(ROUNDS):
        before = count(wf, t.reconciliation_runs, t.reconciliation_runs.c.operation_id == op)
        outcomes = race(lambda: r1.recheck(op), lambda: r2.recheck(op))
        assert sorted(o is True for o in outcomes) == [False, True], outcomes
        assert count(wf, t.reconciliation_runs, t.reconciliation_runs.c.operation_id == op) == before + 1
        assert wf.state(op)[0] in (S.UNKNOWN, S.ESCALATED)
    assert count(wf, t.reconciliation_runs, t.reconciliation_runs.c.finished_at.is_(None)) == 0


def test_c09_late_observation_never_overwrites_the_sweeper(wf):
    ex, sweeper = Executor(wf.ctx, "engine-A"), Sweeper(wf.ctx, "engine-B")
    outcomes = {"observed": 0, "late": 0}
    for _ in range(ROUNDS):
        op = wf.draft("halden")
        wf.submit(op)  # earlier rounds stay unresolved, so I5 requires an audited override every round
        wf.approve(op, by="marta", override_reason="C-09 round: previous rounds intentionally left unresolved")
        auth = ex.claim()
        request, _stored, content_hash = ex._request(auth)
        tx_id = ex._begin(auth, request, wf.ports["halden"].render(request), content_hash)
        wf.clock.advance(40)  # the transmission lease (5 s + 30 s) has expired
        obs = TransportObservation(kind=K.UNCERTAIN, observed_at=wf.clock(), detail="late")
        observe = lambda tx_id=tx_id, obs=obs: ex._observe_with_retry(tx_id, obs)  # noqa: E731 - production path
        race(observe, sweeper.interrupted_transmissions)  # production path
        with wf.engine.connect() as conn:
            x = conn.execute(select(t.transmissions).where(t.transmissions.c.id == tx_id)).one()
        events = wf.events(op)
        if x.state == "OBSERVED":
            outcomes["observed"] += 1
            assert "TRANSMISSION_INTERRUPTED" not in events and "LATE_OBSERVATION" not in events
        else:
            assert x.state == "INTERRUPTED" and x.observation is None
            outcomes["late"] += 1
            assert "LATE_OBSERVATION" in events and wf.state(op)[0] in (S.UNKNOWN, S.RECONCILING, S.ESCALATED)
        assert count(wf, t.transmissions, t.transmissions.c.operation_id == op) == 1
    assert outcomes["observed"] + outcomes["late"] == ROUNDS


def test_c10_singleton_engine_lease(wf):
    for _ in range(ROUNDS):
        a = EngineLease(wf.engine, "engine-A", wf.clock)
        b = EngineLease(wf.engine, "engine-B", wf.clock)
        got = race(a.acquire, b.acquire)
        assert sorted(got) == [False, True]
        (a if got[0] else b).release()
