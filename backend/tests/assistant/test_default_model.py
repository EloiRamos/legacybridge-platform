"""The authorized OpenAI model: opt-in, not overridable, reported exactly. Mocked transport, no call."""

from __future__ import annotations

import pytest
from assistant_helpers import FINAL, TOOL, live_config
from test_write_integrity_reproduction import args, p

from lbf.assistant.config import AUTHORIZED_OPENAI_MODEL, AssistantConfig
from lbf.assistant.factory import build_assistant
from lbf.assistant.tools import ALLOWLIST

pytestmark = pytest.mark.db
ANSWER = {"summary": "ok", "facts": [], "draft_operation_id": None, "warnings": [], "next_human_actions": ["none"]}


@pytest.fixture
def env(monkeypatch):
    for name in ("OPENAI_MODEL", "LBF_ASSISTANT_PROVIDER", "LBF_LIVE_INFERENCE", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", "/nonexistent")
    return monkeypatch


def test_the_default_is_luna_and_only_for_the_openai_provider(env):
    assert AUTHORIZED_OPENAI_MODEL == "gpt-6-luna"
    off = AssistantConfig.from_env()
    assert (off.provider, off.live_inference, off.model) == ("disabled", False, None)
    env.setenv("LBF_ASSISTANT_PROVIDER", "openai")
    on = AssistantConfig.from_env()
    assert (on.model, on.live_inference) == ("gpt-6-luna", False)
    for empty in ("", "  "):
        env.setenv("OPENAI_MODEL", empty)
        assert AssistantConfig.from_env().model == "gpt-6-luna"
    env.setenv("OPENAI_MODEL", "gpt-5.4-mini-2026-03-17")
    assert "OPENAI_MODEL" in AssistantConfig.from_env().live_problems()  # another model is rejected, not an override


def test_a_default_model_enables_nothing(env, h):
    """Every other element is still required: no client exists, and the deterministic router answers."""
    env.setenv("LBF_ASSISTANT_PROVIDER", "openai")
    a = build_assistant(h.api.svc)
    missing = a.config.live_problems()
    assert a.provider.name == "disabled" and "OPENAI_MODEL" not in missing
    assert {"LBF_LIVE_INFERENCE=true", "OPENAI_API_KEY_FILE", "LBF_OPENAI_INPUT_PRICE_PER_MTOK",
            "LBF_OPENAI_PRICE_SOURCE"} <= set(missing)


def test_the_request_contract_and_the_exact_model_telemetry(h, mock_openai):
    first = TOOL("prepare_draft", args(3), "call_1")
    last = FINAL(ANSWER)
    for body in (first, last):
        body["model"] = "gpt-6-luna-2026-11-01"  # fictional: a dated form the validation grammar accepts
    first["output"].insert(0, {"type": "reasoning", "id": "rs_1", "summary": []})
    mock_openai.queue = [first, last]
    out = h.openai(mock_openai, live_config(model=AUTHORIZED_OPENAI_MODEL)).run_turn(
        p(h), "Prepare a HALDEN draft for account 00417: 3 x BW-1001.")
    assert out.fallback_used is False and out.receipts[0].status == "STORED"
    for body in mock_openai.bodies():
        assert body["model"] == "gpt-6-luna" and body["store"] is False and body["parallel_tool_calls"] is False
        assert body["max_output_tokens"] == 500 and not {"temperature", "reasoning", "previous_response_id"} & set(body)
        assert {x["name"] for x in body["tools"]} == ALLOWLIST and len(body["tools"]) == 9
        schemas = [x["parameters"] for x in body["tools"]] + [body["text"]["format"]["schema"]]
        assert all(x["strict"] for x in body["tools"]) and body["text"]["format"]["strict"] is True
        for schema in schemas:
            assert_strict(schema)
    assert [i.get("type", "message") for i in mock_openai.bodies()[1]["input"]] == [
        "message", "function_call", "function_call_output"]
    assert {(c.model_configured, c.model_reported, c.transport) for c in h.calls()} == {
        ("gpt-6-luna", "gpt-6-luna-2026-11-01", "mock")}


def assert_strict(node):
    if isinstance(node, dict):
        assert "$ref" not in node and "default" not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and set(node["required"]) == set(node.get("properties", {}))
        for v in node.values():
            assert_strict(v)
    elif isinstance(node, list):
        for v in node:
            assert_strict(v)
