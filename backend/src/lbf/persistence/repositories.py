"""Row-level persistence primitives shared by workflow commands and engines.

Repositories return SQLAlchemy rows or domain models; they never decide business rules. Every function
takes the caller's connection so that it participates in the caller's Unit of Work.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, func, select

from lbf.domain.canonical import canonical_json
from lbf.domain.enums import AuthorizationKind, AuthorizationState, DraftOrigin, OperationState
from lbf.domain.models import DraftContent, DraftVersion, ValidationSnapshot
from lbf.persistence import tables as t


def jsonable(value: Any) -> Any:
    return json.loads(canonical_json(value))


def insert_operation(
    conn: Connection,
    *,
    operation_id: UUID,
    correlation_value: str,
    backend_id: str,
    native_customer_id: str,
    created_by: UUID,
    origin: DraftOrigin,
    now: datetime,
) -> Row[Any]:
    return conn.execute(
        t.operations.insert()
        .values(
            id=operation_id,
            correlation_value=correlation_value,
            backend_id=backend_id,
            native_customer_id=native_customer_id,
            state=OperationState.DRAFT.value,
            current_version=1,
            attempt_count=0,
            created_by=created_by,
            origin=origin.value,
            created_at=now,
            updated_at=now,
            row_version=0,
            auto_rechecks=0,
        )
        .returning(*t.operations.c)
    ).one()


def insert_draft_version(conn: Connection, version: DraftVersion) -> None:
    conn.execute(
        t.draft_versions.insert().values(
            operation_id=version.operation_id,
            version=version.version,
            content=jsonable(version.content.model_dump(mode="python")),
            content_hash=version.content_hash,
            created_by=version.created_by,
            origin=version.origin.value,
            created_at=version.created_at,
        )
    )


def load_draft_version(conn: Connection, operation_id: UUID, version: int) -> DraftVersion | None:
    row = conn.execute(
        select(t.draft_versions).where(
            t.draft_versions.c.operation_id == operation_id, t.draft_versions.c.version == version
        )
    ).first()
    if row is None:
        return None
    return DraftVersion(
        operation_id=row.operation_id,
        version=row.version,
        content=DraftContent.model_validate(row.content),
        content_hash=row.content_hash,
        created_by=row.created_by,
        origin=DraftOrigin(row.origin),
        created_at=row.created_at,
    )


def version_authors(conn: Connection, operation_id: UUID) -> frozenset[UUID]:
    rows = conn.execute(
        select(t.draft_versions.c.created_by).where(t.draft_versions.c.operation_id == operation_id).distinct()
    ).scalars()
    return frozenset(rows)


def insert_validation_snapshot(conn: Connection, snap: ValidationSnapshot) -> UUID:
    sid = uuid.uuid4()
    dumped = snap.model_dump(mode="json")
    conn.execute(
        t.validation_snapshots.insert().values(
            id=sid,
            operation_id=snap.operation_id,
            version=snap.version,
            validated_at=snap.validated_at,
            checks=dumped["checks"],
            availability=dumped["availability"],
            lines=dumped["lines"],
            estimated_total=snap.estimated_total,
            blocking=snap.blocking,
        )
    )
    return sid


def latest_validation(conn: Connection, operation_id: UUID, version: int) -> Row[Any] | None:
    return conn.execute(
        select(t.validation_snapshots)
        .where(t.validation_snapshots.c.operation_id == operation_id, t.validation_snapshots.c.version == version)
        .order_by(t.validation_snapshots.c.validated_at.desc())
        .limit(1)
    ).first()


def insert_approval(
    conn: Connection,
    *,
    operation_id: UUID,
    version: int,
    content_hash: str,
    attempt_no: int,
    decision: str,
    decided_by: UUID,
    now: datetime,
    policy_version: str,
    checks: Any,
    override_reason: str | None = None,
    reason: str | None = None,
) -> UUID:
    aid = uuid.uuid4()
    conn.execute(
        t.approvals.insert().values(
            id=aid,
            operation_id=operation_id,
            version=version,
            content_hash=content_hash,
            attempt_no=attempt_no,
            decision=decision,
            decided_by=decided_by,
            decided_at=now,
            policy_version=policy_version,
            checks=jsonable(checks),
            override_reason=override_reason,
            reason=reason,
        )
    )
    return aid


def insert_authorization(
    conn: Connection,
    *,
    operation_id: UUID,
    kind: AuthorizationKind,
    attempt_no: int,
    approval_id: UUID | None,
    granted_by: UUID,
    now: datetime,
    expires_at: datetime,
) -> UUID:
    auth_id = uuid.uuid4()
    conn.execute(
        t.authorizations.insert().values(
            id=auth_id,
            operation_id=operation_id,
            kind=kind.value,
            attempt_no=attempt_no,
            approval_id=approval_id,
            granted_by=granted_by,
            granted_at=now,
            expires_at=expires_at,
            state=AuthorizationState.PENDING.value,
        )
    )
    return auth_id


def open_authorization(conn: Connection, operation_id: UUID) -> Row[Any] | None:
    return conn.execute(
        select(t.authorizations).where(
            t.authorizations.c.operation_id == operation_id,
            t.authorizations.c.state.in_([AuthorizationState.PENDING.value, AuthorizationState.CLAIMED.value]),
        )
    ).first()


def count(conn: Connection, table: Any, *where: Any) -> int:
    q = select(func.count()).select_from(table)
    for w in where:
        q = q.where(w)
    return int(conn.execute(q).scalar_one())
