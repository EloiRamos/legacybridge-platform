"""V2-5: the audit chain verifies after 1 000 random transitions; tampering is located exactly."""

from __future__ import annotations

import random

import pytest
from sqlalchemy import text
from support import make_operation, make_user

from lbf.domain import state_machine as sm
from lbf.domain.enums import TERMINAL_STATES, OperationState, Role
from lbf.persistence.audit import AuditEvent, verify_chain
from lbf.persistence.uow import UnitOfWork

pytestmark = pytest.mark.db


def _random_walk(engine, creator, steps: int, rng: random.Random) -> int:
    done = 0
    op_id = make_operation(engine, creator)
    while done < steps:
        with UnitOfWork(engine) as uow:
            row = uow.lock_operation(op_id)
            state = OperationState(row.state)
            options = [(t.id, t.target) for t in sm.TRANSITIONS if state in t.sources]
            if state in TERMINAL_STATES or not options:
                pass
            else:
                tid, target = rng.choice(options)
                uow.transition(row, target, tid, AuditEvent("TEST_TRANSITION", payload={"step": done}))
                done += 1
                continue
        op_id = make_operation(engine, creator)
    return done


def test_chain_verifies_after_1000_random_transitions(app_engine, engines):
    creator = make_user(app_engine, "walker", Role.SALES_OPS)
    assert _random_walk(app_engine, creator, 1000, random.Random(20261121)) == 1000
    with engines["audit_reader"].connect() as conn:
        result = verify_chain(conn)
        transitions = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE event_type='TEST_TRANSITION'")
        ).scalar_one()
    assert result.valid, result
    assert transitions == 1000
    assert result.count >= 1000


def test_tampered_row_is_located(app_engine, engines):
    creator = make_user(app_engine, "walker2", Role.SALES_OPS)
    _random_walk(app_engine, creator, 20, random.Random(1))
    with engines["superuser"].begin() as conn:  # a privileged DBA bypassing the append-only trigger
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE lbf.audit_events SET payload = '{\"step\": 999}'::jsonb WHERE seq = 7"))
    with engines["audit_reader"].connect() as conn:
        result = verify_chain(conn)
    assert not result.valid and result.first_broken_seq == 7 and result.reason == "event_hash mismatch"


def test_rolled_back_transaction_consumes_no_sequence(app_engine, engines):
    creator = make_user(app_engine, "rb", Role.SALES_OPS)
    op_id = make_operation(app_engine, creator)
    with pytest.raises(RuntimeError), UnitOfWork(app_engine) as uow:
        row = uow.lock_operation(op_id)
        uow.transition(row, OperationState.READY, "T03", AuditEvent("TEST_TRANSITION"))
        raise RuntimeError("abort")
    with UnitOfWork(app_engine) as uow:
        row = uow.lock_operation(op_id)
        uow.transition(row, OperationState.READY, "T03", AuditEvent("TEST_TRANSITION"))
    with engines["audit_reader"].connect() as conn:
        assert verify_chain(conn).valid
