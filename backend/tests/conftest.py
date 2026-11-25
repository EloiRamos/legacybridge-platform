"""Shared test fixtures.

* Network guard (AI-OAI-12): a default-on, session-wide guard makes every Python-level socket connection to a
  non-loopback address fail the test session. PostgreSQL connections go through libpq (C) and are not seen
  by this guard; the test DSN is pinned to 127.0.0.1.
* Database: ``db``-marked tests use a disposable database ``lbf_test`` on the test-profile PostgreSQL
  (127.0.0.1:55432), migrated to head with Alembic as ``lbf_migrator``; tables are truncated between tests
  by the bootstrap superuser (seed tables are kept).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SECRETS = Path(os.environ.get("LBF_TEST_SECRETS_DIR") or REPO / "deploy" / "secrets")  # synthetic values elsewhere

# ----------------------------------------------------------------------------- network guard

NON_LOOPBACK_ATTEMPTS: list[str] = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_loopback(address: object) -> bool:
    if isinstance(address, str | bytes):  # AF_UNIX path
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host in ("localhost",)
    return False


def _guarded_connect(self: socket.socket, address: object) -> object:
    if not _is_loopback(address):
        NON_LOOPBACK_ATTEMPTS.append(repr(address))
        raise ConnectionRefusedError(f"network guard: non-loopback connection blocked: {address!r}")
    return _real_connect(self, address)  # type: ignore[arg-type]


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if not _is_loopback(address):
        NON_LOOPBACK_ATTEMPTS.append(repr(address))
        raise ConnectionRefusedError(f"network guard: non-loopback connection blocked: {address!r}")
    return _real_connect_ex(self, address)  # type: ignore[arg-type]


def pytest_configure(config: pytest.Config) -> None:
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"network guard: {len(NON_LOOPBACK_ATTEMPTS)} non-loopback connection attempts")
    if NON_LOOPBACK_ATTEMPTS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ----------------------------------------------------------------------------- database

TEST_DB = os.environ.get("LBF_TEST_DB_NAME", "lbf_test")
TEST_PORT = int(os.environ.get("LBF_TEST_DB_PORT", "55432"))
KEEP_TABLES = {"backends", "parties", "party_accounts", "item_mappings", "allowed_transitions", "alembic_version"}


def _secret(name: str) -> str:
    path = SECRETS / f"{name}.txt"
    if not path.exists():
        pytest.fail(f"missing {path}; run `make secrets` and `make test-db-up` first")
    return path.read_text(encoding="utf-8").strip()


@pytest.fixture(scope="session")
def db_settings():
    from lbf.persistence.settings import DbSettings

    return DbSettings(
        host="127.0.0.1",
        port=TEST_PORT,
        name=TEST_DB,
        passwords={
            "migrator": _secret("db_migrator"),
            "app": _secret("db_app"),
            "audit_reader": _secret("db_audit_reader"),
            "superuser": _secret("db_superuser"),
        },
    )


@pytest.fixture(scope="session")
def _test_database(db_settings) -> Iterator[None]:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    admin = create_engine(db_settings.url("superuser", database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}" OWNER lbf_migrator'))
            conn.execute(text(f'REVOKE ALL ON DATABASE "{TEST_DB}" FROM PUBLIC'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{TEST_DB}" TO lbf_app, lbf_audit_reader'))
    except Exception as exc:
        pytest.fail(
            f"test PostgreSQL not reachable on 127.0.0.1:{TEST_PORT} ({exc.__class__.__name__}); run `make test-db-up`"
        )
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["url"] = db_settings.url("migrator")
    command.upgrade(cfg, "head")
    yield
    admin.dispose()


@pytest.fixture(scope="session")
def engines(db_settings, _test_database):
    from sqlalchemy import create_engine

    from lbf.persistence.db import make_engine

    made = {
        "app": make_engine(db_settings, "app", pool_size=20),
        "audit_reader": make_engine(db_settings, "audit_reader", pool_size=2),
        "superuser": create_engine(db_settings.url("superuser"), connect_args={"options": "-c search_path=lbf"}),
        "migrator": create_engine(db_settings.url("migrator"), connect_args={"options": "-c search_path=lbf"}),
    }
    yield made
    for e in made.values():
        e.dispose()


@pytest.fixture
def app_engine(engines):
    return engines["app"]


@pytest.fixture(autouse=True)
def _clean_tables(request):
    if request.node.get_closest_marker("db") is None:
        yield
        return
    from sqlalchemy import text

    engines = request.getfixturevalue("engines")
    with engines["superuser"].begin() as conn:
        names = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'lbf'"))]
        victims = [n for n in names if n not in KEEP_TABLES]
        if victims:
            conn.execute(text("TRUNCATE " + ", ".join(f"lbf.{n}" for n in victims) + " RESTART IDENTITY CASCADE"))
    yield
