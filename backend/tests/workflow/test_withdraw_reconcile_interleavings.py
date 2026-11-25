"""Deterministic regressions for withdrawal (withdrawal evidence lost to a re-check interleaving), commands
(issued during a transient RECONCILING) and the REDELIVER-expiry edge case (R1–R10).

Every interleaving is forced, never timed: the reconciler is parked at the shipped test-only pause point
``mid_reconcile`` on a helper thread (``HeldRecheck``), or invoked synchronously from inside the withdraw handler's
file-operation gap (``in_withdraw_gap``). The manual clock, the in-process SKARVIK simulator and the stepped engines
make every outcome reproducible.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from workflow_support import WF

from lbf.domain.enums import EvidenceClass as E
from lbf.domain.enums import OperationState as S
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.persistence.audit import verify_chain
from lbf.workflow import commands
from lbf.workflow.withdraw_unclaimed import withdraw_unclaimed

pytestmark = pytest.mark.db


@pytest.fixture
def wf(app_engine, tmp_path):
    return WF(app_engine, tmp_path)


# --------------------------------------------------------------------------- deterministic interleaving tools


class HeldRecheck:
    """Runs ``Reconciler.recheck(op)`` on a thread and parks it at ``mid_reconcile`` (state RECONCILING, no
    transaction open) until ``finish()``. If the reconciler declines to start, the thread ends without parking."""

    def __init__(self, wf: WF, op: Any) -> None:
        self.wf, self.op = wf, op
        self.reached, self.release = threading.Event(), threading.Event()
        self.result: list[bool] = []
        self.thread = threading.Thread(target=lambda: self.result.append(wf.engines.reconciler.recheck(op)),
                                       name="held-recheck", daemon=True)
        wf.hook = self._hook

    def _hook(self, name: str) -> None:
        if name == "mid_reconcile" and threading.current_thread() is self.thread:
            self.reached.set()
            assert self.release.wait(20), "held re-check was never released"

    def start(self) -> HeldRecheck:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.reached.is_set() and self.thread.is_alive():
            assert time.monotonic() < deadline, "re-check neither parked nor finished"
            time.sleep(0.005)
        return self

    @property
    def parked(self) -> bool:
        return self.reached.is_set() and self.thread.is_alive()

    def finish(self) -> bool | None:
        self.release.set()
        self.thread.join(20)
        assert not self.thread.is_alive()
        self.wf.hook = None
        return self.result[0] if self.result else None


def in_withdraw_gap(wf: WF, *, before: Callable[[], Any] | None = None, after: Callable[[], Any] | None = None,
                    replace: Callable[[Callable[[str], Any], str], Any] | None = None) -> None:
    """Instrument the SKARVIK port so that ``before``/``after`` run inside the withdraw handler's gap: after tx 1
    (CLAIMED authorization committed) and before tx 2, around the real ``inbox → withdrawn`` rename."""
    port = wf.ports["skarvik"]
    original = port.withdraw_unclaimed

    def wrapped(message_id: str) -> Any:
        if before is not None:
            before()
        result = replace(original, message_id) if replace is not None else original(message_id)
        if after is not None:
            after()
        return result

    port.withdraw_unclaimed = wrapped


def unknown_unclaimed(wf: WF) -> Any:
    """SKARVIK order handed off, never picked up (pickup paused), no ack ⇒ UNKNOWN/ACK_OVERDUE, file in the inbox."""
    op = wf.approved("skarvik")
    wf.skarvik.faults.pause_pickup = True
    wf.step()
    assert wf.state(op)[0] == S.AWAITING_ACK
    wf.clock.advance(21)
    wf.engines.reconciler.ack_overdue()
    assert wf.state(op) == (S.UNKNOWN, E.ACK_OVERDUE)
    assert inbox_files(wf) == [f"ORD_{msgid(wf, op)}.csv"]
    return op


def unknown_picked_up(wf: WF) -> Any:
    """SKARVIK order picked up and booked, acknowledgement lost ⇒ UNKNOWN/ACK_OVERDUE, inbox empty."""
    op = wf.approved("skarvik")
    wf.skarvik.faults.counted["drop_ack"] = 1
    wf.step()
    wf.settle_skarvik()
    wf.clock.advance(21)
    wf.step()
    assert wf.state(op)[0] == S.UNKNOWN and inbox_files(wf) == []
    return op


def msgid(wf: WF, op: Any) -> str:
    return wf.transmissions(op)[0].message_id


def inbox_files(wf: WF) -> list[str]:
    return sorted(p.name for p in wf.skarvik.inbox.glob("ORD_*.csv"))


def withdrawn_files(wf: WF) -> list[str]:
    return sorted(p.name for p in wf.skarvik.withdrawn.glob("ORD_*.csv"))


def authorizations(wf: WF, op: Any) -> list[Any]:
    with wf.engine.connect() as conn:
        return list(conn.execute(select(t.authorizations).where(t.authorizations.c.operation_id == op)
                                 .order_by(t.authorizations.c.granted_at)).all())


def audit(wf: WF, op: Any) -> list[Any]:
    with wf.engine.connect() as conn:
        return list(conn.execute(select(t.audit_events).where(t.audit_events.c.operation_id == op)
                                 .order_by(t.audit_events.c.seq)).all())


def states_seen(wf: WF, op: Any) -> list[str]:
    return [e.state_change["to"] for e in audit(wf, op) if e.state_change]


def chain_valid(engines: dict[str, Any]) -> bool:
    with engines["audit_reader"].connect() as conn:
        return verify_chain(conn).valid


def due_now(wf: WF, op: Any) -> None:
    """Advance the manual clock until the operation's next automatic re-check is due."""
    due = wf.row(op).next_recheck_at
    assert due is not None
    if due > wf.clock():
        wf.clock.now = due


# --------------------------------------------------------------------------- R1 — withdraw before re-check


def test_r1_withdraw_completes_before_the_reconciler_claims_the_operation(wf, engines):
    op = unknown_unclaimed(wf)
    due_now(wf, op)  # a re-check is due, but the withdrawal is first
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert (out.withdrawn, out.state) == (True, S.NOT_APPLIED)
    assert wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)
    assert wf.engines.reconciler.run_once() == 0  # nothing left for the reconciler: NOT_APPLIED is final
    assert wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)
    assert withdrawn_files(wf) == [f"ORD_{msgid(wf, op)}.csv"] and inbox_files(wf) == []
    wf.skarvik.faults.pause_pickup = False
    wf.settle_skarvik()
    assert wf.skarvik.received() == [] and wf.backend_orders(op) == [] and wf.handoffs(op) == 0
    assert chain_valid(engines)


# --------------------------------------------------------------------------- R2 — re-check inside the gap (D-G2-01)


@pytest.mark.parametrize("where", ["before_rename", "after_rename"])
def test_r2_recheck_attempted_between_withdraw_transactions_keeps_positive_evidence(wf, engines, where):
    """A contested order of events, forced: the reconciler tries to enter RECONCILING after tx 1 committed."""
    op = unknown_unclaimed(wf)
    due_now(wf, op)
    held: list[HeldRecheck] = []

    def reconciler_attempts() -> None:
        held.append(HeldRecheck(wf, op).start())
        assert wf.engines.reconciler.due_rechecks() == 0  # the engine's own sweep is deferred as well

    in_withdraw_gap(wf, **{"before" if where == "before_rename" else "after": reconciler_attempts})
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    parked = held[0].parked
    started = held[0].finish()
    assert parked is False and started is False  # the reconciler did not take the row: an authorization is open
    assert (out.withdrawn, out.state) == (True, S.NOT_APPLIED)
    assert wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)
    assert S.RECONCILING.value not in states_seen(wf, op)
    assert wf.row(op).next_recheck_at is None  # the deferred re-check is retired by the final state, not run
    assert wf.engines.reconciler.run_once() == 0
    assert wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)
    assert chain_valid(engines)


def test_r2_age_escalation_inside_the_gap_is_deferred_not_lost(wf):
    """The INSPECTED sibling of D-G2-01: T28 (UNKNOWN for 30 min ⇒ ESCALATED) lands between tx 1 and tx 2."""
    op = unknown_unclaimed(wf)
    wf.clock.now = wf.ctx.settings.unknown_escalation_at(wf.row(op).unknown_since) - timedelta(seconds=1)
    escalations: list[int] = []

    def age_passes() -> None:
        wf.clock.advance(2)  # now past the escalation age, well inside the 2-minute withdraw lease
        escalations.append(wf.engines.reconciler.age_escalations())

    in_withdraw_gap(wf, before=age_passes)
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert escalations == [0]
    assert (out.withdrawn, out.state) == (True, S.NOT_APPLIED)
    assert wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)
    assert S.ESCALATED.value not in states_seen(wf, op)
    assert wf.engines.reconciler.age_escalations() == 0  # NOT_APPLIED is not UNKNOWN any more


def test_r2_deferral_ends_with_the_authorization_and_the_schedule_is_kept(wf):
    """After a withdrawal that proves nothing, the deferred re-check runs (no re-check is lost)."""
    op = unknown_picked_up(wf)
    due_now(wf, op)
    seen: list[int] = []
    in_withdraw_gap(wf, before=lambda: seen.append(wf.engines.reconciler.due_rechecks()))
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert seen == [0] and (out.withdrawn, out.state) == (False, S.UNKNOWN)
    assert wf.row(op).next_recheck_at is not None
    assert wf.engines.reconciler.due_rechecks() == 1  # the authorization is consumed: the reconciler resumes
    assert wf.state(op)[0] == S.CONFIRMED  # it had been picked up and booked: never NOT_APPLIED


# --------------------------------------------------------------------------- R3 — re-check already active (D-G2-02)


def test_r3_commands_during_an_active_recheck_are_refused_and_change_nothing(wf):
    op = unknown_unclaimed(wf)
    due_now(wf, op)
    held = HeldRecheck(wf, op).start()
    assert held.parked and wf.state(op)[0] == S.RECONCILING
    before_events = len(audit(wf, op))
    with pytest.raises(DomainError) as withdraw_refused:
        withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    with pytest.raises(DomainError) as redeliver_refused:
        commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    for refused in (withdraw_refused, redeliver_refused):
        assert refused.value.code is ErrorCode.CONFLICT_STATE
        assert refused.value.message == "not permitted from state RECONCILING"
    assert authorizations(wf, op)[-1].kind == "SUBMIT" and len(authorizations(wf, op)) == 1
    assert len(audit(wf, op)) == before_events  # no AUTHORIZATION_CREATED, no WITHDRAW_RESULT
    assert inbox_files(wf) == [f"ORD_{msgid(wf, op)}.csv"] and withdrawn_files(wf) == []  # no file operation
    assert held.finish() is True
    assert wf.state(op)[0] == S.UNKNOWN  # the re-check ends; the operator re-issues the command
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert (out.withdrawn, out.state) == (True, S.NOT_APPLIED)
    assert wf.state(op) == (S.NOT_APPLIED, E.WITHDRAWN_BEFORE_PICKUP)


# --------------------------------------------------------------------------- R4 — file already picked up


def test_r4_withdraw_after_pickup_with_interleaved_recheck_never_claims_not_applied(wf):
    op = unknown_picked_up(wf)
    due_now(wf, op)
    held: list[HeldRecheck] = []
    in_withdraw_gap(wf, before=lambda: held.append(HeldRecheck(wf, op).start()))
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert held[0].finish() is False
    assert out.withdrawn is False and out.state is S.UNKNOWN
    assert wf.state(op)[0] == S.UNKNOWN and S.NOT_APPLIED.value not in states_seen(wf, op)
    assert withdrawn_files(wf) == []
    assert wf.row(op).pending_recheck_trigger == "MANUAL"
    wf.engines.reconciler.run_once()
    assert wf.state(op)[0] == S.CONFIRMED  # the order was applied; the extract proves it
    assert len(wf.backend_orders(op)) == 1 and wf.handoffs(op) == 1


# --------------------------------------------------------------------------- R5 — duplicate withdrawal requests


def test_r5_duplicate_withdrawals_have_no_duplicate_side_effects(wf):
    op = unknown_unclaimed(wf)
    concurrent: list[DomainError] = []

    def second_request() -> None:
        try:
            withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
        except DomainError as exc:
            concurrent.append(exc)

    in_withdraw_gap(wf, before=second_request)
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert (out.withdrawn, out.state) == (True, S.NOT_APPLIED)
    assert [(e.code, e.message) for e in concurrent] == [(ErrorCode.CONFLICT_STATE, "an authorization is already open")]
    with pytest.raises(DomainError) as again:
        withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert again.value.code is ErrorCode.CONFLICT_STATE and "NOT_APPLIED" in again.value.message
    withdraws = [a for a in authorizations(wf, op) if a.kind == "WITHDRAW"]
    assert [a.state for a in withdraws] == ["CONSUMED"]
    names = [(e.event_type, e.payload.get("kind")) for e in audit(wf, op)]
    assert names.count(("WITHDRAW_RESULT", None)) == 1 and names.count(("AUTHORIZATION_CREATED", "WITHDRAW")) == 1
    assert withdrawn_files(wf) == [f"ORD_{msgid(wf, op)}.csv"] and inbox_files(wf) == []


# --------------------------------------------------------------------------- R6 — redelivery vs RECONCILING


def test_r6_recheck_cannot_expire_a_pending_redelivery_authorization(wf):
    """Found by inspection (executor `_begin` finds RECONCILING ⇒ EXPIRED ⇒ "at most one redelivery" blocks a
    retry). Forced here: the reconciler is due while the REDELIVER authorization waits for the executor."""
    op = unknown_picked_up(wf)
    due_now(wf, op)
    auth_id = commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    held = HeldRecheck(wf, op).start()
    parked = held.parked
    if parked:  # pre-fix behaviour: the executor runs while the operation is RECONCILING
        wf.engines.executor.run_once()
    assert held.finish() is False and parked is False
    assert wf.engines.executor.run_once() is True
    auth = next(a for a in authorizations(wf, op) if a.id == auth_id)
    assert auth.state == "CONSUMED"
    assert [x.kind for x in wf.transmissions(op)] == ["SUBMIT", "REDELIVER"]
    assert "AUTHORIZATION_EXPIRED" not in [e.event_type for e in audit(wf, op)]


def test_r6_recheck_cannot_interleave_a_claimed_redelivery(wf):
    op = unknown_picked_up(wf)
    due_now(wf, op)
    commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    claimed = wf.engines.executor.claim()
    assert claimed is not None and claimed.state == "CLAIMED"
    held = HeldRecheck(wf, op).start()
    assert held.finish() is False
    wf.engines.executor.process(claimed)
    assert [x.kind for x in wf.transmissions(op)] == ["SUBMIT", "REDELIVER"]
    assert wf.state(op)[0] in (S.AWAITING_ACK, S.CONFIRMED)


def test_r6_attempt_limit_is_unchanged_after_a_consumed_redelivery(wf):
    op = unknown_picked_up(wf)
    commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    wf.skarvik.faults.pause_pickup = True
    wf.engines.executor.run_once()
    wf.clock.advance(21)
    wf.engines.reconciler.ack_overdue()
    assert wf.state(op)[0] == S.UNKNOWN
    with pytest.raises(DomainError) as twice:
        commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    assert twice.value.code is ErrorCode.CONFLICT_STATE and twice.value.message == "at most one redelivery per attempt"


# --------------------------------------------------------------------------- R7 — stale authorizations


def test_r7_withdraw_whose_authorization_went_stale_is_not_projected(wf, engines):
    """The handler stalls past its 2-minute claim lease; the sweeper expires the authorization. The late result
    transaction must not resurrect it (EXPIRED → CONSUMED) nor project NOT_APPLIED from it."""
    op = unknown_unclaimed(wf)

    def stall_past_lease() -> None:
        wf.clock.advance(121)
        assert wf.engines.sweeper.stale_claims() == 1

    in_withdraw_gap(wf, before=stall_past_lease)
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert out.withdrawn is False and out.state is S.UNKNOWN
    assert "no longer valid" in out.detail
    withdraw = next(a for a in authorizations(wf, op) if a.kind == "WITHDRAW")
    assert withdraw.state == "EXPIRED" and withdraw.consumed_at is None
    assert wf.state(op)[0] == S.UNKNOWN and S.NOT_APPLIED.value not in states_seen(wf, op)
    result = [e for e in audit(wf, op) if e.event_type == "WITHDRAW_RESULT"]
    assert len(result) == 1 and result[0].payload["withdrawn"] is True
    assert result[0].payload["authorization_state"] == "EXPIRED"
    assert withdrawn_files(wf) == [f"ORD_{msgid(wf, op)}.csv"]  # the physical evidence is preserved for a human
    assert wf.row(op).next_recheck_at is not None
    assert chain_valid(engines)


def test_r7_redelivery_authorization_past_its_ttl_is_never_executed(wf):
    op = unknown_picked_up(wf)
    commands.authorize_redelivery(wf.ctx, wf.p("marta"), op)
    wf.clock.advance(wf.ctx.settings.authorization_ttl.total_seconds() + 1)
    assert wf.engines.executor.run_once() is False
    wf.engines.sweeper.run_once()
    redeliver = next(a for a in authorizations(wf, op) if a.kind == "REDELIVER")
    assert redeliver.state == "EXPIRED"
    assert [x.kind for x in wf.transmissions(op)] == ["SUBMIT"] and wf.handoffs(op) == 1


# --------------------------------------------------------------------------- R8 — crash boundary


class ProcessDeath(BaseException):
    """Models the process dying: not an ``Exception``, so no handler converts it into an outcome."""


def test_r8_crash_between_rename_and_result_transaction_invents_no_outcome(wf, engines):
    op = unknown_unclaimed(wf)
    name = f"ORD_{msgid(wf, op)}.csv"

    def rename_then_die(original: Callable[[str], Any], message_id: str) -> Any:
        original(message_id)
        raise ProcessDeath

    in_withdraw_gap(wf, replace=rename_then_die)
    with pytest.raises(ProcessDeath):
        withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert withdrawn_files(wf) == [name] and inbox_files(wf) == []
    assert next(a for a in authorizations(wf, op) if a.kind == "WITHDRAW").state == "CLAIMED"
    due_now(wf, op)
    assert wf.engines.reconciler.due_rechecks() == 0  # deferred while the claim lease is valid
    wf.clock.advance(121)
    wf.engines.sweeper.run_once()  # claim lease expired ⇒ EXPIRED + RECOVERY re-check
    assert next(a for a in authorizations(wf, op) if a.kind == "WITHDRAW").state == "EXPIRED"
    wf.skarvik.extracts()
    wf.engines.reconciler.run_once()
    assert wf.state(op)[0] == S.UNKNOWN  # file absence + no order in a fresh extract proves nothing
    assert S.NOT_APPLIED.value not in states_seen(wf, op)
    assert "WITHDRAW_RESULT" not in [e.event_type for e in audit(wf, op)]
    assert withdrawn_files(wf) == [name] and wf.skarvik.received() == []  # evidence kept; nothing processed
    assert chain_valid(engines)


def test_r8_ambiguous_filesystem_failure_after_rename_is_not_proof(wf):
    op = unknown_unclaimed(wf)

    def rename_then_fail(original: Callable[[str], Any], message_id: str) -> Any:
        original(message_id)
        raise OSError("fsync failed")

    in_withdraw_gap(wf, replace=rename_then_fail)
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert out.withdrawn is False and out.state is S.UNKNOWN and "withdrawal failed" in out.detail
    assert wf.state(op)[0] == S.UNKNOWN and wf.row(op).pending_recheck_trigger == "MANUAL"


# --------------------------------------------------------------------------- R9 — audit continuity


def test_r9_authorization_evidence_and_state_are_one_consistent_chain(wf, engines):
    op = unknown_unclaimed(wf)
    due_now(wf, op)
    held: list[HeldRecheck] = []
    in_withdraw_gap(wf, after=lambda: held.append(HeldRecheck(wf, op).start()))
    withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    held[0].finish()
    events = audit(wf, op)
    created = next(e for e in events if e.event_type == "AUTHORIZATION_CREATED" and e.payload.get("kind") == "WITHDRAW")
    tail = [e for e in events if e.seq > created.seq]
    assert [e.event_type for e in tail] == ["WITHDRAW_RESULT", "OUTCOME_RECORDED"]
    auth_id = created.correlation["authorization_id"]
    assert tail[0].correlation["authorization_id"] == auth_id
    assert tail[0].correlation["message_id"] == created.correlation["message_id"] == msgid(wf, op)
    assert tail[0].payload["withdrawn"] is True and tail[0].actor_username == "marta"
    assert tail[1].state_change["from"] == S.UNKNOWN.value and tail[1].state_change["to"] == S.NOT_APPLIED.value
    assert tail[1].payload["evidence_class"] == E.WITHDRAWN_BEFORE_PICKUP.value
    withdraw = next(a for a in authorizations(wf, op) if str(a.id) == auth_id)
    assert withdraw.state == "CONSUMED" and withdraw.consumed_at is not None
    assert chain_valid(engines)


# --------------------------------------------------------------------------- R10 — negative control


def test_r10_missing_file_without_withdrawal_evidence_is_never_not_applied(wf):
    op = unknown_unclaimed(wf)
    name = f"ORD_{msgid(wf, op)}.csv"
    (wf.skarvik.inbox / name).unlink()  # gone, but not by our rename: no positive evidence
    (wf.skarvik.withdrawn / name).write_text("not,a,valid\norder\n")  # an artifact we did not produce
    (wf.skarvik.withdrawn / "ORD_SOMEONE-ELSE-01.csv").write_text("x")  # another message's artifact
    wf.skarvik.extracts()
    for _ in range(3):
        due_now(wf, op)
        wf.engines.reconciler.run_once()
    assert wf.state(op)[0] in (S.UNKNOWN, S.ESCALATED)
    out = withdraw_unclaimed(wf.ctx, wf.p("marta"), op) if wf.state(op)[0] == S.UNKNOWN else None
    if out is not None:
        assert out.withdrawn is False and out.state is S.UNKNOWN
    assert S.NOT_APPLIED.value not in states_seen(wf, op)


def test_r10_withdraw_is_refused_from_terminal_states(wf):
    op = unknown_picked_up(wf)
    due_now(wf, op)
    wf.engines.reconciler.run_once()
    assert wf.state(op)[0] == S.CONFIRMED
    with pytest.raises(DomainError) as refused:
        withdraw_unclaimed(wf.ctx, wf.p("marta"), op)
    assert refused.value.code is ErrorCode.CONFLICT_STATE
    assert [a.kind for a in authorizations(wf, op)] == ["SUBMIT"]
