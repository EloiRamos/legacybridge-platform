"""T20 — withdraw an unclaimed message before pickup (capability ``can_withdraw_before_pickup``).

The only workflow module allowed to call a backend write method besides the executor (boundary test B-6).
Sequence: authorization transaction → file operation (no transaction open) → result transaction.
A successful atomic rename ``inbox → withdrawn`` is positive evidence (S-I1) ⇒ ``NOT_APPLIED /
WITHDRAWN_BEFORE_PICKUP``. A missing file proves nothing (already claimed or never delivered) ⇒ the operation
keeps its state and a re-check is scheduled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update

from lbf.auth.policy import Principal
from lbf.domain.enums import AuthorizationKind, AuthorizationState, EvidenceClass, OperationState
from lbf.domain.enums import Permission as P
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.ports.backend import WithdrawResult
from lbf.workflow.classify import Decision
from lbf.workflow.commands import require, require_state, require_step_up
from lbf.workflow.context import WorkflowContext
from lbf.workflow.records import apply_decision, user_event

S = OperationState


@dataclass(frozen=True)
class WithdrawOutcome:
    withdrawn: bool
    state: OperationState
    detail: str


def withdraw_unclaimed(ctx: WorkflowContext, principal: Principal, operation_id: UUID) -> WithdrawOutcome:
    require(principal, P.SKARVIK_WITHDRAW_AUTHORIZE)
    handler_id = f"withdraw-handler:{principal.user_id}"
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        now = uow.clock()
        require_step_up(principal, now)
        row = uow.lock_operation(operation_id)
        require_state(row, S.AWAITING_ACK, S.UNKNOWN)
        caps = ctx.caps(row.backend_id)
        if not caps.can_withdraw_before_pickup:
            raise DomainError(ErrorCode.CAPABILITY_UNSUPPORTED, "this backend cannot withdraw a handed-off order")
        if repo.open_authorization(uow.conn, operation_id) is not None:
            raise DomainError(ErrorCode.CONFLICT_STATE, "an authorization is already open")
        message_id = uow.conn.execute(select(t.transmissions.c.message_id).where(
            t.transmissions.c.operation_id == operation_id, t.transmissions.c.attempt_no == row.attempt_count)
            .order_by(t.transmissions.c.transmission_no.desc())).scalar()
        if not message_id:
            raise DomainError(ErrorCode.CONFLICT_STATE, "no handed-off message for the current attempt")
        auth_id = repo.insert_authorization(uow.conn, operation_id=operation_id, kind=AuthorizationKind.WITHDRAW,
                                            attempt_no=row.attempt_count, approval_id=None,
                                            granted_by=principal.user_id, now=now,
                                            expires_at=now + timedelta(minutes=2))
        uow.conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(
            state=AuthorizationState.CLAIMED.value, claimed_by=handler_id, lease_until=now + timedelta(minutes=2)))
        uow.append_event(user_event(principal, "AUTHORIZATION_CREATED", operation_id=operation_id,
                                    correlation={"correlation_value": row.correlation_value,
                                                 "attempt_no": row.attempt_count, "authorization_id": str(auth_id),
                                                 "message_id": message_id},
                                    payload={"kind": "WITHDRAW"}))
    # ---- the file operation: no transaction open -------------------------------------------------------------
    try:
        result = ctx.port(row.backend_id).withdraw_unclaimed(message_id)
    except DomainError:
        raise
    except Exception as exc:
        result = WithdrawResult(withdrawn=False, absence_evidence=None,
                                detail=f"withdrawal failed ({exc.__class__.__name__})")
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        now = uow.clock()
        row = uow.lock_operation(operation_id)
        auth = uow.conn.execute(select(t.authorizations).where(t.authorizations.c.id == auth_id)
                                .with_for_update()).one()
        # the claim must still be ours and live (as in the executor's T-BEGIN); otherwise the sweeper has expired it
        # and the reconciler may have acted meanwhile: record the file result, never project an outcome from it
        valid = (auth.state == AuthorizationState.CLAIMED.value and auth.claimed_by == handler_id
                 and auth.lease_until is not None and auth.lease_until > now and auth.expires_at > now)
        payload: dict[str, object] = {"withdrawn": result.withdrawn, "detail": result.detail}
        if valid:
            uow.conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth_id).values(
                state=AuthorizationState.CONSUMED.value, consumed_at=now))
        else:
            payload["authorization_state"] = auth.state
            result = WithdrawResult(withdrawn=result.withdrawn, absence_evidence=None,
                                    detail=f"{result.detail}; the withdrawal authorization was no longer valid, so "
                                           "no outcome is recorded from it")
        uow.append_event(user_event(principal, "WITHDRAW_RESULT", operation_id=operation_id,
                                    correlation={"correlation_value": row.correlation_value,
                                                 "authorization_id": str(auth_id), "message_id": message_id},
                                    payload=payload))
        if valid and result.withdrawn and result.absence_evidence is EvidenceClass.WITHDRAWN_BEFORE_PICKUP \
                and row.state in (S.AWAITING_ACK.value, S.UNKNOWN.value):
            apply_decision(uow, row, Decision(S.NOT_APPLIED, "T20", EvidenceClass.WITHDRAWN_BEFORE_PICKUP,
                                              detail=result.detail),
                           user_event(principal, "OUTCOME_RECORDED"), ctx.settings, now=now)
            return WithdrawOutcome(True, S.NOT_APPLIED, result.detail)
        if row.state == S.UNKNOWN.value:
            uow.conn.execute(update(t.operations).where(t.operations.c.id == row.id).values(
                next_recheck_at=now, pending_recheck_trigger="MANUAL", row_version=row.row_version + 1,
                updated_at=now))
        return WithdrawOutcome(False, S(row.state), result.detail)
