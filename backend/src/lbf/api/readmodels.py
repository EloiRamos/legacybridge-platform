"""Read models for the API (queries only; decisions stay in ``lbf.workflow``). Permitted actions are computed by
the pure ``lbf.workflow.permissions.permitted_actions`` from database facts on every request."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, and_, func, select

from lbf.auth.policy import Principal, requirements
from lbf.domain.enums import EvidenceClass, OperationState, Outcome
from lbf.domain.evidence import verification_level
from lbf.domain.models import PolicyCheck
from lbf.persistence import tables as t
from lbf.ports.capabilities import BackendCapabilities
from lbf.workflow.permissions import OperationFacts, permitted_actions

S = OperationState


def _level(row: Row[Any]) -> str | None:
    if row.last_outcome and row.last_evidence:
        level = verification_level(Outcome(row.last_outcome), EvidenceClass(row.last_evidence))
        return level.value if level else None
    return None


def usernames(conn: Connection, ids: set[Any]) -> dict[Any, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {r.id: str(r.username) for r in conn.execute(select(t.users.c.id, t.users.c.username)
                                                        .where(t.users.c.id.in_(ids)))}


def summary(row: Row[Any], names: dict[Any, str], total: Any = None) -> dict[str, Any]:
    return {
        "operation_id": row.id, "correlation_value": row.correlation_value, "backend_id": row.backend_id,
        "native_customer_id": row.native_customer_id, "state": row.state, "last_outcome": row.last_outcome,
        "last_evidence": row.last_evidence, "verification_level": _level(row), "current_version": row.current_version,
        "attempt_count": row.attempt_count, "origin": row.origin, "created_by": names.get(row.created_by, "?"),
        "created_at": row.created_at, "updated_at": row.updated_at,
        "estimated_total": str(total) if total is not None else None, "anchor_order_ref": row.anchor_order_ref,
    }


def summaries(conn: Connection, rows: Sequence[Row[Any]]) -> list[dict[str, Any]]:
    names = usernames(conn, {r.created_by for r in rows})
    totals: dict[Any, Any] = {}
    if rows:
        vs = t.validation_snapshots
        for r in conn.execute(select(vs.c.operation_id, vs.c.version, vs.c.estimated_total, vs.c.validated_at)
                              .where(vs.c.operation_id.in_([x.id for x in rows]))
                              .order_by(vs.c.validated_at)):
            totals[(r.operation_id, r.version)] = r.estimated_total
    return [summary(r, names, totals.get((r.id, r.current_version))) for r in rows]


def list_operations(conn: Connection, *, states: Sequence[str] = (), backend: str | None = None,
                    customer: str | None = None, created_by: UUID | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:
    o = t.operations
    q = select(o).order_by(o.c.updated_at.desc()).limit(limit)
    if states:
        q = q.where(o.c.state.in_(list(states)))
    if backend:
        q = q.where(o.c.backend_id == backend)
    if customer:
        q = q.where(o.c.native_customer_id == customer)
    if created_by:
        q = q.where(o.c.created_by == created_by)
    return summaries(conn, conn.execute(q).all())


def facts(conn: Connection, row: Row[Any], caps: BackendCapabilities) -> OperationFacts:
    authors = frozenset(conn.execute(select(t.draft_versions.c.created_by)
                                     .where(t.draft_versions.c.operation_id == row.id)).scalars())
    open_auth = conn.execute(select(t.authorizations.c.id).where(
        t.authorizations.c.operation_id == row.id,
        t.authorizations.c.state.in_(["PENDING", "CLAIMED"]))).first() is not None
    approver = conn.execute(select(t.approvals.c.decided_by).where(
        t.approvals.c.operation_id == row.id, t.approvals.c.attempt_no == row.attempt_count,
        t.approvals.c.decision == "APPROVED")).scalar()
    xs = conn.execute(select(t.transmissions).where(t.transmissions.c.operation_id == row.id,
                                                    t.transmissions.c.attempt_no == row.attempt_count)
                      .order_by(t.transmissions.c.transmission_no)).all()
    req = None
    if row.state == S.READY.value:
        snap = conn.execute(select(t.validation_snapshots).where(
            t.validation_snapshots.c.operation_id == row.id, t.validation_snapshots.c.version == row.current_version)
            .order_by(t.validation_snapshots.c.validated_at.desc()).limit(1)).first()
        if snap is not None:
            req = requirements([PolicyCheck.model_validate(c) for c in snap.checks])
    return OperationFacts(
        state=S(row.state), created_by=row.created_by, version_authors=authors,
        last_evidence=EvidenceClass(row.last_evidence) if row.last_evidence else None,
        has_open_authorization=open_auth, approver_of_current_attempt=approver,
        first_handoff_at=xs[0].started_at if xs else None, message_id=xs[0].message_id if xs else None,
        redeliveries_in_attempt=sum(1 for x in xs if x.kind == "REDELIVER"),
        last_manual_recheck_at=row.last_manual_recheck_at, approval_requirements=req)


def actions_for(conn: Connection, row: Row[Any], caps: BackendCapabilities, principal: Principal,
                now: datetime) -> list[str]:
    return sorted(a.value for a in permitted_actions(facts(conn, row, caps), caps, principal, now))


def _jsonish(row: Row[Any], drop: Sequence[str] = ()) -> dict[str, Any]:
    return {k: v for k, v in row._mapping.items() if k not in drop}


def detail(conn: Connection, operation_id: UUID, caps: BackendCapabilities, principal: Principal,
           now: datetime, settings: Any) -> dict[str, Any] | None:
    row = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).first()
    if row is None:
        return None
    def by_op(table: Any) -> Any:
        return conn.execute(select(table).where(table.c.operation_id == operation_id))

    versions = [_jsonish(v) for v in by_op(t.draft_versions).all()]
    names = usernames(conn, {row.created_by} | {v["created_by"] for v in versions})
    for v in versions:
        v["created_by"] = names.get(v["created_by"], "?")
    snaps = conn.execute(select(t.validation_snapshots).where(
        t.validation_snapshots.c.operation_id == operation_id,
        t.validation_snapshots.c.version == row.current_version)
        .order_by(t.validation_snapshots.c.validated_at.desc()).limit(1)).first()
    approvals = [_jsonish(a) for a in by_op(t.approvals).all()]
    authz = [_jsonish(a) for a in by_op(t.authorizations).all()]
    names.update(usernames(conn, {a["decided_by"] for a in approvals} | {a["granted_by"] for a in authz}))
    for a in approvals:
        a["decided_by"] = names.get(a["decided_by"], "?")
    for a in authz:
        a["granted_by"] = names.get(a["granted_by"], "?")
    transmissions = [_jsonish(x, drop=("payload",)) for x in conn.execute(
        select(t.transmissions).where(t.transmissions.c.operation_id == operation_id)
        .order_by(t.transmissions.c.started_at)).all()]
    msgids = {x["message_id"] for x in transmissions if x["message_id"]}
    acks = [_jsonish(a) for a in conn.execute(select(t.acknowledgements).where(
        t.acknowledgements.c.message_id.in_(msgids))).all()] if msgids else []
    runs = [_jsonish(r) for r in conn.execute(select(t.reconciliation_runs).where(
        t.reconciliation_runs.c.operation_id == operation_id).order_by(t.reconciliation_runs.c.started_at)).all()]
    escalations = [_jsonish(e) for e in by_op(t.escalations).all()]
    timeline = [{"seq": e.seq, "event_type": e.event_type, "occurred_at": e.occurred_at,
                 "actor_type": e.actor_type, "actor_username": e.actor_username, "on_behalf_of": e.on_behalf_of,
                 "state": e.state_change, "correlation": e.correlation, "payload": e.payload,
                 "event_hash": e.event_hash}
                for e in conn.execute(select(t.audit_events).where(t.audit_events.c.operation_id == operation_id)
                                      .order_by(t.audit_events.c.seq)).all()]
    total = snaps.estimated_total if snaps is not None else None
    schedule = {"unknown_since": row.unknown_since, "next_recheck_at": row.next_recheck_at,
                "auto_rechecks": row.auto_rechecks, "awaiting_ack_since": row.awaiting_ack_since,
                "ack_deadline_at": (row.awaiting_ack_since + timedelta(seconds=caps.ack_deadline_seconds))
                if row.awaiting_ack_since and caps.ack_deadline_seconds else None,
                "escalation_at": settings.unknown_escalation_at(row.unknown_since)
                if row.state == S.UNKNOWN.value and row.unknown_since else None,
                "redelivery_deadline": (transmissions[0]["started_at"] + timedelta(
                    seconds=(caps.dedup_window_seconds or 0) - (caps.dedup_safety_margin_seconds or 0)))
                if transmissions and caps.supports_redelivery else None}
    return {
        "operation": summary(row, names, total), "versions": versions,
        "validation": _jsonish(snaps) if snaps is not None else None, "approvals": approvals,
        "authorizations": authz, "transmissions": transmissions, "acknowledgements": acks,
        "reconciliation_runs": runs, "escalations": escalations, "timeline": timeline,
        "permitted_actions": actions_for(conn, row, caps, principal, now),
        "capabilities": caps.model_dump(mode="json"), "schedule": schedule,
    }


def counts_by_state(conn: Connection) -> dict[str, int]:
    rows = conn.execute(select(t.operations.c.state, func.count()).group_by(t.operations.c.state)).all()
    return {r[0]: int(r[1]) for r in rows}


def approval_queue(conn: Connection, principal: Principal) -> list[dict[str, Any]]:
    o = t.operations
    rows = conn.execute(select(o).where(o.c.state == S.READY.value, o.c.created_by != principal.user_id,
                                        ~select(t.draft_versions.c.operation_id).where(and_(
                                            t.draft_versions.c.operation_id == o.c.id,
                                            t.draft_versions.c.created_by == principal.user_id)).exists())
                        .order_by(o.c.updated_at)).all()
    return summaries(conn, rows)


def reconciliation_queue(conn: Connection, now: datetime, deadlines: dict[str, int | None]) -> list[dict[str, Any]]:
    o = t.operations
    rows = conn.execute(select(o).where(o.c.state.in_([S.UNKNOWN.value, S.RECONCILING.value, S.ESCALATED.value,
                                                       S.AWAITING_ACK.value])).order_by(o.c.updated_at)).all()
    keep = []
    for r in rows:
        if r.state == S.AWAITING_ACK.value:
            deadline = deadlines.get(r.backend_id)
            if not (deadline and r.awaiting_ack_since and r.awaiting_ack_since + timedelta(seconds=deadline) < now):
                continue
        keep.append(r)
    return summaries(conn, keep)
