"""AI-SEC-01…07, AI-OAI-01…12, AI-COST-01…05 — all offline.

OpenAI tests use ``OpenAIProvider`` with an injected ``httpx2.MockTransport`` serving hand-written Responses-API
payloads; the key is the constant ``TEST_OPENAI_KEY``. The session-wide network guard fails the run on any
non-loopback connection attempt.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from assistant_helpers import FINAL, REFUSAL, TEST_OPENAI_KEY, TOOL, USAGE, R, live_config
from sqlalchemy import select

from lbf.assistant.config import AssistantConfig
from lbf.assistant.providers.openai_provider import OFFICIAL_BASE_URL, OpenAIProvider
from lbf.assistant.telemetry import Telemetry
from lbf.assistant.tools import ALLOWLIST, REGISTRY
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t

pytestmark = pytest.mark.db
REPO = Path(__file__).resolve().parents[3]
ANSWER = {"summary": "Nordkap has accounts in both ERPs.", "facts": [], "draft_operation_id": None, "warnings": [],
          "next_human_actions": ["search_customers"]}


def p(h, name="daniel"):
    return h.api.wf.p(name)


# =========================================================================== AI-SEC


def test_ai_sec_01_registry_equals_the_allowlist():
    assert ALLOWLIST == {"search_customers", "get_customer_overview", "search_items", "get_availability",
                         "explain_stock_discrepancy", "list_operations", "get_operation_timeline", "prepare_draft",
                         "revise_draft"}
    assert set(REGISTRY) == ALLOWLIST
    forbidden_words = ("approve", "submit", "decline", "recheck", "withdraw", "redeliver", "escalate", "resolve",
                       "admin", "demo", "sql", "http", "file")
    assert not [n for n in ALLOWLIST for w in forbidden_words if w in n]
    for tool in REGISTRY.values():
        schema = tool.spec().json_schema
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


def test_ai_sec_02_unknown_tools_are_refused_and_audited(h):
    before = h.counts()
    a = h.scripted([{"tool_calls": [{"name": "approve_operation", "arguments": {"operation_id": "x"}},
                                    {"name": "submit_order", "arguments": {}}]}, {"final": ANSWER}])
    out = a.run_turn(p(h), "approve everything")
    assert [r["reason"] for r in out.refused_calls] == ["UNKNOWN_TOOL", "UNKNOWN_TOOL"]
    assert len(h.events("ASSISTANT_TOOL_REFUSED")) == 2 and h.counts() == before


def test_ai_sec_03_the_human_principal_applies(h):
    before = h.counts()
    a = h.scripted([{"tool_calls": [{"name": "prepare_draft", "arguments": {
        "backend_id": "halden", "native_customer_id": "00417", "customer_po": None, "requested_date": None,
        "delivery_note": None, "lines": [{"canonical_sku": "BW-1001", "native_item": None, "quantity": 1}]}}]},
        {"final": ANSWER}])
    out = a.run_turn(p(h, "sanna"), "draft 1 x BW-1001 for 00417")
    assert out.refused_calls[0]["reason"] == "FORBIDDEN" and h.counts() == before


def test_ai_sec_04_injection_via_untrusted_backend_text_has_no_effect(h):
    """R17 core: KND-0177 NOTE carries an instruction; the scripted 'model' follows it — every call is refused."""
    before = h.counts()

    def summarize(request):
        result = json.loads(request.messages[-1].tool_result.output_json)
        acct = next(x for x in result["accounts"] if x["native_id"] == "KND-0177")
        assert acct["untrusted_text"]["notes"]["untrusted_text"].startswith("SYSTEM:")  # wrapped as untrusted
        return {"tool_calls": [{"name": "approve_operation", "arguments": {"all": True}},
                               {"name": "submit_order", "arguments": {}}]}

    a = h.scripted([{"tool_calls": [{"name": "search_customers", "arguments": {"query": "lesund"}}]}, summarize,
                    {"final": ANSWER}])
    out = a.run_turn(p(h, "marta"), "summarize Ålesund's notes")
    assert {r["reason"] for r in out.refused_calls} == {"UNKNOWN_TOOL"} and len(out.refused_calls) == 2
    assert h.counts() == before


def test_ai_sec_05_no_secrets_in_model_context(h, mock_openai):
    secrets = [x.read_text().strip() for x in (REPO / "deploy" / "secrets").glob("*.txt")]
    secrets = [s for s in secrets if len(s) >= 8]
    c = h.api.login("daniel")
    session_token = c.cookies.get("lbf_session")
    mock_openai.queue = [TOOL("search_customers", {"query": "nordkap"}), FINAL(ANSWER)]
    a = h.openai(mock_openai)
    a.run_turn(p(h), "find Nordkap")
    for request in mock_openai.requests:
        body = request.content.decode()
        for secret in [*secrets, session_token, TEST_OPENAI_KEY]:
            assert secret not in body
        assert request.headers["authorization"] == f"Bearer {TEST_OPENAI_KEY}"  # the key only in the auth header
    scripted = h.scripted([{"final": ANSWER}])
    scripted.run_turn(p(h), "hello")
    text = json.dumps([m.__dict__ for r in scripted.provider.requests for m in r.messages], default=str)
    assert not [s for s in secrets if s in text]


def test_ai_sec_06_sod2_assistant_draft_cannot_be_approved_by_its_invoker(h):
    a = h.scripted([{"tool_calls": [{"name": "prepare_draft", "arguments": {
        "backend_id": "halden", "native_customer_id": "00417", "customer_po": None, "requested_date": None,
        "delivery_note": None, "lines": [{"canonical_sku": "BW-1001", "native_item": None, "quantity": 2}]}}]},
        {"final": ANSWER}])
    a.run_turn(p(h, "daniel"), "draft 2 x BW-1001 for 00417")
    with h.api.wf.engine.connect() as conn:
        op = conn.execute(select(t.operations)).one()
    assert op.origin == "ASSISTANT" and op.state == "DRAFT"  # never submitted by the assistant
    h.api.wf.submit(op.id)
    with pytest.raises(DomainError) as sod:
        h.api.wf.approve(op.id, by="daniel")
    assert sod.value.code is ErrorCode.SOD_VIOLATION


def test_ai_sec_07_ungrounded_answer_falls_back(h):
    bad = {**ANSWER, "facts": [{"claim": "Nordkap has 999 accounts",
                                "source": {"tool_call_id": "call-1", "field_path": "accounts"}}]}
    a = h.scripted([{"tool_calls": [{"name": "search_customers", "arguments": {"query": "nordkap"}}]},
                    {"final": bad}, {"final": bad}])
    out = a.run_turn(p(h), "how many Nordkap accounts?")
    assert out.fallback_used and out.fallback_reason == "GROUNDING_FAILED"
    good = {**ANSWER, "facts": [{"claim": "HALDEN account 00417", "source": {"tool_call_id": "call-1",
                                                                             "field_path": "accounts[0].native_id"}}]}
    ok = h.scripted([{"tool_calls": [{"name": "search_customers", "arguments": {"query": "nordkap"}}]},
                     {"final": good}]).run_turn(p(h), "Nordkap?")
    assert ok.fallback_used is False and ok.answer.facts[0].claim == "HALDEN account 00417"


def test_disabled_provider_routes_to_the_deterministic_router(h):
    from lbf.assistant.providers.disabled import DisabledProvider
    from lbf.assistant.service import AssistantService

    a = AssistantService(h.api.svc, DisabledProvider(), AssistantConfig())
    out = a.run_turn(p(h), "find customer alder")
    assert out.fallback_used and out.fallback_reason == "DISABLED"
    assert {f.claim.split()[1] for f in out.answer.facts} >= {"10233", "10871"}
    assert out.answer.warnings  # ambiguity is flagged, never merged


# =========================================================================== AI-OAI


def test_ai_oai_01_request_and_response_mapping(h, mock_openai):
    mock_openai.queue = [TOOL("search_customers", {"query": "nordkap"}),
                         FINAL({**ANSWER, "facts": [{"claim": "SKARVIK KND-0081", "source": {
                             "tool_call_id": "call_1", "field_path": "accounts[1].native_id"}}]})]
    out = h.openai(mock_openai).run_turn(p(h), "find Nordkap")
    assert out.fallback_used is False and out.answer.facts[0].claim == "SKARVIK KND-0081"
    first, second = mock_openai.bodies()
    assert str(mock_openai.requests[0].url) == OFFICIAL_BASE_URL + "/responses"
    assert first["model"] == "gpt-6-luna" and first["store"] is False and first["parallel_tool_calls"] is False
    assert first["max_output_tokens"] == 500
    assert first["text"]["format"]["type"] == "json_schema" and first["text"]["format"]["strict"] is True
    assert {tl["type"] for tl in first["tools"]} == {"function"} and all(tl["strict"] for tl in first["tools"])
    assert {tl["name"] for tl in first["tools"]} == ALLOWLIST
    assert "previous_response_id" not in first  # client-side conversation state only
    kinds = [i.get("type", "message") for i in second["input"]]
    assert kinds == ["message", "function_call", "function_call_output"]
    mock_openai.queue = [REFUSAL()]
    assert h.openai(mock_openai).run_turn(p(h), "x").fallback_reason == "REFUSED"
    mock_openai.queue = [R([], status="incomplete")]
    assert h.openai(mock_openai).run_turn(p(h), "y").fallback_reason == "INCOMPLETE"


def test_ai_oai_02_timeout_no_retry_worst_case_settlement(h, mock_openai):
    mock_openai.queue = [httpx2.ReadTimeout("slow"), httpx2.ReadTimeout("slow")]
    out = h.openai(mock_openai).run_turn(p(h), "find customer nordkap")
    assert out.fallback_reason == "TIMEOUT" and len(mock_openai.requests) == 1
    [call] = h.calls()
    assert (call.status, call.error_category, call.cost_basis) == ("FAILED", "TIMEOUT", "WORST_CASE_RESERVATION")
    assert call.estimated_cost_usd == call.reserved_usd


def test_ai_oai_03_rate_limit_one_capped_retry_then_fallback(h, mock_openai):
    limited = httpx2.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "30"})
    mock_openai.queue = [limited, limited, limited]
    out = h.openai(mock_openai).run_turn(p(h), "find customer nordkap")
    assert out.fallback_reason == "RATE_LIMITED" and len(mock_openai.requests) == 2  # 1 + LBF_OPENAI_MAX_RETRIES
    assert h.sleeps == [10.0]  # Retry-After 30 s capped at LBF_OPENAI_RETRY_MAX_WAIT_SEC


@pytest.mark.parametrize(("status", "retried"), [(500, True), (502, True), (503, True), (504, True), (400, False),
                                                 (401, False), (403, False), (404, False), (422, False)])
def test_ai_oai_04_retry_bounds_and_no_sdk_retries(h, mock_openai, status, retried):
    mock_openai.queue = [httpx2.Response(status, json={"error": {"message": "x"}})] * 4
    h.openai(mock_openai).run_turn(p(h), "find customer nordkap")
    assert len(mock_openai.requests) == (2 if retried else 1)  # SDK built-in retries are 0


@pytest.mark.parametrize("payload", [
    httpx2.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
    R([{"type": "mystery_item", "id": "x"}]),
    R([{"type": "message", "id": "m", "role": "assistant", "status": "completed", "content": []}]),
])
def test_ai_oai_05_malformed_responses_fall_back(h, mock_openai, payload):
    mock_openai.queue = [payload]
    out = h.openai(mock_openai).run_turn(p(h), "find customer nordkap")
    assert out.fallback_used and out.fallback_reason == "MALFORMED_RESPONSE"


def test_ai_oai_06_schema_invalid_one_repair_then_fallback(h, mock_openai):
    mock_openai.queue = [FINAL({"summary": "x"}), FINAL("not even json")]
    out = h.openai(mock_openai).run_turn(p(h), "find customer nordkap")
    assert len(mock_openai.requests) == 2 and out.fallback_reason == "GROUNDING_FAILED"


def test_ai_oai_07_malformed_arguments_and_unknown_tools_refused(h, mock_openai):
    before = h.counts()
    mock_openai.queue = [TOOL("search_customers", "{bad json"), TOOL("approve_operation", {"id": 1}, "call_2"),
                         TOOL("prepare_draft", {"backend_id": "halden"}, "call_3"), FINAL(ANSWER)]
    out = h.openai(mock_openai).run_turn(p(h), "x")
    assert [r["reason"] for r in out.refused_calls] == ["MALFORMED_ARGUMENTS", "UNKNOWN_TOOL", "MALFORMED_ARGUMENTS"]
    assert h.counts() == before and len(h.events("ASSISTANT_TOOL_REFUSED")) == 3


def test_ai_oai_08_concurrency_limit(h, mock_openai):
    in_flight, peak, lock, gate = [0], [0], threading.Lock(), threading.Event()

    def slow(_request):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        gate.wait(3)
        with lock:
            in_flight[0] -= 1
        return httpx2.Response(200, json=FINAL(ANSWER))

    mock_openai.queue = [slow] * 5
    a = h.openai(mock_openai, live_config(max_concurrency=2))
    threads = [threading.Thread(target=a.run_turn, args=(p(h, n), "q")) for n in
               ("daniel", "priya", "marta", "rafael", "sanna")]
    for th in threads:
        th.start()
    time.sleep(1.0)
    gate.set()
    for th in threads:
        th.join(15)
    assert peak[0] == 2


def test_ai_oai_09_no_hosted_tools_no_custom_endpoint(h, mock_openai, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    mock_openai.queue = [FINAL(ANSWER)]
    a = h.openai(mock_openai)
    a.run_turn(p(h), "x")
    assert mock_openai.requests[0].url.host == "api.openai.com"
    assert {tl["type"] for tl in mock_openai.bodies()[0]["tools"]} == {"function"}
    assert "base_url" not in AssistantConfig.__dataclass_fields__


def test_ai_oai_10_test_key_never_leaks(h, mock_openai, caplog):
    caplog.set_level(logging.DEBUG)
    mock_openai.queue = [httpx2.Response(401, json={"error": {"message": f"bad key {TEST_OPENAI_KEY}"}}),
                         TOOL("search_customers", {"query": "nordkap"}), FINAL(ANSWER)]
    a = h.openai(mock_openai)
    out1 = a.run_turn(p(h), "x")
    out2 = a.run_turn(p(h), "y")
    c = h.api.as_("daniel")
    status = c.get("/api/v1/auth/session").text
    with h.api.wf.engine.connect() as conn:
        dumps = json.dumps([dict(r._mapping) for r in conn.execute(select(t.audit_events)).all()], default=str)
        dumps += json.dumps([dict(r._mapping) for r in conn.execute(select(t.assistant_turns)).all()], default=str)
    dumps += json.dumps([dict(r._mapping) for r in h.calls()], default=str)
    dumps += out1.model_dump_json() if hasattr(out1, "model_dump_json") else json.dumps(out1.answer.model_dump(
        mode="json")) + json.dumps(out2.answer.model_dump(mode="json"))
    for haystack in (dumps, caplog.text, status):
        assert TEST_OPENAI_KEY not in haystack


@pytest.mark.parametrize("missing", ["provider", "live", "key", "model", "price", "source"])
def test_ai_oai_11_fail_closed_configuration(h, mock_openai, missing):
    overrides = {"provider": {"provider": "scripted"}, "live": {"live_inference": False},
                 "key": {"key_present": False}, "model": {"model": "gpt-6.1-terra"},
                 "price": {"output_price_per_mtok": None}, "source": {"price_source": None}}[missing]
    config = live_config(**overrides)
    provider = OpenAIProvider(config, budget=None if missing == "key" else object(),  # type: ignore[arg-type]
                              telemetry=Telemetry(None, h.api.svc.clock), clock=h.api.svc.clock,
                              http_client=httpx2.Client(transport=httpx2.MockTransport(mock_openai)),
                              api_key=None if missing == "key" else TEST_OPENAI_KEY)
    assert provider.ready is False and provider.problems
    from lbf.assistant.service import AssistantService

    out = AssistantService(h.api.svc, provider, config).run_turn(p(h), "find customer nordkap")
    assert out.fallback_reason == "CONFIG_REJECTED" and mock_openai.requests == []


def test_ai_oai_11_config_rejected_is_audited_and_readyz_reports_misconfigured(h, monkeypatch):
    monkeypatch.setenv("LBF_ASSISTANT_PROVIDER", "openai")
    monkeypatch.setenv("LBF_LIVE_INFERENCE", "true")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", "/nonexistent")
    from lbf.assistant.factory import build_assistant

    a = build_assistant(h.api.svc)
    assert a.provider.name == "disabled" and h.api.svc.extras["assistant_status"] == "misconfigured"
    [ev] = h.events("ASSISTANT_CONFIG_REJECTED")
    assert "OPENAI_API_KEY_FILE" in ev.payload["missing"] and "LBF_OPENAI_PRICE_SOURCE" in ev.payload["missing"]
    assert "OPENAI_MODEL" not in ev.payload["missing"]  # the approved default applies; nothing else is defaulted
    assert h.api.anon().get("/readyz").json()["assistant"] == "misconfigured"


def test_ai_oai_12_network_guard_blocks_non_loopback():
    import socket

    NON_LOOPBACK_ATTEMPTS = socket.socket.connect.__globals__["NON_LOOPBACK_ATTEMPTS"]

    before = len(NON_LOOPBACK_ATTEMPTS)
    s = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect(("203.0.113.10", 443))  # TEST-NET-3, never routed
    finally:
        s.close()
        assert len(NON_LOOPBACK_ATTEMPTS) == before + 1
        del NON_LOOPBACK_ATTEMPTS[before:]  # the deliberate probe must not fail the session


# =========================================================================== AI-COST


def test_ai_cost_01_request_ceiling_denies_before_sending(h, mock_openai):
    config = live_config(input_price_per_mtok=Decimal("100000"), max_request_cost_usd=Decimal("0.01"))
    out = h.openai(mock_openai, config).run_turn(p(h), "find customer nordkap")
    assert out.fallback_reason == "BUDGET_DENIED" and mock_openai.requests == []
    assert h.calls()[0].status == "BUDGET_DENIED" and h.ledger() == []


def test_ai_cost_02_daily_budget_exhaustion_audited_once_and_resets_at_utc_midnight(h, mock_openai):
    clock = h.api.wf.clock
    # worst-case reservation ≈ 0.012 USD; each answer is reported at 0.009 USD ⇒ two turns fit, the third is denied
    config = live_config(daily_budget_usd=Decimal("0.025"), max_turn_cost_usd=Decimal("1"))
    mock_openai.queue = [FINAL(ANSWER, usage=USAGE(5000, 1000, 0))] * 6
    a = h.openai(mock_openai, config)
    outs = [a.run_turn(p(h), "q") for _ in range(4)]
    sent = len(mock_openai.requests)
    assert [o.fallback_reason for o in outs] == [None, None, "BUDGET_DENIED", "BUDGET_DENIED"] and sent == 2
    assert outs[-1].notice == ("AI assistant budget for today is exhausted; deterministic commands remain "
                               "available")
    assert len(h.events("ASSISTANT_BUDGET_EXHAUSTED")) == 1
    clock.now = (clock.now + timedelta(days=1)).replace(hour=0, minute=0, second=1)
    out = a.run_turn(p(h), "q")
    assert out.fallback_used is False and len(mock_openai.requests) == sent + 1


def test_ai_cost_02_turn_budget(h, mock_openai):
    config = live_config(max_turn_cost_usd=Decimal("0.03"), daily_budget_usd=Decimal("5"))
    heavy = R([{"type": "function_call", "id": "fc", "call_id": "call_1", "name": "search_items",
                "arguments": json.dumps({"query": None, "category": None}), "status": "completed"}],
              usage=USAGE(5000, 1000, 0))  # 0.009 USD each as reported
    mock_openai.queue = [heavy] * 6
    out = h.openai(mock_openai, config).run_turn(p(h), "q")
    assert out.fallback_reason == "BUDGET_DENIED" and 1 <= len(mock_openai.requests) < 6
    assert h.calls()[-1].status == "BUDGET_DENIED"


def test_ai_cost_04_settlement_and_ledger_equals_telemetry(h, mock_openai):
    mock_openai.queue = [TOOL("search_customers", {"query": "nordkap"}),
                         FINAL(ANSWER, usage=False), httpx2.ReadTimeout("x")]
    a = h.openai(mock_openai)
    a.run_turn(p(h), "q")
    calls = h.calls()
    assert [c.cost_basis for c in calls] == ["REPORTED_USAGE", "WORST_CASE_RESERVATION"]
    reported = calls[0]
    expected = (Decimal(100) * Decimal("1.00") + Decimal(20) * Decimal("0.50") + Decimal(30) * Decimal("4.00")) \
        / Decimal(1_000_000)
    assert reported.estimated_cost_usd == expected.quantize(Decimal("0.000001"))
    settles = [r for r in h.ledger() if r.entry == "SETTLE" and r.scope == "DAILY"]
    assert sum(r.amount_usd for r in settles) == sum(c.estimated_cost_usd for c in calls)


def test_api_turn_endpoint_and_status(h):
    from lbf.assistant.providers.disabled import DisabledProvider
    from lbf.assistant.service import AssistantService

    h.api.svc.extras["assistant"] = AssistantService(h.api.svc, DisabledProvider(), AssistantConfig())
    c = h.api.as_("daniel")
    assert c.get("/api/v1/assistant/status").json()["mode"] == "disabled"
    r = c.post("/api/v1/assistant/turns", json={"text": "stock BW-1001"})
    assert r.status_code == 200 and r.json()["fallback_reason"] == "DISABLED"
    assert h.api.as_("helena").post("/api/v1/assistant/turns", json={"text": "x"}).status_code == 403
    req = c.build_request("POST", "/api/v1/assistant/turns", json={"text": "x"})
    req.headers.pop("X-CSRF-Token")
    assert c.send(req).status_code == 403


def test_user_turn_rate_limit(h):
    a = h.scripted([{"final": ANSWER}] * 25)
    for _ in range(20):
        a.run_turn(p(h), "q")
    with pytest.raises(DomainError) as limited:
        a.run_turn(p(h), "q")
    assert limited.value.code is ErrorCode.RATE_LIMITED


def test_clock_is_aware():
    assert datetime.now(UTC).tzinfo is UTC
