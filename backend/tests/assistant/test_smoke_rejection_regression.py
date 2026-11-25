"""Live-smoke S2 regression (offline only).

Historical mechanism (``service.py`` before this change): S2 ran with ``max_requests_per_turn = 2`` (tool call +
final answer). When the final answer failed ``validate()``, the service queued its single repair, looped, found no
request left and fell back with ``LIMIT`` — discarding which validation branch rejected the answer, against the contract
(violation not corrected by the repair ⇒ fallback flagged ``GROUNDING_FAILED``).

Contract gap: the grounding check requires ``source.tool_call_id``, but the tool result the model reads never
contained that id (it only travelled as protocol metadata), and the ``field_path`` dialect was nowhere stated.

All model outputs below are hand-written fixtures, not the historical response (which was never persisted). Every
OpenAI call goes to the mocked ``httpx2`` transport; the session network guard stays active.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from assistant_helpers import FINAL, TOOL, live_config

from lbf.assistant import smoke
from lbf.assistant.answer import failure_code, output_schema, validate

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
import test_smoke_executor as sx  # noqa: E402  (module import: its tests are not re-collected here)

pytestmark = pytest.mark.db
SMOKE_LIMITS = {"max_retries": 0, "max_requests_per_turn": smoke.S2_TURN_REQUESTS}  # the historical S2 limits


def answer(*facts: tuple[str, str, str]) -> dict[str, Any]:
    return {"summary": "Two SKARVIK accounts look alike; they are separate accounts.", "draft_operation_id": None,
            "warnings": ["Possible duplicate; choose explicitly."], "next_human_actions": ["search_customers"],
            "facts": [{"claim": c, "source": {"tool_call_id": i, "field_path": f}} for c, i, f in facts]}


GROUNDED = answer(("SKARVIK KND-0150", "call_1", "accounts[0].native_id"),
                  ("SKARVIK KND-0151", "call_1", "accounts[1].native_id"))

# One plausible, semantically correct-looking answer per rejection branch of ``validate()``.
REJECTED = {
    "UNKNOWN_TOOL_CALL": answer(("SKARVIK KND-0150", "search_customers", "accounts[0].native_id")),
    "PATH_UNRESOLVED_JSONPATH": answer(("SKARVIK KND-0150", "call_1", "$.accounts[0].native_id")),
    "PATH_UNRESOLVED_POINTER": answer(("SKARVIK KND-0150", "call_1", "/accounts/0/native_id")),
    "NUMBER_UNGROUNDED": answer(("Duplicates KND-0150 and KND-0151", "call_1", "accounts[0].native_id")),
    "SCHEMA_INVALID_SHAPE": {"summary": "Göta Hamnservice AB, KND-0150"},
    "SCHEMA_INVALID_LENGTH": answer(("x" * 401, "call_1", "accounts[0].native_id")),
}
EXPECTED_CODE = {"UNKNOWN_TOOL_CALL": "UNKNOWN_TOOL_CALL", "PATH_UNRESOLVED_JSONPATH": "PATH_UNRESOLVED",
                 "PATH_UNRESOLVED_POINTER": "PATH_UNRESOLVED", "NUMBER_UNGROUNDED": "NUMBER_UNGROUNDED",
                 "SCHEMA_INVALID_SHAPE": "SCHEMA_INVALID", "SCHEMA_INVALID_LENGTH": "SCHEMA_INVALID"}


def p(h, name="daniel"):
    return h.api.wf.p(name)


def search(mock) -> None:
    mock.queue.insert(0, TOOL("search_customers", {"query": "Göta Hamnservice"}))


# ------------------------------------------------------------------------------------------- the reproduced mechanism
@pytest.mark.parametrize("branch", sorted(REJECTED))
def test_historical_limits_rejected_final_falls_back_grounding_failed_with_branch(h, mock_openai, branch):
    mock_openai.queue = [FINAL(REJECTED[branch])]
    search(mock_openai)
    before = h.counts()
    out = h.openai(mock_openai, live_config(**SMOKE_LIMITS)).run_turn(p(h), smoke.S2_PROMPT)
    assert len(mock_openai.requests) == 2  # tool call + final; no repair could be sent, none was
    assert out.fallback_used and out.fallback_reason == "GROUNDING_FAILED"  # no longer masked as LIMIT
    assert out.validation_failure == EXPECTED_CODE[branch]
    assert h.counts() == before


def test_valid_grounded_answer_passes_within_the_historical_limits(h, mock_openai):
    mock_openai.queue = [FINAL(GROUNDED)]
    search(mock_openai)
    out = h.openai(mock_openai, live_config(**SMOKE_LIMITS)).run_turn(p(h), smoke.S2_PROMPT)
    assert len(mock_openai.requests) == 2 and out.fallback_used is False and out.validation_failure is None
    assert [f.claim for f in out.answer.facts] == ["SKARVIK KND-0150", "SKARVIK KND-0151"]
    assert out.tool_calls == [{"name": "search_customers", "args_sha256": out.tool_calls[0]["args_sha256"],
                               "permitted": True, "result_sha256": out.tool_calls[0]["result_sha256"],
                               "call_id": "call_1", "outcome": "SUCCEEDED", "error_code": None,
                               "refused_reason": None}]


# ------------------------------------------------------------------------------------------- the citation contract
def test_tool_result_shown_to_the_model_names_its_tool_call_id(h, mock_openai):
    mock_openai.queue = [FINAL(GROUNDED)]
    search(mock_openai)
    h.openai(mock_openai, live_config(**SMOKE_LIMITS)).run_turn(p(h), smoke.S2_PROMPT)
    [output] = [i for i in mock_openai.bodies()[1]["input"] if i.get("type") == "function_call_output"]
    shown = json.loads(output["output"])
    assert output["call_id"] == "call_1" and shown["tool_call_id"] == "call_1"
    assert shown["accounts"][0]["native_id"] == "KND-0150"  # field paths are unchanged by the added id


def test_output_schema_states_the_citation_dialect():
    source = output_schema()["properties"]["facts"]["items"]["properties"]["source"]["properties"]
    assert "tool_call_id" in source["tool_call_id"]["description"]
    assert "accounts[0].native_id" in source["field_path"]["description"]
    assert "$" in source["field_path"]["description"]  # tells the model not to use a leading $ or /


def test_validator_is_not_weakened():
    payload = {"accounts": [{"native_id": "KND-0150"}, {"native_id": "KND-0151"}]}
    for branch, bad in REJECTED.items():
        ok, problem = validate(json.dumps(bad), {"call_1": payload})
        assert ok is None and failure_code(problem) == EXPECTED_CODE[branch], branch
    ok, problem = validate(json.dumps(GROUNDED), {"call_1": payload})
    assert ok is not None and problem is None and failure_code(None) is None


# ------------------------------------------------------------------------------------------- repair path and limits
def test_correction_path_repairs_when_the_allowance_permits(h, mock_openai):
    mock_openai.queue = [FINAL(REJECTED["NUMBER_UNGROUNDED"]), FINAL(GROUNDED)]
    search(mock_openai)
    out = h.openai(mock_openai, live_config(max_retries=0, max_requests_per_turn=3)).run_turn(p(h), smoke.S2_PROMPT)
    assert len(mock_openai.requests) == 3 and out.fallback_used is False and out.validation_failure is None
    repair = mock_openai.bodies()[2]["input"][-1]
    assert repair["role"] == "user" and "rejected (GROUNDING_FAILED" in repair["content"]


def test_failed_repair_falls_back_grounding_failed(h, mock_openai):
    mock_openai.queue = [FINAL(REJECTED["UNKNOWN_TOOL_CALL"]), FINAL(REJECTED["PATH_UNRESOLVED_JSONPATH"])]
    search(mock_openai)
    out = h.openai(mock_openai, live_config(max_retries=0, max_requests_per_turn=3)).run_turn(p(h), smoke.S2_PROMPT)
    assert len(mock_openai.requests) == 3
    assert out.fallback_reason == "GROUNDING_FAILED" and out.validation_failure == "PATH_UNRESOLVED"


def test_exhaustion_by_tool_calls_is_limit_and_sends_nothing_more(h, mock_openai):
    mock_openai.queue = [TOOL("search_customers", {"query": "Göta"}, call_id=f"c{i}") for i in range(5)]
    out = h.openai(mock_openai, live_config(**SMOKE_LIMITS)).run_turn(p(h), smoke.S2_PROMPT)
    assert len(mock_openai.requests) == 2 and out.fallback_reason == "LIMIT" and out.validation_failure is None


def test_unauthorized_tool_call_is_refused_and_never_executed(h, mock_openai):
    mock_openai.queue = [TOOL("approve_operation", {"operation_id": "x"}), FINAL(answer())]
    before = h.counts()
    out = h.openai(mock_openai, live_config(**SMOKE_LIMITS)).run_turn(p(h), smoke.S2_PROMPT)
    assert len(mock_openai.requests) == 2
    assert out.refused_calls and out.refused_calls[0]["reason"] == "UNKNOWN_TOOL"
    assert h.counts() == before and h.events("ASSISTANT_TOOL_REFUSED")


# ------------------------------------------------------------------------------------------- smoke harness evidence
@pytest.fixture(scope="module")
def eval_env():
    from evals.harness.env import EvalEnv

    env = EvalEnv().build()
    yield env
    env.close()


def test_smoke_s2_evidence_names_the_branch_without_model_text(eval_env, tmp_path):
    mock = sx.MockOpenAI()
    bad = answer(("ZZMARKER duplicates KND-0150 and KND-0151", "call_1", "accounts[0].native_id"))
    mock.queue = [FINAL(sx.ANSWER), TOOL("search_customers", {"query": "Göta Hamnservice"}), FINAL(bad)]
    code, summary = sx.run(eval_env, tmp_path, mock, run_id="o1a-s2-regression-fail")
    s2 = summary["scenarios"]["S2_function_tool_round_trip"]
    assert code == 1 and len(mock.requests) == 3 and summary["requests_attempted"] == 3
    assert (s2["fallback_reason"], s2["validation_failure"]) == ("GROUNDING_FAILED", "NUMBER_UNGROUNDED")
    text = (tmp_path / "o1a-s2-regression-fail" / "summary.json").read_text()
    assert "ZZMARKER" not in text and "KND-0151" not in text  # still no model output in the evidence


def test_smoke_passes_with_a_grounded_fact_bearing_answer(eval_env, tmp_path):
    mock = sx.MockOpenAI()
    mock.queue = [FINAL(sx.ANSWER), TOOL("search_customers", {"query": "Göta Hamnservice"}), FINAL(GROUNDED)]
    code, summary = sx.run(eval_env, tmp_path, mock, run_id="o1a-s2-regression-pass")
    s2 = summary["scenarios"]["S2_function_tool_round_trip"]
    assert code == 0 and summary["status"] == "PASS" and len(mock.requests) == 3
    assert s2["passed"] is True and s2["validation_failure"] is None
