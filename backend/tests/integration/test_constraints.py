"""Database constraints behind G1 and the redelivery limit."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from support import approve_tx, make_operation, make_user, now

from lbf.domain.enums import AuthorizationKind, OperationState, Role
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t

pytestmark = pytest.mark.db


def _transmission(conn, op_id, auth_id, *, attempt=1, no=1, kind="SUBMIT"):
    payload = b"acct=00417&ref=LB0000000000"
    conn.execute(
        t.transmissions.insert().values(
            id=uuid.uuid4(),
            authorization_id=auth_id,
            operation_id=op_id,
            attempt_no=attempt,
            transmission_no=no,
            kind=kind,
            message_id=None,
            payload=payload,
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            transport_descriptor="test",
            state="SENDING",
            engine_id="e1",
            started_at=now(),
            lease_until=now() + timedelta(seconds=30),
        )
    )


@pytest.fixture
def approved(app_engine):
    creator = make_user(app_engine, "creator", Role.SALES_OPS)
    approver = make_user(app_engine, "approver", Role.OPS_MANAGER)
    op_id = make_operation(app_engine, creator, state=OperationState.READY)
    auth_id = approve_tx(app_engine, op_id, approver)
    return op_id, auth_id, approver


def test_f2_02_unique_authorization_id_on_transmissions(app_engine, approved):
    op_id, auth_id, _ = approved
    with app_engine.begin() as conn:
        _transmission(conn, op_id, auth_id)
    with pytest.raises(IntegrityError, match="transmissions_authorization_id_key"), app_engine.begin() as conn:
        _transmission(conn, op_id, auth_id, no=2)


def test_m4_database_rejects_second_redelivery_for_same_attempt(app_engine, approved):
    op_id, auth_id, approver = approved
    with app_engine.begin() as conn:
        _transmission(conn, op_id, auth_id)
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(state="CLAIMED"))
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(state="CONSUMED"))
        r1 = repo.insert_authorization(
            conn,
            operation_id=op_id,
            kind=AuthorizationKind.REDELIVER,
            attempt_no=1,
            approval_id=None,
            granted_by=approver,
            now=now(),
            expires_at=now() + timedelta(minutes=15),
        )
        _transmission(conn, op_id, r1, no=2, kind="REDELIVER")
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == r1).values(state="CLAIMED"))
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == r1).values(state="CONSUMED"))
    with app_engine.begin() as conn:
        r2 = repo.insert_authorization(
            conn,
            operation_id=op_id,
            kind=AuthorizationKind.REDELIVER,
            attempt_no=1,
            approval_id=None,
            granted_by=approver,
            now=now(),
            expires_at=now() + timedelta(minutes=15),
        )
    with pytest.raises(IntegrityError, match="transmissions_one_redelivery_per_attempt"), app_engine.begin() as conn:
        _transmission(conn, op_id, r2, no=3, kind="REDELIVER")
    with app_engine.connect() as conn:
        assert repo.count(conn, t.transmissions, t.transmissions.c.kind == "REDELIVER") == 1


def test_one_open_authorization_per_operation(app_engine, approved):
    op_id, _, approver = approved
    with pytest.raises(IntegrityError, match="authorizations_one_open_per_operation"), app_engine.begin() as conn:
        repo.insert_authorization(
            conn,
            operation_id=op_id,
            kind=AuthorizationKind.WITHDRAW,
            attempt_no=1,
            approval_id=None,
            granted_by=approver,
            now=now(),
            expires_at=now() + timedelta(minutes=15),
        )


def test_one_approval_per_attempt(app_engine, approved):
    op_id, _, approver = approved
    with app_engine.connect() as conn:
        draft = repo.load_draft_version(conn, op_id, 1)
    with pytest.raises(IntegrityError, match="approvals_one_per_attempt"), app_engine.begin() as conn:
        repo.insert_approval(
            conn,
            operation_id=op_id,
            version=1,
            content_hash=draft.content_hash,
            attempt_no=1,
            decision="APPROVED",
            decided_by=approver,
            now=now(),
            policy_version="x",
            checks=[],
        )


def test_authorization_state_regression_and_release_with_transmission_refused(app_engine, approved):
    op_id, auth_id, _ = approved
    with app_engine.begin() as conn:
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(state="CLAIMED"))
        _transmission(conn, op_id, auth_id)
    with pytest.raises(DBAPIError, match="cannot be released"), app_engine.begin() as conn:
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(state="PENDING"))
    with app_engine.begin() as conn:
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(state="CONSUMED"))
    with pytest.raises(DBAPIError, match="not permitted"), app_engine.begin() as conn:
        conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(state="PENDING"))


def test_transmission_lifecycle_guard(app_engine, approved):
    op_id, auth_id, _ = approved
    with app_engine.begin() as conn:
        _transmission(conn, op_id, auth_id)
        conn.execute(
            update(t.transmissions)
            .where(t.transmissions.c.authorization_id == auth_id)
            .values(state="OBSERVED", observed_at=now())
        )
    for values in ({"state": "SENDING"}, {"state": "INTERRUPTED"}):
        with pytest.raises(DBAPIError, match="not permitted"), app_engine.begin() as conn:
            conn.execute(update(t.transmissions).where(t.transmissions.c.authorization_id == auth_id).values(**values))


def test_payload_size_limit(app_engine, approved):
    op_id, auth_id, _ = approved
    big = b"x" * 65537
    with pytest.raises(IntegrityError), app_engine.begin() as conn:
        conn.execute(
            t.transmissions.insert().values(
                id=uuid.uuid4(),
                authorization_id=auth_id,
                operation_id=op_id,
                attempt_no=1,
                transmission_no=1,
                kind="SUBMIT",
                payload=big,
                payload_sha256="0" * 64,
                transport_descriptor="t",
                state="SENDING",
                engine_id="e",
                started_at=now(),
                lease_until=now(),
            )
        )


def test_new_operation_must_start_in_draft(app_engine):
    creator = make_user(app_engine, "c2", Role.SALES_OPS)
    with pytest.raises(DBAPIError, match="must start in DRAFT"), app_engine.begin() as conn:
        conn.execute(
            t.operations.insert().values(
                id=uuid.uuid4(),
                correlation_value="LB0000000009",
                backend_id="halden",
                native_customer_id="00417",
                state="APPROVED",
                current_version=1,
                attempt_count=0,
                created_by=creator,
                origin="UI",
                created_at=now(),
                updated_at=now(),
                row_version=0,
                auto_rechecks=0,
            )
        )
    with app_engine.connect() as conn:
        assert conn.execute(select(text("count(*)")).select_from(t.operations)).scalar_one() == 0
