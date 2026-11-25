"""SEC-12 / E-06: the application role cannot rewrite append-only evidence (privileges + triggers)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from support import approve_tx, make_operation, make_user

from lbf.domain.enums import OperationState, Role

pytestmark = pytest.mark.db


@pytest.fixture
def populated(app_engine):
    creator = make_user(app_engine, "creator", Role.SALES_OPS)
    approver = make_user(app_engine, "approver", Role.OPS_MANAGER)
    op_id = make_operation(app_engine, creator, state=OperationState.READY)
    approve_tx(app_engine, op_id, approver)
    return op_id


REFUSED_AS_APP = [
    "UPDATE audit_events SET payload = '{}'::jsonb",
    "DELETE FROM audit_events",
    "TRUNCATE audit_events",
    "UPDATE approvals SET decision = 'DECLINED'",
    "DELETE FROM approvals",
    "UPDATE draft_versions SET content_hash = repeat('0', 64)",
    "DELETE FROM draft_versions",
    "DELETE FROM operations",
    "DELETE FROM authorizations",
    "UPDATE authorizations SET kind = 'REDELIVER'",
    "UPDATE authorizations SET expires_at = now() + interval '1 day'",
    "UPDATE operations SET correlation_value = 'LB0000000000'",
    "UPDATE operations SET created_by = gen_random_uuid()",
    "INSERT INTO backends (id, capabilities, contract_version) VALUES ('halden', '{}', 'x')",
    "UPDATE item_mappings SET factor_num = 2",
    "INSERT INTO allowed_transitions VALUES ('UNKNOWN', 'READY')",
    "UPDATE audit_exports SET head_hash = repeat('0', 64)",
    "DELETE FROM validation_snapshots",
]


@pytest.mark.parametrize("statement", REFUSED_AS_APP)
def test_sec12_app_role_refused(app_engine, populated, statement):
    with pytest.raises((ProgrammingError, DBAPIError)) as exc, app_engine.begin() as conn:
        conn.execute(text(statement))
    assert "permission denied" in str(exc.value) or "append-only" in str(exc.value)


def test_owner_is_still_stopped_by_append_only_triggers(engines, populated):
    """Even the schema owner (lbf_migrator) cannot UPDATE/DELETE evidence rows: the trigger fires."""
    for statement in ("UPDATE lbf.audit_events SET payload = '{}'::jsonb", "DELETE FROM lbf.approvals"):
        with pytest.raises(DBAPIError, match="append-only"), engines["migrator"].begin() as conn:
            conn.execute(text(statement))


def test_audit_reader_sees_only_audit_tables(engines, populated):
    with engines["audit_reader"].connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() > 0
    with pytest.raises(ProgrammingError, match="permission denied"), engines["audit_reader"].connect() as conn:
        conn.execute(text("SELECT count(*) FROM operations"))
    with pytest.raises(ProgrammingError, match="permission denied"), engines["audit_reader"].begin() as conn:
        conn.execute(
            text("INSERT INTO audit_exports VALUES (gen_random_uuid(), gen_random_uuid(), 1, 1, 'x', 'y', now())")
        )


def test_app_role_holds_no_ddl_or_superuser(engines):
    with engines["superuser"].connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname='lbf_app'")
        ).one()
    assert tuple(row) == (False, False, False)
    with pytest.raises(ProgrammingError), engines["app"].begin() as conn:
        conn.execute(text("CREATE TABLE lbf.x (id int)"))
