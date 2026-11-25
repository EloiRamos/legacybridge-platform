"""Opaque server-side sessions and CSRF synchronizer tokens.

Tokens are 32-byte ``secrets.token_urlsafe`` values; only their SHA-256 is stored. Idle timeout 30 min,
absolute lifetime 8 h; rotated at login and re-authentication; revoked at logout and on role change.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, select, update

from lbf.persistence import tables as t

IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_LIFETIME = timedelta(hours=8)
COOKIE_NAME = "lbf_session"
CSRF_HEADER = "X-CSRF-Token"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionInfo:
    token_sha256: str
    user_id: UUID
    csrf_sha256: str
    created_at: datetime
    authenticated_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    info: SessionInfo


def create_session(
    conn: Connection, user_id: UUID, now: datetime, *, authenticated_at: datetime | None = None
) -> IssuedSession:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    info = SessionInfo(
        token_sha256=digest(token),
        user_id=user_id,
        csrf_sha256=digest(csrf),
        created_at=now,
        authenticated_at=authenticated_at or now,
        expires_at=now + ABSOLUTE_LIFETIME,
    )
    conn.execute(
        t.sessions.insert().values(
            token_sha256=info.token_sha256,
            user_id=user_id,
            csrf_sha256=info.csrf_sha256,
            created_at=now,
            last_seen_at=now,
            authenticated_at=info.authenticated_at,
            expires_at=info.expires_at,
        )
    )
    return IssuedSession(token=token, csrf_token=csrf, info=info)


def load_session(conn: Connection, token: str | None, now: datetime, *, touch: bool = True) -> SessionInfo | None:
    if not token or len(token) > 200:
        return None
    row = conn.execute(select(t.sessions).where(t.sessions.c.token_sha256 == digest(token))).first()
    if row is None or row.revoked_at is not None:
        return None
    if now >= row.expires_at or now - row.last_seen_at > IDLE_TIMEOUT:
        return None
    if touch:
        conn.execute(update(t.sessions).where(t.sessions.c.token_sha256 == row.token_sha256).values(last_seen_at=now))
    return SessionInfo(
        token_sha256=row.token_sha256,
        user_id=row.user_id,
        csrf_sha256=row.csrf_sha256,
        created_at=row.created_at,
        authenticated_at=row.authenticated_at,
        expires_at=row.expires_at,
    )


def revoke(conn: Connection, token_sha256: str, now: datetime) -> None:
    conn.execute(
        update(t.sessions)
        .where(t.sessions.c.token_sha256 == token_sha256, t.sessions.c.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_all_for_user(conn: Connection, user_id: UUID, now: datetime) -> int:
    result = conn.execute(
        update(t.sessions)
        .where(t.sessions.c.user_id == user_id, t.sessions.c.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return result.rowcount or 0


def rotate(conn: Connection, old: SessionInfo, now: datetime, *, reauthenticated: bool) -> IssuedSession:
    """Rotate the session token (login and re-auth); re-auth also refreshes ``authenticated_at``."""
    revoke(conn, old.token_sha256, now)
    return create_session(conn, old.user_id, now, authenticated_at=now if reauthenticated else old.authenticated_at)


def csrf_matches(session: SessionInfo, header_token: str | None) -> bool:
    if not header_token or len(header_token) > 200:
        return False
    return hmac.compare_digest(session.csrf_sha256, digest(header_token))


def origin_allowed(origin: str | None, referer: str | None, public_origin: str) -> bool:
    """Origin check for state-changing requests: the Origin (or, if absent, the Referer's origin) must
    equal the configured public origin. A request carrying neither header is refused."""
    if origin:
        return origin.rstrip("/") == public_origin.rstrip("/")
    if referer:
        return referer.startswith(public_origin.rstrip("/") + "/") or referer == public_origin
    return False
