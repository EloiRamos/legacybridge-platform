"""Operations: list, detail (with server-computed permitted actions), and every human command.

Each route depends on ``require(permission)``; each command re-checks permission, SoD, step-up and state inside
its transaction. Hidden buttons are not a control.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select

from lbf.api import readmodels
from lbf.api.deps import Auth, require, services
from lbf.api.schemas import (
    ApproveIn,
    CommandOut,
    DeclineIn,
    DraftCreatedOut,
    DraftIn,
    EscalateIn,
    OperationDetailOut,
    OperationSummaryOut,
    QueueOut,
    ResolveIn,
    ReviseIn,
    VersionRefIn,
)
from lbf.api.services import AppServices
from lbf.domain.enums import OperationState, Permission
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.workflow import commands
from lbf.workflow.drafts import DraftRequest, LineRequest
from lbf.workflow.withdraw_unclaimed import withdraw_unclaimed

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])
P = Permission


def _request(body: DraftIn) -> DraftRequest:
    return DraftRequest(backend_id=body.backend_id, native_customer_id=body.native_customer_id,
                        lines=[LineRequest(canonical_sku=ln.canonical_sku, native_item=ln.native_item,
                                           quantity=ln.quantity) for ln in body.lines],
                        customer_po=body.customer_po, requested_date=body.requested_date,
                        delivery_note=body.delivery_note)


def _state(svc: AppServices, operation_id: UUID) -> OperationState:
    with svc.engine.connect() as conn:
        state = conn.execute(select(t.operations.c.state).where(t.operations.c.id == operation_id)).scalar()
    if state is None:
        raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
    return OperationState(state)


@router.get("", response_model=QueueOut, operation_id="listOperations")
def list_operations(state: list[OperationState] = Query(default=[]),
                    backend: Literal["halden", "skarvik"] | None = None,
                    customer: str | None = Query(default=None, max_length=16),
                    mine: bool = False, auth: Auth = Depends(require(P.OPERATION_READ)),
                    svc: AppServices = Depends(services)) -> QueueOut:
    with svc.engine.connect() as conn:
        items = readmodels.list_operations(conn, states=[s.value for s in state], backend=backend, customer=customer,
                                           created_by=auth.principal.user_id if mine else None)
    return QueueOut(items=[OperationSummaryOut(**i) for i in items])


@router.get("/{operation_id}", response_model=OperationDetailOut, operation_id="getOperation")
def get_operation(operation_id: UUID, auth: Auth = Depends(require(P.OPERATION_READ)),
                  svc: AppServices = Depends(services)) -> OperationDetailOut:
    with svc.engine.connect() as conn:
        backend = conn.execute(select(t.operations.c.backend_id).where(t.operations.c.id == operation_id)).scalar()
        if backend is None:
            raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
        detail = readmodels.detail(conn, operation_id, svc.workflow.caps(backend), auth.principal, svc.clock(),
                                   svc.workflow.settings)
    assert detail is not None
    return OperationDetailOut(**detail)


@router.post("", response_model=DraftCreatedOut, status_code=201, operation_id="createDraft")
def create(body: DraftIn, idempotency_key: str | None = Header(default=None, max_length=80),
           auth: Auth = Depends(require(P.DRAFT_CREATE)), svc: AppServices = Depends(services)) -> DraftCreatedOut:
    r = commands.create_draft(svc.workflow, auth.principal, _request(body), idempotency_key=idempotency_key,
                              raw_request=body.model_dump(mode="json"))
    return DraftCreatedOut(operation_id=r.operation_id, version=r.version, content_hash=r.content_hash, state=r.state)


@router.post("/{operation_id}/revisions", response_model=DraftCreatedOut, operation_id="reviseDraft")
def revise(operation_id: UUID, body: ReviseIn, auth: Auth = Depends(require(P.DRAFT_EDIT)),
           svc: AppServices = Depends(services)) -> DraftCreatedOut:
    r = commands.revise_draft(svc.workflow, auth.principal, operation_id, body.expected_version, _request(body))
    return DraftCreatedOut(operation_id=r.operation_id, version=r.version, content_hash=r.content_hash, state=r.state)


@router.post("/{operation_id}/submit", response_model=CommandOut, operation_id="submitForApproval")
def submit(operation_id: UUID, body: VersionRefIn, auth: Auth = Depends(require(P.DRAFT_SUBMIT)),
           svc: AppServices = Depends(services)) -> CommandOut:
    r = commands.submit_for_approval(svc.workflow, auth.principal, operation_id, body.expected_version)
    return CommandOut(operation_id=operation_id, state=r.state,
                      detail="blocking checks: validation recorded, still a draft" if r.blocking else "ready",
                      checks=[c.model_dump(mode="json") for c in r.checks])


@router.post("/{operation_id}/approve", response_model=CommandOut, operation_id="approve")
def approve(operation_id: UUID, body: ApproveIn,
            auth: Auth = Depends(require(P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY)),
            svc: AppServices = Depends(services)) -> CommandOut:
    r = commands.approve(svc.workflow, auth.principal, operation_id, version=body.version,
                         content_hash=body.content_hash, override_reason=body.override_reason)
    return CommandOut(operation_id=operation_id, state=OperationState.APPROVED,
                      detail=f"attempt {r.attempt_no} authorized")


@router.post("/{operation_id}/decline", response_model=CommandOut, operation_id="decline")
def decline(operation_id: UUID, body: DeclineIn,
            auth: Auth = Depends(require(P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY)),
            svc: AppServices = Depends(services)) -> CommandOut:
    commands.decline(svc.workflow, auth.principal, operation_id, version=body.version, content_hash=body.content_hash,
                     reason=body.reason)
    return CommandOut(operation_id=operation_id, state=OperationState.DECLINED)


@router.post("/{operation_id}/withdraw", response_model=CommandOut, operation_id="withdraw")
def withdraw(operation_id: UUID, auth: Auth = Depends(require(P.DRAFT_WITHDRAW, P.APPROVAL_GRANT_ANY)),
             svc: AppServices = Depends(services)) -> CommandOut:
    commands.withdraw(svc.workflow, auth.principal, operation_id)
    return CommandOut(operation_id=operation_id, state=OperationState.WITHDRAWN)


@router.post("/{operation_id}/recheck", response_model=CommandOut, status_code=202, operation_id="requestRecheck")
def recheck(operation_id: UUID, auth: Auth = Depends(require(P.RECONCILE_RECHECK)),
            svc: AppServices = Depends(services)) -> CommandOut:
    commands.request_recheck(svc.workflow, auth.principal, operation_id)
    return CommandOut(operation_id=operation_id, state=_state(svc, operation_id), detail="re-check scheduled")


@router.post("/{operation_id}/escalate", response_model=CommandOut, operation_id="escalate")
def escalate(operation_id: UUID, body: EscalateIn, auth: Auth = Depends(require(P.ESCALATION_OPEN)),
             svc: AppServices = Depends(services)) -> CommandOut:
    commands.escalate(svc.workflow, auth.principal, operation_id, body.note)
    return CommandOut(operation_id=operation_id, state=OperationState.ESCALATED)


@router.post("/{operation_id}/resolve", response_model=CommandOut, operation_id="resolve")
def resolve(operation_id: UUID, body: ResolveIn, auth: Auth = Depends(require(P.ESCALATION_RESOLVE)),
            svc: AppServices = Depends(services)) -> CommandOut:
    state = commands.resolve(svc.workflow, auth.principal, operation_id, kind=body.kind,
                             external_reference=body.external_reference, note=body.note)
    return CommandOut(operation_id=operation_id, state=state)


@router.post("/{operation_id}/withdraw-unclaimed", response_model=CommandOut, operation_id="withdrawUnclaimed")
def withdraw_message(operation_id: UUID, auth: Auth = Depends(require(P.SKARVIK_WITHDRAW_AUTHORIZE)),
                     svc: AppServices = Depends(services)) -> CommandOut:
    out = withdraw_unclaimed(svc.workflow, auth.principal, operation_id)
    return CommandOut(operation_id=operation_id, state=out.state, detail=out.detail)


@router.post("/{operation_id}/redelivery", response_model=CommandOut, status_code=202,
             operation_id="authorizeRedelivery")
def redelivery(operation_id: UUID, auth: Auth = Depends(require(P.SKARVIK_REDELIVER_AUTHORIZE)),
               svc: AppServices = Depends(services)) -> CommandOut:
    commands.authorize_redelivery(svc.workflow, auth.principal, operation_id)
    return CommandOut(operation_id=operation_id, state=_state(svc, operation_id),
                      detail="redelivery authorized; the engine hands off the stored bytes once")
