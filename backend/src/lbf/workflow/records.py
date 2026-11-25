"""Shared persistence of workflow decisions: one state change + its audit events in the caller's transaction.

``apply_decision`` is the only way commands and engines record an outcome: it performs the transition through
``UnitOfWork.transition`` (table-checked, row-versioned, audited atomically — G6) and maintains the bookkeeping
columns the schedules and recovery depend on.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Row

from lbf.auth.policy import Principal
from lbf.domain.enums import EvidenceClass, OperationState, ReconciliationTrigger
from lbf.domain.evidence import EVIDENCE_OUTCOME, verification_level
from lbf.domain.models import SourceRead
from lbf.persistence import tables as t
from lbf.persistence.audit import AuditEvent
from lbf.persistence.uow import UnitOfWork
from lbf.workflow.classify import Decision
from lbf.workflow.context import WorkflowSettings

S = OperationState


def user_event(principal: Principal, event_type: str, **kw: Any) -> AuditEvent:
    actor_type = "ASSISTANT" if principal.on_behalf_of is not None else "USER"
    return AuditEvent(event_type, actor_type=actor_type, actor_id=str(principal.user_id),
                      actor_username=principal.username,
                      on_behalf_of=str(principal.on_behalf_of) if principal.on_behalf_of else None, **kw)


def system_event(engine_id: str, event_type: str, **kw: Any) -> AuditEvent:
    return AuditEvent(event_type, actor_type="SYSTEM", actor_id=engine_id, **kw)


def reads_payload(reads: list[SourceRead]) -> list[dict[str, Any]]:
    return [{"source": r.source, "read_at": r.read_at, "snapshot_at": r.snapshot_at,
             "sha256": r.response_sha256, "summary": r.summary} for r in reads]


def apply_decision(
    uow: UnitOfWork,
    row: Row[Any],
    decision: Decision,
    event: AuditEvent,
    settings: WorkflowSettings,
    *,
    now: datetime,
    source_reads: list[SourceRead] | None = None,
    attempt_no: int | None = None,
    opened_by: uuid.UUID | None = None,
    **updates: Any,
) -> Row[Any]:
    """Transition ``row`` according to ``decision`` and record the outcome (OUTCOME_RECORDED payload)."""
    target = decision.target
    values: dict[str, Any] = dict(updates)
    if decision.evidence is not None:
        outcome = EVIDENCE_OUTCOME[decision.evidence]
        values["last_outcome"] = outcome.value
        values["last_evidence"] = decision.evidence.value
        level = verification_level(outcome, decision.evidence)
        event.payload.update({
            "outcome": outcome.value, "evidence_class": decision.evidence.value,
            "verification_level": level.value if level else None,
        })
    if decision.order_ref:
        values.setdefault("anchor_order_ref", decision.order_ref)
        event.payload["backend_order_ref"] = decision.order_ref
    if decision.detail:
        event.payload["detail"] = decision.detail[:500]
    if source_reads:
        event.payload["source_reads"] = reads_payload(source_reads)
    if target is S.UNKNOWN:
        from_unknown_cycle = row.state in (S.RECONCILING.value,) and row.unknown_since is not None
        if not from_unknown_cycle:
            values.setdefault("unknown_since", now)
            values.setdefault("auto_rechecks", 0)
            values.setdefault("next_recheck_at", settings.recheck_due(now, 0))
            values.setdefault("pending_recheck_trigger", ReconciliationTrigger.AUTO.value)
    if target is S.AWAITING_ACK:
        values.setdefault("awaiting_ack_since", now)
    if target in (S.CONFIRMED, S.REJECTED, S.NOT_APPLIED, S.CLOSED_ATTESTED, S.ESCALATED):
        values.setdefault("next_recheck_at", None)
        values.setdefault("pending_recheck_trigger", None)
    if target is S.ESCALATED and decision.evidence is not None:
        escalation_id = uuid.uuid4()
        values["escalation_id"] = escalation_id
    new_row = uow.transition(row, target, decision.transition, event, **values)
    if target is S.ESCALATED and decision.evidence is not None:
        uow.conn.execute(t.escalations.insert().values(
            id=values["escalation_id"], operation_id=row.id, reason=decision.evidence.value,
            attempt_no=attempt_no if attempt_no is not None else row.attempt_count, opened_at=now,
            opened_by=opened_by))
        esc = AuditEvent("ESCALATION_OPENED", actor_type=event.actor_type, actor_id=event.actor_id,
                         actor_username=event.actor_username, operation_id=row.id,
                         correlation={"correlation_value": row.correlation_value},
                         payload={"escalation_id": str(values["escalation_id"]), "reason": decision.evidence.value})
        uow.append_event(esc)
    return new_row


def evidence_upgrade(uow: UnitOfWork, row: Row[Any], evidence: EvidenceClass, engine_id: str,
                     reads: list[SourceRead], order_ref: str) -> None:
    """``EVIDENCE_UPGRADED``: an acknowledged-only confirmation becomes verified by a read (no state change)."""
    from sqlalchemy import update

    uow.conn.execute(update(t.operations).where(t.operations.c.id == row.id,
                                                t.operations.c.row_version == row.row_version)
                     .values(last_evidence=evidence.value, row_version=row.row_version + 1,
                             updated_at=uow.clock()))
    level = verification_level(EVIDENCE_OUTCOME[evidence], evidence)
    uow.append_event(system_event(engine_id, "EVIDENCE_UPGRADED", operation_id=row.id,
                                  correlation={"correlation_value": row.correlation_value},
                                  payload={"from": row.last_evidence, "to": evidence.value,
                                           "outcome": "CONFIRMED", "evidence_class": evidence.value,
                                           "verification_level": level.value if level else None,
                                           "backend_order_ref": order_ref,
                                           "source_reads": reads_payload(reads)}))
