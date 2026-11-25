"""Validation refresher: keeps ``READY`` operations' validation snapshots fresh so approvers are not refused with
``APPROVAL_STALE_VALIDATION`` (10-minute rule). Read-only towards the backends; no state change — a
snapshot that has turned blocking makes the next approval attempt refuse."""

from __future__ import annotations

import logging

from sqlalchemy import select

from lbf.domain.enums import OperationState
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import drafts
from lbf.workflow.context import WorkflowContext, linked_accounts
from lbf.workflow.records import system_event

log = logging.getLogger("lbf.engines")


class ValidationRefresher:
    def __init__(self, ctx: WorkflowContext, engine_id: str) -> None:
        self.ctx = ctx
        self.engine_id = engine_id

    def run_once(self) -> int:
        now, n = self.ctx.clock(), 0
        with self.ctx.engine.connect() as conn:
            ops = conn.execute(select(t.operations).where(t.operations.c.state == OperationState.READY.value)).all()
        for op in ops:
            with self.ctx.engine.connect() as conn:
                latest = repo.latest_validation(conn, op.id, op.current_version)
                if latest is not None and latest.validated_at > now - self.ctx.settings.validation_refresh_after:
                    continue
                version = repo.load_draft_version(conn, op.id, op.current_version)
                links = linked_accounts(conn, op.backend_id, op.native_customer_id)
            if version is None:
                continue
            try:
                snap = drafts.validate(operation_id=op.id, version=version.version, content=version.content,
                                       port=self.ctx.port(op.backend_id),
                                       linked=[(self.ctx.port(b), x) for b, x in links if b in self.ctx.backends],
                                       now=self.ctx.clock())
            except Exception:  # a failed refresh leaves the old snapshot; approval re-checks its age
                log.warning("validation refresh failed for %s", op.id, exc_info=True)
                continue
            with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
                row = uow.lock_operation(op.id)
                if row.state != OperationState.READY.value or row.current_version != snap.version:
                    continue
                repo.insert_validation_snapshot(uow.conn, snap)
                uow.append_event(system_event(self.engine_id, "VALIDATION_RECORDED", operation_id=op.id,
                                              correlation={"correlation_value": row.correlation_value,
                                                           "draft_version": snap.version},
                                              payload={"refresh": True, "blocking": snap.blocking,
                                                       "checks": [c.model_dump(mode="json") for c in snap.checks]}))
            n += 1
        return n
