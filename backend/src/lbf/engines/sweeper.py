"""Recovery sweeper (T-SWEEP) — at engine start and every 5 s. **Never resends.**

1. ``SENDING`` transmissions with an expired lease ⇒ ``INTERRUPTED``; the operation ⇒ ``UNKNOWN/INTERRUPTED``
   with an immediate reconciliation (trigger RECOVERY). G4: never ``NOT_APPLIED``, never a second handoff.
2. ``CLAIMED`` authorizations with an expired lease and no transmission ⇒ ``PENDING`` (still valid) or
   ``EXPIRED``; an expired ``SUBMIT`` ⇒ ``NOT_APPLIED/APPROVAL_EXPIRED`` (positive evidence: no transmission row
   ⇒ no handoff, because a handoff happens only after the row commits). ``PENDING`` past expiry likewise (T10).
3. Reconciliation runs with an expired lease ⇒ finished as ``READ_FAILED``; operation back to ``reconciling_from``.
4. Phase 2 still pending after a crash ⇒ re-run phase 2 (read-only).
5. ``UNKNOWN`` operations without a scheduled re-check ⇒ re-armed from their stored ``unknown_since``.
(Acknowledgement files left unarchived are re-read by the ack poller and de-duplicated by digest.)
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, select, update

from lbf.domain.enums import AuthorizationKind, AuthorizationState, EvidenceClass, OperationState, ReconciliationTrigger
from lbf.domain.enums import ReconciliationVerdict as V
from lbf.engines.common import run_phase2, schedule_now
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import metrics
from lbf.workflow.classify import Decision
from lbf.workflow.context import RECHECK_LIMIT, WorkflowContext
from lbf.workflow.records import apply_decision, system_event

S = OperationState


class Sweeper:
    """Lock order everywhere in the engines and commands: the ``operations`` row first, then the dependent row
    (transmission, authorization, reconciliation run) — so the sweeper can never deadlock with the executor,
    the reconciler or a command. Candidates are found without locks and re-checked under the locks."""

    def __init__(self, ctx: WorkflowContext, engine_id: str) -> None:
        self.ctx = ctx
        self.engine_id = engine_id

    def _candidates(self, table: Any, *where: Any) -> list[Row[Any]]:
        with self.ctx.engine.connect() as conn:
            return list(conn.execute(select(table.c.id, table.c.operation_id).where(*where).limit(200)).all())

    def run_once(self) -> int:
        return (self.interrupted_transmissions() + self.stale_claims() + self.expired_authorizations()
                + self.expired_runs() + self.stalled_phase2() + self.rearm())

    # ------------------------------------------------------------------ 1
    def interrupted_transmissions(self) -> int:
        x, n = t.transmissions, 0
        for cand in self._candidates(x, x.c.state == "SENDING", x.c.lease_until < self.ctx.clock()):
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                now = uow.clock()
                op = uow.lock_operation(cand.operation_id)
                tx_row = uow.conn.execute(select(x).where(x.c.id == cand.id, x.c.state == "SENDING",
                                                          x.c.lease_until < now).with_for_update()).first()
                if tx_row is None:
                    continue  # observed meanwhile (or another sweeper won)
                uow.conn.execute(update(x).where(x.c.id == tx_row.id, x.c.state == "SENDING")
                                 .values(state="INTERRUPTED", observed_at=now))
                corr = {"correlation_value": op.correlation_value, "attempt_no": tx_row.attempt_no,
                        "transmission_no": tx_row.transmission_no, "transmission_id": str(tx_row.id),
                        "message_id": tx_row.message_id}
                uow.append_event(system_event(self.engine_id, "TRANSMISSION_INTERRUPTED", operation_id=op.id,
                                              correlation=dict(corr),
                                              payload={"reason": "no observation before the transmission lease "
                                                       "expired (process died or stalled)"}))
                if op.state == S.SUBMITTING.value:
                    apply_decision(uow, op, Decision(S.UNKNOWN, "T14", EvidenceClass.INTERRUPTED,
                                                     detail="interrupted transmission; never resent"),
                                   system_event(self.engine_id, "OUTCOME_RECORDED", correlation=dict(corr)),
                                   self.ctx.settings, now=now, next_recheck_at=now, phase2_pending=False,
                                   pending_recheck_trigger=ReconciliationTrigger.RECOVERY.value)
                    metrics.TRANSITIONS.labels(S.UNKNOWN.value).inc()
                n += 1
        return n

    # ------------------------------------------------------------------ 2
    def _expire(self, uow: UnitOfWork, op: Row[Any], auth: Row[Any], reason: str) -> None:
        a = t.authorizations
        now = uow.clock()
        uow.conn.execute(update(a).where(a.c.id == auth.id).values(state=AuthorizationState.EXPIRED.value))
        corr = {"correlation_value": op.correlation_value, "attempt_no": auth.attempt_no,
                "authorization_id": str(auth.id)}
        uow.append_event(system_event(self.engine_id, "AUTHORIZATION_EXPIRED", operation_id=op.id,
                                      correlation=dict(corr), payload={"kind": auth.kind, "reason": reason}))
        if auth.kind == AuthorizationKind.SUBMIT.value and op.state == S.APPROVED.value:
            apply_decision(uow, op, Decision(S.NOT_APPLIED, "T10", EvidenceClass.APPROVAL_EXPIRED,
                                             detail="authorization expired before any transmission row existed"),
                           system_event(self.engine_id, "OUTCOME_RECORDED", correlation=dict(corr)),
                           self.ctx.settings, now=now)
            metrics.TRANSITIONS.labels(S.NOT_APPLIED.value).inc()
        elif auth.kind == AuthorizationKind.WITHDRAW.value and op.state in (S.UNKNOWN.value, S.ESCALATED.value):
            schedule_now(uow, op, ReconciliationTrigger.RECOVERY)

    def stale_claims(self) -> int:
        a, n = t.authorizations, 0
        for cand in self._candidates(a, a.c.state == AuthorizationState.CLAIMED.value,
                                     a.c.lease_until < self.ctx.clock()):
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                now = uow.clock()
                op = uow.lock_operation(cand.operation_id)
                auth = uow.conn.execute(select(a).where(
                    a.c.id == cand.id, a.c.state == AuthorizationState.CLAIMED.value, a.c.lease_until < now)
                    .with_for_update()).first()
                if auth is None:
                    continue
                has_tx = uow.conn.execute(select(t.transmissions.c.id).where(
                    t.transmissions.c.authorization_id == auth.id)).first() is not None
                if has_tx:  # cannot happen (T-BEGIN consumes in the same tx); never release a used claim
                    uow.append_event(system_event(self.engine_id, "INTEGRITY_ALERT", operation_id=auth.operation_id,
                                                  payload={"reason": "claimed authorization with a transmission",
                                                           "authorization_id": str(auth.id)}))
                elif auth.expires_at > now and auth.kind != AuthorizationKind.WITHDRAW.value:
                    uow.conn.execute(update(a).where(a.c.id == auth.id).values(
                        state=AuthorizationState.PENDING.value, claimed_by=None, lease_until=None))
                    uow.append_event(system_event(self.engine_id, "AUTHORIZATION_RELEASED",
                                                  operation_id=auth.operation_id,
                                                  payload={"authorization_id": str(auth.id),
                                                           "claimed_by": auth.claimed_by}))
                else:
                    self._expire(uow, op, auth, "CLAIM_LEASE_EXPIRED")
                n += 1
        return n

    def expired_authorizations(self) -> int:
        a, n = t.authorizations, 0
        for cand in self._candidates(a, a.c.state == AuthorizationState.PENDING.value,
                                     a.c.expires_at <= self.ctx.clock()):
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                now = uow.clock()
                op = uow.lock_operation(cand.operation_id)
                auth = uow.conn.execute(select(a).where(
                    a.c.id == cand.id, a.c.state == AuthorizationState.PENDING.value, a.c.expires_at <= now)
                    .with_for_update()).first()
                if auth is None:
                    continue
                self._expire(uow, op, auth, "AUTHORIZATION_TTL")
                n += 1
        return n

    # ------------------------------------------------------------------ 3
    def expired_runs(self) -> int:
        r, n = t.reconciliation_runs, 0
        for cand in self._candidates(r, r.c.finished_at.is_(None), r.c.lease_until < self.ctx.clock()):
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                now = uow.clock()
                op = uow.lock_operation(cand.operation_id)
                run = uow.conn.execute(select(r).where(r.c.id == cand.id, r.c.finished_at.is_(None),
                                                       r.c.lease_until < now).with_for_update()).first()
                if run is None:
                    continue
                uow.conn.execute(update(r).where(r.c.id == run.id).values(
                    finished_at=now, verdict=V.READ_FAILED.value,
                    result={"verdict": "READ_FAILED", "detail": "reconciliation lease expired"}))
                if op.state == S.RECONCILING.value:
                    back = S(op.reconciling_from or S.UNKNOWN.value)
                    corr = {"reconciliation_run_id": str(run.id)}
                    if back is S.UNKNOWN:
                        decision = Decision(S.UNKNOWN, "T23", EvidenceClass.RECONCILIATION_FAILED,
                                            detail="reconciliation interrupted")
                        apply_decision(uow, op, decision, system_event(self.engine_id, "OUTCOME_RECORDED",
                                                                       correlation=corr),
                                       self.ctx.settings, now=now, reconciling_from=None,
                                       next_recheck_at=self.ctx.settings.recheck_due(op.unknown_since or now,
                                                                                     op.auto_rechecks),
                                       pending_recheck_trigger=ReconciliationTrigger.AUTO.value)
                    else:
                        apply_decision(uow, op, Decision(S.ESCALATED, "T24", None,
                                                         detail="reconciliation interrupted"),
                                       system_event(self.engine_id, "RECONCILIATION_FINISHED", correlation=corr,
                                                    payload={"verdict": "READ_FAILED"}),
                                       self.ctx.settings, now=now, reconciling_from=None)
                n += 1
        return n

    # ------------------------------------------------------------------ 4
    def stalled_phase2(self) -> int:
        now, n = self.ctx.clock(), 0
        with self.ctx.engine.connect() as conn:
            ops = conn.execute(select(t.operations).where(
                t.operations.c.state == S.SUBMITTING.value, t.operations.c.phase2_pending.is_(True),
                t.operations.c.updated_at < now - self.ctx.settings.claim_lease)).all()
        for op in ops:
            if run_phase2(self.ctx, self.engine_id, op.id):
                n += 1
        return n

    # ------------------------------------------------------------------ 5
    def rearm(self) -> int:
        n = 0
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            rows = uow.conn.execute(select(t.operations).where(
                t.operations.c.state == S.UNKNOWN.value, t.operations.c.next_recheck_at.is_(None),
                t.operations.c.auto_rechecks < RECHECK_LIMIT).with_for_update(skip_locked=True)).all()
            for op in rows:
                uow.conn.execute(update(t.operations).where(t.operations.c.id == op.id).values(
                    next_recheck_at=self.ctx.settings.recheck_due(op.unknown_since or now, op.auto_rechecks),
                    pending_recheck_trigger=op.pending_recheck_trigger or ReconciliationTrigger.AUTO.value,
                    row_version=op.row_version + 1, updated_at=now))
                n += 1
        return n
