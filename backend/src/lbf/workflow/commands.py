"""Workflow commands — every human-initiated change.

Each command re-checks permission, segregation of duties, step-up and state **inside** its transaction under the
operation row lock; the API and the assistant call these same functions (no special path). Backend reads
happen before the transaction. No command performs a write handoff: transmissions are performed only by the
executor engine; the withdraw handler lives in ``lbf.workflow.withdraw_unclaimed`` (B-6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, and_, select
from sqlalchemy.exc import IntegrityError

from lbf.auth.policy import (
    APPROVAL_VALUE_THRESHOLD,
    ATTESTATION_NOTE_MIN,
    DECLINE_REASON_MIN,
    POLICY_VERSION,
    Principal,
    approval_permitted,
    has_permission,
    requirements,
    sod1_violated,
    sod4_violated,
    sod5_violated,
    step_up_fresh,
)
from lbf.domain.correlation import new_correlation_value
from lbf.domain.enums import (
    OVERLAP_BLOCKING_STATES,
    AuthorizationKind,
    DraftOrigin,
    EvidenceClass,
    OperationState,
    PolicyResult,
    ReconciliationTrigger,
    ReconciliationVerdict,
    ResolutionKind,
)
from lbf.domain.enums import Permission as P
from lbf.domain.errors import DomainError, ErrorCode
from lbf.domain.evidence import NOT_APPLIED_EVIDENCE
from lbf.domain.models import DraftContent, DraftVersion, PolicyCheck, ReconciliationQuery, SubmissionLine
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.audit import AppendedEvent
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import drafts
from lbf.workflow.classify import Decision
from lbf.workflow.context import WorkflowContext, linked_accounts
from lbf.workflow.permissions import MANUAL_RECHECK_INTERVAL, redelivery_window_open
from lbf.workflow.records import apply_decision, reads_payload, user_event

S = OperationState


# --------------------------------------------------------------------------- guards


def require(principal: Principal, *perms: P, profile: str = "default") -> None:
    if not any(has_permission(principal, p, profile=profile) for p in perms):
        raise DomainError(ErrorCode.FORBIDDEN, "permission missing: " + " or ".join(p.value for p in perms))


def require_step_up(principal: Principal, now: datetime) -> None:
    if not step_up_fresh(principal, now):
        raise DomainError(ErrorCode.REAUTH_REQUIRED, "re-authenticate to perform this action")


def require_state(row: Row[Any], *states: S) -> None:
    if row.state not in {s.value for s in states}:
        raise DomainError(ErrorCode.CONFLICT_STATE, f"not permitted from state {row.state}")


def _content_event_payload(version: DraftVersion) -> dict[str, Any]:
    return {"version": version.version, "content_hash": version.content_hash, "origin": version.origin.value,
            "lines": [{"native_item": ln.native_item, "canonical_sku": ln.canonical_sku,
                       "quantity_base": ln.quantity_base, "quantity_native": ln.quantity_native,
                       "native_uom": ln.native_uom} for ln in version.content.lines]}


# --------------------------------------------------------------------------- T01 create, T02/T04/T07/T32 revise


@dataclass(frozen=True)
class DraftResult:
    operation_id: UUID
    version: int
    content_hash: str
    state: S
    event: AppendedEvent | None = None  # the DRAFT_VERSION_CREATED event of this very commit (None on a replayed key)


def create_draft(ctx: WorkflowContext, principal: Principal, req: drafts.DraftRequest, *,
                 origin: DraftOrigin = DraftOrigin.UI, idempotency_key: str | None = None,
                 raw_request: dict[str, Any] | None = None) -> DraftResult:
    """T01. Reads (customer, catalogue) first; then one T-DRAFT transaction."""
    require(principal, P.DRAFT_CREATE)
    author = principal.on_behalf_of or principal.user_id  # SoD-2: the invoking human authors assistant drafts
    if idempotency_key:
        existing = _operation_for_key(ctx, author, idempotency_key)
        if existing is not None:
            return _result(ctx, existing)
    port = ctx.port(req.backend_id)
    caps = port.capabilities()
    port.get_customer(req.native_customer_id)  # precondition: customer resolvable (raises NOT_FOUND)
    content = drafts.build_content(req, caps, ctx.mappings_for(req.backend_id), drafts.catalogue_prices(port))
    operation_id = uuid.uuid4()
    for _ in range(5):  # correlation value collision ⇒ regenerate
        try:
            with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
                now = uow.clock()
                row = repo.insert_operation(uow.conn, operation_id=operation_id,
                                            correlation_value=new_correlation_value(), backend_id=req.backend_id,
                                            native_customer_id=req.native_customer_id, created_by=author,
                                            origin=origin, now=now)
                version = DraftVersion.build(operation_id=operation_id, version=1, content=content, created_by=author,
                                             origin=origin, created_at=now)
                repo.insert_draft_version(uow.conn, version)
                if idempotency_key:
                    uow.conn.execute(t.idempotency_keys.insert().values(user_id=author, key=idempotency_key,
                                                                        operation_id=operation_id, created_at=now))
                corr = {"correlation_value": row.correlation_value, "draft_version": 1,
                        "content_hash": version.content_hash}
                uow.append_event(user_event(principal, "OPERATION_CREATED", operation_id=operation_id,
                                            correlation=dict(corr), state={"from": None, "to": "DRAFT",
                                                                           "transition": "T01"},
                                            payload={"channel": origin.value, "request": raw_request or
                                                     req.model_dump(mode="json")}))
                uow.append_event(user_event(principal, "INTENT_NORMALIZED", operation_id=operation_id,
                                            correlation=dict(corr), payload=_content_event_payload(version)))
                created = uow.append_event(user_event(principal, "DRAFT_VERSION_CREATED", operation_id=operation_id,
                                                      correlation=dict(corr),
                                                      payload=_content_event_payload(version)))
            return DraftResult(operation_id, 1, version.content_hash, S.DRAFT, created)
        except IntegrityError as exc:
            diag = getattr(exc.orig, "diag", None)
            if idempotency_key and getattr(diag, "constraint_name", None) == "idempotency_keys_pkey":
                # lost the same-key race: this transaction is rolled back and the winner is committed (PostgreSQL
                # reports the conflict only after the winner's commit) ⇒ answer like a sequential repeat
                winner = _operation_for_key(ctx, author, idempotency_key)
                if winner is not None:
                    return _result(ctx, winner)
            if "correlation_value" not in str(exc.orig):
                raise
    raise DomainError(ErrorCode.INTERNAL, "could not allocate a unique correlation value")


def _operation_for_key(ctx: WorkflowContext, author: UUID, key: str) -> UUID | None:
    """Keys are per user and kept at least 24 h (a minimum retention, not an expiry)."""
    with ctx.engine.connect() as conn:
        found: UUID | None = conn.execute(select(t.idempotency_keys.c.operation_id).where(
            t.idempotency_keys.c.user_id == author, t.idempotency_keys.c.key == key)).scalar()
    return found


def _result(ctx: WorkflowContext, operation_id: UUID) -> DraftResult:
    with ctx.engine.connect() as conn:
        row = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).one()
        v = repo.load_draft_version(conn, operation_id, row.current_version)
    assert v is not None
    return DraftResult(operation_id, v.version, v.content_hash, S(row.state))


REVISE_FROM = {S.DRAFT: "T02", S.READY: "T04", S.DECLINED: "T07", S.NOT_APPLIED: "T32"}


def revise_draft(ctx: WorkflowContext, principal: Principal, operation_id: UUID, expected_version: int,
                 req: drafts.DraftRequest | drafts.DraftPatch, *, origin: DraftOrigin = DraftOrigin.UI) -> DraftResult:
    """T02/T04/T07/T32. A request replaces the whole content; a patch keeps every field of ``expected_version`` that it
    omits, and an explicit null clears. A patch's base is read outside the transaction, which is safe because a
    stored version never changes and a base that is no longer current is refused under the row lock."""
    require(principal, P.DRAFT_EDIT)
    author = principal.on_behalf_of or principal.user_id
    with ctx.engine.connect() as conn:
        current = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).first()
    if current is None:
        raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
    if isinstance(req, drafts.DraftPatch):
        with ctx.engine.connect() as conn:
            base = repo.load_draft_version(conn, operation_id, expected_version)
        if base is None:
            raise DomainError(ErrorCode.CONFLICT_VERSION, f"version {expected_version} of this draft does not exist")
        req = req.apply(base.content)
    if req.backend_id != current.backend_id or req.native_customer_id != current.native_customer_id:
        raise DomainError(ErrorCode.VALIDATION, "backend and customer of an operation cannot change")
    port = ctx.port(current.backend_id)
    content = drafts.build_content(req, port.capabilities(), ctx.mappings_for(current.backend_id),
                                   drafts.catalogue_prices(port))
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        row = uow.lock_operation(operation_id)
        if row.current_version != expected_version:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "the draft was changed by someone else")
        state = S(row.state)
        if state not in REVISE_FROM:
            raise DomainError(ErrorCode.CONFLICT_STATE, f"cannot revise in state {state}")
        if repo.open_authorization(uow.conn, operation_id) is not None:
            raise DomainError(ErrorCode.CONFLICT_STATE, "an authorization is open")
        now = uow.clock()
        new_version = row.current_version + 1
        version = DraftVersion.build(operation_id=operation_id, version=new_version, content=content,
                                     created_by=author, origin=origin, created_at=now)
        repo.insert_draft_version(uow.conn, version)
        event = user_event(principal, "DRAFT_VERSION_CREATED",
                           correlation={"draft_version": new_version, "content_hash": version.content_hash},
                           payload=_content_event_payload(version))
        uow.transition(row, S.DRAFT, REVISE_FROM[state], event, current_version=new_version)
        created = uow.last_event
    return DraftResult(operation_id, new_version, version.content_hash, S.DRAFT, created)


# --------------------------------------------------------------------------- T03 / T31 submit for approval


@dataclass(frozen=True)
class SubmitResult:
    state: S
    blocking: bool
    checks: list[PolicyCheck]


def submit_for_approval(ctx: WorkflowContext, principal: Principal, operation_id: UUID,
                        expected_version: int) -> SubmitResult:
    require(principal, P.DRAFT_SUBMIT)
    with ctx.engine.connect() as conn:
        current = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).first()
        if current is None:
            raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
        version = repo.load_draft_version(conn, operation_id, current.current_version)
        links = linked_accounts(conn, current.backend_id, current.native_customer_id)
    assert version is not None
    port = ctx.port(current.backend_id)
    linked = [(ctx.port(b), n) for b, n in links if b in ctx.backends]
    snapshot = drafts.validate(operation_id=operation_id, version=version.version, content=version.content,
                               port=port, linked=linked, now=ctx.clock())
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        row = uow.lock_operation(operation_id)
        if row.current_version != expected_version or row.current_version != snapshot.version:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "the draft was changed while validating")
        state = S(row.state)
        if state is S.DRAFT:
            tid = "T03"
        elif state is S.NOT_APPLIED and row.last_evidence in {e.value for e in NOT_APPLIED_EVIDENCE}:
            tid = "T31"
        else:
            raise DomainError(ErrorCode.CONFLICT_STATE, f"cannot submit for approval from {state}")
        repo.insert_validation_snapshot(uow.conn, snapshot)
        payload = {"version": snapshot.version, "blocking": snapshot.blocking,
                   "checks": [c.model_dump(mode="json") for c in snapshot.checks],
                   "availability_digests": [{"native_item": a.native_item, "status": a.status.value,
                                             "available_base": a.available_base} for a in snapshot.availability]}
        if snapshot.blocking:
            uow.append_event(user_event(principal, "VALIDATION_RECORDED", operation_id=operation_id,
                                        correlation={"correlation_value": row.correlation_value,
                                                     "draft_version": snapshot.version}, payload=payload))
            return SubmitResult(state, True, snapshot.checks)
        uow.append_event(user_event(principal, "VALIDATION_RECORDED", operation_id=operation_id,
                                    correlation={"correlation_value": row.correlation_value,
                                                 "draft_version": snapshot.version}, payload=payload))
        uow.transition(row, S.READY, tid, user_event(principal, "SUBMITTED_FOR_APPROVAL",
                                                     correlation={"draft_version": snapshot.version,
                                                                  "content_hash": version.content_hash},
                                                     payload={"attempt_no_next": row.attempt_count + 1}))
    return SubmitResult(S.READY, False, snapshot.checks)


# --------------------------------------------------------------------------- T05 approve / T06 decline


def _overlap(uow: UnitOfWork, row: Row[Any], content: DraftContent) -> list[str]:
    """I5: unresolved operations for the same backend account sharing ≥ 1 native item (under the advisory lock)."""
    others = uow.conn.execute(select(t.operations).where(
        t.operations.c.backend_id == row.backend_id, t.operations.c.native_customer_id == row.native_customer_id,
        t.operations.c.id != row.id, t.operations.c.state.in_([s.value for s in OVERLAP_BLOCKING_STATES]))).all()
    items = {ln.native_item for ln in content.lines}
    hits = []
    for other in others:
        v = repo.load_draft_version(uow.conn, other.id, other.current_version)
        if v is not None and items & {ln.native_item for ln in v.content.lines}:
            hits.append(other.correlation_value)
    return hits


def _stored_checks(snap_row: Row[Any]) -> list[PolicyCheck]:
    return [PolicyCheck.model_validate(c) for c in snap_row.checks]


@dataclass(frozen=True)
class ApprovalResult:
    approval_id: UUID
    authorization_id: UUID
    attempt_no: int


def approve(ctx: WorkflowContext, principal: Principal, operation_id: UUID, *, version: int, content_hash: str,
            override_reason: str | None = None) -> ApprovalResult:
    """T05 in one T-APPROVE transaction: row lock + per-account advisory lock (I5) + policy + approval +
    SUBMIT authorization + READY→APPROVED + audit."""
    require(principal, P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY)
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        now = uow.clock()
        require_step_up(principal, now)
        row = uow.lock_operation(operation_id)
        require_state(row, S.READY)
        if row.current_version != version:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "approval refers to a version that is not current")
        draft = repo.load_draft_version(uow.conn, operation_id, version)
        assert draft is not None
        if draft.content_hash != content_hash:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "content hash does not match the current version")
        if sod1_violated(principal.user_id, row.created_by, repo.version_authors(uow.conn, operation_id)):
            raise DomainError(ErrorCode.SOD_VIOLATION, "nobody approves an order they prepared (SoD-1)")
        uow.advisory_xact_lock(f"{row.backend_id}:{row.native_customer_id}")
        snap = repo.latest_validation(uow.conn, operation_id, version)
        if snap is None:
            raise DomainError(ErrorCode.APPROVAL_STALE_VALIDATION, "no validation for this version")
        checks = _stored_checks(snap)
        overlap = _overlap(uow, row, draft.content)
        checks.append(PolicyCheck(check="overlap_I5",
                                  result=PolicyResult.OVERRIDE_REQUIRED if overlap else PolicyResult.PASS,
                                  detail=", ".join(sorted(overlap))))
        stale = snap.validated_at < now - _validation_max_age()
        checks.append(PolicyCheck(check="validation_age",
                                  result=PolicyResult.STALE_VALIDATION if stale else PolicyResult.PASS))
        req = requirements(checks)
        ok, why = approval_permitted(principal, req, override_reason=override_reason)
        if not ok:
            code = {"STALE_VALIDATION": ErrorCode.APPROVAL_STALE_VALIDATION, "BLOCKING": ErrorCode.CONFLICT_STATE,
                    "GRANT_ANY_REQUIRED": ErrorCode.FORBIDDEN, "FORBIDDEN": ErrorCode.FORBIDDEN,
                    "OVERRIDE_REQUIRED": ErrorCode.CONFLICT_OVERLAP if req.has_overlap else ErrorCode.FORBIDDEN,
                    "OVERRIDE_REASON_REQUIRED": ErrorCode.CONFLICT_OVERLAP if req.has_overlap
                    else ErrorCode.VALIDATION}[why]
            raise DomainError(code, f"approval refused: {why}", detail={"checks": [c.check for c in checks]})
        attempt = row.attempt_count + 1
        check_dump = [c.model_dump(mode="json") for c in checks]
        approval_id = repo.insert_approval(uow.conn, operation_id=operation_id, version=version,
                                           content_hash=content_hash, attempt_no=attempt, decision="APPROVED",
                                           decided_by=principal.user_id, now=now, policy_version=POLICY_VERSION,
                                           checks=check_dump,
                                           override_reason=override_reason if req.needs_override else None)
        auth_id = repo.insert_authorization(uow.conn, operation_id=operation_id, kind=AuthorizationKind.SUBMIT,
                                            attempt_no=attempt, approval_id=approval_id,
                                            granted_by=principal.user_id, now=now,
                                            expires_at=now + ctx.settings.authorization_ttl)
        corr = {"correlation_value": row.correlation_value, "attempt_no": attempt, "draft_version": version,
                "content_hash": content_hash, "approval_id": str(approval_id), "authorization_id": str(auth_id)}
        if req.needs_override:
            uow.append_event(user_event(principal, "OVERRIDE_RECORDED", operation_id=operation_id,
                                        correlation=dict(corr), payload={"reason": override_reason,
                                                                         "overlap_with": overlap}))
        uow.transition(row, S.APPROVED, "T05", user_event(
            principal, "APPROVAL_GRANTED", correlation=dict(corr),
            payload={"policy_version": POLICY_VERSION, "checks": check_dump,
                     "threshold_eur": str(APPROVAL_VALUE_THRESHOLD)}), attempt_count=attempt)
        uow.append_event(user_event(principal, "AUTHORIZATION_CREATED", operation_id=operation_id,
                                    correlation=dict(corr), payload={"kind": "SUBMIT", "expires_at":
                                                                     now + ctx.settings.authorization_ttl}))
    return ApprovalResult(approval_id, auth_id, attempt)


def _validation_max_age() -> Any:
    from lbf.auth.policy import VALIDATION_MAX_AGE

    return VALIDATION_MAX_AGE


def decline(ctx: WorkflowContext, principal: Principal, operation_id: UUID, *, version: int, content_hash: str,
            reason: str) -> UUID:
    require(principal, P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY)
    if len((reason or "").strip()) < DECLINE_REASON_MIN:
        raise DomainError(ErrorCode.VALIDATION, f"a decline reason of at least {DECLINE_REASON_MIN} characters")
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        now = uow.clock()
        require_step_up(principal, now)
        row = uow.lock_operation(operation_id)
        require_state(row, S.READY)
        draft = repo.load_draft_version(uow.conn, operation_id, row.current_version)
        assert draft is not None
        if row.current_version != version or draft.content_hash != content_hash:
            raise DomainError(ErrorCode.CONFLICT_VERSION, "decline refers to a version that is not current")
        if sod1_violated(principal.user_id, row.created_by, repo.version_authors(uow.conn, operation_id)):
            raise DomainError(ErrorCode.SOD_VIOLATION, "nobody decides on an order they prepared (SoD-1)")
        approval_id = repo.insert_approval(uow.conn, operation_id=operation_id, version=version,
                                           content_hash=content_hash, attempt_no=row.attempt_count + 1,
                                           decision="DECLINED", decided_by=principal.user_id, now=now,
                                           policy_version=POLICY_VERSION, checks=[], reason=reason.strip())
        uow.transition(row, S.DECLINED, "T06", user_event(
            principal, "APPROVAL_DECLINED", correlation={"draft_version": version, "content_hash": content_hash,
                                                         "approval_id": str(approval_id)},
            payload={"reason": reason.strip()}))
    return approval_id


# --------------------------------------------------------------------------- T08 withdraw


def withdraw(ctx: WorkflowContext, principal: Principal, operation_id: UUID) -> None:
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        row = uow.lock_operation(operation_id)
        own = row.created_by == principal.user_id and has_permission(principal, P.DRAFT_WITHDRAW)
        if not (own or has_permission(principal, P.APPROVAL_GRANT_ANY)):
            raise DomainError(ErrorCode.FORBIDDEN, "only the creator or an operations manager may withdraw")
        require_state(row, S.DRAFT, S.READY, S.DECLINED, S.NOT_APPLIED)
        if repo.open_authorization(uow.conn, operation_id) is not None:
            raise DomainError(ErrorCode.CONFLICT_STATE, "an authorization is open")
        uow.transition(row, S.WITHDRAWN, "T08", user_event(principal, "OPERATION_WITHDRAWN"))


# --------------------------------------------------------------------------- re-check (request), T28 escalate


def request_recheck(ctx: WorkflowContext, principal: Principal, operation_id: UUID) -> None:
    """A human *request* (``reconcile.recheck``): the reconciler executes T21 at once. Rate limit 1 per 10 s."""
    require(principal, P.RECONCILE_RECHECK)
    from sqlalchemy import update

    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        now = uow.clock()
        row = uow.lock_operation(operation_id)
        require_state(row, S.UNKNOWN, S.ESCALATED)
        if row.last_manual_recheck_at is not None and now - row.last_manual_recheck_at < MANUAL_RECHECK_INTERVAL:
            raise DomainError(ErrorCode.RATE_LIMITED, "one manual re-check per operation per 10 s")
        uow.conn.execute(update(t.operations).where(t.operations.c.id == row.id).values(
            last_manual_recheck_at=now, next_recheck_at=now,
            pending_recheck_trigger=ReconciliationTrigger.MANUAL.value,
            row_version=row.row_version + 1, updated_at=now))
        uow.append_event(user_event(principal, "RECHECK_REQUESTED", operation_id=operation_id,
                                    correlation={"correlation_value": row.correlation_value}))


def escalate(ctx: WorkflowContext, principal: Principal, operation_id: UUID, note: str = "") -> None:
    require(principal, P.ESCALATION_OPEN)
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        row = uow.lock_operation(operation_id)
        require_state(row, S.UNKNOWN)
        apply_decision(uow, row, Decision(S.ESCALATED, "T28", EvidenceClass.MANUAL, detail=note),
                       user_event(principal, "OUTCOME_RECORDED"), ctx.settings, now=uow.clock(),
                       opened_by=principal.user_id)


# --------------------------------------------------------------------------- T29 / T30 resolve


def _attempt_approver(uow: UnitOfWork, operation_id: UUID, attempt_no: int) -> UUID | None:
    return uow.conn.execute(select(t.approvals.c.decided_by).where(
        t.approvals.c.operation_id == operation_id, t.approvals.c.attempt_no == attempt_no,
        t.approvals.c.decision == "APPROVED")).scalar()


def resolve(ctx: WorkflowContext, principal: Principal, operation_id: UUID, *, kind: ResolutionKind,
            external_reference: str, note: str = "") -> S:
    require(principal, P.ESCALATION_RESOLVE)
    now = ctx.clock()
    require_step_up(principal, now)
    ref = (external_reference or "").strip()
    if not ref:
        raise DomainError(ErrorCode.VALIDATION, "a backend order reference or external reference is required")
    result = None
    if kind is ResolutionKind.VERIFIED:
        with ctx.engine.connect() as conn:
            op = conn.execute(select(t.operations).where(t.operations.c.id == operation_id)).first()
            if op is None:
                raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
            version = repo.load_draft_version(conn, operation_id, op.current_version)
            first = conn.execute(select(t.transmissions.c.started_at, t.transmissions.c.message_id).where(
                t.transmissions.c.operation_id == operation_id, t.transmissions.c.attempt_no == op.attempt_count
            ).order_by(t.transmissions.c.transmission_no)).first()
        assert version is not None
        result = ctx.port(op.backend_id).reconcile(ReconciliationQuery(
            operation_id=operation_id, backend_id=op.backend_id, native_customer_id=op.native_customer_id,
            correlation_value=op.correlation_value, message_id=first.message_id if first else None,
            expected_lines=[SubmissionLine(native_item=ln.native_item, quantity_native=ln.quantity_native)
                            for ln in version.content.lines],
            anchor_order_ref=ref, first_handoff_at=first.started_at if first else now,
            attempt_no=op.attempt_count))
        if result.verdict is not ReconciliationVerdict.MATCHED:
            raise DomainError(ErrorCode.VALIDATION, f"order {ref} could not be verified: {result.verdict.value}",
                              detail={"verdict": result.verdict.value, "detail": result.detail})
    elif len((note or "").strip()) < ATTESTATION_NOTE_MIN:
        raise DomainError(ErrorCode.VALIDATION, f"an attestation note of at least {ATTESTATION_NOTE_MIN} characters")
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        row = uow.lock_operation(operation_id)
        require_state(row, S.ESCALATED)
        if sod5_violated(principal.user_id, _attempt_approver(uow, operation_id, row.attempt_count)):
            raise DomainError(ErrorCode.SOD_VIOLATION, "the approver of the attempt cannot resolve it (SoD-5)")
        if kind is ResolutionKind.VERIFIED:
            assert result is not None
            decision = Decision(S.CONFIRMED, "T29", EvidenceClass.MANUAL_VERIFIED, ref, result.detail)
            evidence: dict[str, Any] = {"verdict": result.verdict.value,
                                        "source_reads": reads_payload(result.source_reads)}
        else:
            ev = EvidenceClass(kind.value)
            decision = Decision(S.CLOSED_ATTESTED, "T30", ev, None, note.strip())
            evidence = {"attestation": kind.value}
        if row.escalation_id is not None:
            from sqlalchemy import update

            uow.conn.execute(update(t.escalations).where(t.escalations.c.id == row.escalation_id).values(
                resolved_at=uow.clock(), resolved_by=principal.user_id, resolution=kind.value,
                resolution_evidence=repo.jsonable(evidence), external_reference=ref, note=note.strip() or None))
        event = user_event(principal, "OUTCOME_RECORDED", payload={"external_reference": ref})
        apply_decision(uow, row, decision, event, ctx.settings, now=uow.clock(),
                       source_reads=result.source_reads if result else None)
        uow.append_event(user_event(principal, "ESCALATION_RESOLVED", operation_id=operation_id,
                                    correlation={"correlation_value": row.correlation_value},
                                    payload={"resolution": kind.value, "external_reference": ref,
                                             "verified": kind is ResolutionKind.VERIFIED}))
    return decision.target


# --------------------------------------------------------------------------- T27 authorize redelivery


def authorize_redelivery(ctx: WorkflowContext, principal: Principal, operation_id: UUID) -> UUID:
    """Creates a REDELIVER authorization; the executor performs T27 (UNKNOWN → SUBMITTING) with the stored bytes."""
    require(principal, P.SKARVIK_REDELIVER_AUTHORIZE)
    with UnitOfWork(ctx.engine, clock=ctx.clock) as uow:
        now = uow.clock()
        require_step_up(principal, now)
        row = uow.lock_operation(operation_id)
        require_state(row, S.UNKNOWN)
        caps = ctx.caps(row.backend_id)
        if not caps.supports_redelivery:
            raise DomainError(ErrorCode.CAPABILITY_UNSUPPORTED, "redelivery is not supported by this backend")
        if sod4_violated(principal.user_id, repo.version_authors(uow.conn, operation_id), row.created_by):
            raise DomainError(ErrorCode.SOD_VIOLATION, "an author of the order cannot authorize its redelivery")
        if repo.open_authorization(uow.conn, operation_id) is not None:
            raise DomainError(ErrorCode.CONFLICT_STATE, "an authorization is already open")
        tx = t.transmissions
        first = uow.conn.execute(select(tx).where(tx.c.operation_id == operation_id,
                                                  tx.c.attempt_no == row.attempt_count)
                                 .order_by(tx.c.transmission_no)).all()
        if not first:
            raise DomainError(ErrorCode.CONFLICT_STATE, "nothing was handed off in this attempt")
        if any(x.kind == "REDELIVER" for x in first) or uow.conn.execute(select(t.authorizations.c.id).where(and_(
                t.authorizations.c.operation_id == operation_id, t.authorizations.c.attempt_no == row.attempt_count,
                t.authorizations.c.kind == "REDELIVER"))).first() is not None:
            raise DomainError(ErrorCode.CONFLICT_STATE, "at most one redelivery per attempt")
        from lbf.workflow.permissions import OperationFacts

        facts = OperationFacts(state=S.UNKNOWN, created_by=row.created_by, version_authors=frozenset(),
                               first_handoff_at=first[0].started_at, message_id=first[0].message_id)
        if not redelivery_window_open(facts, caps, now):
            raise DomainError(ErrorCode.CONFLICT_STATE, "the deduplication window (minus margin) has closed")
        auth_id = repo.insert_authorization(uow.conn, operation_id=operation_id, kind=AuthorizationKind.REDELIVER,
                                            attempt_no=row.attempt_count, approval_id=None,
                                            granted_by=principal.user_id, now=now,
                                            expires_at=now + ctx.settings.authorization_ttl)
        uow.append_event(user_event(principal, "AUTHORIZATION_CREATED", operation_id=operation_id,
                                    correlation={"correlation_value": row.correlation_value,
                                                 "attempt_no": row.attempt_count, "authorization_id": str(auth_id),
                                                 "message_id": first[0].message_id},
                                    payload={"kind": "REDELIVER", "payload_sha256": first[0].payload_sha256}))
    return auth_id
