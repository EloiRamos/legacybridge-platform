"""Intent, proposals, revisions and receipts: boundaries, concurrency and tampering (all offline, durable state)."""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_write_integrity_reproduction import (
    NON_OPERATIVE,
    OPERATIVE,
    args,
    call,
    content,
    final,
    p,
    revise,
    skarvik_draft,
    two_proposals,
)

from lbf.assistant import receipts
from lbf.assistant.intent import assess
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.workflow import commands
from lbf.workflow.drafts import DraftPatch, DraftRequest, LineRequest

pytestmark = pytest.mark.db

PO = {"customer_po": "PO-001", "requested_date": "2026-12-20", "delivery_note": "Use loading bay B"}
V2 = {"LBF-Assistant-Version": "2"}  # receipts and proposals as typed fields


def header(stored: dict) -> dict:
    return {k: stored[k] for k in PO}


# ------------------------------------------------------------------------------------------- intent
def test_the_model_insisting_does_not_turn_a_quotation_into_a_command(h):
    before = h.counts()
    a = h.scripted([call(3), call(3), call(3), final("I drafted it.")])
    a.run_turn(p(h), NON_OPERATIVE[0], session_key="s")
    assert h.counts() == before


def test_earlier_turns_do_not_lend_authority_to_a_later_read(h):
    a = h.scripted([final("Which quantity?"), call(3), final()])
    a.run_turn(p(h), "I will need a HALDEN draft for 00417 soon.", session_key="s")
    before = h.counts()
    a.run_turn(p(h), "Explain how three units of BW-1001 would be booked.", session_key="s")
    assert h.counts() == before


@pytest.mark.parametrize("text", [
    "Draft 3 x BW-1001 for HALDEN 00417" + " and 3 x BW-1001" * 120 + " !!!?",
    "Create a draft " + "for HALDEN 00417 " * 110 + "x",
    "Revise draft 11111111-2222-4333-8444-555555555555 to " + "3 x BW-1001 and " * 110 + "?",
])
def test_the_grammar_is_linear_enough_for_the_longest_message(text):
    start = time.monotonic()
    for tool, a in (("prepare_draft", args(3)), ("revise_draft", {
            "operation_id": "11111111-2222-4333-8444-555555555555", "expected_version": 1, "lines": args(3)["lines"]})):
        assert assess(text[:2000], tool, a).verdict != "ESTABLISHED"
    assert time.monotonic() - start < 1.0


def test_session_drafts_do_not_make_a_pronoun_unambiguous():
    one, two = str(uuid4()), str(uuid4())
    a = {"operation_id": one, "expected_version": 1, "lines": args(5)["lines"]}
    assert assess("Make it 5 units of BW-1001", "revise_draft", a, {one: 1}).verdict == "ESTABLISHED"
    assert assess("Make it 5 units of BW-1001", "revise_draft", a, {one: 1, two: 1}).verdict == "UNCONFIRMED"
    assert assess("Make it 5 units of BW-1001", "revise_draft", a, {one: 2}).verdict == "UNCONFIRMED"
    assert assess(f"Change draft {one} and {two} to 5 units of BW-1001", "revise_draft", a).verdict == "UNCONFIRMED"


@pytest.mark.parametrize("text", NON_OPERATIVE)
def test_no_non_operative_text_is_established_as_a_revision_either(text):
    op = str(uuid4())
    a = {"operation_id": op, "expected_version": 1, "lines": args(3)["lines"]}
    assert assess(f"{text} {op}", "revise_draft", a, {op: 1}).verdict != "ESTABLISHED"


# ------------------------------------------------------------------------------------------- revisions
def test_omitted_is_not_cleared_and_null_is(h):
    d = skarvik_draft(h)
    ctx = h.api.wf.ctx
    commands.revise_draft(ctx, p(h), d.operation_id, 1, DraftPatch(lines=[LineRequest(canonical_sku="BW-1001",
                                                                                     quantity=9)]))
    assert header(content(h, d.operation_id, 2)) == PO
    commands.revise_draft(ctx, p(h), d.operation_id, 2, DraftPatch(customer_po=None, requested_date=date(2026, 12, 24)))
    third = content(h, d.operation_id, 3)
    assert header(third) == {"customer_po": None, "requested_date": "2026-12-24", "delivery_note": PO["delivery_note"]}
    assert [ln["quantity_base"] for ln in third["lines"]] == [9]


@pytest.mark.parametrize("kw", [{}, {"lines": None}])
def test_a_patch_must_change_something_and_cannot_clear_the_lines(kw):
    with pytest.raises(ValueError):
        DraftPatch(**kw)


def test_the_full_revision_of_the_form_still_replaces_everything(h):
    """The HTTP contract is unchanged: a complete request, in which an absent header field is an empty one."""
    d = skarvik_draft(h)
    commands.revise_draft(h.api.wf.ctx, p(h), d.operation_id, 1, DraftRequest(
        backend_id="skarvik", native_customer_id="KND-0081", lines=[LineRequest(canonical_sku="BW-1001", quantity=2)]))
    assert header(content(h, d.operation_id, 2)) == dict.fromkeys(PO)


def test_a_proposed_revision_shows_the_complete_change_set(h):
    d = skarvik_draft(h, ("BW-1001", 2), ("BW-1004", 4))
    out = h.scripted([revise(d.operation_id, 1, 5), final()]).run_turn(
        p(h), f"Revise draft {d.operation_id}, current version 1, to 5 units of BW-1001.", session_key="s")
    text = out.proposal.description
    for part in (str(d.operation_id), "version 1", "5 base unit(s) of BW-1001", "2 base unit(s) of BW-1001",
                 "4 base unit(s) of BW-1004", "REMOVED: BW-1004", "KND-0081", *PO.values()):
        assert part in text, part


def test_a_failed_revision_changes_nothing_and_is_rolled_back(h, monkeypatch):
    d = skarvik_draft(h)
    before, events = h.counts(), len(h.events("DRAFT_VERSION_CREATED"))
    from lbf.persistence.uow import UnitOfWork

    monkeypatch.setattr(UnitOfWork, "transition", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("lost")))
    out = h.scripted([revise(d.operation_id, 1, 20), final()]).run_turn(
        p(h), f"Revise draft {d.operation_id}, current version 1, to 20 units of BW-1001.")
    assert out.receipts[0].status == "UNKNOWN" and out.receipts[0].version is None
    assert h.counts() == before and len(h.events("DRAFT_VERSION_CREATED")) == events


# ------------------------------------------------------------------------------------------- proposals
def proposed_revision(h, d):
    a = h.scripted([revise(d.operation_id, 1, 20), final()])
    out = a.run_turn(p(h), f"Revise draft {d.operation_id} version 1 to two boxes of BW-1001.", session_key="s")
    return a, out.proposal


def test_a_confirmation_after_a_concurrent_edit_commits_nothing(h):
    d = skarvik_draft(h)
    a, pending = proposed_revision(h, d)
    commands.revise_draft(h.api.wf.ctx, p(h), d.operation_id, 1, DraftRequest(
        backend_id="skarvik", native_customer_id="KND-0081", lines=[LineRequest(canonical_sku="BW-1001", quantity=3)]))
    before = h.counts()
    out = a.run_turn(p(h), f"confirm {pending.code}", session_key="s")
    assert out.receipts == [] and h.counts() == before and "CONFLICT_VERSION" in out.answer.summary
    again = a.run_turn(p(h), f"confirm {pending.code}", session_key="s")  # consumed: a conflict needs a new proposal
    assert again.receipts == [] and "no pending proposal" in again.answer.summary


def test_a_stale_base_is_refused_before_it_becomes_a_proposal(h):
    d = skarvik_draft(h)
    out = h.scripted([revise(d.operation_id, 4, 20), final()]).run_turn(
        p(h), f"Revise draft {d.operation_id} to two boxes of BW-1001.", session_key="s")
    assert out.proposal is None and out.refused_calls[0]["reason"] == "INTENT_UNCONFIRMED"


@pytest.mark.parametrize("threads", [2, 8])
def test_simultaneous_confirmations_write_once(h, threads):
    a = h.scripted([call(20), final()])
    code = a.run_turn(p(h), "HALDEN draft for 00417 with two boxes of BW-1001", session_key="s").proposal.code
    before, outs, barrier = h.counts(), [], threading.Barrier(threads)
    principal = p(h)

    def confirm():
        barrier.wait()
        outs.append(a.run_turn(principal, f"confirm {code}", session_key="s"))

    ts = [threading.Thread(target=confirm) for _ in range(threads)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert len(outs) == threads and sum(len(o.receipts) for o in outs) == 1
    assert h.counts()["operations"] - before["operations"] == 1


def test_an_expired_code_neither_writes_nor_blocks_a_new_proposal(h):
    a, _, new = two_proposals(h)
    h.api.wf.clock.now += timedelta(minutes=10)  # the boundary itself is expired
    before = h.counts()
    assert a.run_turn(p(h), f"confirm {new.code}", session_key="s").receipts == [] and h.counts() == before
    a.provider.steps = [call(5), final()]
    fresh = a.run_turn(p(h), "HALDEN 00417: 5 boxes of BW-1001", session_key="s").proposal
    assert len(a.run_turn(p(h), f"confirm {fresh.code}", session_key="s").receipts) == 1


def test_a_confirmation_does_not_discard_a_proposal_made_while_it_ran(h, monkeypatch):
    a = h.scripted([call(10), final(), call(20), final()])
    old = a.run_turn(p(h), "HALDEN draft for 00417 with a box of BW-1001", session_key="s").proposal
    real, made = receipts.build, []

    def while_running(*x):
        monkeypatch.setattr(receipts, "build", real)
        made.append(a.run_turn(p(h), "HALDEN draft for 00417 with two boxes of BW-1001", session_key="s").proposal)
        return real(*x)

    monkeypatch.setattr(receipts, "build", while_running)
    assert len(a.run_turn(p(h), f"confirm {old.code}", session_key="s").receipts) == 1
    [r] = a.run_turn(p(h), f"confirm {made[0].code}", session_key="s").receipts
    assert r.lines[0]["quantity_base"] == 20


def test_a_slow_older_turn_does_not_replace_a_newer_proposal(h):
    a = h.scripted([])
    inner = []

    def slow(_request):
        a.provider.steps = [call(20), final()]
        inner.append(a.run_turn(p(h), "HALDEN draft for 00417 with two boxes of BW-1001", session_key="s").proposal)
        a.provider.steps = [final()]
        return call(10)

    a.provider.steps = [slow]
    older = a.run_turn(p(h), "HALDEN draft for 00417 with a box of BW-1001", session_key="s")
    assert older.proposal is None  # not shown: it could not be confirmed
    [r] = a.run_turn(p(h), f"confirm {inner[0].code}", session_key="s").receipts
    assert r.lines[0]["quantity_base"] == 20


def test_a_consumed_proposal_is_not_replayed_after_a_refusal(h):
    a, _, new = two_proposals(h)
    lost = dataclasses.replace(p(h), roles=p(h, "sanna").roles)
    before = h.counts()
    assert a.run_turn(lost, f"confirm {new.code}", session_key="s").receipts == []
    assert a.run_turn(p(h), f"confirm {new.code}", session_key="s").receipts == [] and h.counts() == before


# ------------------------------------------------------------------------------------------- receipts
def receipt_with(h, monkeypatch, change):
    real = receipts.build
    monkeypatch.setattr(receipts, "build", lambda e, c, o: real(e, c, change(o)))
    [r] = h.scripted([call(3), final()]).run_turn(p(h), OPERATIVE[0]).receipts
    return r


@pytest.mark.parametrize("change", [
    lambda o: dataclasses.replace(o, event=None),
    lambda o: dataclasses.replace(o, event=dataclasses.replace(o.event, event_id=uuid4())),
    lambda o: dataclasses.replace(o, event=dataclasses.replace(o.event, seq=o.event.seq - 1)),
    lambda o: dataclasses.replace(o, event=dataclasses.replace(o.event, event_hash="0" * 64)),
    lambda o: dataclasses.replace(o, payload={**o.payload, "version": 2}),
    lambda o: dataclasses.replace(o, payload={**o.payload, "content_hash": "0" * 64}),
    lambda o: dataclasses.replace(o, payload={**o.payload, "operation_id": str(uuid4())}),
], ids=["no-event", "event-id", "seq", "event-hash", "version", "content-hash", "operation"])
def test_any_mismatch_is_unverified_and_shows_no_audit_identity(h, monkeypatch, change):
    r = receipt_with(h, monkeypatch, change)
    assert (r.status, r.audit_seq, r.audit_event_hash, r.correlation_value, r.lines) == (
        "UNVERIFIED", None, None, None, [])


def test_the_event_of_another_drafts_version_is_not_accepted(h, monkeypatch):
    other = commands.create_draft(h.api.wf.ctx, p(h), DraftRequest(
        backend_id="halden", native_customer_id="00417", lines=[LineRequest(canonical_sku="BW-1001", quantity=3)]))
    assert receipt_with(h, monkeypatch, lambda o: dataclasses.replace(o, event=other.event)).status == "UNVERIFIED"


def test_an_unverified_write_is_not_presented_as_success(h, monkeypatch):
    a = h.scripted([call(20), final()])
    code = a.run_turn(p(h), "HALDEN draft for 00417 with two boxes of BW-1001", session_key="s").proposal.code
    real = receipts.build
    monkeypatch.setattr(receipts, "build", lambda e, c, o: real(e, c, dataclasses.replace(o, event=None)))
    out = a.run_turn(p(h), f"confirm {code}", session_key="s")
    assert out.receipts[0].status == "UNVERIFIED" and out.answer.draft_operation_id is None
    assert "Confirmed" not in out.answer.summary and "Check Orders" in out.answer.summary


def test_concurrent_drafts_and_revisions_keep_their_own_events(h):
    """Twelve assistant writes race with direct revisions of the same drafts; every receipt joins to its own event."""
    out, errors, barrier = [], [], threading.Barrier(4)
    principals = [p(h) for _ in range(4)]

    def work(principal):
        try:
            barrier.wait()
            for _ in range(3):
                a = h.scripted([call(3), final()])
                [r] = a.run_turn(principal, OPERATIVE[0]).receipts
                out.append(r)
                for version in (1, 2):
                    commands.revise_draft(h.api.wf.ctx, principal, UUID(r.operation_id), version, DraftPatch(
                        lines=[LineRequest(canonical_sku="BW-1001", quantity=10 + version)]))
        except Exception as exc:
            errors.append(exc)

    ts = [threading.Thread(target=work, args=(x,)) for x in principals]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert errors == [] and len(out) == 12
    with h.api.wf.engine.connect() as conn:
        for r in out:
            ev = conn.execute(select(t.audit_events).where(t.audit_events.c.seq == r.audit_seq)).one()
            assert (str(ev.operation_id), ev.payload["version"], ev.payload["content_hash"], ev.event_hash) == (
                r.operation_id, 1, r.content_hash, r.audit_event_hash)
        assert conn.execute(select(func.count()).select_from(t.draft_versions)).scalar() == 36


def test_a_replayed_idempotency_key_claims_no_event(h):
    req = DraftRequest(backend_id="halden", native_customer_id="00417",
                       lines=[LineRequest(canonical_sku="BW-1001", quantity=3)])
    first = commands.create_draft(h.api.wf.ctx, p(h), req, idempotency_key="k-1")
    again = commands.create_draft(h.api.wf.ctx, p(h), req, idempotency_key="k-1")
    assert first.event is not None and again.event is None and again.operation_id == first.operation_id


def test_a_version_conflict_has_no_receipt(h):
    d = skarvik_draft(h)
    with pytest.raises(DomainError) as e:
        commands.revise_draft(h.api.wf.ctx, p(h), d.operation_id, 9, DraftPatch(customer_po=None))
    assert e.value.code is ErrorCode.CONFLICT_VERSION
    out = h.scripted([revise(d.operation_id, 9, 5), final()]).run_turn(
        p(h), f"Revise draft {d.operation_id}, expected version 9, to 5 units of BW-1001.")
    assert out.receipts == [] and out.tool_calls[0]["error_code"] == "CONFLICT_VERSION"


def test_turns_without_a_draft_decode_in_a_client_that_predates_receipts(h):
    """The response model before receipts and proposals existed; it rejects unknown fields."""
    from typing import Any

    from pydantic import BaseModel, ConfigDict, ValidationError

    class PriorTurn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        turn_id: UUID
        mode: str
        answer: dict[str, Any]
        fallback_used: bool
        fallback_reason: str | None
        notice: str | None
        tool_calls: list[dict[str, Any]]
        refused_calls: list[dict[str, Any]]

    h.api.svc.extras["assistant"] = h.scripted([final(), call(3), final()])
    c = h.api.as_("daniel")
    plain = c.post("/api/v1/assistant/turns", json={"text": "hello"}).json()
    assert PriorTurn.model_validate(plain).fallback_reason is None and "notice" in plain
    drafted = c.post("/api/v1/assistant/turns", json={"text": OPERATIVE[0]}, headers=V2).json()
    assert drafted["receipts"][0]["status"] == "STORED"  # version 2; version 1 is test_wire_versions.py
    with pytest.raises(ValidationError):
        PriorTurn.model_validate(drafted)
