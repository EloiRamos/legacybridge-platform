"""Authentication and user administration services.

Login failures use one generic message. Per-account lockout after 5 failures for 15 minutes; per client
address at most 10 attempts per minute (in-process sliding window: the application is a single process,
ADR-F04). Every security-relevant action is audited in the same transaction.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, Engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError

from lbf.auth import passwords, sessions
from lbf.auth.policy import Principal, sod3_violated
from lbf.domain.enums import Role
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.persistence.audit import AuditEvent, AuditWriter
from lbf.persistence.uow import utcnow

LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION = timedelta(minutes=15)
ADDRESS_LIMIT = 10
ADDRESS_WINDOW_SECONDS = 60.0
GENERIC_LOGIN_FAILURE = "Invalid username or password."


class AddressRateLimiter:
    def __init__(
        self,
        limit: int = ADDRESS_LIMIT,
        window: float = ADDRESS_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window
        self._monotonic = monotonic
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


@dataclass(frozen=True)
class LoginResult:
    issued: sessions.IssuedSession
    principal: Principal


def load_roles(conn: Connection, user_id: UUID) -> frozenset[Role]:
    rows = conn.execute(select(t.user_roles.c.role_code).where(t.user_roles.c.user_id == user_id)).scalars()
    return frozenset(Role(r) for r in rows)


def load_principal(conn: Connection, info: sessions.SessionInfo) -> Principal | None:
    user = conn.execute(select(t.users).where(t.users.c.id == info.user_id)).first()
    if user is None or not user.is_active:
        return None
    return Principal(
        user_id=user.id,
        username=str(user.username),
        roles=load_roles(conn, user.id),
        authenticated_at=info.authenticated_at,
    )


def _user_event(event_type: str, *, actor: Principal | None, payload: dict[str, object]) -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        actor_type="USER" if actor else "SYSTEM",
        actor_id=str(actor.user_id) if actor else None,
        actor_username=actor.username if actor else None,
        payload=payload,
    )


class AuthService:
    def __init__(
        self, engine: Engine, *, clock: Callable[[], datetime] = utcnow, limiter: AddressRateLimiter | None = None
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.audit = AuditWriter(clock)
        self.limiter = limiter or AddressRateLimiter()

    # ------------------------------------------------------------------ login / logout / step-up

    def login(self, username: str, password: str, client_address: str) -> LoginResult:
        if not self.limiter.allow(client_address):
            raise DomainError(ErrorCode.RATE_LIMITED, "Too many login attempts; try again shortly.")
        now = self.clock()
        with self.engine.begin() as conn:
            user = conn.execute(select(t.users).where(t.users.c.username == username).with_for_update()).first()
            ok = passwords.verify_password(user.password_hash if user else None, password)
            if user is None:
                self.audit.append(
                    conn,
                    _user_event(
                        "USER_LOGIN_FAILED", actor=None, payload={"reason": "unknown_user", "client": client_address}
                    ),
                )
                failure = True
            elif not user.is_active or (user.locked_until is not None and user.locked_until > now):
                self.audit.append(
                    conn,
                    _user_event(
                        "USER_LOGIN_FAILED",
                        actor=None,
                        payload={"user_id": str(user.id), "reason": "locked_or_inactive", "client": client_address},
                    ),
                )
                failure = True
            elif not ok:
                failures = user.failed_logins + 1
                values: dict[str, object] = {"failed_logins": failures}
                self.audit.append(
                    conn,
                    _user_event(
                        "USER_LOGIN_FAILED",
                        actor=None,
                        payload={"user_id": str(user.id), "reason": "bad_password", "client": client_address},
                    ),
                )
                if failures >= LOCKOUT_THRESHOLD:
                    values = {"failed_logins": 0, "locked_until": now + LOCKOUT_DURATION}
                    self.audit.append(
                        conn,
                        _user_event(
                            "USER_LOCKED",
                            actor=None,
                            payload={"user_id": str(user.id), "until": (now + LOCKOUT_DURATION).isoformat()},
                        ),
                    )
                conn.execute(update(t.users).where(t.users.c.id == user.id).values(**values))
                failure = True
            else:
                failure = False
            if not failure:
                assert user is not None
                conn.execute(update(t.users).where(t.users.c.id == user.id).values(failed_logins=0, locked_until=None))
                issued = sessions.create_session(conn, user.id, now)
                principal = Principal(
                    user_id=user.id, username=str(user.username), roles=load_roles(conn, user.id), authenticated_at=now
                )
                self.audit.append(conn, _user_event("USER_LOGIN", actor=principal, payload={"client": client_address}))
        if failure:
            raise DomainError(ErrorCode.UNAUTHENTICATED, GENERIC_LOGIN_FAILURE)
        return LoginResult(issued=issued, principal=principal)

    def reauthenticate(self, info: sessions.SessionInfo, password: str, client_address: str) -> LoginResult:
        if not self.limiter.allow(client_address):
            raise DomainError(ErrorCode.RATE_LIMITED, "Too many attempts; try again shortly.")
        now = self.clock()
        with self.engine.begin() as conn:
            user = conn.execute(select(t.users).where(t.users.c.id == info.user_id)).first()
            if user is None or not user.is_active or not passwords.verify_password(user.password_hash, password):
                self.audit.append(
                    conn, _user_event("USER_REAUTH_FAILED", actor=None, payload={"user_id": str(info.user_id)})
                )
                failed = True
            else:
                failed = False
                issued = sessions.rotate(conn, info, now, reauthenticated=True)
                principal = Principal(
                    user_id=user.id, username=str(user.username), roles=load_roles(conn, user.id), authenticated_at=now
                )
                self.audit.append(conn, _user_event("USER_REAUTHENTICATED", actor=principal, payload={}))
        if failed:
            raise DomainError(ErrorCode.UNAUTHENTICATED, GENERIC_LOGIN_FAILURE)
        return LoginResult(issued=issued, principal=principal)

    def logout(self, info: sessions.SessionInfo, principal: Principal | None) -> None:
        now = self.clock()
        with self.engine.begin() as conn:
            sessions.revoke(conn, info.token_sha256, now)
            self.audit.append(conn, _user_event("SESSION_REVOKED", actor=principal, payload={"reason": "logout"}))

    # ------------------------------------------------------------------ administration (SoD-3)

    def create_user(
        self,
        conn: Connection,
        *,
        username: str,
        display_name: str,
        password: str,
        roles: Iterable[Role],
        actor: Principal | None,
    ) -> UUID:
        user_id = uuid.uuid4()
        now = self.clock()
        try:
            conn.execute(
                t.users.insert().values(
                    id=user_id,
                    username=username,
                    display_name=display_name,
                    password_hash=passwords.hash_password(password),
                    is_active=True,
                    failed_logins=0,
                    password_changed_at=now,
                    created_at=now,
                )
            )
        except IntegrityError as exc:
            raise DomainError(ErrorCode.CONFLICT_STATE, "username already exists") from exc
        self.audit.append(
            conn, _user_event("USER_CREATED", actor=actor, payload={"user_id": str(user_id), "username": username})
        )
        for role in roles:
            self._grant(conn, user_id, role, actor)
        return user_id

    def _grant(self, conn: Connection, user_id: UUID, role: Role, actor: Principal | None) -> None:
        if actor is not None and sod3_violated(actor.user_id, user_id):
            raise DomainError(ErrorCode.SOD_VIOLATION, "users cannot change their own roles (SoD-3)")
        conn.execute(
            t.user_roles.insert().values(
                user_id=user_id,
                role_code=role.value,
                granted_by=actor.user_id if actor else None,
                granted_at=self.clock(),
            )
        )
        sessions.revoke_all_for_user(conn, user_id, self.clock())
        self.audit.append(
            conn, _user_event("ROLE_GRANTED", actor=actor, payload={"user_id": str(user_id), "role": role.value})
        )

    def grant_role(self, conn: Connection, user_id: UUID, role: Role, actor: Principal | None) -> None:
        self._grant(conn, user_id, role, actor)

    def revoke_role(self, conn: Connection, user_id: UUID, role: Role, actor: Principal | None) -> None:
        if actor is not None and sod3_violated(actor.user_id, user_id):
            raise DomainError(ErrorCode.SOD_VIOLATION, "users cannot change their own roles (SoD-3)")
        result = conn.execute(
            delete(t.user_roles).where(t.user_roles.c.user_id == user_id, t.user_roles.c.role_code == role.value)
        )
        if not result.rowcount:
            raise DomainError(ErrorCode.NOT_FOUND, "role not held")
        sessions.revoke_all_for_user(conn, user_id, self.clock())
        self.audit.append(
            conn, _user_event("ROLE_REVOKED", actor=actor, payload={"user_id": str(user_id), "role": role.value})
        )

    def count_users(self, conn: Connection) -> int:
        return int(conn.execute(select(func.count()).select_from(t.users)).scalar_one())
