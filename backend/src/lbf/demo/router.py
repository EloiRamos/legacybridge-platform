"""Demo panel endpoints: reset the simulators and arm faults with the R01–R18 presets.

``demo.control`` is granted only to ``sysadmin`` and only in the ``demo``/``test`` profiles. Reset is refused while
any operation is ``APPROVED``, ``SUBMITTING``, ``AWAITING_ACK`` or ``RECONCILING`` (closes v1 T3). Every action is
audited. The HALDEN control plane is reached only through the v1 ``SimControlClient`` (allowed here, B-4); the
application's own adapters never call a control plane.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from lbf.adapters.halden.v1bridge.client import SimControlClient
from lbf.api.deps import Auth, require, services
from lbf.api.services import AppServices
from lbf.domain.enums import RESET_BLOCKING_STATES, Permission
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow.records import user_event

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

PRESETS: dict[str, dict[str, Any]] = {
    "halden_commit_then_500": {"backend": "halden", "method": "POST", "path": "/halden/orders",
                               "mode": "commit_then_500"},
    "halden_delay_2500ms": {"backend": "halden", "method": "POST", "path": "/halden/orders", "mode": "delay",
                            "delay_ms": 2500},
    "halden_error_500": {"backend": "halden", "method": "POST", "path": "/halden/orders", "mode": "error_500"},
    "halden_listing_500x3": {"backend": "halden", "method": "GET", "path": "/halden/orders", "mode": "error_500",
                             "count": 3},
    "skarvik_drop_ack": {"backend": "skarvik", "fault": "drop_ack"},
    "skarvik_contradict_ack": {"backend": "skarvik", "fault": "contradict_ack"},
    "skarvik_malformed_ack": {"backend": "skarvik", "fault": "malformed_ack"},
    "skarvik_ack_delay_30s": {"backend": "skarvik", "fault": "ack_delay", "value": 30},
    "skarvik_pause_pickup": {"backend": "skarvik", "fault": "pause_pickup", "value": True},
}


class FaultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["halden_commit_then_500", "halden_delay_2500ms", "halden_error_500", "halden_listing_500x3",
                    "skarvik_drop_ack", "skarvik_contradict_ack", "skarvik_malformed_ack", "skarvik_ack_delay_30s",
                    "skarvik_pause_pickup"]
    count: int = Field(default=1, ge=1, le=10)


def _audit(svc: AppServices, auth: Auth, event: str, payload: dict[str, Any]) -> None:
    with UnitOfWork(svc.engine, clock=svc.clock) as uow:
        uow.append_event(user_event(auth.principal, event, payload=payload))


@router.post("/reset", operation_id="demoReset")
def reset(auth: Auth = Depends(require(Permission.DEMO_CONTROL)),
          svc: AppServices = Depends(services)) -> dict[str, Any]:
    with svc.engine.connect() as conn:
        busy = conn.execute(select(func.count()).select_from(t.operations).where(
            t.operations.c.state.in_([s.value for s in RESET_BLOCKING_STATES]))).scalar()
    if busy:
        raise DomainError(ErrorCode.CONFLICT_STATE, f"{busy} operation(s) in flight; reset refused")
    halden = SimControlClient(svc.config.halden_url)
    try:
        ok_halden = halden.reset()
    finally:
        halden.close()
    ok_skarvik = httpx.post(f"{svc.config.skarvik_control_url}/_sim/reset", timeout=10).status_code == 200
    _audit(svc, auth, "DEMO_RESET", {"halden": ok_halden, "skarvik": ok_skarvik})
    return {"halden": ok_halden, "skarvik": ok_skarvik}


@router.post("/faults", operation_id="demoArmFault")
def arm(body: FaultIn, auth: Auth = Depends(require(Permission.DEMO_CONTROL)),
        svc: AppServices = Depends(services)) -> dict[str, Any]:
    p = dict(PRESETS[body.preset])
    if p["backend"] == "halden":
        client = SimControlClient(svc.config.halden_url)
        try:
            armed = client.arm_fault(p["method"], p["path"], p["mode"], p.get("count", body.count),
                                     p.get("delay_ms", 0))
        finally:
            client.close()
        result: dict[str, Any] = {"armed": armed}
    else:
        r = httpx.post(f"{svc.config.skarvik_control_url}/_sim/faults",
                       json={"fault": p["fault"], "value": p.get("value"), "count": body.count}, timeout=10)
        result = {"armed": r.status_code == 200}
    _audit(svc, auth, "DEMO_FAULT_ARMED", {"preset": body.preset, "count": body.count, **result})
    return result
