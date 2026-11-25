"""Acknowledgement poller (async backends only, by capability): record in the DB **first**, then archive the file.

Idempotent by file digest (``acknowledgements.file_sha256`` UNIQUE): a file seen again after a crash between the
DB commit and the archive move is moved without being recorded twice.
* ``ACCEPTED``/``DUPLICATE`` for an ``AWAITING_ACK`` operation ⇒ T17 ``CONFIRMED`` / ``ACKNOWLEDGED_ONLY``
  (read-back later upgrades the evidence).
* ``REJECTED`` ⇒ recorded only; the reconciler finalizes it after a confirming extract read (T18/T18b).
* An acknowledgement for an ``UNKNOWN``/``ESCALATED`` operation (late) schedules a reconciliation (trigger ACK).
* Unparseable or unmatched files ⇒ ``UNMATCHED_ACK`` (and ``INTEGRITY_ALERT`` when malformed) for the
  integration engineer's queue; never treated as acceptance or rejection.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from lbf.domain.enums import EvidenceClass, OperationState, ReconciliationTrigger
from lbf.engines.common import schedule_now
from lbf.engines.pause import PausePoints
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.ports.backend import AckObservation
from lbf.workflow import metrics
from lbf.workflow.classify import Decision
from lbf.workflow.context import WorkflowContext
from lbf.workflow.records import apply_decision, system_event

S = OperationState


class AckPoller:
    def __init__(self, ctx: WorkflowContext, engine_id: str, pauses: PausePoints | None = None) -> None:
        self.ctx = ctx
        self.engine_id = engine_id
        self.pauses = pauses or PausePoints()

    def run_once(self) -> int:
        handled = 0
        for port in self.ctx.backends.values():
            if not port.capabilities().is_async:
                continue
            for ack in port.poll_acknowledgements():
                self.pauses.reach("before_ack_record")
                self.record(ack)
                port.archive_acknowledgement(ack)  # only after the DB record committed
                handled += 1
        return handled

    def record(self, ack: AckObservation) -> bool:
        a = t.acknowledgements
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            if uow.conn.execute(select(a.c.id).where(a.c.file_sha256 == ack.file_sha256)).first() is not None:
                return False
            x = None
            if ack.message_id:
                x = uow.conn.execute(select(t.transmissions).where(t.transmissions.c.message_id == ack.message_id)
                                     .order_by(t.transmissions.c.started_at.desc())).first()
            op: Any = uow.lock_operation(x.operation_id) if x is not None else None
            uow.conn.execute(a.insert().values(
                id=uuid.uuid4(), message_id=ack.message_id, seq=ack.seq, status=ack.status, order_ref=ack.order_ref,
                code=ack.code, ts_utc=ack.ts_utc, file_sha256=ack.file_sha256, file_name=ack.file_name,
                transmission_id=x.id if x is not None else None, operation_id=op.id if op is not None else None,
                recorded_at=now))
            corr = {"message_id": ack.message_id}
            payload = {"status": ack.status, "order_ref": ack.order_ref, "code": ack.code, "seq": ack.seq,
                       "file_name": ack.file_name, "file_sha256": ack.file_sha256, "detail": ack.detail}
            matched = op is not None and x is not None and ack.status != "MALFORMED"
            metrics.ACKS.labels(ack.status, str(matched).lower()).inc()
            if not matched or x is None:
                uow.append_event(system_event(self.engine_id, "UNMATCHED_ACK",
                                              operation_id=op.id if op is not None else None,
                                              correlation=corr, payload=payload))
                if ack.status == "MALFORMED":
                    uow.append_event(system_event(self.engine_id, "INTEGRITY_ALERT", correlation=corr,
                                                  payload={"reason": "unparseable acknowledgement", **payload}))
                return True
            corr = {"correlation_value": op.correlation_value, "attempt_no": op.attempt_count,
                    "transmission_id": str(x.id), "message_id": ack.message_id}
            uow.append_event(system_event(self.engine_id, "ACK_RECORDED", operation_id=op.id, correlation=corr,
                                          payload=payload))
            current_attempt = x.attempt_no == op.attempt_count
            if op.state == S.AWAITING_ACK.value and current_attempt and ack.status in ("ACCEPTED", "DUPLICATE") \
                    and ack.order_ref:
                apply_decision(uow, op, Decision(S.CONFIRMED, "T17", EvidenceClass.ACKNOWLEDGED_ONLY, ack.order_ref,
                                                 f"acknowledged {ack.status}; not yet read back"),
                               system_event(self.engine_id, "OUTCOME_RECORDED", correlation=corr),
                               self.ctx.settings, now=now)
                metrics.TRANSITIONS.labels(S.CONFIRMED.value).inc()
            elif op.state in (S.UNKNOWN.value, S.ESCALATED.value):
                schedule_now(uow, op, ReconciliationTrigger.ACK)
            return True
