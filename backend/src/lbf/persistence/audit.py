"""Hash-chained audit events, written inside the business transaction.

``event_hash = SHA-256(canonical_json(event document without "event_hash"))``; genesis ``prev_hash`` is
64 zeros. ``seq`` is allocated as ``last_seq + 1`` under ``pg_advisory_xact_lock(LBF_AUDIT_LOCK)``, so a
rolled-back transaction consumes no sequence number and the chain stays contiguous.

HASH-CHAIN INTEGRITY != EVENT COMPLETENESS != AUTHENTICITY: the chain detects modification of recorded
events (relative to retained head hashes); it does not prove that every real-world action was recorded,
nor who physically performed it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, select, text

from lbf.domain.canonical import canonical_json, sha256_hex
from lbf.domain.enums import AUDIT_SCHEMA
from lbf.persistence import tables as t

LBF_AUDIT_LOCK = 0x4C42460002
GENESIS = "0" * 64


def fmt_ts(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _jsonable(value: Any) -> Any:
    """The exact JSON value that is hashed and stored (Decimals/UUIDs/dates normalized)."""
    return json.loads(canonical_json(value))


@dataclass
class AuditEvent:
    event_type: str
    actor_type: str = "SYSTEM"  # USER | SYSTEM | ASSISTANT
    actor_id: str | None = None
    actor_username: str | None = None
    on_behalf_of: str | None = None
    operation_id: UUID | None = None
    correlation: dict[str, Any] = field(default_factory=dict)
    state: dict[str, str] | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class AppendedEvent:
    seq: int
    event_id: UUID
    event_hash: str


def document(
    *,
    seq: int,
    event_id: UUID,
    event_type: str,
    occurred_at: datetime,
    recorded_at: datetime,
    actor_type: str,
    actor_id: str | None,
    actor_username: str | None,
    on_behalf_of: str | None,
    operation_id: UUID | None,
    correlation: Any,
    state: Any,
    payload: Any,
    prev_hash: str,
) -> dict[str, Any]:
    return {
        "schema": AUDIT_SCHEMA,
        "seq": seq,
        "event_id": str(event_id),
        "event_type": event_type,
        "occurred_at": fmt_ts(occurred_at),
        "recorded_at": fmt_ts(recorded_at),
        "actor": {"type": actor_type, "id": actor_id, "username": actor_username, "on_behalf_of": on_behalf_of},
        "operation_id": str(operation_id) if operation_id else None,
        "correlation": correlation,
        "state": state,
        "payload": payload,
        "prev_hash": prev_hash,
    }


def hash_document(doc: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({k: v for k, v in doc.items() if k != "event_hash"}))


class AuditWriter:
    def __init__(self, clock: Any = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def append(self, conn: Connection, event: AuditEvent) -> AppendedEvent:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LBF_AUDIT_LOCK})
        last = conn.execute(
            select(t.audit_events.c.seq, t.audit_events.c.event_hash).order_by(t.audit_events.c.seq.desc()).limit(1)
        ).first()
        seq = (last.seq + 1) if last else 1
        prev_hash = last.event_hash if last else GENESIS
        now = self._clock()
        occurred_at = event.occurred_at or now
        event_id = uuid.uuid4()
        correlation = _jsonable(event.correlation)
        state = _jsonable(event.state) if event.state is not None else None
        payload = _jsonable(event.payload)
        doc = document(
            seq=seq,
            event_id=event_id,
            event_type=event.event_type,
            occurred_at=occurred_at,
            recorded_at=now,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            actor_username=event.actor_username,
            on_behalf_of=event.on_behalf_of,
            operation_id=event.operation_id,
            correlation=correlation,
            state=state,
            payload=payload,
            prev_hash=prev_hash,
        )
        event_hash = hash_document(doc)
        conn.execute(
            t.audit_events.insert().values(
                seq=seq,
                event_id=event_id,
                schema_version=AUDIT_SCHEMA,
                occurred_at=occurred_at,
                recorded_at=now,
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                actor_username=event.actor_username,
                on_behalf_of=event.on_behalf_of,
                operation_id=event.operation_id,
                correlation=correlation,
                state_change=state,
                payload=payload,
                prev_hash=prev_hash,
                event_hash=event_hash,
            )
        )
        return AppendedEvent(seq=seq, event_id=event_id, event_hash=event_hash)


def row_document(row: Any) -> dict[str, Any]:
    doc = document(
        seq=row.seq,
        event_id=row.event_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        actor_username=row.actor_username,
        on_behalf_of=row.on_behalf_of,
        operation_id=row.operation_id,
        correlation=row.correlation,
        state=row.state_change,
        payload=row.payload,
        prev_hash=row.prev_hash,
    )
    doc["event_hash"] = row.event_hash
    return doc


def iter_documents(conn: Connection, from_seq: int = 1, to_seq: int | None = None) -> Iterator[dict[str, Any]]:
    q = select(t.audit_events).where(t.audit_events.c.seq >= from_seq).order_by(t.audit_events.c.seq)
    if to_seq is not None:
        q = q.where(t.audit_events.c.seq <= to_seq)
    for row in conn.execute(q):
        yield row_document(row)


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    count: int
    head_seq: int
    head_hash: str
    first_broken_seq: int | None = None
    reason: str = ""


def verify_documents(
    docs: Iterator[dict[str, Any]] | list[dict[str, Any]], *, start_prev: str = GENESIS, start_seq: int = 1
) -> ChainVerification:
    expected_seq = start_seq
    prev = start_prev
    count = 0
    for doc in docs:
        seq = doc.get("seq")
        if seq != expected_seq:
            return ChainVerification(
                False, count, expected_seq - 1, prev, expected_seq, f"sequence gap at {expected_seq}"
            )
        if doc.get("prev_hash") != prev:
            return ChainVerification(False, count, seq - 1, prev, seq, "prev_hash does not link")
        if hash_document(doc) != doc.get("event_hash"):
            return ChainVerification(False, count, seq - 1, prev, seq, "event_hash mismatch")
        prev = doc["event_hash"]
        expected_seq += 1
        count += 1
    return ChainVerification(True, count, expected_seq - 1, prev)


def verify_chain(conn: Connection) -> ChainVerification:
    return verify_documents(iter_documents(conn))
