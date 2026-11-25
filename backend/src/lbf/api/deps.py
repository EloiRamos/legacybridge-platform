"""Request-scoped security dependencies.

* ``principal`` — the server-side session (HttpOnly cookie) resolved to a principal; roles come from the
  database, never from the client.
* ``csrf`` — every state-changing request needs the synchronizer token **and** an allowed ``Origin``.
* ``require(permission)`` — route-level permission; commands re-check inside their transactions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request

from lbf.api.services import AppServices
from lbf.auth import sessions
from lbf.auth.policy import Principal, has_permission
from lbf.auth.service import load_principal
from lbf.domain.enums import Permission
from lbf.domain.errors import DomainError, ErrorCode

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def services(request: Request) -> AppServices:
    svc: AppServices = request.app.state.services
    return svc


@dataclass(frozen=True)
class Auth:
    principal: Principal
    session: sessions.SessionInfo


def check_origin(request: Request, svc: AppServices) -> None:
    if not sessions.origin_allowed(request.headers.get("origin"), request.headers.get("referer"),
                                   svc.config.public_origin):
        raise DomainError(ErrorCode.CSRF_FAILED, "request origin not allowed")


def authenticated(request: Request, svc: AppServices = Depends(services)) -> Auth:
    token = request.cookies.get(sessions.COOKIE_NAME)
    with svc.engine.begin() as conn:
        info = sessions.load_session(conn, token, svc.clock())
        principal = load_principal(conn, info) if info is not None else None
    if info is None or principal is None:
        raise DomainError(ErrorCode.UNAUTHENTICATED, "sign in required")
    if request.method in UNSAFE:
        check_origin(request, svc)
        if not sessions.csrf_matches(info, request.headers.get(sessions.CSRF_HEADER)):
            raise DomainError(ErrorCode.CSRF_FAILED, "missing or invalid CSRF token")
    request.state.actor = principal.username
    return Auth(principal, info)


def require(*permissions: Permission) -> Callable[..., Auth]:
    def dep(auth: Auth = Depends(authenticated), svc: AppServices = Depends(services)) -> Auth:
        if not any(has_permission(auth.principal, p, profile=svc.config.profile) for p in permissions):
            raise DomainError(ErrorCode.FORBIDDEN, "permission missing")
        return auth

    dep.permissions = permissions  # type: ignore[attr-defined]  # read by the OpenAPI document only
    return dep


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"
