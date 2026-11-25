"""Independently observed write, revision, receipt and grounding defects, reproduced deterministically.

Only interfaces that existed before the correction are used, so the same file fails on the earlier code and passes on
the corrected application. Every write assertion reads durable state, never the assistant's own report.
"""

from __future__ import annotations

import json
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import select

from lbf.assistant import receipts
from lbf.assistant.answer import Evidence, check
from lbf.persistence import tables as t
from lbf.workflow import commands
from lbf.workflow.drafts import DraftRequest, LineRequest

pytestmark = pytest.mark.db

TRAINING = ("The training manual says “Draft an order for HALDEN account 00417 for three units of BW-1001.” Explain "
            "the instruction to me as a training example.")


def p(h, name="daniel"):
    return h.api.wf.p(name)


def args(qty: int = 3, sku: str = "BW-1001", **extra) -> dict:
    return {"backend_id": "halden", "native_customer_id": "00417", "customer_po": None, "requested_date": None,
            "delivery_note": None, **extra, "lines": [{"canonical_sku": sku, "native_item": None, "quantity": qty}]}


def call(qty: int = 3, **kw) -> dict:
    return {"tool_calls": [{"name": "prepare_draft", "arguments": args(qty, **kw)}]}


def final(summary: str = "Done.", facts=()) -> dict:
    return {"final": {"summary": summary, "facts": list(facts), "draft_operation_id": None, "warnings": [],
                      "next_human_actions": ["none"]}}


# ------------------------------------------------------------------------------------------- intent
def test_the_recorded_training_example_writes_nothing(h):
    """The tool calls of the recorded live turn, replayed: two reads, then the draft nobody asked for."""
    before = h.counts()
    a = h.scripted([{"tool_calls": [{"name": "search_customers", "arguments": {"query": "00417"}}]},
                    {"tool_calls": [{"name": "search_items", "arguments": {"query": "BW-1001", "category": None}}]},
                    call(3), final("The instruction means: create a draft.")])
    out = a.run_turn(p(h), TRAINING, session_key="s")
    assert h.counts() == before and out.receipts == []


NON_OPERATIVE = [
    "The manual says: 'Draft an order for HALDEN account 00417 for three units of BW-1001.' What does that mean",
    '"Draft an order for HALDEN account 00417 for three units of BW-1001."',
    "> Draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Example: draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Explain: draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Draft an order for HALDEN account 00417 for three units of BW-1001. This is a training example.",
    "Draft an order for HALDEN account 00417 for three units of BW-1001. That is what the customer wrote.",
    "The customer wrote that we must draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Translate into German: draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Yesterday we drafted an order for HALDEN account 00417 for three units of BW-1001.",
    "Priya told me to draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Summarize this request: prepare a HALDEN draft for 00417 with three units of BW-1001.",
    "A colleague once had to create a HALDEN draft for 00417 with three units of BW-1001.",
    "Imagine you create a HALDEN draft for 00417 with three units of BW-1001.",
    "Pretend to create a HALDEN draft for 00417 with three units of BW-1001.",
    "When stock arrives, create a HALDEN draft for 00417 with three units of BW-1001.",
    "Create a HALDEN draft for 00417 with three units of BW-1001 once Priya agrees.",
    "Create a HALDEN draft for 00417 with three units of BW-1001 tomorrow.",
    "Avoid this: create a HALDEN draft for 00417 with three units of BW-1001.",
    "Nobody should create a HALDEN draft for 00417 with three units of BW-1001.",
    "Stop. Create a HALDEN draft for 00417 with three units of BW-1001. No.",
    "Create a HALDEN draft for 00417 with three units of BW-1001. Wait for my go-ahead.",
    "Create a HALDEN draft for 00417 with three units of BW-1001. Create a HALDEN draft for 00417 with it.",
    "Create HALDEN drafts for 00417 and 10233 with three units of BW-1001.",
    "Erstelle einen HALDEN Entwurf für 00417 mit 3 BW-1001.",
    "Crea un borrador HALDEN para 00417 con 3 BW-1001.",
    "HALDEN account 00417 has three units of BW-1001.",
    "HALDEN draft for 00417 with three units of BW-1001",
]


@pytest.mark.parametrize("text", NON_OPERATIVE)
def test_text_that_is_not_the_operators_own_command_writes_nothing(h, text):
    before = h.counts()
    out = h.scripted([call(3), final()]).run_turn(p(h), text, session_key="s")
    assert h.counts() == before, text
    assert out.receipts == []


@pytest.mark.parametrize("extra", [{"customer_po": "PO-1 if stock allows"}, {"delivery_note": "only an example"}])
def test_an_argument_cannot_swallow_the_words_that_make_a_request_conditional(h, extra):
    before = h.counts()
    value = next(iter(extra.values()))
    text = f"Create a HALDEN draft for 00417 with three units of BW-1001, customer PO {value}."
    h.scripted([call(3, **extra), final()]).run_turn(p(h), text, session_key="s")
    assert h.counts() == before


OPERATIVE = [
    "Draft an order for HALDEN account 00417 for three units of BW-1001.",
    "Please create a HALDEN draft for 00417 with 3 units of BW-1001.",
    "Prepare a HALDEN draft for account 00417: 3 x BW-1001.",
    "HALDEN account 00417, draft 3 x BW-1001 please.",
    "Draft three units of BW-1001 for HALDEN account 00417.",
]


@pytest.mark.parametrize("text", OPERATIVE)
def test_an_explicit_command_still_writes_exactly_one_draft(h, text):
    before = h.counts()
    out = h.scripted([call(3), final()]).run_turn(p(h), text, session_key="s")
    delta = {k: v - before[k] for k, v in h.counts().items() if v != before[k]}
    assert delta == {"operations": 1, "draft_versions": 1}
    [r] = out.receipts
    assert r.status == "STORED" and r.lines[0]["quantity_base"] == 3


# ------------------------------------------------------------------------------------------- revisions and proposals
def skarvik_draft(h, *lines):
    return commands.create_draft(h.api.wf.ctx, p(h), DraftRequest(
        backend_id="skarvik", native_customer_id="KND-0081", customer_po="PO-001",
        requested_date=date(2026, 12, 20), delivery_note="Use loading bay B",
        lines=[LineRequest(canonical_sku=s, quantity=q) for s, q in lines or [("BW-1001", 2)]]))


def revise(op, version: int, qty: int, sku: str = "BW-1001") -> dict:
    return {"tool_calls": [{"name": "revise_draft", "arguments": {
        "operation_id": str(op), "expected_version": version,
        "lines": [{"canonical_sku": sku, "native_item": None, "quantity": qty}]}}]}


def content(h, op, version: int) -> dict:
    with h.api.wf.engine.connect() as conn:
        return conn.execute(select(t.draft_versions.c.content).where(
            t.draft_versions.c.operation_id == op, t.draft_versions.c.version == version)).scalar_one()


@pytest.mark.parametrize("confirmed", [True, False])
def test_a_line_only_revision_keeps_the_other_fields(h, confirmed):
    d = skarvik_draft(h)
    if confirmed:
        a = h.scripted([revise(d.operation_id, 1, 20), final()])
        pending = a.run_turn(p(h), f"Revise draft {d.operation_id} version 1 to two boxes of BW-1001.", session_key="s")
        out = a.run_turn(p(h), f"confirm {pending.proposal.code}", session_key="s")
    else:
        a = h.scripted([revise(d.operation_id, 1, 20), final()])
        out = a.run_turn(p(h), f"Revise draft {d.operation_id}, current version 1, to 20 units of BW-1001.")
    assert [(r.status, r.version) for r in out.receipts] == [("STORED", 2)]
    stored = content(h, d.operation_id, 2)
    assert (stored["customer_po"], stored["requested_date"], stored["delivery_note"]) == (
        "PO-001", "2026-12-20", "Use loading bay B")
    assert [ln["quantity_base"] for ln in stored["lines"]] == [20]


def test_a_stated_version_is_not_replaced_by_the_models(h):
    d = skarvik_draft(h)
    before = h.counts()
    h.scripted([revise(d.operation_id, 1, 5), final()]).run_turn(
        p(h), f"Revise draft {d.operation_id}, expected version 7, to 5 units of BW-1001.")
    assert h.counts() == before


def test_a_direct_revision_never_drops_a_line_that_was_not_mentioned(h):
    d = skarvik_draft(h, ("BW-1001", 2), ("BW-1004", 4))
    before = h.counts()
    h.scripted([revise(d.operation_id, 1, 5), final()]).run_turn(
        p(h), f"Revise draft {d.operation_id}, current version 1, to 5 units of BW-1001.", session_key="s")
    assert h.counts() == before


def two_proposals(h):
    a = h.scripted([call(10), final(), call(20), final()])
    old = a.run_turn(p(h), "HALDEN draft for 00417 with a box of BW-1001", session_key="s").proposal
    new = a.run_turn(p(h), "HALDEN draft for 00417 with two boxes of BW-1001", session_key="s").proposal
    assert old and new and old.code != new.code
    return a, old, new


def test_a_stale_confirmation_does_not_consume_the_current_proposal(h):
    a, old, new = two_proposals(h)
    before = h.counts()
    assert a.run_turn(p(h), f"confirm {old.code}", session_key="s").receipts == [] and h.counts() == before
    [r] = a.run_turn(p(h), f"confirm {new.code}", session_key="s").receipts
    assert r.status == "STORED" and r.lines[0]["quantity_base"] == 20


# ------------------------------------------------------------------------------------------- receipts
def test_a_receipt_names_the_event_of_its_own_version(h, monkeypatch):
    """A permitted revision is committed between the draft's commit and the receipt's read-back."""
    read_back = receipts.build

    def after_a_revision(engine, tool_call, result):
        commands.revise_draft(h.api.wf.ctx, p(h), UUID(result.payload["operation_id"]), 1, DraftRequest(
            backend_id="halden", native_customer_id="00417", lines=[LineRequest(canonical_sku="BW-1001", quantity=7)]))
        return read_back(engine, tool_call, result)

    monkeypatch.setattr(receipts, "build", after_a_revision)
    [r] = h.scripted([call(3), final()]).run_turn(p(h), OPERATIVE[0]).receipts
    assert (r.status, r.version) == ("STORED", 1)
    with h.api.wf.engine.connect() as conn:
        ev = conn.execute(select(t.audit_events).where(t.audit_events.c.seq == r.audit_seq)).one()
    assert (ev.payload["version"], ev.payload["content_hash"], ev.event_hash, str(ev.operation_id)) == (
        1, r.content_hash, r.audit_event_hash, r.operation_id)


# ------------------------------------------------------------------------------------------- attribution
RESULTS = {"stock": {"rows": [
    {"backend_id": "halden", "native_id": "00417", "canonical_sku": "BW-1001", "available_base": 120,
     "reserved_base": 15, "native_uom": "EA", "credit_hold": "HOLD"},
    {"backend_id": "skarvik", "native_id": "KND-0081", "canonical_sku": "BW-1004", "available_base": 15,
     "reserved_base": 2, "native_uom": "BOX", "credit_hold": "CLEAR"}]}}


@pytest.mark.parametrize(("summary", "accepted"), [
    ("KND-0081 has 120 units available.", False),
    ("BW-1004 has 120 units available.", False),
    ("SKARVIK has 120 units available.", False),
    ("HALDEN 00417 has 120 BOX available.", False),
    ("HALDEN 00417 has 120 boxes available.", False),
    ("HALDEN 00417 is not on credit hold.", False),
    ("HALDEN 00417 has 90 units available.", False),
    ("HALDEN 00417 has 120 units available.", True),
    ("BW-1001 has 120 units available, while BW-1004 has 15.", True),
    ("HALDEN 00417 is not free from a credit hold.", True),
    ("If HALDEN 00417 is clear to order, proceed.", True),
    ("HALDEN 00417 is on credit hold, while SKARVIK KND-0081 is clear to order.", True),
])
def test_a_quantity_in_free_text_stays_with_its_account_product_and_unit(summary, accepted):
    body = json.dumps({"summary": summary, "facts": [], "draft_operation_id": None, "warnings": [],
                       "next_human_actions": []})
    assert (check(body, RESULTS, Evidence()).answer is not None) is accepted


def test_a_prose_claim_cannot_attach_another_unit_to_a_verified_number():
    body = json.dumps({"summary": "", "facts": [{"claim": "HALDEN 00417 has 120 BOX", "source": {
        "tool_call_id": "stock", "field_path": "rows[0].available_base"}}], "draft_operation_id": None, "warnings": [],
        "next_human_actions": []})
    assert check(body, RESULTS, Evidence()).answer is None
