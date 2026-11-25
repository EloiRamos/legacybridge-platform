"""Reconciler engine — read-only (T28).

Per tick: (a) ``AWAITING_ACK`` past the ack deadline ⇒ ``UNKNOWN/ACK_OVERDUE`` (T19); (b) a recorded ``REJECTED``
ack is finalized only after a confirming snapshot read taken after the ack (T18) or escalated when contradicted
(T18b); (c) due re-checks run T21 → T22–T26 with a reconciliation lease; (d) ``UNKNOWN`` older than 30 min
(scaled in the test profile) ⇒ ``ESCALATED`` (T28); (e) acknowledged-only confirmations are upgraded by a read
(``EVIDENCE_UPGRADED``, no state change). Nothing here ever hands anything to a backend.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select, update

from lbf.domain.enums import EvidenceClass, OperationState, ReconciliationTrigger
from lbf.domain.enums import ReconciliationVerdict as V
from lbf.domain.models import ReconciliationResult
from lbf.engines.common import ack_facts, build_query, record_run
from lbf.engines.pause import PausePoints
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import classify, metrics
from lbf.workflow.classify import Decision
from lbf.workflow.context import RECHECK_LIMIT, WorkflowContext
from lbf.workflow.records import apply_decision, evidence_upgrade, reads_payload, system_event

S = OperationState


class Reconciler:
    def __init__(self, ctx: WorkflowContext, engine_id: str, pauses: PausePoints | None = None,
                 upgrade_interval: timedelta = timedelta(seconds=2)) -> None:
        self.ctx = ctx
        self.engine_id = engine_id
        self.pauses = pauses or PausePoints()
        self.upgrade_interval = upgrade_interval
        self._last_upgrade_try: dict[UUID, datetime] = {}

    def _ops(self, *where: Any) -> list[Row[Any]]:
        with self.ctx.engine.connect() as conn:
            return list(conn.execute(select(t.operations).where(*where).order_by(t.operations.c.updated_at)).all())

    def _read(self, op: Row[Any], query: Any) -> ReconciliationResult:
        try:
            return self.ctx.port(op.backend_id).reconcile(query)
        except Exception as exc:
            return ReconciliationResult(verdict=V.READ_FAILED, detail=f"read failed ({exc.__class__.__name__})")

    def run_once(self) -> int:
        n = self.ack_overdue() + self.verify_rejected_acks() + self.age_escalations()
        n += self.due_rechecks() + self.upgrade_acknowledged()
        return n

    # ------------------------------------------------------------------ (a) T19
    def ack_overdue(self) -> int:
        now, n = self.ctx.clock(), 0
        for op in self._ops(t.operations.c.state == S.AWAITING_ACK.value):
            deadline = self.ctx.caps(op.backend_id).ack_deadline_seconds
            if deadline is None or op.awaiting_ack_since is None:
                continue
            if op.awaiting_ack_since + timedelta(seconds=deadline) > now:
                continue
            with self.ctx.engine.connect() as conn:
                acks, _ = ack_facts(conn, op)
            if acks.rejected or acks.accepted_ref:
                continue  # an acknowledgement arrived; (b) or the poller handles it
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                row = uow.lock_operation(op.id)
                if row.state != S.AWAITING_ACK.value or row.row_version != op.row_version:
                    continue
                apply_decision(uow, row, Decision(S.UNKNOWN, "T19", EvidenceClass.ACK_OVERDUE,
                                                  detail=f"no acknowledgement within {deadline} s"),
                               system_event(self.engine_id, "OUTCOME_RECORDED"), self.ctx.settings, now=uow.clock())
            metrics.TRANSITIONS.labels(S.UNKNOWN.value).inc()
            n += 1
        return n

    # ------------------------------------------------------------------ (b) T18 / T18b
    def verify_rejected_acks(self) -> int:
        n = 0
        for op in self._ops(t.operations.c.state == S.AWAITING_ACK.value):
            with self.ctx.engine.connect() as conn:
                acks, rejected_at = ack_facts(conn, op)
                if not acks.rejected or acks.accepted_ref or rejected_at is None:
                    continue
                query = build_query(conn, op, not_before=rejected_at)  # the read must postdate the ack (T18)
            started = self.ctx.clock()
            result = self._read(op, query)
            decision = classify.rejected_ack_verification(result)
            if decision is None:
                continue
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                row = uow.lock_operation(op.id)
                if row.state != S.AWAITING_ACK.value:
                    continue
                run_id = record_run(uow, row, trigger=ReconciliationTrigger.ACK, engine_id=self.engine_id,
                                    started_at=started, result=result)
                apply_decision(uow, row, decision, system_event(
                    self.engine_id, "OUTCOME_RECORDED", correlation={"reconciliation_run_id": str(run_id)}),
                    self.ctx.settings, now=uow.clock(), source_reads=result.source_reads)
            metrics.TRANSITIONS.labels(decision.target.value).inc()
            n += 1
        return n

    # ------------------------------------------------------------------ (d) T28 (system)
    def age_escalations(self) -> int:
        now, n = self.ctx.clock(), 0
        for op in self._ops(t.operations.c.state == S.UNKNOWN.value, t.operations.c.unknown_since.is_not(None)):
            if self.ctx.settings.unknown_escalation_at(op.unknown_since) > now:
                continue
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                row = uow.lock_operation(op.id)
                if row.state != S.UNKNOWN.value:
                    continue
                if repo.open_authorization(uow.conn, op.id) is not None:
                    continue  # an operator command is in flight (D-G2-01): escalate after it, if still UNKNOWN
                apply_decision(uow, row, Decision(S.ESCALATED, "T28s", EvidenceClass.RECHECK_LIMIT,
                                                  detail="outcome still unknown after 30 minutes"),
                               system_event(self.engine_id, "OUTCOME_RECORDED"), self.ctx.settings, now=uow.clock())
            metrics.TRANSITIONS.labels(S.ESCALATED.value).inc()
            n += 1
        return n

    # ------------------------------------------------------------------ (c) T21 → T22..T26
    def due_rechecks(self) -> int:
        now, n = self.ctx.clock(), 0
        for op in self._ops(t.operations.c.state.in_([S.UNKNOWN.value, S.ESCALATED.value]),
                            t.operations.c.next_recheck_at.is_not(None), t.operations.c.next_recheck_at <= now):
            if self.recheck(op.id):
                n += 1
        return n

    def recheck(self, operation_id: UUID) -> bool:
        run_id = uuid.uuid4()
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            row = uow.lock_operation(operation_id)
            if row.state not in (S.UNKNOWN.value, S.ESCALATED.value):
                return False
            if uow.conn.execute(select(t.reconciliation_runs.c.id).where(
                    t.reconciliation_runs.c.operation_id == operation_id,
                    t.reconciliation_runs.c.finished_at.is_(None))).first() is not None:
                return False  # C-07: one run per operation
            if repo.open_authorization(uow.conn, operation_id) is not None:
                # D-G2-01: a WITHDRAW in flight or a REDELIVER not yet begun needs the state it was authorized in;
                # defer (next_recheck_at stays due) until the authorization is consumed or expired
                return False
            trigger = ReconciliationTrigger(row.pending_recheck_trigger or "AUTO")
            uow.conn.execute(t.reconciliation_runs.insert().values(
                id=run_id, operation_id=operation_id, trigger=trigger.value, engine_id=self.engine_id,
                started_at=now, lease_until=now + self.ctx.settings.reconciliation_lease))
            uow.transition(row, S.RECONCILING, "T21", system_event(
                self.engine_id, "RECONCILIATION_STARTED",
                correlation={"attempt_no": row.attempt_count, "reconciliation_run_id": str(run_id)},
                payload={"trigger": trigger.value}),
                reconciling_from=row.state, next_recheck_at=None, pending_recheck_trigger=None)
        self.pauses.reach("mid_reconcile")
        with self.ctx.engine.connect() as conn:
            op = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).one()
            acks, rejected_at = ack_facts(conn, op)
            query = build_query(conn, op, anchor=op.anchor_order_ref,
                                not_before=rejected_at if acks.rejected else None)
        result = self._read(op, query)
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            row = uow.lock_operation(operation_id)
            run = uow.conn.execute(select(t.reconciliation_runs).where(t.reconciliation_runs.c.id == run_id)
                                   .with_for_update()).one()
            if row.state != S.RECONCILING.value or run.finished_at is not None:
                return False  # the sweeper expired our lease meanwhile; its verdict stands
            auto = trigger is ReconciliationTrigger.AUTO
            count = row.auto_rechecks + (1 if auto else 0)
            decision = classify.reconciliation_outcome(
                result, reconciling_from=S(row.reconciling_from), acks=acks,
                auto_limit_reached=auto and count >= RECHECK_LIMIT)
            uow.conn.execute(update(t.reconciliation_runs).where(t.reconciliation_runs.c.id == run_id).values(
                finished_at=now, verdict=result.verdict.value,
                result=_jsonable(result.model_dump(mode="python", exclude={"source_reads"})),
                source_reads=_jsonable(reads_payload(result.source_reads))))
            metrics.RECONCILIATIONS.labels(trigger.value, result.verdict.value).inc()
            corr = {"attempt_no": row.attempt_count, "reconciliation_run_id": str(run_id)}
            finished = system_event(self.engine_id, "RECONCILIATION_FINISHED", correlation=dict(corr),
                                    payload={"trigger": trigger.value, "verdict": result.verdict.value,
                                             "candidates": result.candidates, "detail": result.detail[:500],
                                             "source_reads": reads_payload(result.source_reads)})
            extra: dict[str, Any] = {"reconciling_from": None}
            if decision.target is S.UNKNOWN:
                extra.update(auto_rechecks=count,
                             next_recheck_at=self.ctx.settings.recheck_due(row.unknown_since or now, count),
                             pending_recheck_trigger=ReconciliationTrigger.AUTO.value)
            if decision.evidence is None:
                apply_decision(uow, row, decision, finished, self.ctx.settings, now=now, **extra)
            else:
                finished.operation_id = row.id
                finished.correlation = {"correlation_value": row.correlation_value, **corr}
                uow.append_event(finished)
                apply_decision(uow, row, decision, system_event(self.engine_id, "OUTCOME_RECORDED",
                                                                correlation=dict(corr)),
                               self.ctx.settings, now=now, source_reads=result.source_reads, **extra)
        metrics.TRANSITIONS.labels(decision.target.value).inc()
        return True

    # ------------------------------------------------------------------ (e) EVIDENCE_UPGRADED
    def upgrade_acknowledged(self) -> int:
        now, n = self.ctx.clock(), 0
        for op in self._ops(t.operations.c.state == S.CONFIRMED.value,
                            t.operations.c.last_evidence == EvidenceClass.ACKNOWLEDGED_ONLY.value):
            last = self._last_upgrade_try.get(op.id)
            if last is not None and now - last < self.upgrade_interval:
                continue
            self._last_upgrade_try[op.id] = now
            with self.ctx.engine.connect() as conn:
                acks, _ = ack_facts(conn, op)
                query = build_query(conn, op, anchor=op.anchor_order_ref)
            result = self._read(op, query)
            if result.verdict is not V.MATCHED or result.matched_order_ref != op.anchor_order_ref:
                continue
            evidence = EvidenceClass.DEDUP_CONFIRMED if acks.duplicate else EvidenceClass.READ_BACK
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                row = uow.lock_operation(op.id)
                if row.last_evidence != EvidenceClass.ACKNOWLEDGED_ONLY.value:
                    continue
                evidence_upgrade(uow, row, evidence, self.engine_id, result.source_reads, op.anchor_order_ref)
            self._last_upgrade_try.pop(op.id, None)
            n += 1
        return n


def _jsonable(value: Any) -> Any:
    from lbf.persistence.repositories import jsonable

    return jsonable(value)
