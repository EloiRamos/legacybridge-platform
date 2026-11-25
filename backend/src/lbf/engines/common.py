"""Helpers shared by engines: reconstructing what was approved and handed off, and the phase-2 step."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select, update

from lbf.domain.enums import OperationState, ReconciliationTrigger, ReconciliationVerdict
from lbf.domain.models import (
    DraftVersion,
    ReconciliationQuery,
    ReconciliationResult,
    SubmissionLine,
    TransportObservation,
)
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import classify, metrics
from lbf.workflow.context import WorkflowContext
from lbf.workflow.records import apply_decision, reads_payload, system_event

S = OperationState


def approved_version(conn: Connection, operation_id: UUID, attempt_no: int) -> DraftVersion:
    """The exact draft version the approval of ``attempt_no`` bound (G3)."""
    approval = conn.execute(select(t.approvals).where(
        t.approvals.c.operation_id == operation_id, t.approvals.c.attempt_no == attempt_no,
        t.approvals.c.decision == "APPROVED")).first()
    if approval is None:
        raise RuntimeError(f"no approval for attempt {attempt_no}")
    version = repo.load_draft_version(conn, operation_id, approval.version)
    if version is None or version.content_hash != approval.content_hash:
        raise RuntimeError("approved version missing or hash mismatch")
    return version


def attempt_transmissions(conn: Connection, operation_id: UUID, attempt_no: int) -> list[Row[Any]]:
    return list(conn.execute(select(t.transmissions).where(
        t.transmissions.c.operation_id == operation_id, t.transmissions.c.attempt_no == attempt_no)
        .order_by(t.transmissions.c.transmission_no)).all())


def build_query(conn: Connection, op: Row[Any], *, anchor: str | None = None,
                not_before: datetime | None = None) -> ReconciliationQuery:
    version = approved_version(conn, op.id, op.attempt_count)
    xs = attempt_transmissions(conn, op.id, op.attempt_count)
    first = xs[0] if xs else None
    handoff_at = first.started_at if first is not None else op.updated_at
    if not_before is not None and not_before > handoff_at:
        handoff_at = not_before
    return ReconciliationQuery(
        operation_id=op.id, backend_id=op.backend_id, native_customer_id=op.native_customer_id,
        correlation_value=op.correlation_value, message_id=first.message_id if first is not None else None,
        expected_lines=[SubmissionLine(native_item=ln.native_item, quantity_native=ln.quantity_native)
                        for ln in version.content.lines],
        anchor_order_ref=anchor, first_handoff_at=handoff_at, attempt_no=op.attempt_count)


def ack_facts(conn: Connection, op: Row[Any]) -> tuple[classify.AckFacts, datetime | None]:
    xs = attempt_transmissions(conn, op.id, op.attempt_count)
    msgids = {x.message_id for x in xs if x.message_id}
    if not msgids:
        return classify.AckFacts(), None
    rows = conn.execute(select(t.acknowledgements).where(t.acknowledgements.c.message_id.in_(msgids))
                        .order_by(t.acknowledgements.c.recorded_at)).all()
    accepted = [r for r in rows if r.status in ("ACCEPTED", "DUPLICATE") and r.order_ref]
    rejected = [r for r in rows if r.status == "REJECTED"]
    last_rejected_at = rejected[-1].recorded_at if rejected else None
    return classify.AckFacts(accepted_ref=accepted[-1].order_ref if accepted else None,
                             duplicate=any(r.status == "DUPLICATE" for r in accepted),
                             rejected=bool(rejected)), last_rejected_at


def record_run(uow: UnitOfWork, op: Row[Any], *, trigger: ReconciliationTrigger, engine_id: str,
               started_at: datetime, result: ReconciliationResult, requested_by: UUID | None = None) -> UUID:
    run_id = uuid.uuid4()
    now = uow.clock()
    uow.conn.execute(t.reconciliation_runs.insert().values(
        id=run_id, operation_id=op.id, trigger=trigger.value, requested_by=requested_by, engine_id=engine_id,
        started_at=started_at, finished_at=now, verdict=result.verdict.value,
        result=repo.jsonable(result.model_dump(mode="python", exclude={"source_reads"})),
        source_reads=repo.jsonable(reads_payload(result.source_reads)), lease_until=started_at))
    metrics.RECONCILIATIONS.labels(trigger.value, result.verdict.value).inc()
    return run_id


def latest_observed(conn: Connection, op: Row[Any]) -> Row[Any] | None:
    xs = [x for x in attempt_transmissions(conn, op.id, op.attempt_count) if x.state == "OBSERVED"]
    return xs[-1] if xs else None


def run_phase2(ctx: WorkflowContext, engine_id: str, operation_id: UUID) -> bool:
    """Phase 2 for an observed ``ACCEPTED``/``UNCERTAIN`` handoff (read-only; safe to repeat after a crash)."""
    with ctx.engine.connect() as conn:
        op = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).first()
        if op is None or op.state != S.SUBMITTING.value or not op.phase2_pending:
            return False
        x = latest_observed(conn, op)
        if x is None or x.observation is None:
            return False
        obs = TransportObservation.model_validate(x.observation)
        p1 = classify.phase1(obs, ctx.caps(op.backend_id))
        query = build_query(conn, op, anchor=p1.anchor)
    started = ctx.clock()
    try:
        result = ctx.port(op.backend_id).reconcile(query)
    except Exception as exc:
        result = ReconciliationResult(verdict=ReconciliationVerdict.READ_FAILED,
                                      detail=f"read failed ({exc.__class__.__name__})")
    decision = (classify.phase2_read_back(result, p1.anchor) if p1.phase2 == "READ_BACK" and p1.anchor
                else classify.phase2_correlation(result))
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        row = uow.lock_operation(operation_id)
        if row.state != S.SUBMITTING.value or not row.phase2_pending:
            return False
        run_id = record_run(uow, row, trigger=ReconciliationTrigger.AUTO, engine_id=engine_id, started_at=started,
                            result=result)
        uow.append_event(system_event(engine_id, "RECONCILIATION_FINISHED", operation_id=row.id,
                                      correlation={"correlation_value": row.correlation_value,
                                                   "attempt_no": row.attempt_count,
                                                   "reconciliation_run_id": str(run_id)},
                                      payload={"phase": 2, "kind": p1.phase2, "verdict": result.verdict.value,
                                               "candidates": result.candidates,
                                               "source_reads": reads_payload(result.source_reads)}))
        event = system_event(engine_id, "OUTCOME_RECORDED", correlation={"attempt_no": row.attempt_count,
                                                                         "reconciliation_run_id": str(run_id)})
        apply_decision(uow, row, decision, event, ctx.settings, now=uow.clock(), source_reads=result.source_reads,
                       phase2_pending=False)
    metrics.TRANSITIONS.labels(decision.target.value).inc()
    return True


def schedule_now(uow: UnitOfWork, op: Row[Any], trigger: ReconciliationTrigger) -> None:
    now = uow.clock()
    uow.conn.execute(update(t.operations).where(t.operations.c.id == op.id).values(
        next_recheck_at=now, pending_recheck_trigger=trigger.value, row_version=op.row_version + 1, updated_at=now))
