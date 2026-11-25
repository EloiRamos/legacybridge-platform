"""Sessions, CSRF, lockout, rate limiting, step-up and role administration (SEC-04/05/06, SoD-3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from support import PASSWORD, make_user

from lbf.auth import passwords, sessions
from lbf.auth.policy import step_up_fresh
from lbf.auth.service import AddressRateLimiter, AuthService, load_principal
from lbf.domain.enums import Role
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t

pytestmark = pytest.mark.db


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_password_policy_and_hashing():
    with pytest.raises(ValueError):
        passwords.hash_password("short")
    h = passwords.hash_password(PASSWORD)
    assert h.startswith("$argon2id$")
    assert passwords.verify_password(h, PASSWORD) and not passwords.verify_password(h, PASSWORD + "x")
    assert not passwords.verify_password(None, PASSWORD)


def test_login_session_lifecycle_and_rotation(app_engine):
    make_user(app_engine, "daniel.sales", Role.SALES_OPS)
    clock = Clock()
    svc = AuthService(app_engine, clock=clock)
    result = svc.login("daniel.sales", PASSWORD, "127.0.0.1")
    with app_engine.begin() as conn:
        info = sessions.load_session(conn, result.issued.token, clock())
        assert info is not None and load_principal(conn, info).roles == {Role.SALES_OPS}
        assert sessions.csrf_matches(info, result.issued.csrf_token)
        assert not sessions.csrf_matches(info, "forged") and not sessions.csrf_matches(info, None)
        stored = conn.execute(select(t.sessions.c.token_sha256)).scalars().all()
        assert result.issued.token not in stored  # only digests are stored
    clock.now += timedelta(minutes=31)  # idle timeout
    with app_engine.begin() as conn:
        assert sessions.load_session(conn, result.issued.token, clock()) is None
    again = svc.login("daniel.sales", PASSWORD, "127.0.0.1")
    for _ in range(8):  # stay active, but the absolute lifetime ends after 8 h
        clock.now += timedelta(minutes=29)
        with app_engine.begin() as conn:
            assert sessions.load_session(conn, again.issued.token, clock()) is not None
    clock.now += timedelta(hours=4)
    with app_engine.begin() as conn:
        assert sessions.load_session(conn, again.issued.token, clock()) is None


def test_reauth_rotates_and_refreshes_step_up(app_engine):
    make_user(app_engine, "marta.ops", Role.OPS_MANAGER)
    clock = Clock()
    svc = AuthService(app_engine, clock=clock)
    first = svc.login("marta.ops", PASSWORD, "127.0.0.1")
    clock.now += timedelta(minutes=16)
    assert not step_up_fresh(first.principal, clock())
    with app_engine.begin() as conn:
        info = sessions.load_session(conn, first.issued.token, clock())
    second = svc.reauthenticate(info, PASSWORD, "127.0.0.1")
    assert step_up_fresh(second.principal, clock())
    with app_engine.begin() as conn:
        assert sessions.load_session(conn, first.issued.token, clock()) is None  # rotated
        assert sessions.load_session(conn, second.issued.token, clock()) is not None
    with pytest.raises(DomainError):
        svc.reauthenticate(second.issued.info, "wrong-password-123", "127.0.0.1")


def test_lockout_after_five_failures_generic_message(app_engine):
    make_user(app_engine, "priya.sales", Role.SALES_OPS)
    clock = Clock()
    svc = AuthService(app_engine, clock=clock, limiter=AddressRateLimiter(limit=1000))
    messages = set()
    for _ in range(5):
        with pytest.raises(DomainError) as exc:
            svc.login("priya.sales", "wrong-password-123", "127.0.0.1")
        messages.add(exc.value.message)
    with pytest.raises(DomainError) as exc:  # correct password while locked
        svc.login("priya.sales", PASSWORD, "127.0.0.1")
    messages.add(exc.value.message)
    with pytest.raises(DomainError) as exc:
        svc.login("nobody", PASSWORD, "127.0.0.1")
    messages.add(exc.value.message)
    assert messages == {"Invalid username or password."}
    clock.now += timedelta(minutes=15, seconds=1)
    assert svc.login("priya.sales", PASSWORD, "127.0.0.1").principal.username == "priya.sales"
    with app_engine.connect() as conn:
        types = conn.execute(text("SELECT event_type FROM audit_events ORDER BY seq")).scalars().all()
    assert "USER_LOCKED" in types and types.count("USER_LOGIN_FAILED") == 7


def test_address_rate_limit():
    ticks = iter(range(100))
    limiter = AddressRateLimiter(limit=10, window=60, monotonic=lambda: float(next(ticks)))
    assert all(limiter.allow("10.0.0.1") for _ in range(10))
    assert not limiter.allow("10.0.0.1")
    assert limiter.allow("10.0.0.2")


def test_rate_limited_login_is_refused(app_engine):
    svc = AuthService(app_engine, limiter=AddressRateLimiter(limit=1))
    with pytest.raises(DomainError):
        svc.login("x", "y" * 12, "9.9.9.9")
    with pytest.raises(DomainError) as exc:
        svc.login("x", "y" * 12, "9.9.9.9")
    assert exc.value.code is ErrorCode.RATE_LIMITED


def test_sod3_and_last_sysadmin(app_engine):
    admin = make_user(app_engine, "tomas.admin", Role.SYSADMIN)
    svc = AuthService(app_engine)
    from lbf.auth.policy import Principal

    actor = Principal(user_id=admin, username="tomas.admin", roles=frozenset({Role.SYSADMIN}))
    with pytest.raises(DomainError) as exc, app_engine.begin() as conn:
        svc.grant_role(conn, admin, Role.OPS_MANAGER, actor)
    assert exc.value.code is ErrorCode.SOD_VIOLATION
    with pytest.raises(Exception, match="last sysadmin"), app_engine.begin() as conn:
        svc.revoke_role(conn, admin, Role.SYSADMIN, None)
    other = make_user(app_engine, "second.admin", Role.SYSADMIN)
    with app_engine.begin() as conn:
        svc.revoke_role(conn, admin, Role.SYSADMIN, Principal(user_id=other, username="s", roles=frozenset()))


def test_role_change_revokes_sessions(app_engine):
    uid = make_user(app_engine, "rafael.integration", Role.INTEGRATION_ENGINEER)
    svc = AuthService(app_engine)
    issued = svc.login("rafael.integration", PASSWORD, "127.0.0.1").issued
    with app_engine.begin() as conn:
        svc.grant_role(conn, uid, Role.SALES_OPS, None)
        assert sessions.load_session(conn, issued.token, datetime.now(UTC)) is None


def test_origin_check():
    assert sessions.origin_allowed("http://127.0.0.1:8080", None, "http://127.0.0.1:8080")
    assert not sessions.origin_allowed("http://evil.example", None, "http://127.0.0.1:8080")
    assert sessions.origin_allowed(None, "http://127.0.0.1:8080/operations", "http://127.0.0.1:8080")
    assert not sessions.origin_allowed(None, None, "http://127.0.0.1:8080")
