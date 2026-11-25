"""Unit of Work: one PostgreSQL transaction holding a state change and its audit event (G6).

Every state change goes through :meth:`UnitOfWork.transition`, which (1) asserts the transition exists in
the domain table, (2) updates ``state`` and ``row_version`` guarded by the expected row version, and (3)
appends the audit event — all in the same transaction. The database trigger independently rejects any
state change that is not in the table.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, Row, select, text, update

from lbf.domain import state_machine
from lbf.domain.enums import OperationState
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.persistence.audit import AppendedEvent, AuditEvent, AuditWriter

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


class UnitOfWork:
    def __init__(self, engine: Engine, *, clock: Clock = utcnow, audit: AuditWriter | None = None) -> None:
        self._engine = engine
        self.clock = clock
        self.audit = audit or AuditWriter(clock)
        self.conn: Connection
        self.last_event: AppendedEvent | None = None  # identity of the newest event of this transaction

    def __enter__(self) -> UnitOfWork:
        self.conn = self._engine.connect()
        self._tx = self.conn.begin()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                self._tx.commit()
            else:
                self._tx.rollback()
        finally:
            self.conn.close()

    # ------------------------------------------------------------------ helpers

    def lock_operation(self, operation_id: UUID, *, nowait: bool = False) -> Row[Any]:
        q = select(t.operations).where(t.operations.c.id == operation_id).with_for_update(nowait=nowait)
        row = self.conn.execute(q).first()
        if row is None:
            raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
        return row

    def advisory_xact_lock(self, key: str) -> None:
        self.conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})

    def append_event(self, event: AuditEvent) -> AppendedEvent:
        self.last_event = self.audit.append(self.conn, event)
        return self.last_event

    def transition(
        self,
        operation: Row[Any],
        to_state: OperationState,
        transition_id: str,
        event: AuditEvent,
        **updates: Any,
    ) -> Row[Any]:
        from_state = OperationState(operation.state)
        try:
            state_machine.require(from_state, to_state, transition_id)
        except state_machine.InvalidTransition as exc:
            raise DomainError(ErrorCode.CONFLICT_STATE, str(exc)) from exc
        now = self.clock()
        result = self.conn.execute(
            update(t.operations)
            .where(t.operations.c.id == operation.id, t.operations.c.row_version == operation.row_version)
            .values(state=to_state.value, row_version=operation.row_version + 1, updated_at=now, **updates)
            .returning(*t.operations.c)
        )
        row = result.first()
        if row is None:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "operation changed concurrently")
        event.operation_id = operation.id
        event.state = {"from": from_state.value, "to": to_state.value, "transition": transition_id}
        event.correlation = {"correlation_value": operation.correlation_value, **event.correlation}
        self.append_event(event)
        return row
