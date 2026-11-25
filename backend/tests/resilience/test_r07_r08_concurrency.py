"""R07 variant B (two API processes, 50 rounds) and R08 variant C (two engine instances claim simultaneously)."""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import r07_worker
from sqlalchemy import func, select
from workflow_support import WF

from lbf.domain.enums import OperationState as S
from lbf.engines.executor import Executor
from lbf.persistence import tables as t

pytestmark = pytest.mark.db
ROUNDS = 50


def single(values: set[int]) -> int | list[int]:
    """The per-round value when every round agrees, otherwise every distinct value (a failure)."""
    return next(iter(values)) if len(values) == 1 else sorted(values)


def test_r07_two_api_processes_one_approval_per_round(api, engines, db_settings, tmp_path, record):
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    outbox = ctx.Queue()
    inboxes = {p: ctx.Queue() for p in ("marta", "priya")}
    procs = [ctx.Process(target=r07_worker.serve,
                         args=(db_settings.passwords, db_settings.name, db_settings.port, str(tmp_path / p), p,
                               inboxes[p], outbox, barrier))
             for p in inboxes]
    for proc in procs:
        proc.start()
    try:
        assert sorted(outbox.get(timeout=120)[1] for _ in procs) == ["marta", "priya"]
        rounds = []
        for _ in range(ROUNDS):
            op = api.ready()
            ref = api.version_ref(op)
            for q in inboxes.values():
                q.put((op, ref))
            results = [outbox.get(timeout=60) for _ in procs]
            api.wf.step()  # the parent's engine set hands the authorized operation off (HALDEN fake)
            # oracle SQL on the bootstrap superuser connection (lbf_audit_reader may read only the audit log)
            with engines["superuser"].connect() as conn:
                n_appr = conn.execute(select(func.count()).select_from(t.approvals)
                                      .where(t.approvals.c.operation_id == op)).scalar()
                n_auth = conn.execute(select(func.count()).select_from(t.authorizations)
                                      .where(t.authorizations.c.operation_id == op)).scalar()
            rounds.append({"approvals": n_appr, "authorizations": n_auth, "handoffs": api.wf.handoffs(op),
                           "statuses": sorted(r[1] for r in results), "state": api.wf.state(op)[0]})
    finally:
        for q in inboxes.values():
            q.put(None)
        for proc in procs:
            proc.join(timeout=60)
    worker_pids = {proc.pid for proc in procs}
    obs = {"rounds": len(rounds), "distinct_processes": len(worker_pids | {os.getpid()}),
           "per_round": {"approvals": single({r["approvals"] for r in rounds}),
                         "authorizations": single({r["authorizations"] for r in rounds}),
                         "max_handoffs": max(r["handoffs"] for r in rounds)},
           "status_pairs": sorted({tuple(r["statuses"]) for r in rounds}),
           "final_states": sorted({r["state"] for r in rounds})}
    record("R07", "two_processes", obs)
    assert all(r["approvals"] == 1 and r["authorizations"] == 1 and r["handoffs"] <= 1 for r in rounds), rounds
    assert all(r["statuses"] == [200, 409] for r in rounds), rounds


def test_r08_two_engines_claim_simultaneously(app_engine, tmp_path, record):
    wf = WF(app_engine, tmp_path)
    a, b = Executor(wf.ctx, "engine-A"), Executor(wf.ctx, "engine-B")
    per_op = []
    for _ in range(ROUNDS):
        op = wf.approved("halden")
        barrier = threading.Barrier(2)

        def claim(ex: Executor, barrier: threading.Barrier = barrier) -> object:
            barrier.wait()
            return ex.claim()

        with ThreadPoolExecutor(2) as pool:
            claims = [f.result() for f in [pool.submit(claim, a), pool.submit(claim, b)]]
        winners = [(ex, c) for ex, c in zip((a, b), claims, strict=True) if c is not None]
        assert len(winners) == 1, claims
        winners[0][0].process(winners[0][1])
        per_op.append((len(wf.transmissions(op)), wf.handoffs(op), wf.state(op)[0]))
    obs = {"rounds": ROUNDS, "transmissions": max(p[0] for p in per_op), "fake_handoffs": max(p[1] for p in per_op),
           "total_e8_at_fake": wf.halden_client.e8_request_count, "final_states": sorted({p[2] for p in per_op})}
    record("R08", "two_engines_claim", obs)
    assert obs["transmissions"] == 1 and obs["fake_handoffs"] == 1 and obs["total_e8_at_fake"] == ROUNDS
    assert obs["final_states"] == [S.CONFIRMED.value]
