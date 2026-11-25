"""SEC-13: the SQL trigger rejects every state change absent from the transition table and accepts each one in it."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from support import make_operation, make_user

from lbf.domain.enums import OperationState, Role
from lbf.domain.state_machine import allowed_state_changes

pytestmark = pytest.mark.db

ALL_PAIRS = [(a, b) for a, b in itertools.product(OperationState, OperationState) if a != b]


def test_every_pair_matches_the_python_table(app_engine, engines):
    creator = make_user(app_engine, "trig", Role.SALES_OPS)
    op_id = make_operation(app_engine, creator)
    allowed = allowed_state_changes()
    accepted = rejected = 0
    for a, b in ALL_PAIRS:
        with engines["superuser"].begin() as conn:  # put the row into state `a` without the trigger
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text("UPDATE lbf.operations SET state = :s WHERE id = :id"), {"s": a.value, "id": op_id})
        try:
            with app_engine.begin() as conn:
                conn.execute(text("UPDATE operations SET state = :s WHERE id = :id"), {"s": b.value, "id": op_id})
            assert (a, b) in allowed, f"SQL accepted invalid {a}->{b}"
            accepted += 1
        except DBAPIError as exc:
            assert "is not permitted" in str(exc)
            assert (a, b) not in allowed, f"SQL rejected valid {a}->{b}"
            rejected += 1
    assert accepted == len(allowed)
    assert accepted + rejected == len(ALL_PAIRS)
