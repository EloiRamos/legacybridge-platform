"""Executor engine: T-CLAIM → preflight (reads) → T-BEGIN → ONE handoff → T-OBSERVE → phase 2.

Guarantees implemented here and enforced underneath by the database:
* G1 — the transmission row (``UNIQUE(authorization_id)``) commits **before** the handoff; at most one handoff per
  authorization.
* G2 — no code path calls ``handoff``/``redeliver`` a second time for an authorization; failures of any kind after
  T-BEGIN become observations or, after a crash, ``INTERRUPTED`` → ``UNKNOWN`` (never a resend).
This module and ``lbf.workflow.withdraw_unclaimed`` are the only callers of backend write methods (B-6).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import Row, select, update

from lbf.domain.correlation import message_id as make_message_id
from lbf.domain.enums import (
    AuthorizationKind,
    AuthorizationState,
    EvidenceClass,
    OperationState,
    ReconciliationTrigger,
)
from lbf.domain.enums import TransportObservationKind as K
from lbf.domain.errors import DomainError
from lbf.domain.models import RenderedRequest, SubmissionLine, SubmissionRequest, TransportObservation
from lbf.engines.common import approved_version, attempt_transmissions, run_phase2, schedule_now
from lbf.engines.pause import PausePoints
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import classify, metrics
from lbf.workflow.classify import Decision
from lbf.workflow.context import WorkflowContext
from lbf.workflow.records import apply_decision, system_event

S = OperationState
CLAIMABLE = (AuthorizationKind.SUBMIT.value, AuthorizationKind.REDELIVER.value)


class Executor:
    def __init__(self, ctx: WorkflowContext, engine_id: str, pauses: PausePoints | None = None) -> None:
        self.ctx = ctx
        self.engine_id = engine_id
        self.pauses = pauses or PausePoints()
        for port in ctx.backends.values():  # engine pause point ``before_handoff`` lives inside the adapters
            if hasattr(port, "before_publish"):
                port.before_publish = lambda _rendered: self.pauses.reach("before_handoff")

    # ------------------------------------------------------------------ T-CLAIM
    def claim(self) -> Row[Any] | None:
        a = t.authorizations
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            row = uow.conn.execute(
                select(a).where(a.c.state == AuthorizationState.PENDING.value, a.c.kind.in_(CLAIMABLE),
                                a.c.expires_at > now)
                .order_by(a.c.granted_at).limit(1).with_for_update(skip_locked=True)).first()
            if row is None:
                return None
            return uow.conn.execute(update(a).where(a.c.id == row.id, a.c.state == AuthorizationState.PENDING.value)
                                    .values(state=AuthorizationState.CLAIMED.value, claimed_by=self.engine_id,
                                            lease_until=now + self.ctx.settings.claim_lease)
                                    .returning(*a.c)).first()

    def run_once(self) -> bool:
        auth = self.claim()
        if auth is None:
            return False
        self.pauses.reach("after_claim")
        self.process(auth)
        return True

    # ------------------------------------------------------------------ request reconstruction
    def _request(self, auth: Row[Any]) -> tuple[SubmissionRequest, RenderedRequest | None, str]:
        with self.ctx.engine.connect() as conn:
            op = conn.execute(select(t.operations).where(t.operations.c.id == auth.operation_id)).one()
            version = approved_version(conn, op.id, auth.attempt_no)
            previous = attempt_transmissions(conn, op.id, auth.attempt_no)
        caps = self.ctx.caps(op.backend_id)
        c = version.content
        request = SubmissionRequest(
            operation_id=op.id, attempt_no=auth.attempt_no, transmission_no=len(previous) + 1,
            authorization_id=auth.id, backend_id=op.backend_id, native_customer_id=op.native_customer_id,
            lines=[SubmissionLine(native_item=ln.native_item, quantity_native=ln.quantity_native) for ln in c.lines],
            correlation_value=op.correlation_value,
            message_id=make_message_id(op.correlation_value, auth.attempt_no) if caps.is_async else None,
            customer_po=c.customer_po, requested_date=c.requested_date, delivery_note=c.delivery_note)
        stored = None
        if auth.kind == AuthorizationKind.REDELIVER.value:
            original = next((x for x in previous if x.kind == AuthorizationKind.SUBMIT.value), None)
            if original is None:
                raise RuntimeError("redelivery without an original transmission")
            # the STORED original bytes, never re-rendered
            stored = RenderedRequest(request=request.model_copy(update={"message_id": original.message_id}),
                                     payload=bytes(original.payload), payload_sha256=original.payload_sha256,
                                     transport_descriptor=original.transport_descriptor)
        return request, stored, version.content_hash

    # ------------------------------------------------------------------ the attempt
    def process(self, auth: Row[Any]) -> None:
        request, stored, content_hash = self._request(auth)
        port = self.ctx.port(request.backend_id)
        if auth.kind == AuthorizationKind.SUBMIT.value:
            try:
                pre = port.preflight(request)
                ok, detail = pre.ok, pre.detail
            except Exception as exc:
                ok, detail = False, f"preflight failed ({exc.__class__.__name__})"
            if not ok:
                self._not_sent(auth, f"pre-send preparation failed: {detail}; no request was sent")
                return
        try:
            rendered = stored if stored is not None else port.render(request)
        except DomainError as exc:
            self._not_sent(auth, f"render refused before any transmission: {exc.message}")
            return
        transmission_id = self._begin(auth, request, rendered, content_hash)
        if transmission_id is None:
            return
        # ---- exactly ONE handoff; no transaction is open ---------------------------------------------------
        try:
            if auth.kind == AuthorizationKind.REDELIVER.value:
                observation = port.redeliver(rendered)
            else:
                observation = port.handoff(rendered)
        except Exception as exc:
            observation = TransportObservation(kind=K.UNCERTAIN, observed_at=self.ctx.clock(),
                                               detail=f"adapter exception ({exc.__class__.__name__})")
        metrics.TRANSMISSIONS.labels(auth.kind, observation.kind.value).inc()
        self.pauses.reach("after_handoff_before_observe")
        if self._observe_with_retry(transmission_id, observation):
            run_phase2(self.ctx, self.engine_id, request.operation_id)

    def _not_sent(self, auth: Row[Any], detail: str) -> None:
        """T10: preflight failed — no transmission row exists, so no handoff happened (positive evidence)."""
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            op = uow.lock_operation(auth.operation_id)
            a = uow.conn.execute(select(t.authorizations).where(t.authorizations.c.id == auth.id)
                                 .with_for_update()).one()
            if a.state != AuthorizationState.CLAIMED.value or a.claimed_by != self.engine_id:
                return
            uow.conn.execute(update(t.authorizations).where(t.authorizations.c.id == auth.id)
                             .values(state=AuthorizationState.EXPIRED.value))
            corr = {"attempt_no": auth.attempt_no, "authorization_id": str(auth.id)}
            uow.append_event(system_event(self.engine_id, "AUTHORIZATION_EXPIRED", operation_id=op.id,
                                          correlation={"correlation_value": op.correlation_value, **corr},
                                          payload={"reason": "PREFLIGHT_FAILED", "detail": detail[:500]}))
            if op.state == S.APPROVED.value:
                apply_decision(uow, op, Decision(S.NOT_APPLIED, "T10", EvidenceClass.NOT_SENT, detail=detail),
                               system_event(self.engine_id, "OUTCOME_RECORDED", correlation=corr),
                               self.ctx.settings, now=uow.clock())
                metrics.TRANSITIONS.labels(S.NOT_APPLIED.value).inc()

    # ------------------------------------------------------------------ T-BEGIN
    def _begin(self, auth: Row[Any], request: SubmissionRequest, rendered: RenderedRequest,
               content_hash: str) -> uuid.UUID | None:
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            op = uow.lock_operation(auth.operation_id)
            a = uow.conn.execute(select(t.authorizations).where(t.authorizations.c.id == auth.id)
                                 .with_for_update()).one()
            if (a.state != AuthorizationState.CLAIMED.value or a.claimed_by != self.engine_id
                    or a.lease_until is None or a.lease_until <= now or a.expires_at <= now):
                return None  # lost the claim (lease or expiry): the sweeper decides; nothing was sent
            redelivery = a.kind == AuthorizationKind.REDELIVER.value
            expected = S.UNKNOWN if redelivery else S.APPROVED
            if op.state != expected.value:
                uow.conn.execute(update(t.authorizations).where(t.authorizations.c.id == a.id)
                                 .values(state=AuthorizationState.EXPIRED.value))
                uow.append_event(system_event(self.engine_id, "AUTHORIZATION_EXPIRED", operation_id=op.id,
                                              correlation={"correlation_value": op.correlation_value,
                                                           "authorization_id": str(a.id)},
                                              payload={"reason": f"OPERATION_STATE_{op.state}"}))
                return None
            transmission_id = uuid.uuid4()
            uow.conn.execute(t.transmissions.insert().values(
                id=transmission_id, authorization_id=a.id, operation_id=op.id, attempt_no=request.attempt_no,
                transmission_no=request.transmission_no, kind=a.kind, message_id=rendered.request.message_id,
                payload=rendered.payload, payload_sha256=rendered.payload_sha256,
                transport_descriptor=rendered.transport_descriptor, state="SENDING", engine_id=self.engine_id,
                started_at=now, lease_until=now + self.ctx.settings.transmission_lease()))
            uow.conn.execute(update(t.authorizations).where(t.authorizations.c.id == a.id)
                             .values(state=AuthorizationState.CONSUMED.value, consumed_at=now))
            event = system_event(self.engine_id, "TRANSMISSION_STARTED", correlation={
                "attempt_no": request.attempt_no, "transmission_no": request.transmission_no,
                "authorization_id": str(a.id), "approval_id": str(a.approval_id) if a.approval_id else None,
                "transmission_id": str(transmission_id), "message_id": rendered.request.message_id,
                "content_hash": content_hash},
                payload={"kind": a.kind, "payload_sha256": rendered.payload_sha256, "content_hash": content_hash,
                         "transport_descriptor": rendered.transport_descriptor})
            uow.transition(op, S.SUBMITTING, "T27" if redelivery else "T09", event, phase2_pending=False)
        return transmission_id

    # ------------------------------------------------------------------ T-OBSERVE
    def _observe_with_retry(self, transmission_id: uuid.UUID, obs: TransportObservation) -> bool:
        delay = 0.2
        deadline = time.monotonic() + self.ctx.settings.transmission_lease().total_seconds()
        while True:
            try:
                return self.observe(transmission_id, obs)
            except DomainError:
                raise
            except Exception:  # database unavailable: retry within the lease; never resend
                if time.monotonic() + delay > deadline:
                    return False
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    def observe(self, transmission_id: uuid.UUID, obs: TransportObservation) -> bool:
        """Returns True when phase 2 (read-only) must run."""
        x = t.transmissions
        with UnitOfWork(self.ctx.engine, clock=self.ctx.clock) as uow:
            now = uow.clock()
            tx_row = uow.conn.execute(select(x).where(x.c.id == transmission_id)).one()
            op = uow.lock_operation(tx_row.operation_id)
            updated = uow.conn.execute(
                update(x).where(x.c.id == transmission_id, x.c.state == "SENDING")
                .values(state="OBSERVED", observed_at=now, observation=repo.jsonable(obs.model_dump(mode="python")),
                        response_sha256=obs.response_sha256, response_excerpt=obs.response_excerpt)
                .returning(x.c.id)).first()
            corr = {"correlation_value": op.correlation_value, "attempt_no": tx_row.attempt_no,
                    "transmission_no": tx_row.transmission_no, "transmission_id": str(transmission_id),
                    "message_id": tx_row.message_id}
            obs_payload = {"observation": obs.kind.value, "http_status": obs.http_status,
                           "backend_code": obs.backend_code, "backend_order_ref": obs.backend_order_ref,
                           "response_sha256": obs.response_sha256, "excerpt": obs.response_excerpt,
                           "payload_sha256_on_wire": obs.payload_sha256_on_wire, "detail": obs.detail[:500]}
            if updated is None:  # the sweeper already marked it INTERRUPTED: record, never overwrite (C-09)
                uow.append_event(system_event(self.engine_id, "LATE_OBSERVATION", operation_id=op.id,
                                              correlation=corr, payload=obs_payload))
                if op.state in (S.UNKNOWN.value, S.ESCALATED.value):
                    schedule_now(uow, op, ReconciliationTrigger.RECOVERY)
                return False
            uow.append_event(system_event(self.engine_id, "TRANSMISSION_OBSERVED", operation_id=op.id,
                                          correlation=dict(corr), payload=obs_payload))
            if obs.payload_sha256_on_wire and obs.payload_sha256_on_wire != tx_row.payload_sha256:
                uow.append_event(system_event(self.engine_id, "INTEGRITY_ALERT", operation_id=op.id,
                                              correlation=dict(corr),
                                              payload={"reason": "bytes handed to the transport differ from the "
                                                       "stored payload", "stored": tx_row.payload_sha256,
                                                       "on_wire": obs.payload_sha256_on_wire}))
            p1 = classify.phase1(obs, self.ctx.caps(op.backend_id))
            if p1.decision is not None:
                event = system_event(self.engine_id, "OUTCOME_RECORDED" if p1.decision.evidence else
                                     "AWAITING_ACKNOWLEDGEMENT", correlation=dict(corr),
                                     payload={"observation": obs.kind.value})
                apply_decision(uow, op, p1.decision, event, self.ctx.settings, now=now)
                metrics.TRANSITIONS.labels(p1.decision.target.value).inc()
                return False
            uow.conn.execute(update(t.operations).where(t.operations.c.id == op.id).values(
                phase2_pending=True, anchor_order_ref=p1.anchor or op.anchor_order_ref,
                row_version=op.row_version + 1, updated_at=now))
            return True


def new_engine_id() -> str:
    return f"engine-{uuid.uuid4().hex[:12]}"
