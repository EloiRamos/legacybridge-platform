"""Workflow integration tests — every transition (T01–T32 + T18b) exercised end to end on the
offline backends (HALDEN fake, frozen SKARVIK simulator in-process) against PostgreSQL. Oracles are the fake
backend states, never the application's own records."""

from __future__ import annotations

import time

import pytest
from workflow_support import ACCOUNT, WF

from lbf.domain.enums import EvidenceClass as E
from lbf.domain.enums import OperationState as S
from lbf.domain.enums import ResolutionKind
from lbf.domain.errors import DomainError, ErrorCode
from lbf.workflow import commands
from lbf.workflow.withdraw_unclaimed import withdraw_unclaimed

pytestmark = pytest.mark.db
BOTH = ["halden", "skarvik"]


@pytest.fixture
def wf(app_engine, tmp_path):
    return WF(app_engine, tmp_path)


def to_confirmed(wf: WF, op) -> None:
    wf.step()
    if wf.state(op)[0] == S.AWAITING_ACK:
        wf.settle_skarvik()
        wf.step()


# --------------------------------------------------------------------------- T01 T03 T05 T09 T11 / T15 T17


def test_t01_t03_t05_t09_t11_halden_happy_path_read_back(wf):
    op = wf.draft("halden")
    assert wf.state(op) == (S.DRAFT, None)
    assert wf.submit(op).blocking is False and wf.state(op)[0] == S.READY
    wf.approve(op)
    assert wf.state(op)[0] == S.APPROVED
    wf.step()
    assert wf.state(op) == (S.CONFIRMED, E.READ_BACK)
    assert wf.handoffs(op) == 1 and len(wf.backend_orders(op)) == 1
    ev = wf.events(op)
    for name in ("OPERATION_CREATED", "INTENT_NORMALIZED", "DRAFT_VERSION_CREATED", "VALIDATION_RECORDED",
                 "SUBMITTED_FOR_APPROVAL", "APPROVAL_GRANTED", "AUTHORIZATION_CREATED", "TRANSMISSION_STARTED",
                 "TRANSMISSION_OBSERVED", "RECONCILIATION_FINISHED", "OUTCOME_RECORDED"):
        assert name in ev, name
    assert ev.index("AUTHORIZATION_CREATED") < ev.index("TRANSMISSION_STARTED") < ev.index("TRANSMISSION_OBSERVED")


def test_t15_t17_skarvik_ack_then_evidence_upgraded_by_extract_read(wf):
    op = wf.approved("skarvik")
    wf.step()
    assert wf.state(op)[0] == S.AWAITING_ACK
    wf.settle_skarvik()
    wf.engines.ack_poller.run_once()
    assert wf.state(op) == (S.CONFIRMED, E.ACKNOWLEDGED_ONLY)
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.CONFIRMED, E.READ_BACK)
    assert "EVIDENCE_UPGRADED" in wf.events(op)
    assert wf.handoffs(op) == 1 and len(wf.backend_orders(op)) == 1


# --------------------------------------------------------------------------- revise T02 T04 T07 T32, T06, T08


def test_t02_t04_t06_t07_revisions_create_new_immutable_versions(wf):
    op = wf.draft("halden")
    r = commands.revise_draft(wf.ctx, wf.p("daniel"), op, 1, wf.request("halden", qty=11))  # T02
    assert (r.version, wf.state(op)[0]) == (2, S.DRAFT)
    wf.submit(op)
    commands.revise_draft(wf.ctx, wf.p("daniel"), op, 2, wf.request("halden", qty=12))  # T04 READY→DRAFT
    assert wf.row(op).current_version == 3 and wf.state(op)[0] == S.DRAFT
    wf.submit(op)
    row = wf.row(op)
    with pytest.raises(DomainError) as short:
        commands.decline(wf.ctx, wf.p("priya"), op, version=3, content_hash="0" * 64, reason="too short?")
    assert short.value.code in (ErrorCode.CONFLICT_VERSION, ErrorCode.VALIDATION)
    h = _hash(wf, op, row.current_version)
    commands.decline(wf.ctx, wf.p("priya"), op, version=3, content_hash=h, reason="wrong quantity for this site")
    assert wf.state(op)[0] == S.DECLINED  # T06
    commands.revise_draft(wf.ctx, wf.p("daniel"), op, 3, wf.request("halden", qty=5))  # T07
    assert wf.state(op)[0] == S.DRAFT and wf.row(op).current_version == 4
    with pytest.raises(DomainError) as stale:
        commands.revise_draft(wf.ctx, wf.p("daniel"), op, 3, wf.request("halden", qty=6))
    assert stale.value.code is ErrorCode.CONFLICT_VERSION


def _hash(wf, op, version):
    from sqlalchemy import select

    from lbf.persistence import tables as t

    with wf.engine.connect() as conn:
        return conn.execute(select(t.draft_versions.c.content_hash).where(
            t.draft_versions.c.operation_id == op, t.draft_versions.c.version == version)).scalar()


@pytest.mark.parametrize("state", ["DRAFT", "READY", "DECLINED", "NOT_APPLIED"])
def test_t08_withdraw_from_every_editable_state(wf, state):
    op = wf.draft("halden")
    if state in ("READY", "DECLINED"):
        wf.submit(op)
    if state == "DECLINED":
        commands.decline(wf.ctx, wf.p("priya"), op, version=1, content_hash=_hash(wf, op, 1),
                         reason="customer cancelled by phone")
    if state == "NOT_APPLIED":
        wf.submit(op)
        wf.approve(op)
        wf.halden.next_e8_modes.append("refuse_440")
        wf.step()
    assert wf.state(op)[0] == state
    with pytest.raises(DomainError) as forbidden:
        commands.withdraw(wf.ctx, wf.p("priya"), op)  # not the creator, no grant.any
    assert forbidden.value.code is ErrorCode.FORBIDDEN
    commands.withdraw(wf.ctx, wf.p("daniel"), op)
    assert wf.state(op)[0] == S.WITHDRAWN


def test_t32_t31_not_applied_then_new_attempt(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("refuse_440")
    wf.step()
    assert wf.state(op) == (S.NOT_APPLIED, E.PRE_PROCESSING_REFUSAL)  # T13
    assert wf.submit(op).blocking is False and wf.state(op)[0] == S.READY  # T31, same version
    second = wf.approve(op)
    assert second.attempt_no == 2
    wf.step()
    assert wf.state(op) == (S.CONFIRMED, E.READ_BACK)
    assert wf.handoffs(op) == 2 and len(wf.backend_orders(op)) == 1  # refused E8 changed nothing (I1)
    op2 = wf.approved("halden", qty=3)
    wf.halden.next_e8_modes.append("not_delivered")
    wf.step()
    commands.revise_draft(wf.ctx, wf.p("daniel"), op2, 1, wf.request("halden", qty=4))  # T32
    assert wf.state(op2)[0] == S.DRAFT


# --------------------------------------------------------------------------- T10 T12 T13


def test_t10_preflight_failure_is_not_sent_and_hands_off_nothing(wf):
    op = wf.approved("halden")
    wf.halden.down = True
    wf.step()
    assert wf.state(op) == (S.NOT_APPLIED, E.NOT_SENT)
    assert wf.transmissions(op) == [] and wf.halden.e8_received == 0


@pytest.mark.parametrize("backend", BOTH)
def test_t10_authorization_expiry_without_transmission(wf, backend):
    op = wf.approved(backend)
    wf.clock.advance(16 * 60)
    wf.engines.sweeper.run_once()
    assert wf.state(op) == (S.NOT_APPLIED, E.APPROVAL_EXPIRED)
    wf.step()
    assert wf.transmissions(op) == [] and wf.handoffs(op) == 0


def test_t12_halden_business_rejection(wf):
    op = wf.approved("halden", sku="BW-1004", qty=15)  # 19 free at validation time
    for row in wf.halden.stock:  # stock allocated elsewhere after approval (v1 LV row 5; the fake keeps stock rows)
        if row[0] == "BW-1004":
            row[3] = row[2]
    wf.step()
    assert wf.state(op) == (S.REJECTED, E.BACKEND_REJECTION)
    assert wf.backend_orders(op) == []


@pytest.mark.parametrize(("mode", "evidence"), [("not_delivered", E.NOT_SENT),
                                                ("refuse_440", E.PRE_PROCESSING_REFUSAL)])
def test_t13_halden_not_delivered_or_refused(wf, mode, evidence):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append(mode)
    wf.step()
    assert wf.state(op) == (S.NOT_APPLIED, evidence) and wf.backend_orders(op) == []


def test_t13_skarvik_unpublished_message_is_never_delivered(wf):
    op = wf.approved("skarvik")
    wf.hook = lambda name: (_ for _ in ()).throw(RuntimeError("broken")) if name == "before_handoff" else None
    wf.step()
    wf.hook = None
    assert wf.state(op) == (S.NOT_APPLIED, E.NEVER_DELIVERED)
    wf.settle_skarvik()
    assert wf.handoffs(op) == 0


# --------------------------------------------------------------------------- T14 T16 T18 T18b T19


def test_t14_uncertain_without_order_is_unknown_and_never_resent(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step(3)
    assert wf.state(op) == (S.UNKNOWN, E.NO_MATCH_AFTER_UNCERTAIN)
    assert len(wf.transmissions(op)) == 1 and wf.halden_client.e8_request_count == 1


def test_t11_uncertain_with_committed_order_reconciles_immediately(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("commit_then_uncertain")
    wf.step()
    assert wf.state(op) == (S.CONFIRMED, E.RECONCILED) and wf.handoffs(op) == 1


def test_t16_ambiguous_correlation_escalates(wf):
    op = wf.approved("halden")
    cv = wf.row(op).correlation_value
    wf.halden.book(ACCOUNT["halden"], cv, [("BW-1001", 10)])  # planted outside the application (R10)
    wf.halden.next_e8_modes.append("commit_then_uncertain")
    wf.step()
    assert wf.state(op) == (S.ESCALATED, E.AMBIGUOUS_MATCH)
    assert wf.halden_client.e8_request_count == 1


def test_t18_rejected_ack_is_final_only_after_a_confirming_extract(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["truncate_on_pickup"] = 1
    wf.step()
    wf.skarvik.pickup()
    wf.skarvik.flush_acks()
    wf.engines.ack_poller.run_once()
    wf.engines.reconciler.run_once()
    assert wf.state(op)[0] == S.AWAITING_ACK  # REJECTED ack recorded; no fresh extract yet
    wf.clock.advance(7)
    wf.skarvik.extracts()
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.REJECTED, E.BACKEND_REJECTION) and wf.backend_orders(op) == []


def test_t18b_rejected_ack_contradicted_by_extract_escalates(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["contradict_ack"] = 1
    wf.step()
    wf.settle_skarvik()
    wf.engines.ack_poller.run_once()
    wf.clock.advance(7)
    wf.skarvik.extracts()
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.ESCALATED, E.CONTRADICTION) and len(wf.backend_orders(op)) == 1


def test_t19_ack_overdue(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["drop_ack"] = 1
    wf.step()
    wf.settle_skarvik()
    wf.step()
    assert wf.state(op)[0] == S.AWAITING_ACK
    wf.clock.advance(21)
    wf.engines.reconciler.ack_overdue()
    assert wf.state(op) == (S.UNKNOWN, E.ACK_OVERDUE)


# --------------------------------------------------------------------------- T20 T21 T22 T23 T24 T25 T26


def test_t20_withdraw_unclaimed_before_pickup(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.pause_pickup = True
    wf.step()
    assert wf.state(op)[0] == S.AWAITING_ACK
    with pytest.raises(DomainError) as forbidden:
        withdraw_unclaimed(wf.ctx, wf.p("daniel"), op)
    assert forbidden.value.code is ErrorCode.FORBIDDEN
    with pytest.raises(DomainError) as reauth:
        withdraw_unclaimed(wf.ctx, wf.p("marta", fresh=False), op)
    assert reauth.value.code is ErrorCode.REAUTH_REQUIRED
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert out.withdrawn and wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)
    wf.skarvik.faults.pause_pickup = False
    wf.settle_skarvik()
    assert wf.skarvik.received() == [] and wf.backend_orders(op) == []


def test_t20_withdraw_after_pickup_proves_nothing(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["drop_ack"] = 1
    wf.step()
    wf.settle_skarvik()
    wf.clock.advance(21)
    wf.step()
    assert wf.state(op)[0] == S.UNKNOWN
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert out.withdrawn is False and wf.state(op)[0] == S.UNKNOWN


def test_t21_t22_recheck_confirms_after_read_failures(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("commit_then_uncertain")
    wf.hook = lambda name: setattr(wf.halden, "read_failures", 1000) if name == "after_handoff_before_observe" else None
    wf.step()
    wf.hook = None
    assert wf.state(op) == (S.UNKNOWN, E.RECONCILIATION_FAILED)
    wf.halden.read_failures = 0
    wf.clock.advance(5)
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.CONFIRMED, E.RECONCILED)
    ev = wf.events(op)
    assert "RECONCILIATION_STARTED" in ev and wf.handoffs(op) == 1


def test_t23_t24_schedule_then_recheck_limit_escalates(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    for n, offset in enumerate((5, 15, 60, 300), start=1):
        wf.clock.now = wf.row(op).unknown_since
        wf.clock.advance(offset)
        wf.engines.reconciler.due_rechecks()
        assert wf.state(op)[0] == S.UNKNOWN and wf.row(op).auto_rechecks == n  # T23
    wf.clock.now = wf.row(op).unknown_since
    wf.clock.advance(900)
    wf.engines.reconciler.due_rechecks()
    assert wf.state(op) == (S.ESCALATED, E.RECHECK_LIMIT)  # T24
    assert wf.halden_client.e8_request_count == 1


def test_t25_interrupted_skarvik_message_proven_never_delivered(wf):
    from workflow_support import S as _S  # noqa: F401

    op = wf.approved("skarvik")

    class Kill(BaseException):
        pass

    def kill(name):
        if name == "before_handoff":
            raise Kill

    wf.hook = kill
    with pytest.raises(Kill):
        wf.engines.executor.run_once()
    wf.hook = None
    assert wf.state(op)[0] == S.SUBMITTING and wf.transmissions(op)[0].state == "SENDING"
    wf.clock.advance(60)
    wf.engines.sweeper.run_once()
    assert wf.state(op) == (S.UNKNOWN, E.INTERRUPTED)  # G4: never NOT_APPLIED directly
    wf.settle_skarvik()
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.NOT_APPLIED, E.NEVER_DELIVERED)  # T25
    assert wf.handoffs(op) == 0


def test_t26_late_rejected_ack_after_ack_overdue(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["truncate_on_pickup"] = 1
    # the frozen simulator delays acks on its own monotonic (real) clock, so the delay is short and real
    wf.skarvik.faults.ack_delay_seconds, wf.skarvik.faults.ack_delay_count = 0.3, 1
    wf.step()
    wf.skarvik.pickup()
    wf.clock.advance(21)
    wf.engines.reconciler.ack_overdue()
    assert wf.state(op) == (S.UNKNOWN, E.ACK_OVERDUE)
    time.sleep(0.4)
    assert wf.skarvik.flush_acks() == 1
    wf.engines.ack_poller.run_once()
    wf.clock.advance(7)
    wf.skarvik.extracts()
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.REJECTED, E.BACKEND_REJECTION) and wf.backend_orders(op) == []


# --------------------------------------------------------------------------- T27 redelivery


def test_t27_redelivery_within_window_is_deduplicated(wf):
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["drop_ack"] = 1
    wf.step()
    wf.settle_skarvik()
    wf.clock.advance(21)
    wf.step()
    assert wf.state(op)[0] == S.UNKNOWN
    with pytest.raises(DomainError) as sod:
        commands.authorize_redelivery(wf.ctx, wf.p("daniel"), op)
    assert sod.value.code is ErrorCode.FORBIDDEN
    commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    wf.engines.executor.run_once()
    assert wf.state(op)[0] == S.AWAITING_ACK
    xs = wf.transmissions(op)
    assert [x.kind for x in xs] == ["SUBMIT", "REDELIVER"] and xs[0].payload == xs[1].payload
    wf.settle_skarvik()
    wf.engines.ack_poller.run_once()
    assert wf.state(op) == (S.CONFIRMED, E.ACKNOWLEDGED_ONLY)
    wf.engines.reconciler.run_once()
    assert wf.state(op) == (S.CONFIRMED, E.DEDUP_CONFIRMED)
    assert len(wf.backend_orders(op)) == 1 and wf.handoffs(op) == 2


def test_t27_second_redelivery_and_unsupported_backend_refused(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    with pytest.raises(DomainError) as unsupported:
        commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    assert unsupported.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    sk = wf.approved("skarvik")
    wf.skarvik.faults.counted["drop_ack"] = 1
    wf.step()
    wf.settle_skarvik()
    wf.clock.advance(21)
    wf.step()
    commands.authorize_redelivery(wf.ctx, wf.p("marta"), sk)
    with pytest.raises(DomainError) as open_auth:
        commands.authorize_redelivery(wf.ctx, wf.p("marta"), sk)
    assert open_auth.value.code is ErrorCode.CONFLICT_STATE
    wf.skarvik.faults.pause_pickup = True  # the redelivered message is not picked up: no ack again
    wf.engines.executor.run_once()
    wf.clock.advance(21)
    wf.engines.reconciler.ack_overdue()
    assert wf.state(sk)[0] == S.UNKNOWN
    with pytest.raises(DomainError) as twice:
        commands.authorize_redelivery(wf.ctx, wf.p("marta"), sk)
    assert twice.value.code is ErrorCode.CONFLICT_STATE and "one redelivery" in twice.value.message


# --------------------------------------------------------------------------- T28 T29 T30


def test_t28_manual_and_age_escalation(wf):
    op = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    commands.escalate(wf.ctx, wf.p("rafael"), op)
    assert wf.state(op) == (S.ESCALATED, E.MANUAL)
    op2 = wf.approved("halden", sku="BW-1004", qty=2)  # same item would hit the I5 overlap guard (op is ESCALATED)
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    wf.clock.now = wf.row(op2).unknown_since
    wf.clock.advance(1801)
    wf.engines.reconciler.age_escalations()
    assert wf.state(op2) == (S.ESCALATED, E.RECHECK_LIMIT)


def test_t29_verified_resolution_requires_a_matching_read_and_sod5(wf):
    op = wf.approved("halden")  # approved by priya
    wf.halden.next_e8_modes.append("commit_then_uncertain")
    wf.hook = lambda name: setattr(wf.halden, "read_failures", 1000) if name == "after_handoff_before_observe" else None
    wf.step()
    wf.hook = None
    wf.halden.read_failures = 0
    commands.escalate(wf.ctx, wf.p("marta"), op)
    [order] = wf.backend_orders(op)
    with pytest.raises(DomainError) as wrong:
        commands.resolve(wf.ctx, wf.p("marta"), op, kind=ResolutionKind.VERIFIED, external_reference="SO-240101")
    assert wrong.value.code is ErrorCode.VALIDATION and wf.state(op)[0] == S.ESCALATED
    assert commands.resolve(wf.ctx, wf.p("marta"), op, kind=ResolutionKind.VERIFIED,
                            external_reference=order) is S.CONFIRMED
    assert wf.state(op) == (S.CONFIRMED, E.MANUAL_VERIFIED)
    op2 = wf.approved("halden", qty=2)
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    commands.escalate(wf.ctx, wf.p("marta"), op2)
    op2_approver = wf.p("marta")
    with pytest.raises(DomainError) as sod5:
        commands.resolve(wf.ctx, wf.p("priya"), op2, kind=ResolutionKind.ATTESTED_NOT_BOOKED,
                         external_reference="CALL-123", note="phoned the warehouse, nothing booked")
    assert sod5.value.code is ErrorCode.FORBIDDEN  # priya lacks escalation.resolve
    assert op2_approver is not None


def test_t30_attestation_is_closed_not_verified(wf):
    op = wf.approved("halden", qty=2)
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    commands.escalate(wf.ctx, wf.p("rafael"), op)
    with pytest.raises(DomainError) as short:
        commands.resolve(wf.ctx, wf.p("marta"), op, kind=ResolutionKind.ATTESTED_NOT_BOOKED,
                         external_reference="TICKET-7", note="too short")
    assert short.value.code is ErrorCode.VALIDATION
    commands.resolve(wf.ctx, wf.p("marta"), op, kind=ResolutionKind.ATTESTED_NOT_BOOKED,
                     external_reference="TICKET-7", note="HALDEN desk confirmed by phone: no order exists")
    assert wf.state(op) == (S.CLOSED_ATTESTED, E.ATTESTED_NOT_BOOKED)
