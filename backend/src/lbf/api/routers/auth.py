"""Authentication: login, step-up re-authentication, logout, session (with a fresh CSRF token)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import update

from lbf.api.deps import Auth, authenticated, check_origin, client_address, services
from lbf.api.schemas import LoginIn, ReauthIn, SessionOut
from lbf.api.services import AppServices
from lbf.auth import sessions
from lbf.auth.policy import Principal, permissions_for
from lbf.persistence import tables as t

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_cookie(response: Response, token: str, svc: AppServices) -> None:
    response.set_cookie(sessions.COOKIE_NAME, token, httponly=True, samesite="strict", path="/",
                        secure=svc.config.cookie_secure, max_age=int(sessions.ABSOLUTE_LIFETIME.total_seconds()))


def _out(principal: Principal, csrf: str, svc: AppServices) -> SessionOut:
    return SessionOut(user_id=principal.user_id, username=principal.username, roles=sorted(principal.roles),
                      permissions=sorted(permissions_for(principal.roles, profile=svc.config.profile)),
                      authenticated_at=principal.authenticated_at, csrf_token=csrf, profile=svc.config.profile)


@router.post("/login", response_model=SessionOut, operation_id="login")
def login(body: LoginIn, request: Request, response: Response, svc: AppServices = Depends(services)) -> SessionOut:
    check_origin(request, svc)
    result = svc.auth.login(body.username, body.password, client_address(request))
    _set_cookie(response, result.issued.token, svc)
    return _out(result.principal, result.issued.csrf_token, svc)


@router.post("/reauth", response_model=SessionOut, operation_id="reauthenticate")
def reauth(body: ReauthIn, request: Request, response: Response, auth: Auth = Depends(authenticated),
           svc: AppServices = Depends(services)) -> SessionOut:
    result = svc.auth.reauthenticate(auth.session, body.password, client_address(request))
    _set_cookie(response, result.issued.token, svc)
    return _out(result.principal, result.issued.csrf_token, svc)


@router.post("/logout", status_code=204, operation_id="logout")
def logout(response: Response, auth: Auth = Depends(authenticated), svc: AppServices = Depends(services)) -> None:
    svc.auth.logout(auth.session, auth.principal)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")


@router.get("/session", response_model=SessionOut, operation_id="getSession")
def session(auth: Auth = Depends(authenticated), svc: AppServices = Depends(services)) -> SessionOut:
    """Returns the principal and a fresh synchronizer token (the SPA keeps it in memory only, never storage)."""
    csrf = secrets.token_urlsafe(32)
    with svc.engine.begin() as conn:
        conn.execute(update(t.sessions).where(t.sessions.c.token_sha256 == auth.session.token_sha256)
                     .values(csrf_sha256=sessions.digest(csrf)))
    return _out(auth.principal, csrf, svc)
