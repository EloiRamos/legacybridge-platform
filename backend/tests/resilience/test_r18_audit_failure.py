"""R18a: an audit insert that fails rolls the whole approval back — no approval, no authorization, state READY."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from api_support import ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from lbf.persistence import tables as t

pytestmark = pytest.mark.db
BACKEND = __import__("pathlib").Path(__file__).resolve().parents[2]


@pytest.fixture
def audit_fault(db_settings, engines):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["url"] = db_settings.url("migrator")
    locations = [BACKEND / "migrations" / "versions", BACKEND / "migrations" / "test_faults"]
    cfg.set_main_option("version_locations", ":".join(str(x) for x in locations))
    command.upgrade(cfg, "test_faults@head")
    try:
        with engines["migrator"].begin() as conn:
            conn.execute(text("INSERT INTO lbf.test_fault_audit VALUES ('APPROVAL_GRANTED')"))
        yield
    finally:
        command.downgrade(cfg, "test_faults@base")
    with engines["migrator"].connect() as conn:
        assert conn.execute(text("SELECT to_regclass('lbf.test_fault_audit')")).scalar() is None


def test_r18a_audit_failure_rolls_back_the_approval(api, audit_fault, engines, record):
    op = api.ready()
    events_before = api.wf.events(op)
    marta = api.as_("marta")
    client = TestClient(api.app, base_url=ORIGIN, raise_server_exceptions=False)  # as a real server: a 500 response
    client.cookies = marta.cookies
    client.headers.update(marta.headers)
    r = client.post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    with engines["superuser"].connect() as conn:
        approvals = conn.execute(select(func.count()).select_from(t.approvals)
                                 .where(t.approvals.c.operation_id == op)).scalar()
        authorizations = conn.execute(select(func.count()).select_from(t.authorizations)
                                      .where(t.authorizations.c.operation_id == op)).scalar()
    obs = {"http_status": r.status_code, "approvals": approvals, "authorizations": authorizations,
           "state": api.wf.state(op)[0], "events_unchanged": api.wf.events(op) == events_before}
    record("R18", "audit_insert_failure", obs)
    assert r.status_code >= 500 and approvals == 0 and authorizations == 0 and obs["state"] == "READY"
    assert obs["events_unchanged"]
