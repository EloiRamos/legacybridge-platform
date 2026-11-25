"""Regressions: same-key create races (D2), database unavailability → 503 (D3), migration-aware
readiness (D5), and the preserved D1/D4 behaviour. The relay below loses real PostgreSQL connections, also exactly
at COMMIT, so uncertain outcomes are produced rather than simulated."""

from __future__ import annotations

import json
import socket
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from api_support import ORIGIN, Api
from conftest import BACKEND, REPO, TEST_PORT
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from support import PASSWORD
from workflow_support import WF

from lbf.api import readmodels
from lbf.api.openapi import document
from lbf.api.routers import health, queues
from lbf.domain.errors import HTTP_STATUS, ErrorCode
from lbf.persistence import tables as t
from lbf.persistence.audit import verify_chain
from lbf.persistence.db import make_engine
from lbf.persistence.settings import DbSettings

BODY = {"backend_id": "halden", "native_customer_id": "00417", "lines": [{"canonical_sku": "BW-1001", "quantity": 10}]}
OPS = "/api/v1/operations"


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


def soft(api, name):
    """A signed-in client that receives the response for unhandled errors, as a real HTTP client would."""
    c = api.as_(name)
    s = TestClient(api.app, base_url=ORIGIN, raise_server_exceptions=False)
    s.cookies.update(c.cookies)
    s.headers.update({"Origin": ORIGIN, "X-CSRF-Token": c.headers["X-CSRF-Token"]})
    return s


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


def created_events(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(t.audit_events)
                            .where(t.audit_events.c.event_type == "OPERATION_CREATED")).scalar()


def race(n, fn):
    gate, out, lock = threading.Barrier(n), [], threading.Lock()

    def go(i):
        gate.wait()
        r = fn(i)
        with lock:
            out.append(r)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for x in threads:
        x.start()
    for x in threads:
        x.join()
    assert len(out) == n
    return out


# ------------------------------------------------------------------------------------------------ D2


@pytest.mark.db
@pytest.mark.parametrize("threads", [2, 8])
def test_d2_same_key_race_creates_one_operation_and_every_caller_gets_it(api, threads):
    clients = [soft(api, "daniel") for _ in range(threads)]
    for n in range(20):
        before = count(api.wf.engine, t.operations)
        out = race(threads, lambda i, n=n: clients[i].post(OPS, json=BODY, headers={"Idempotency-Key": f"k-{n}"}))
        assert Counter(r.status_code for r in out) == {201: threads}, n
        assert len({json.dumps(r.json(), sort_keys=True) for r in out}) == 1, n  # identical documented response
        assert count(api.wf.engine, t.operations) == before + 1
        again = clients[0].post(OPS, json=BODY, headers={"Idempotency-Key": f"k-{n}"})
        assert again.status_code == 201 and again.json() == out[0].json()
    assert count(api.wf.engine, t.operations) == created_events(api.wf.engine) == 20
    assert count(api.wf.engine, t.draft_versions) == count(api.wf.engine, t.idempotency_keys) == 20
    with api.wf.engine.connect() as conn:
        assert verify_chain(conn).valid
    assert api.wf.halden_client.e8_request_count == 0


@pytest.mark.db
def test_d2_different_users_with_the_same_key_stay_isolated(api):
    clients = [soft(api, "daniel"), soft(api, "priya")] * 4
    for n in range(10):
        out = race(8, lambda i, n=n: (i % 2, clients[i].post(OPS, json=BODY, headers={"Idempotency-Key": f"k-{n}"})))
        assert {r.status_code for _, r in out} == {201}
        ids = [{r.json()["operation_id"] for u, r in out if u == user} for user in (0, 1)]
        assert [len(x) for x in ids] == [1, 1] and ids[0] != ids[1]
        for user, name in enumerate(("daniel", "priya")):
            row = api.wf.row(UUID(next(iter(ids[user]))))
            assert row.created_by == api.wf.users[name]
    assert count(api.wf.engine, t.operations) == 20


@pytest.mark.db
def test_d2_different_keys_stay_independent(api):
    clients = [soft(api, "daniel") for _ in range(8)]
    out = race(8, lambda i: clients[i].post(OPS, json=BODY, headers={"Idempotency-Key": f"k-{i}"}))
    assert {r.status_code for r in out} == {201} and len({r.json()["operation_id"] for r in out}) == 8


# ------------------------------------------------------------------------------------------------ D1 (preserved)


@pytest.mark.db
def test_d1_key_is_retained_beyond_the_24h_minimum(api):
    h = {"Idempotency-Key": "d1"}
    first = api.as_("daniel").post(OPS, json=BODY, headers=h).json()
    for delta in (timedelta(hours=23, minutes=59), timedelta(hours=1), timedelta(days=30)):
        api.wf.clock.advance(delta.total_seconds())
        api.clients.clear()
        r = api.as_("daniel").post(OPS, json=BODY, headers=h)
        assert (r.status_code, r.json()) == (201, first)
    assert count(api.wf.engine, t.operations) == count(api.wf.engine, t.idempotency_keys) == 1
    with api.wf.engine.connect() as conn:
        privileges = {p for p in ("SELECT", "INSERT", "UPDATE", "DELETE") if conn.execute(
            text("select has_table_privilege('lbf_app','lbf.idempotency_keys',:p)"), {"p": p}).scalar()}
    assert privileges == {"SELECT", "INSERT"}


# ------------------------------------------------------------------------------------------------ D3


class Relay:
    """TCP relay to the test cluster. ``cut`` drops and refuses connections; ``arm_commit`` drops the connection at
    the next COMMIT after ``skip`` others, either before the server sees it or after the server has answered it."""

    def __init__(self):
        self.up, self.socks, self.armed, self.fired = True, [], None, False
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.server.accept()
            except OSError:
                return
            if not self.up:
                c.close()
                continue
            u = socket.create_connection(("127.0.0.1", TEST_PORT))
            self.socks += [c, u]
            state = {"drop_reply": False}
            threading.Thread(target=self._pump, args=(c, u, state, True), daemon=True).start()
            threading.Thread(target=self._pump, args=(u, c, state, False), daemon=True).start()

    def _pump(self, a, b, state, from_client):
        try:
            while d := a.recv(65536):
                if from_client and self.armed is not None and b"COMMIT" in d:
                    mode, skip = self.armed
                    if skip:
                        self.armed = (mode, skip - 1)
                    else:
                        self.armed, self.fired = None, True
                        if mode == "never-sent":
                            break
                        state["drop_reply"] = True
                elif not from_client and state["drop_reply"]:
                    break  # the server has answered the COMMIT; the client never learns it
                b.sendall(d)
        except OSError:
            pass
        for x in (a, b):
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def arm_commit(self, mode, skip=0):
        self.armed, self.fired = (mode, skip), False

    def cut(self):
        self.up = False
        for x in self.socks:
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.socks.clear()

    def restore(self):
        self.up = True


@pytest.fixture
def relayed(db_settings, engines, tmp_path):
    relay = Relay()
    engine = make_engine(DbSettings(host="127.0.0.1", port=relay.port, name=db_settings.name,
                                    passwords=db_settings.passwords), "app", pool_size=4)
    api = Api(WF(engine, tmp_path))
    api.relay = relay
    yield api
    engine.dispose()
    relay.server.close()


def direct(engines):
    return engines["app"]  # reads the truth without the relay


def assert_database_unavailable(r):
    assert r.status_code == 503, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert set(body) == {"type", "title", "status", "code", "detail", "request_id"}
    assert (body["code"], body["status"], body["title"]) == ("DATABASE_UNAVAILABLE", 503, "Database unavailable")
    assert body["type"] == "https://legacybridge.invalid/problems/database_unavailable"
    assert body["request_id"] and body["request_id"] == r.headers["x-request-id"]
    low = r.text.lower()
    for internal in ("psycopg", "sqlalchemy", "postgres", "127.0.0.1", "lbf_app", "select", "insert", "traceback",
                     "connection", "server closed"):
        assert internal not in low
    for claim in ("not applied", "nothing was changed", "no change", "was not saved", "rolled back"):
        assert claim not in low
    assert "may or may not have been applied" in low  # never claims an outcome it cannot know


@pytest.mark.db
def test_d3_outage_is_503_for_commands_reads_and_login_without_backend_io(relayed, engines):
    api = relayed
    op = api.ready()
    ref = api.version_ref(op)
    d, m = soft(api, "daniel"), soft(api, "marta")
    anon = TestClient(api.app, base_url=ORIGIN, raise_server_exceptions=False)
    assert d.get(OPS).status_code == 200  # positive control
    ops, events = count(direct(engines), t.operations), count(direct(engines), t.audit_events)
    reads, e8 = api.wf.halden.read_count, api.wf.halden_client.e8_request_count
    api.relay.cut()
    for _ in range(2):  # first with stale pooled connections, then with connections that cannot be opened
        for call in (lambda: d.get(OPS), lambda: d.get("/api/v1/dashboard"),
                     lambda: d.post(OPS, json=BODY, headers={"Idempotency-Key": "d3"}),
                     lambda: d.post(f"{OPS}/{op}/submit", json={"expected_version": 1}),
                     lambda: m.post(f"{OPS}/{op}/approve", json=ref),
                     lambda: anon.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                                       json={"username": "daniel.sales", "password": PASSWORD})):
            assert_database_unavailable(call())
    assert (api.wf.halden.read_count, api.wf.halden_client.e8_request_count) == (reads, e8)
    assert anon.get("/healthz").status_code == 200
    ready = anon.get("/readyz")
    assert ready.status_code == 503 and ready.json()["db"] == "unavailable"
    api.relay.restore()
    assert d.get(OPS).status_code == 200
    assert (count(direct(engines), t.operations), count(direct(engines), t.audit_events)) == (ops, events)
    assert api.wf.state(UUID(op))[0] == "READY"


@pytest.mark.db
def test_d3_outage_mid_command_is_503_without_handoff(relayed, engines):
    api = relayed
    d = soft(api, "daniel")
    port, original = api.wf.ports["halden"], api.wf.ports["halden"].get_customer

    def cut_after_read(*a, **k):
        x = original(*a, **k)
        api.relay.cut()
        return x

    port.get_customer = cut_after_read
    assert_database_unavailable(d.post(OPS, json=BODY))
    assert count(direct(engines), t.operations) == 0 and api.wf.halden_client.e8_request_count == 0


@pytest.mark.db
def test_d3_pool_exhaustion_is_503(api):
    d = soft(api, "daniel")
    engine = make_engine(DbSettings(host="127.0.0.1", port=TEST_PORT, name=api.wf.engine.url.database,
                                    passwords={"app": api.wf.engine.url.password}), "app", pool_size=1)
    engine.pool._max_overflow, engine.pool._timeout = 0, 0.2
    api.svc.engine = engine
    try:
        with engine.connect():
            assert_database_unavailable(d.get(OPS))
        assert d.get(OPS).status_code == 200
    finally:
        engine.dispose()


@pytest.mark.db
def test_d3_connection_refused_is_503(api, db_settings):
    d = soft(api, "daniel")
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    api.svc.engine = make_engine(DbSettings(host="127.0.0.1", port=port, name=db_settings.name,
                                            passwords=db_settings.passwords), "app")
    assert_database_unavailable(d.get(OPS))


@pytest.mark.db
def test_d3_no_connection_slot_is_503(api, db_settings, engines):
    d = soft(api, "daniel")
    api.svc.engine = make_engine(db_settings, "app")
    with engines["superuser"].begin() as conn:
        conn.execute(text("alter role lbf_app connection limit 0"))
    try:
        assert_database_unavailable(d.get(OPS))
    finally:
        with engines["superuser"].begin() as conn:
            conn.execute(text("alter role lbf_app connection limit -1"))
    assert d.get(OPS).status_code == 200
    api.svc.engine.dispose()


@pytest.mark.db
def test_d3_backend_terminated_by_the_server_is_503(api, engines, monkeypatch):
    d = soft(api, "daniel")
    original = readmodels.list_operations

    def terminated(conn, **kw):
        pid = conn.execute(text("select pg_backend_pid()")).scalar()
        with engines["superuser"].connect() as su:
            su.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
        return original(conn, **kw)

    monkeypatch.setattr(readmodels, "list_operations", terminated)
    assert_database_unavailable(d.get(OPS))
    monkeypatch.undo()
    assert d.get(OPS).status_code == 200


@pytest.mark.db
@pytest.mark.parametrize(("sql", "cls"), [
    ("select pg_sleep(5)", OperationalError),  # QueryCanceled: an OperationalError on a healthy connection
    ("select 1/0", Exception),
    ("select * from lbf.no_such_table", Exception),
])
def test_d3_sql_failures_on_a_healthy_connection_stay_internal_500(api, monkeypatch, sql, cls):
    d = soft(api, "daniel")

    def broken(conn, **_):
        conn.execute(text("set local statement_timeout = 50"))
        with pytest.raises(cls) as e:
            conn.execute(text(sql))
        raise e.value

    monkeypatch.setattr(readmodels, "list_operations", broken)
    r = d.get(OPS)
    assert (r.status_code, r.json()["code"], r.json()["detail"]) == (500, "INTERNAL", "")


@pytest.mark.db
def test_d3_too_many_bind_parameters_is_a_bug_not_an_outage(api, monkeypatch):
    """libpq reports this as an OperationalError without SQLSTATE although the connection is healthy."""
    d = soft(api, "daniel")

    def broken(conn, **_):
        conn.exec_driver_sql("select " + ",".join(["%s"] * 70_000), tuple(["x"] * 70_000))

    monkeypatch.setattr(readmodels, "list_operations", broken)
    r = d.get(OPS)
    assert (r.status_code, r.json()["code"]) == (500, "INTERNAL")


# --- COMMIT uncertainty: the same response whether or not the server applied the transaction

MODES = [("never-sent", False), ("reply-lost", True)]


@pytest.mark.db
@pytest.mark.parametrize(("mode", "applied"), MODES)
def test_d3_uncertain_commit_of_create_claims_no_outcome_and_same_key_retry_is_safe(relayed, engines, mode, applied):
    api = relayed
    d = soft(api, "daniel")
    port, original = api.wf.ports["halden"], api.wf.ports["halden"].get_customer

    def arm_after_read(*a, **k):
        port.get_customer = original
        api.relay.arm_commit(mode)  # authentication has committed; the next COMMIT is T-DRAFT
        return original(*a, **k)

    port.get_customer = arm_after_read
    h = {"Idempotency-Key": "uncertain"}
    assert_database_unavailable(d.post(OPS, json=BODY, headers=h))
    assert api.relay.fired
    assert count(direct(engines), t.operations) == (1 if applied else 0)
    r = d.post(OPS, json=BODY, headers=h)
    assert r.status_code == 201 and count(direct(engines), t.operations) == 1
    assert created_events(direct(engines)) == 1 and api.wf.halden_client.e8_request_count == 0


@pytest.mark.db
@pytest.mark.parametrize(("mode", "applied"), MODES)
def test_d3_uncertain_commit_of_approval_claims_no_outcome(relayed, engines, mode, applied):
    api = relayed
    op = api.ready()
    ref = api.version_ref(op)
    m = soft(api, "marta")
    api.relay.arm_commit(mode, skip=1)  # skip the authentication COMMIT
    assert_database_unavailable(m.post(f"{OPS}/{op}/approve", json=ref))
    assert api.relay.fired
    assert api.wf.state(UUID(op))[0] == ("APPROVED" if applied else "READY")
    assert count(direct(engines), t.authorizations) == (1 if applied else 0)
    assert api.wf.halden_client.e8_request_count == 0


# --- no backend I/O without a committed precondition


@contextmanager
def counted_withdrawals(port):
    calls, original = [], port.withdraw_unclaimed
    port.withdraw_unclaimed = lambda mid: (calls.append(mid), original(mid))[1]
    yield calls
    port.withdraw_unclaimed = original


@pytest.mark.db
@pytest.mark.parametrize(("mode", "applied"), MODES)
def test_d3_withdrawal_file_operation_needs_an_acknowledged_commit(relayed, engines, mode, applied):
    api = relayed
    op = api.wf.approved("skarvik")
    api.wf.step()
    assert api.wf.state(op)[0] == "AWAITING_ACK"
    m = soft(api, "marta")
    before = count(direct(engines), t.authorizations)
    api.relay.arm_commit(mode, skip=1)
    with counted_withdrawals(api.wf.ports["skarvik"]) as calls:
        assert_database_unavailable(m.post(f"{OPS}/{op}/withdraw-unclaimed"))
    assert api.relay.fired and calls == []
    assert count(direct(engines), t.authorizations) == before + (1 if applied else 0)
    assert api.wf.handoffs(op) == 1  # the original message is still there


@pytest.mark.db
def test_d3_outage_after_the_withdrawal_file_operation_claims_no_outcome(relayed, engines):
    api = relayed
    op = api.wf.approved("skarvik")
    api.wf.step()
    m = soft(api, "marta")
    port, original = api.wf.ports["skarvik"], api.wf.ports["skarvik"].withdraw_unclaimed

    def withdraw_then_cut(mid):
        x = original(mid)
        api.relay.cut()
        return x

    port.withdraw_unclaimed = withdraw_then_cut
    assert_database_unavailable(m.post(f"{OPS}/{op}/withdraw-unclaimed"))
    api.relay.restore()
    assert api.wf.state(op)[0] == "AWAITING_ACK"  # no outcome recorded, none claimed


@pytest.mark.db
@pytest.mark.parametrize("mode", ["outage", "never-sent", "reply-lost"])
def test_d3_executor_hands_nothing_off_without_an_acknowledged_t_begin(relayed, engines, mode):
    api = relayed
    op = api.wf.approved("halden")

    def lose(name):
        if name == "after_claim":
            if mode == "outage":
                api.relay.cut()
            else:
                api.relay.arm_commit(mode)

    api.wf.hook = lose
    with pytest.raises(OperationalError):
        api.wf.engines.executor.run_once()
    api.wf.hook = None
    api.relay.restore()
    assert mode == "outage" or api.relay.fired
    assert api.wf.halden_client.e8_request_count == 0 and api.wf.handoffs(op) == 0
    assert count(direct(engines), t.transmissions) == (1 if mode == "reply-lost" else 0)


def test_d3_taxonomy_openapi_and_client_types_agree():
    assert HTTP_STATUS[ErrorCode.DATABASE_UNAVAILABLE] == 503
    doc = document()
    assert doc == json.loads((REPO / "docs/api/openapi.json").read_text(encoding="utf-8"))
    codes = {c.value for c, s in HTTP_STATUS.items() if s == 503}
    for path, methods in doc["paths"].items():
        for method, op in methods.items():
            r = op["responses"].get("503")
            if path == "/readyz":
                continue  # its 503 is the readiness report, not a problem
            assert (r is not None) == path.startswith("/api/v1/"), (method, path)
            if r:
                schema = r["content"]["application/problem+json"]["schema"]
                assert list(r["content"]) == ["application/problem+json"]
                assert set(schema["properties"]["code"]["enum"]) == codes
                assert set(schema["required"]) == {"type", "title", "status", "code", "detail", "request_id"}
    assert "DATABASE_UNAVAILABLE" in (REPO / "frontend/src/api/schema.d.ts").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ D4


@pytest.mark.db
def test_d4_dashboard_reads_stock_on_every_request_and_declares_no_cache(api):
    c, calls = api.as_("daniel"), Counter()
    for b, port in api.wf.ports.items():
        def stock(original=port.stock, b=b):
            calls[b] += 1
            return original()
        port.stock = stock
    for _ in range(3):
        assert c.get("/api/v1/dashboard").status_code == 200
    assert calls == {"halden": 3, "skarvik": 3}
    assert not [n for n in vars(queues) if not n.startswith("__") and ("cache" in n.lower() or "ttl" in n.lower())]


# ------------------------------------------------------------------------------------------------ D5


def test_d5_expected_revision_is_the_packaged_head():
    script = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    assert script.get_heads() == [health.EXPECTED_MIGRATION_HEAD]


@pytest.fixture
def revision(engines):
    """Sets the recorded revision(s) as the superuser and restores the original ones."""
    su = engines["superuser"]

    def put(*values):
        with su.begin() as conn:
            conn.execute(text("delete from lbf.alembic_version"))
            for v in values:
                conn.execute(text("insert into lbf.alembic_version values (:v)"), {"v": v})

    with su.connect() as conn:
        original = conn.execute(text("select version_num from lbf.alembic_version")).scalars().all()
    yield put
    put(*original)


@pytest.mark.db
def test_d5_ready_only_at_head(api):
    r = api.anon().get("/readyz")
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["db"], r.json()["migrations"]) == ("ready", "ok", "head")


@pytest.mark.db
@pytest.mark.parametrize("recorded", [(), ("0003_workflow_engine",), ("0001_initial",), ("9999_unknown",), ("",),
                                      ("0003_workflow_engine", "0004_inference")])
def test_d5_missing_outdated_unknown_or_ambiguous_revision_is_503(api, revision, recorded):
    revision(*recorded)
    r = api.anon().get("/readyz")
    body = r.json()
    assert (r.status_code, body["status"], body["db"], body["migrations"]) == (503, "not-ready", "ok", "not-head")
    assert {b["backend_id"] for b in body["backends"]} == {"halden", "skarvik"}  # the other checks still report
    assert api.anon().get("/healthz").status_code == 200
    revision(health.EXPECTED_MIGRATION_HEAD)
    assert api.anon().get("/readyz").status_code == 200


@pytest.mark.db
@pytest.mark.parametrize(("break_it", "repair"), [
    ("alter table lbf.alembic_version rename to alembic_version_x",
     "alter table lbf.alembic_version_x rename to alembic_version"),
    ("revoke select on lbf.alembic_version from lbf_app", "grant select on lbf.alembic_version to lbf_app"),
])
def test_d5_unreadable_revision_is_503(api, engines, break_it, repair):
    with engines["superuser"].begin() as conn:
        conn.execute(text(break_it))
    try:
        r = api.anon().get("/readyz")
    finally:
        with engines["superuser"].begin() as conn:
            conn.execute(text(repair))
    assert (r.status_code, r.json()["status"], r.json()["db"], r.json()["migrations"]) == (
        503, "not-ready", "ok", "unknown")
    assert "alembic" not in r.text


@pytest.mark.db
def test_d5_real_downgrade_is_503_until_upgraded(api, db_settings):
    from alembic import command

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["url"] = db_settings.url("migrator")
    command.downgrade(cfg, "-1")
    try:
        assert api.anon().get("/readyz").status_code == 503
    finally:
        command.upgrade(cfg, "head")
    assert api.anon().get("/readyz").status_code == 200


@pytest.mark.db
def test_d5_existing_backend_check_is_preserved(api, monkeypatch):
    port = api.wf.ports["halden"]

    def down():
        raise RuntimeError("down")

    monkeypatch.setattr(port, "health", down)
    r = api.anon().get("/readyz")
    assert (r.status_code, r.json()["status"], r.json()["migrations"]) == (503, "not-ready", "head")
