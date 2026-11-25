"""Administration (``sysadmin``): users and roles (SoD-3, step-up for role changes), configuration with secrets
redacted, the unmatched-acknowledgement queue and engine status (``ackqueue.read``/``backend.health.read``)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select

from lbf.api.deps import Auth, require, services
from lbf.api.routers.health import backend_health
from lbf.api.schemas import RoleIn, UserCreateIn, UserOut
from lbf.api.services import AppServices
from lbf.auth.policy import step_up_fresh
from lbf.domain.enums import Permission, Role
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _step_up(auth: Auth, svc: AppServices) -> None:
    if not step_up_fresh(auth.principal, svc.clock()):
        raise DomainError(ErrorCode.REAUTH_REQUIRED, "re-authenticate to change users or roles")


@router.get("/users", response_model=list[UserOut], operation_id="listUsers")
def users(_: Auth = Depends(require(Permission.ADMIN_USERS)), svc: AppServices = Depends(services)) -> list[UserOut]:
    with svc.engine.connect() as conn:
        rows = conn.execute(select(t.users).order_by(t.users.c.username)).all()
        roles: dict[Any, list[Role]] = {}
        for r in conn.execute(select(t.user_roles)):
            roles.setdefault(r.user_id, []).append(Role(r.role_code))
    return [UserOut(user_id=u.id, username=str(u.username), display_name=u.display_name, is_active=u.is_active,
                    roles=sorted(roles.get(u.id, [])), locked_until=u.locked_until) for u in rows]


@router.post("/users", response_model=UserOut, status_code=201, operation_id="createUser")
def create_user(body: UserCreateIn, auth: Auth = Depends(require(Permission.ADMIN_USERS)),
                svc: AppServices = Depends(services)) -> UserOut:
    _step_up(auth, svc)
    with svc.engine.begin() as conn:
        uid = svc.auth.create_user(conn, username=body.username, display_name=body.display_name,
                                   password=body.password, roles=body.roles, actor=auth.principal)
    return UserOut(user_id=uid, username=body.username, display_name=body.display_name, is_active=True,
                   roles=sorted(body.roles), locked_until=None)


@router.post("/users/{user_id}/roles", status_code=204, operation_id="grantRole")
def grant(user_id: UUID, body: RoleIn, auth: Auth = Depends(require(Permission.ADMIN_ROLES)),
          svc: AppServices = Depends(services)) -> None:
    _step_up(auth, svc)
    with svc.engine.begin() as conn:
        svc.auth.grant_role(conn, user_id, body.role, auth.principal)


@router.delete("/users/{user_id}/roles/{role}", status_code=204, operation_id="revokeRole")
def revoke(user_id: UUID, role: Role, auth: Auth = Depends(require(Permission.ADMIN_ROLES)),
           svc: AppServices = Depends(services)) -> None:
    _step_up(auth, svc)
    with svc.engine.begin() as conn:
        svc.auth.revoke_role(conn, user_id, role, auth.principal)


@router.get("/config", operation_id="adminConfig")
def config(_: Auth = Depends(require(Permission.ADMIN_CONFIG_READ)),
           svc: AppServices = Depends(services)) -> dict[str, Any]:
    caps = {b: p.capabilities().model_dump(mode="json") for b, p in svc.workflow.backends.items()}
    return {"application": svc.config.redacted(), "backends": caps,
            "time_scale": svc.workflow.settings.scale, "authorization_ttl_seconds":
            int(svc.workflow.settings.authorization_ttl.total_seconds())}


@router.get("/ack-queue", operation_id="unmatchedAcks")
def ack_queue(_: Auth = Depends(require(Permission.ACKQUEUE_READ)),
              svc: AppServices = Depends(services)) -> list[dict[str, Any]]:
    a = t.acknowledgements
    with svc.engine.connect() as conn:
        rows = conn.execute(select(a).where(or_(a.c.operation_id.is_(None), a.c.status == "MALFORMED"))
                            .order_by(a.c.recorded_at.desc()).limit(200)).all()
    return [{"file_name": r.file_name, "message_id": r.message_id, "status": r.status, "order_ref": r.order_ref,
             "code": r.code, "file_sha256": r.file_sha256, "recorded_at": r.recorded_at} for r in rows]


@router.get("/engine", operation_id="engineStatus")
def engine(_: Auth = Depends(require(Permission.BACKEND_HEALTH_READ)),
           svc: AppServices = Depends(services)) -> dict[str, Any]:
    with svc.engine.connect() as conn:
        beats = [dict(r._mapping) for r in conn.execute(select(t.engine_heartbeats)).all()]
    leader = bool(svc.runner is not None and svc.runner.leader)
    return {"this_process_is_leader": leader, "heartbeats": beats, "backends": backend_health(svc)}
