# ruff: noqa: B023  (lambdas are executed within the same loop iteration)
"""C-01 optimistic edits, C-02 approval race, C-03 approve-vs-revise race, C-08 concurrent audit appends.

Each race runs 50 rounds with two real threads against PostgreSQL (S2-2: any duplicate approval or
authorization in any round is a STOP).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from support import approve_tx, content, make_operation, make_user

from lbf.domain.enums import DraftOrigin, OperationState, Role
from lbf.domain.errors import DomainError, ErrorCode
from lbf.domain.models import DraftVersion
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.audit import AuditEvent, verify_chain
from lbf.persistence.uow import UnitOfWork

pytestmark = pytest.mark.db
ROUNDS = 50


def revise_tx(engine, op_id, author, expected_version):
    """Mirror of T-DRAFT: optimistic version check under the row lock."""
    with UnitOfWork(engine) as uow:
        row = uow.lock_operation(op_id)
        if row.current_version != expected_version:
            raise DomainError(ErrorCode.CONFLICT_VERSION)
        new_version = row.current_version + 1
        repo.insert_draft_version(
            uow.conn,
            DraftVersion.build(
                operation_id=op_id,
                version=new_version,
                content=content(qty=11),
                created_by=author,
                origin=DraftOrigin.UI,
                created_at=uow.clock(),
            ),
        )
        tid = {"DRAFT": "T02", "READY": "T04"}[row.state]
        uow.transition(row, OperationState.DRAFT, tid, AuditEvent("DRAFT_VERSION_CREATED"), current_version=new_version)


def _race(fn_a, fn_b):
    barrier = threading.Barrier(2)

    def wrap(fn):
        barrier.wait()
        try:
            fn()
            return "ok"
        except Exception as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(2) as pool:
        fa, fb = pool.submit(wrap, fn_a), pool.submit(wrap, fn_b)
        return fa.result(), fb.result()


@pytest.fixture
def users(app_engine):
    return (
        make_user(app_engine, "daniel", Role.SALES_OPS),
        make_user(app_engine, "marta", Role.OPS_MANAGER),
        make_user(app_engine, "priya", Role.SALES_OPS),
    )


def test_c01_optimistic_draft_edits(app_engine, users):
    daniel, _, priya = users
    for _ in range(ROUNDS):
        op_id = make_operation(app_engine, daniel)
        outcomes = _race(
            lambda: revise_tx(app_engine, op_id, daniel, 1), lambda: revise_tx(app_engine, op_id, priya, 1)
        )
        assert sorted(outcomes) == ["DomainError", "ok"], outcomes
        with app_engine.connect() as conn:
            assert repo.count(conn, t.draft_versions, t.draft_versions.c.operation_id == op_id) == 2


def test_c02_approval_race(app_engine, users):
    daniel, marta, priya = users
    for _ in range(ROUNDS):
        op_id = make_operation(app_engine, daniel, state=OperationState.READY)
        outcomes = _race(lambda: approve_tx(app_engine, op_id, marta), lambda: approve_tx(app_engine, op_id, priya))
        assert sorted(outcomes) == ["RuntimeError", "ok"], outcomes
        with app_engine.connect() as conn:
            assert repo.count(conn, t.approvals, t.approvals.c.operation_id == op_id) == 1
            assert repo.count(conn, t.authorizations, t.authorizations.c.operation_id == op_id) == 1


def test_c03_approve_versus_revise(app_engine, users):
    daniel, marta, _ = users
    seen = set()
    for _ in range(ROUNDS):
        op_id = make_operation(app_engine, daniel, state=OperationState.READY)
        outcomes = _race(lambda: approve_tx(app_engine, op_id, marta), lambda: revise_tx(app_engine, op_id, daniel, 1))
        seen.add(outcomes)
        with app_engine.connect() as conn:
            op = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).one()
            approvals = conn.execute(select(t.approvals).where(t.approvals.c.operation_id == op_id)).all()
        if op.state == OperationState.APPROVED.value:
            # approval won: it bound version 1, and the revise was refused
            assert outcomes[0] == "ok" and outcomes[1] != "ok"
            assert len(approvals) == 1 and approvals[0].version == 1 and op.current_version == 1
        else:
            # revise won: no approval exists for any version
            assert op.state == OperationState.DRAFT.value and op.current_version == 2 and approvals == []
            assert outcomes == ("RuntimeError", "ok")


def test_c08_concurrent_audit_appends_keep_chain_contiguous(app_engine, engines, users):
    daniel = users[0]
    ops = [make_operation(app_engine, daniel) for _ in range(8)]

    def worker(op_id):
        for _ in range(ROUNDS):
            with UnitOfWork(app_engine) as uow:
                row = uow.lock_operation(op_id)
                to, tid = (OperationState.READY, "T03") if row.state == "DRAFT" else (OperationState.DRAFT, "T04")
                uow.transition(row, to, tid, AuditEvent("TEST_TRANSITION"))

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(worker, ops))
    with engines["audit_reader"].connect() as conn:
        result = verify_chain(conn)
    assert result.valid and result.count == len(ops) + len(ops) * ROUNDS + 3 * 2  # creations, transitions, users
