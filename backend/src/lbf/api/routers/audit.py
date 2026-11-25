"""Audit: event browser (``audit.read``) and in-database chain verification (``audit.verify``). The standalone
verifier (``oracles/audit_verify.py``) checks exports independently of this code."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from lbf.api.deps import Auth, require, services
from lbf.api.schemas import AuditEventOut, AuditPageOut, VerifyOut
from lbf.api.services import AppServices
from lbf.domain.enums import Permission
from lbf.persistence import tables as t
from lbf.persistence.audit import AuditEvent, iter_documents, verify_chain

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("/events", response_model=AuditPageOut, operation_id="auditEvents")
def events(operation_id: UUID | None = None, from_seq: int = Query(default=1, ge=1),
           limit: int = Query(default=200, ge=1, le=1000), _: Auth = Depends(require(Permission.AUDIT_READ)),
           svc: AppServices = Depends(services)) -> AuditPageOut:
    a = t.audit_events
    engine = svc.audit_engine or svc.engine
    with engine.connect() as conn:
        q = select(a).where(a.c.seq >= from_seq).order_by(a.c.seq).limit(limit)
        if operation_id is not None:
            q = q.where(a.c.operation_id == operation_id)
        rows = conn.execute(q).all()
        head = conn.execute(select(func.max(a.c.seq))).scalar() or 0
    return AuditPageOut(head_seq=head, events=[AuditEventOut(
        seq=r.seq, event_type=r.event_type, occurred_at=r.occurred_at, actor_type=r.actor_type,
        actor_username=r.actor_username, on_behalf_of=r.on_behalf_of, operation_id=r.operation_id,
        correlation=r.correlation, state=r.state_change, payload=r.payload, prev_hash=r.prev_hash,
        event_hash=r.event_hash) for r in rows])


@router.get("/verify", response_model=VerifyOut, operation_id="auditVerify")
def verify(_: Auth = Depends(require(Permission.AUDIT_VERIFY)), svc: AppServices = Depends(services)) -> VerifyOut:
    engine = svc.audit_engine or svc.engine
    with engine.connect() as conn:
        v = verify_chain(conn)
    return VerifyOut(valid=v.valid, count=v.count, head_seq=v.head_seq, head_hash=v.head_hash,
                     first_broken_seq=v.first_broken_seq, reason=v.reason)


class ExportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any]
    jsonl: str


@router.post("/exports", response_model=ExportOut, operation_id="auditExport")
def export(from_seq: int = Query(default=1, ge=1), to_seq: int | None = Query(default=None, ge=1),
           auth: Auth = Depends(require(Permission.AUDIT_EXPORT)), svc: AppServices = Depends(services)) -> ExportOut:
    """JSON Lines export + manifest. The compliance reviewer retains ``head_hash`` outside the system; the
    standalone verifier (``oracles/audit_verify.py``) checks the export without application code."""
    import hashlib
    import uuid as _uuid

    from lbf.domain.canonical import canonical_json
    from lbf.persistence.uow import UnitOfWork

    engine = svc.audit_engine or svc.engine
    with engine.connect() as conn:
        docs = list(iter_documents(conn, from_seq, to_seq))
    data = b"".join(canonical_json(d) + b"\n" for d in docs)
    first_prev = docs[0]["prev_hash"] if docs else "0" * 64
    head = docs[-1]["event_hash"] if docs else first_prev
    manifest = {"schema": "lbf-audit/1.0", "from_seq": from_seq, "to_seq": docs[-1]["seq"] if docs else from_seq - 1,
                "count": len(docs), "first_prev_hash": first_prev, "head_hash": head,
                "jsonl_sha256": hashlib.sha256(data).hexdigest(), "exported_at": svc.clock().isoformat(),
                "exported_by": auth.principal.username, "app_version": "2.0.0"}
    with UnitOfWork(svc.engine, clock=svc.clock) as uow:
        audit_exports = t.metadata.tables["lbf.audit_exports"]
        uow.conn.execute(audit_exports.insert().values(
            id=_uuid.uuid4(), requested_by=auth.principal.user_id, from_seq=from_seq, to_seq=manifest["to_seq"],
            head_hash=head, file_sha256=manifest["jsonl_sha256"], created_at=uow.clock()))
        uow.append_event(AuditEvent("AUDIT_EXPORTED", actor_type="USER", actor_id=str(auth.principal.user_id),
                                    actor_username=auth.principal.username,
                                    payload={k: manifest[k] for k in ("from_seq", "to_seq", "count", "head_hash",
                                                                       "jsonl_sha256")}))
    return ExportOut(manifest=manifest, jsonl=data.decode("utf-8"))
