"""Live-validation harness (two turns). Offline only: mocked transport, per-test ledger, loopback server."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from assistant_helpers import FINAL, REFUSAL, TEST_OPENAI_KEY, TOOL, USAGE, MockOpenAI, live_config

from lbf.assistant.budget import BudgetService
from lbf.assistant.providers.openai_provider import OpenAIProvider
from lbf.assistant.smoke_ledger import Ledger, LedgerError, init_ledger
from lbf.assistant.telemetry import Telemetry
from lbf.auth.service import AddressRateLimiter

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from evals import live_assistant as live  # noqa: E402

NOW = datetime.now(UTC)
COMMIT = "c" * 40
PRICES = {"input_price_per_mtok": Decimal("2.75"), "cached_input_price_per_mtok": Decimal("0.055"),
          "output_price_per_mtok": Decimal("5.50")}  # fixture values, not prices of the authorized model


def config(**kw: Any):
    return live_config(**{**PRICES, "price_source": "src", "price_observed_at": "2026-11-23", "timeout_s": 5.0, **kw})


def auth(**kw: Any) -> dict[str, Any]:
    base = {"run_id": "lv-test", "approved_by": "owner (test)", "approved_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=13)).isoformat(), "purpose": "live_validation", "provider": "openai",
            "model": "gpt-6-luna", "commit": COMMIT, "max_requests": 8, "max_output_tokens": 6000,
            "max_estimated_usd": "0.10", "max_request_cost_usd": "0.06", "max_concurrency": 1,
            "price_source_url": "src", "price_observed_at": "2026-11-23",
            **{k: str(v) for k, v in PRICES.items()}}
    base.update(kw)
    return base


def check(cfg=None, a=None, commit=COMMIT, clean=True):
    return live.refusal(cfg or config(), a if a is not None else auth(), commit=commit, clean=clean)


def test_complete_authorization_is_accepted():
    assert check() is None


@pytest.mark.parametrize("change", [
    {"purpose": "smoke"}, {"purpose": "evaluation"}, {"provider": "other"}, {"run_id": "../x"}, {"run_id": None},
    {"model": "other"}, {"commit": "d" * 40}, {"max_requests": 9}, {"max_requests": 0}, {"max_requests": "8"},
    {"max_requests": True}, {"max_output_tokens": 6001}, {"max_concurrency": 2}, {"max_estimated_usd": "0.11"},
    {"max_estimated_usd": "0"}, {"max_estimated_usd": "NaN"}, {"max_request_cost_usd": "0.07"},
    {"input_price_per_mtok": "1.00"}, {"output_price_per_mtok": "5.00"}, {"cached_input_price_per_mtok": "0.05"},
    {"price_source_url": "x"}, {"price_observed_at": "2026-01-01"}, {"expires_at": "2020-01-01T00:00:00Z"},
    {"expires_at": (NOW + timedelta(minutes=30)).isoformat()},
    {"approved_at": (NOW + timedelta(minutes=5)).isoformat()},
    {"expires_at": "garbage"}, {"expires_at": "2099-01-01T00:00:00"}])
def test_every_authorization_mismatch_refuses(change):
    assert check(a=auth(**change)) is not None


def test_configuration_commit_and_prices_fail_closed():
    assert check(commit="e" * 40) and check(clean=False) and live.refusal(config(), None, commit=COMMIT, clean=True)
    assert check(cfg=config(live_inference=False)) and check(cfg=config(key_present=False))
    zero = {"input_price_per_mtok": Decimal("0")}  # nothing would be reserved
    assert "positive" in (check(cfg=config(**zero), a=auth(input_price_per_mtok="0")) or "")


def test_o1a_ledgers_still_cannot_hold_more_than_three(tmp_path):
    init_ledger(tmp_path / "a" / "o1a.sqlite3")
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "a" / "o1a.sqlite3").bind(auth())
    with pytest.raises(LedgerError):
        init_ledger(tmp_path / "b" / "x.sqlite3", max_cap=9)
    init_ledger(tmp_path / "c" / "lv.sqlite3", max_cap=8)
    assert Ledger(tmp_path / "c" / "lv.sqlite3").bind(auth()) is None


# --------------------------------------------------------------------------------------------- execution (mocked)
@pytest.fixture(scope="module")
def eval_env():
    from evals.harness.env import EvalEnv

    env = EvalEnv().build()
    yield env
    env.close()


def http_driver(origin: str, out: Path) -> list[dict[str, Any]]:
    from evals.harness.env import PASSWORD

    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=30) as c:
        c.headers["X-CSRF-Token"] = c.post("/api/v1/auth/login", json={"username": live.USER, "password": PASSWORD}
                                           ).json()["csrf_token"]
        shown = []
        for q in live.QUESTIONS:
            r = c.post("/api/v1/assistant/turns", json={"text": q})
            assert r.status_code == 200, r.text
            shown.append({"question": q, "text": r.json()["answer"]["summary"]})
        return shown


def ans(summary: str, *facts: tuple[str, str, str]) -> dict[str, Any]:
    return {"summary": summary, "draft_operation_id": None, "warnings": [], "next_human_actions": ["none"],
            "facts": [{"claim": c, "source": {"tool_call_id": i, "field_path": f}} for c, i, f in facts]}


T1 = ans("Kiruna Gruvteknik AB is on credit hold.", ("The account is KND-0190", "c1", "accounts[0].native_id"),
         ("Credit status HOLD", "c1", "accounts[0].credit_hold"))
T2 = ans("It is located in Kiruna, Sweden (SE).", ("City: Kiruna", "c2", "account.city"),
         ("Country: SE", "c2", "account.country_code"))
LOOKUP1 = TOOL("search_customers", {"query": "Kiruna Gruvteknik"}, "c1")
LOOKUP2 = TOOL("get_customer_overview", {"backend_id": "skarvik", "native_id": "KND-0190"}, "c2")


def happy() -> list[Any]:
    return [LOOKUP1, FINAL(T1), LOOKUP2, FINAL(T2)]


def clear(env) -> None:
    from sqlalchemy import create_engine, text

    e = create_engine(env.settings.url("superuser"))
    with e.begin() as conn:
        conn.execute(text("TRUNCATE lbf.inference_budget_ledger, lbf.inference_calls"))
    e.dispose()


def run(env, tmp_path, mock, *, driver=http_driver, cfg=None, **a: Any):
    clear(env)
    env.svc.auth.limiter = AddressRateLimiter(limit=10_000)  # many test sign-ins from one address
    path = tmp_path / "l" / "ledger.sqlite3"
    if not path.exists():
        init_ledger(path, max_cap=8)
    ledger = Ledger(path)
    authorization = auth(**{"ledger_id": ledger.ledger_id, **a})
    ledger.close()

    def factory(e, c):
        return OpenAIProvider(c, budget=BudgetService(e.engine, c, e.clock), telemetry=Telemetry(e.engine, e.clock),
                              clock=e.clock, http_client=httpx2.Client(transport=httpx2.MockTransport(mock)),
                              transport_label="mock", api_key=TEST_OPENAI_KEY)
    return live.execute(cfg or config(), authorization, ledger_path=path, out_root=tmp_path / "out", driver=driver,
                        env=env, provider_factory=factory, static_dir=tmp_path)


def failed(summary) -> set[str]:
    return {k for k, v in summary["checks"].items() if not v}


@pytest.mark.db
def test_two_turns_pass_and_the_second_request_carries_the_first_conversation(eval_env, tmp_path, mock_openai):
    mock_openai.queue = happy()
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 0 and s["status"] == "MOCK PASS" and failed(s) == set(), failed(s)
    assert len(mock_openai.requests) == 4 == s["ledger"]["slots_reserved"] == s["ledger"]["responses_observed"]
    assert all(b["store"] is False and b["max_output_tokens"] == 1500 and len(b["tools"]) == 9
               for b in mock_openai.bodies())
    text = (tmp_path / "out" / "lv-test" / "summary.json").read_text()
    assert TEST_OPENAI_KEY not in text and "csrf" not in text.lower() and "lbf_session" not in text


@pytest.mark.db
def test_one_repair_is_allowed_and_still_validated(eval_env, tmp_path, mock_openai):
    bad = ans("x", ("The account is KND-0190", "wrong", "accounts[0].native_id"))
    mock_openai.queue = [LOOKUP1, FINAL(bad), FINAL(T1), LOOKUP2, FINAL(T2)]
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 0 and s["requests_attempted"] == 5


@pytest.mark.db
@pytest.mark.parametrize(("queue", "expected"), [
    ([LOOKUP1, FINAL(ans("x", ("KND-0190", "no", "a"))), FINAL(ans("x", ("KND-0190", "no", "a"))), LOOKUP2, FINAL(T2)],
     {"t1_not_fallback", "t1_validation_ok"}),
    ([LOOKUP1, FINAL(T1), FINAL(ans("It is in Kiruna, Sweden."))], {"t2_cited_facts", "t2_read_tools_only"}),
    # rejected by the application as well: the answer and its single repair both fail
    ([LOOKUP1, *[FINAL(ans("It is not on hold.", ("Account KND-0190", "c1", "accounts[0].native_id")))] * 2, LOOKUP2,
      FINAL(T2)], {"t1_not_fallback", "t1_credit_hold_correct"}),
    ([LOOKUP1, FINAL(T1), LOOKUP2, *[FINAL(ans("It is in Oslo, SE.", ("City: Oslo", "c2", "account.city")))] * 2],
     {"t2_not_fallback", "t2_validation_ok"}),
    ([LOOKUP1, FINAL(T1), REFUSAL()], {"t2_not_fallback"}),
    ([LOOKUP1, FINAL(T1), httpx2.Response(500, json={})], {"t2_not_fallback"})])
def test_failures_are_failures_and_are_not_retried(eval_env, tmp_path, mock_openai, queue, expected):
    mock_openai.queue = list(queue)
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 1 and s["status"] == "MOCK FAIL" and expected <= failed(s), failed(s)
    assert len(mock_openai.requests) == s["requests_attempted"] <= len(queue)


@pytest.mark.db
@pytest.mark.parametrize("tool", [("prepare_draft", {"backend_id": "halden"}), ("approve_operation", {})])
def test_a_tool_outside_the_read_set_stops_the_run_before_execution(eval_env, tmp_path, mock_openai, tool):
    mock_openai.queue = [TOOL(*tool)]
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 1 and s["stop"] == "FORBIDDEN_TOOL" and len(mock_openai.requests) == 1
    assert s["checks"]["workflow_unchanged"] and s["turns"][0]["tool_calls"] == []


@pytest.mark.db
def test_request_caps_hold_when_the_model_keeps_calling_tools(eval_env, tmp_path, mock_openai):
    mock_openai.queue = [TOOL("search_customers", {"query": "Kiruna"}, f"k{i}") for i in range(20)]
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 1 and len(mock_openai.requests) == 8 == s["ledger"]["slots_reserved"]  # 4 per turn, 8 in total
    mock2 = MockOpenAI()
    mock2.queue = [TOOL("search_customers", {"query": "Kiruna"}, f"k{i}") for i in range(20)]
    code, s = run(eval_env, tmp_path, mock2, run_id="lv-cap5", max_requests=5)
    assert len(mock2.requests) == 5


@pytest.mark.db
def test_output_token_total_is_enforced_before_the_request(eval_env, tmp_path, mock_openai):
    big = USAGE(100, 1500, 0)
    mock_openai.queue = [{**TOOL("search_customers", {"query": "Kiruna"}, f"k{i}"), "usage": big} for i in range(20)]
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 1 and len(mock_openai.requests) == 4 and s["stop"] == "OUTPUT_TOKEN_LIMIT"
    assert s["output_tokens_counted"] == 6000
    mock2 = MockOpenAI()  # unreported usage counts as the full per-request cap
    mock2.queue = [httpx2.ReadTimeout("slow")] * 9
    code, s = run(eval_env, tmp_path, mock2, run_id="lv-to", max_output_tokens=2000)
    assert len(mock2.requests) == 1 and s["output_tokens_counted"] == 1500 and s["stop"] == "OUTPUT_TOKEN_LIMIT"


@pytest.mark.db
def test_cost_total_and_request_ceiling_deny_before_sending(eval_env, tmp_path, mock_openai):
    mock_openai.queue = happy()
    code, s = run(eval_env, tmp_path, mock_openai, max_estimated_usd="0.02")
    assert code == 1 and mock_openai.requests == []
    code, s = run(eval_env, tmp_path, mock_openai, run_id="lv-req", max_request_cost_usd="0.005")
    assert code == 1 and mock_openai.requests == []
    dear = USAGE(20000, 100, 0)  # settles near 0.0555 per request; a third reservation cannot fit
    mock_openai.queue = [{**TOOL("search_customers", {"query": "Kiruna"}, f"k{i}"), "usage": dear} for i in range(9)]
    code, s = run(eval_env, tmp_path, mock_openai, run_id="lv-total")
    assert len(mock_openai.requests) == 2 or len(mock_openai.requests) == 1
    assert Decimal(s["estimate_usd"]) <= Decimal("0.12")
    assert s["checks"]["within_cost"] is (Decimal(s["estimate_usd"]) <= Decimal("0.10"))


@pytest.mark.db
def test_time_limit_refuses_locally(eval_env, tmp_path, mock_openai, monkeypatch):
    monkeypatch.setattr(live, "MAX_SECONDS", 1.0)  # shorter than one request timeout
    mock_openai.queue = happy()
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == 1 and s["stop"] == "TIME_LIMIT" and mock_openai.requests == []


@pytest.mark.db
def test_model_mismatch_stops(eval_env, tmp_path, mock_openai):
    mock_openai.queue = [{**LOOKUP1, "model": "another-model"}]
    code, s = run(eval_env, tmp_path, mock_openai)
    assert code == live.ABORT_EXIT and s["stop"] == "MODEL_MISMATCH" and len(mock_openai.requests) == 1
    assert s["status"] == "ABORTED_MODEL_MISMATCH"


@pytest.mark.db
def test_one_invocation_per_authorization_and_ledger_binding(eval_env, tmp_path, mock_openai):
    mock_openai.queue = happy()
    assert run(eval_env, tmp_path, mock_openai)[0] == 0
    again = MockOpenAI()
    code, s = run(eval_env, tmp_path, again)
    assert code == 2 and again.requests == []  # evidence exists
    (tmp_path / "out" / "lv-test").rename(tmp_path / "moved")
    code, s = run(eval_env, tmp_path, again)
    assert code == 2 and s["error"] == "AUTHORIZATION_CONSUMED" and again.requests == []
    code, s = run(eval_env, tmp_path, again, run_id="lv-x", ledger_id="other")
    assert code == 2 and s["error"] == "LEDGER_UNAVAILABLE" and again.requests == []
    code, s = live.execute(config(), auth(), ledger_path=tmp_path / "none.sqlite3", out_root=tmp_path / "o2",
                           env=eval_env, driver=http_driver)
    assert code == 2 and s["error"] == "LEDGER_UNAVAILABLE"


@pytest.mark.db
def test_a_driver_failure_is_a_failure_with_evidence(eval_env, tmp_path, mock_openai):
    def broken(origin, out):
        raise RuntimeError("browser died")

    code, s = run(eval_env, tmp_path, mock_openai, driver=broken)
    assert code == 1 and "browser died" in s["driver_error"] and mock_openai.requests == []
    assert (tmp_path / "out" / "lv-test" / "summary.json").is_file()


def test_the_command_refuses_without_authorization_or_execute_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LBF_EVAL_AUTHORIZATION", str(tmp_path / "missing.json"))
    assert live.main(["run", "--ledger", str(tmp_path / "l"), "--out", str(tmp_path)]) == 2
    a = tmp_path / "a.json"
    key = tmp_path / "k"
    key.write_text(TEST_OPENAI_KEY)
    a.write_text(json.dumps(auth(commit=live.commit_state()[0])))
    for k, v in {"LBF_EVAL_AUTHORIZATION": str(a), "LBF_ASSISTANT_PROVIDER": "openai", "LBF_LIVE_INFERENCE": "true",
                 "OPENAI_MODEL": "gpt-6-luna", "OPENAI_API_KEY_FILE": str(key), "LBF_OPENAI_PRICE_SOURCE": "src",
                 "LBF_OPENAI_PRICE_OBSERVED_AT": "2026-11-23",
                 **{f"LBF_OPENAI_{k.upper()}": str(v) for k, v in PRICES.items()}}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(live, "commit_state", lambda: (json.loads(a.read_text())["commit"], True))
    monkeypatch.setattr(live, "execute", lambda *a, **k: pytest.fail("must not execute"))
    monkeypatch.delenv("LBF_LIVE_VALIDATION_EXECUTE", raising=False)
    capsys.readouterr()
    assert live.main(["run", "--ledger", str(tmp_path / "l"), "--out", str(tmp_path)]) == 2
    assert "AUTHORIZED_NOT_EXECUTED" in capsys.readouterr().out
