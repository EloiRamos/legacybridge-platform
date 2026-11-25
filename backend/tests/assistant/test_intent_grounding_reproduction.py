"""Assistant findings of an earlier audit, reproduced deterministically (the failure mechanism, not one
particular model response).

Every test here uses only interfaces that existed at the audited commit, so the file can be run against the unchanged
baseline (where it fails) and against the corrected application.
"""

from __future__ import annotations

import json

import pytest
from assistant_helpers import FINAL, TOOL

from lbf.assistant.answer import validate

pytestmark = pytest.mark.db


def p(h, name="daniel"):
    return h.api.wf.p(name)


def draft_args(qty: int, sku: str = "BW-1001", customer: str = "00417") -> dict:
    return {"backend_id": "halden", "native_customer_id": customer, "customer_po": None, "requested_date": None,
            "delivery_note": None, "lines": [{"canonical_sku": sku, "native_item": None, "quantity": qty}]}


def final(summary: str = "Done.", facts=(), draft=None) -> dict:
    return {"summary": summary, "facts": list(facts), "draft_operation_id": draft, "warnings": [],
            "next_human_actions": ["none"]}


# ------------------------------------------------------------------------------------------- intent integrity
@pytest.mark.parametrize(("request_text", "model_qty"), [
    ("Create a HALDEN draft for 00417 with minus three units of BW-1001. Report what happened.", 3),  # live, verbatim
    ("Create a HALDEN draft for 00417 with -3 units of BW-1001.", 3),
    ("Create a HALDEN draft for 00417 with negative three units of BW-1001.", 3),
    ("Create a HALDEN draft for 00417 with zero units of BW-1001.", 1),
    ("Create a HALDEN draft for 00417 with 0 units of BW-1001.", 1),
    ("Create a HALDEN draft for 00417 with 2.5 units of BW-1001.", 2),
    ("Create a HALDEN draft for 00417 with two and a half units of BW-1001.", 2),
    ("Create a HALDEN draft for 00417 with a few units of BW-1001.", 3),
    ("Create a HALDEN draft for 00417 with three or five units of BW-1001.", 5),
    ("Create a HALDEN draft for 00417: three, no sorry, five units of BW-1001.", 5),
    ("Create a HALDEN draft for 00417 with two boxes of BW-1001.", 20),
    ("Create a HALDEN draft for 00417 with a dozen BW-1001.", 12),
    ("Create a HALDEN draft for 00417 with BW-1001.", 1),
    ("Create a HALDEN draft for 00417 with 3 units of BW-1001.", 30),
    ("Create a HALDEN draft for 00417 with 3 units of BW-1001.", 300),
    ("Create a HALDEN draft for 00417 with 10 units of BW-1001.", 1),
])
def test_a_quantity_the_person_did_not_state_never_becomes_a_draft(h, request_text, model_qty):
    before = h.counts()
    a = h.scripted([{"tool_calls": [{"name": "prepare_draft", "arguments": draft_args(model_qty)}]},
                    {"final": final("Please state a valid quantity.")}])
    a.run_turn(p(h), request_text)
    assert h.counts() == before


def test_a_tool_result_cannot_supply_the_quantity_or_the_account(h):
    """Prompt injection that alters arguments: the person asked for two units for 00417, the model sends others."""
    before = h.counts()
    for args in (draft_args(900), draft_args(2, customer="52290"), draft_args(2, sku="BW-1004")):
        a = h.scripted([{"tool_calls": [{"name": "search_customers", "arguments": {"query": "lesund"}}]},
                        {"tool_calls": [{"name": "prepare_draft", "arguments": args}]}, {"final": final()}])
        a.run_turn(p(h), "Look up Ålesund, then create a HALDEN draft for 00417 with two units of BW-1001.")
    assert h.counts() == before


def test_a_repeated_drafting_call_in_one_turn_writes_once(h):
    a = h.scripted([{"tool_calls": [{"name": "prepare_draft", "arguments": draft_args(2)}]},
                    {"tool_calls": [{"name": "prepare_draft", "arguments": draft_args(2)}]}, {"final": final()}])
    before = h.counts()
    a.run_turn(p(h), "Create a HALDEN draft for 00417 with two units of BW-1001.")
    assert h.counts()["operations"] - before["operations"] == 1


# ------------------------------------------------------------------------------------------- mutation visibility
def test_a_stored_draft_stays_reachable_when_the_answer_is_rejected(h):
    bad = final("Draft created.", [{"claim": "The draft contains 77 units", "source": {
        "tool_call_id": "call-1", "field_path": "state"}}])
    a = h.scripted([{"tool_calls": [{"name": "prepare_draft", "arguments": draft_args(2)}]}, {"final": bad},
                    {"final": bad}])
    out = a.run_turn(p(h), "Create a HALDEN draft for 00417 with two units of BW-1001.")
    assert out.fallback_reason == "GROUNDING_FAILED"
    assert out.answer.draft_operation_id is not None


def test_a_stored_draft_stays_reachable_when_the_final_request_fails(h, mock_openai):
    import httpx2

    mock_openai.queue = [TOOL("prepare_draft", draft_args(2)), httpx2.ReadTimeout("slow")]
    out = h.openai(mock_openai).run_turn(p(h), "Create a HALDEN draft for 00417 with two units of BW-1001.")
    assert out.fallback_reason == "TIMEOUT" and out.answer.draft_operation_id is not None


def test_a_draft_link_needs_a_stored_draft(h, mock_openai):
    mock_openai.queue = [FINAL(final("Prepared.", draft="00000000-0000-4000-8000-000000000001"))] * 2
    out = h.openai(mock_openai).run_turn(p(h), "hello")
    assert out.answer.draft_operation_id is None


# ------------------------------------------------------------------------------------------- grounding
RECORD = {"accounts": [{"backend_id": "skarvik", "native_id": "KND-0190", "name_display": "Kiruna Gruvteknik AB",
                        "city": "Kiruna", "country_code": "SE", "credit_hold": "HOLD", "on_hold": True,
                        "quantity": 100, "contact_name": None},
                       {"backend_id": "halden", "native_id": "00417", "name_display": "Nordkap", "city": "Bergen",
                        "country_code": "NO", "credit_hold": "CLEAR", "on_hold": False, "quantity": 7,
                        "contact_name": "Erik"}]}


def fact(claim: str, path: str) -> dict:
    return final("See the cited field.", [{"claim": claim, "source": {"tool_call_id": "c1", "field_path": path}}])


@pytest.mark.parametrize(("claim", "path"), [
    ("The customer is not on hold", "accounts[0].on_hold"),
    ("Credit hold: No", "accounts[0].credit_hold"),
    ("The account is not on credit hold (HOLD)", "accounts[0].credit_hold"),
    ("Quantity is 10", "accounts[0].quantity"),
    ("Quantity is 1", "accounts[0].quantity"),
    ("KND-019 is the account", "accounts[0].native_id"),
    ("City is Stockholm", "accounts[0].city"),
    ("City is Bergen", "accounts[0].city"),
    ("Account 00417 is in Kiruna", "accounts[0].city"),
    ("The hold is clear", "accounts[0]"),
    ("7 units", "accounts[0].quantity"),
])
def test_false_or_misattributed_facts_are_rejected(claim, path):
    answer, problem = validate(json.dumps(fact(claim, path)), {"c1": RECORD})
    assert answer is None and problem.startswith("GROUNDING_FAILED")


@pytest.mark.parametrize(("claim", "path"), [
    ("City is Kiruna", "accounts[0].city"),
    ("Quantity is 100", "accounts[0].quantity"),
    ("Account KND-0190 is on credit hold (HOLD)", "accounts[0].credit_hold"),
    ("SKARVIK KND-0190 is Kiruna Gruvteknik AB", "accounts[0].name_display"),
    ("HALDEN 00417: credit_hold CLEAR", "accounts[1].credit_hold"),
    ("The customer is on hold", "accounts[0].on_hold"),
    ("No contact name is recorded", "accounts[0].contact_name"),
])
def test_valid_grounded_facts_stay_usable(claim, path):
    answer, problem = validate(json.dumps(fact(claim, path)), {"c1": RECORD})
    assert problem is None and answer is not None


@pytest.mark.parametrize("summary", [
    "KND-0190 is not on credit hold.",
    "The customer is clear to order.",
    "Kiruna Gruvteknik AB has 42 units reserved.",
    "I approved the order.",
    "The draft was created.",
])
def test_unsupported_critical_statements_in_the_summary_are_rejected(summary):
    result = {"c1": {"accounts": [RECORD["accounts"][0]]}}
    answer, problem = validate(json.dumps(final(summary)), result)
    assert answer is None and problem.startswith("GROUNDING_FAILED")


@pytest.mark.parametrize("summary", ["The hold is clear.", "KND-0190 is on credit hold.",
                                     "BW-1001 has 120 units available."])
def test_a_critical_summary_without_any_observation_is_rejected(summary):
    answer, _ = validate(json.dumps(final(summary)), {})
    assert answer is None


@pytest.mark.parametrize("summary", [
    "KND-0190 is on credit hold.",
    "Kiruna Gruvteknik AB (KND-0190) has credit hold HOLD, so it is not clear to order.",
    "I cannot approve this order; a person must do it in the console. Nothing was approved.",
    "No draft was created. Please state a whole quantity.",
    "It is unknown whether the account is on hold.",
    "Please state a whole number from 1 to 999.",
])
def test_supported_and_hedged_summaries_stay_usable(summary):
    result = {"c1": {"accounts": [RECORD["accounts"][0]]}}
    answer, problem = validate(json.dumps(final(summary)), result)
    assert problem is None and answer is not None


def test_conflicting_holds_are_compared_per_account():
    ok = "HALDEN 00417 is not on hold, while SKARVIK KND-0190 is on credit hold."
    swapped = "HALDEN 00417 is on credit hold, while SKARVIK KND-0190 is not on hold."
    assert validate(json.dumps(final(ok)), {"c1": RECORD})[0] is not None
    assert validate(json.dumps(final(swapped)), {"c1": RECORD})[0] is None
