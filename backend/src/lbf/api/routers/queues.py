"""Work queues and the dashboard: approvals (excludes the viewer's own drafts), reconciliation (UNKNOWN,
RECONCILING, ESCALATED, overdue AWAITING_ACK), dashboard counts and backend health."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select

from lbf.api import readmodels
from lbf.api.deps import Auth, require, services
from lbf.api.routers.health import backend_health
from lbf.api.schemas import DashboardOut, OperationSummaryOut, QueueOut
from lbf.api.services import AppServices
from lbf.domain.enums import OperationState, Permission
from lbf.persistence import tables as t

router = APIRouter(prefix="/api/v1", tags=["queues"])


def _deadlines(svc: AppServices) -> dict[str, int | None]:
    return {b: p.capabilities().ack_deadline_seconds for b, p in svc.workflow.backends.items()}


@router.get("/approvals", response_model=QueueOut, operation_id="approvalQueue")
def approvals(auth: Auth = Depends(require(Permission.APPROVAL_GRANT_STANDARD, Permission.APPROVAL_GRANT_ANY)),
              svc: AppServices = Depends(services)) -> QueueOut:
    with svc.engine.connect() as conn:
        return QueueOut(items=[OperationSummaryOut(**i) for i in readmodels.approval_queue(conn, auth.principal)])


@router.get("/reconciliation", response_model=QueueOut, operation_id="reconciliationQueue")
def reconciliation(_: Auth = Depends(require(Permission.OPERATION_READ)),
                   svc: AppServices = Depends(services)) -> QueueOut:
    with svc.engine.connect() as conn:
        items = readmodels.reconciliation_queue(conn, svc.clock(), _deadlines(svc))
    return QueueOut(items=[OperationSummaryOut(**i) for i in items])


@router.get("/dashboard", response_model=DashboardOut, operation_id="dashboard")
def dashboard(_: Auth = Depends(require(Permission.OPERATION_READ)),
              svc: AppServices = Depends(services)) -> DashboardOut:
    now = svc.clock()
    with svc.engine.connect() as conn:
        counts = readmodels.counts_by_state(conn)
        overdue = 0
        for r in conn.execute(select(t.operations).where(t.operations.c.state == OperationState.AWAITING_ACK.value)):
            deadline = _deadlines(svc).get(r.backend_id)
            if deadline and r.awaiting_ack_since and r.awaiting_ack_since + timedelta(seconds=deadline) < now:
                overdue += 1
        recent = readmodels.list_operations(conn, states=["CONFIRMED", "REJECTED", "NOT_APPLIED", "CLOSED_ATTESTED",
                                                          "WITHDRAWN"], limit=10)
    issues: dict[str, int] = {}
    for b, port in svc.workflow.backends.items():
        try:
            issues[b] = sum(len(a.issues) for a in port.stock().availability)
        except Exception:
            issues[b] = -1
    return DashboardOut(counts_by_state=counts, pending_approvals=counts.get("READY", 0),
                        unknown=counts.get("UNKNOWN", 0), escalated=counts.get("ESCALATED", 0), overdue_acks=overdue,
                        recent_outcomes=[OperationSummaryOut(**i) for i in recent], backends=backend_health(svc),
                        data_quality_issues=issues)
