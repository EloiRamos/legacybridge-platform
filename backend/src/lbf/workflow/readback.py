"""Read-back of one committed draft version and of the audit event its own transaction appended (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, select

from lbf.domain.models import DraftVersion
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.audit import AppendedEvent, hash_document, row_document

EventIdentity = AppendedEvent


@dataclass(frozen=True)
class VerifiedVersion:
    correlation_value: str
    version: DraftVersion
    event: EventIdentity


def verify_version(engine: Engine, operation_id: UUID, version: int, content_hash: str,
                   event: EventIdentity | None) -> VerifiedVersion | None:
    """``None`` unless operation, version, content hash and event identity all agree and both hashes recompute. The
    event is found by its own id, never as "the newest event of the draft"."""
    if event is None:
        return None
    with engine.connect() as conn:
        stored = repo.load_draft_version(conn, operation_id, version)  # recomputes the content hash
        correlation = conn.execute(select(t.operations.c.correlation_value).where(
            t.operations.c.id == operation_id)).scalar()
        row = conn.execute(select(t.audit_events).where(t.audit_events.c.event_id == event.event_id)).first()
    if stored is None or stored.content_hash != content_hash or correlation is None or row is None:
        return None
    if (row.seq, row.event_hash, row.event_type, row.operation_id, row.payload.get("version"),
            row.payload.get("content_hash")) != (event.seq, event.event_hash, "DRAFT_VERSION_CREATED", operation_id,
                                                 version, content_hash):
        return None
    if hash_document(row_document(row)) != event.event_hash:
        return None
    return VerifiedVersion(str(correlation), stored, event)
