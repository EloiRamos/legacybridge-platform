"""Test builders shared by integration, security and concurrency tests (no business rules here)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Engine, select

from lbf.auth.service import AuthService
from lbf.domain.correlation import new_correlation_value
from lbf.domain.enums import AuthorizationKind, DraftOrigin, OperationState, Role
from lbf.domain.models import DraftContent, DraftContentLine, DraftVersion
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.audit import AuditEvent
from lbf.persistence.uow import UnitOfWork

PASSWORD = "correct-horse-battery-staple"


def now() -> datetime:
    return datetime.now(UTC)


def make_user(engine: Engine, username: str, *roles: Role, password: str = PASSWORD) -> UUID:
    svc = AuthService(engine)
    with engine.begin() as conn:
        return svc.create_user(
            conn, username=username, display_name=username, password=password, roles=list(roles), actor=None
        )


def content(sku: str = "BW-1001", qty: int = 10, customer: str = "00417", backend: str = "halden") -> DraftContent:
    return DraftContent(
        backend_id=backend,
        native_customer_id=customer,
        lines=[
            DraftContentLine(
                line_no=1,
                canonical_sku=sku,
                native_item=sku,
                quantity_base=qty,
                quantity_native=qty,
                native_uom="EA",
                unit_price_estimate=Decimal("4.85"),
                line_estimate=Decimal("4.85") * qty,
            )
        ],
    )


def make_operation(
    engine: Engine,
    creator: UUID,
    *,
    state: OperationState = OperationState.DRAFT,
    backend: str = "halden",
    customer: str = "00417",
) -> UUID:
    """Create an operation with version 1 and walk it (through valid transitions) to ``state``."""
    op_id = uuid.uuid4()
    with UnitOfWork(engine) as uow:
        ts = uow.clock()
        repo.insert_operation(
            uow.conn,
            operation_id=op_id,
            correlation_value=new_correlation_value(),
            backend_id=backend,
            native_customer_id=customer,
            created_by=creator,
            origin=DraftOrigin.UI,
            now=ts,
        )
        repo.insert_draft_version(
            uow.conn,
            DraftVersion.build(
                operation_id=op_id,
                version=1,
                content=content(customer=customer, backend=backend),
                created_by=creator,
                origin=DraftOrigin.UI,
                created_at=ts,
            ),
        )
        uow.append_event(AuditEvent("OPERATION_CREATED", actor_type="USER", actor_id=str(creator), operation_id=op_id))
    path = {
        OperationState.DRAFT: [],
        OperationState.READY: [("T03", OperationState.READY)],
    }
    for tid, to in path[state]:
        with UnitOfWork(engine) as uow:
            row = uow.lock_operation(op_id)
            uow.transition(row, to, tid, AuditEvent("TEST_TRANSITION", actor_type="USER", actor_id=str(creator)))
    return op_id


def approve_tx(engine: Engine, op_id: UUID, approver: UUID, *, expected_version: int = 1) -> UUID:
    """Mirror of T-APPROVE built from persistence primitives (used before the command exists)."""
    with UnitOfWork(engine) as uow:
        row = uow.lock_operation(op_id)
        if row.state != OperationState.READY.value or row.current_version != expected_version:
            raise RuntimeError("CONFLICT")
        draft = repo.load_draft_version(uow.conn, op_id, row.current_version)
        assert draft is not None
        attempt = row.attempt_count + 1
        approval_id = repo.insert_approval(
            uow.conn,
            operation_id=op_id,
            version=draft.version,
            content_hash=draft.content_hash,
            attempt_no=attempt,
            decision="APPROVED",
            decided_by=approver,
            now=uow.clock(),
            policy_version="2026.11-1",
            checks=[],
        )
        auth_id = repo.insert_authorization(
            uow.conn,
            operation_id=op_id,
            kind=AuthorizationKind.SUBMIT,
            attempt_no=attempt,
            approval_id=approval_id,
            granted_by=approver,
            now=uow.clock(),
            expires_at=uow.clock() + timedelta(minutes=15),
        )
        uow.transition(
            row,
            OperationState.APPROVED,
            "T05",
            AuditEvent("APPROVAL_GRANTED", actor_type="USER", actor_id=str(approver)),
            attempt_count=attempt,
        )
        return auth_id


def current_state(engine: Engine, op_id: UUID) -> str:
    with engine.connect() as conn:
        return conn.execute(select(t.operations.c.state).where(t.operations.c.id == op_id)).scalar_one()
