"""Live-smoke executor. Offline only: the OpenAI
provider runs on the mocked ``httpx2`` transport and the session network guard stays active. No test here can reach the
OpenAI API. Every execution uses a per-test durable ledger under ``tmp_path`` (never the repository ledger)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from assistant_helpers import FINAL, TEST_OPENAI_KEY, TOOL, MockOpenAI, live_config

from lbf.assistant import smoke
from lbf.assistant.budget import BudgetService
from lbf.assistant.providers.openai_provider import OpenAIProvider
from lbf.assistant.smoke_ledger import Ledger, LedgerError, init_ledger
from lbf.assistant.telemetry import Telemetry

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

ANSWER = {"summary": "ok", "facts": [], "draft_operation_id": None, "warnings": [], "next_human_actions": ["none"]}
RUN_ID = "o1a-test-run"


def authorization(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "approved_by": "owner (test)", "approved_at": "2026-11-22T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
        "purpose": "smoke", "provider": "openai", "model": "gpt-6-luna", "run_id": RUN_ID, "max_requests": 3,
        "max_trials": 1, "max_input_tokens": 20000, "max_output_tokens": 500, "max_estimated_usd": "0.50",
        "max_request_cost_usd": "0.20", "max_concurrency": 2, "input_price_per_mtok": "1.00",
        "cached_input_price_per_mtok": "0.50", "output_price_per_mtok": "4.00",
        "price_source_url": "test fixture (not a real price)", "price_observed_at": "2026-11-22"}
    base.update(overrides)
    return base


# ------------------------------------------------------------------------------------------- B-3 cross-checks (unit)
def test_matching_authorization_and_configuration_is_accepted():
    assert smoke.execution_refusal(live_config(), authorization(), RUN_ID) is None


@pytest.mark.parametrize(("auth_overrides", "config_overrides", "fragment"), [
    ({}, {}, None),
    ({"run_id": None}, {}, "run_id"),
    ({"run_id": "../escape"}, {}, "run_id"),
    ({"run_id": "other-run"}, {}, "LBF_O1A_EXECUTE"),
    ({"max_requests": 0}, {}, "max_requests"),
    ({"max_requests": 4}, {}, "max_requests"),
    ({"max_requests": "3"}, {}, "max_requests"),
    ({"max_requests": True}, {}, "max_requests"),
    ({"max_estimated_usd": "0"}, {}, "max_estimated_usd"),
    ({"max_estimated_usd": "NaN"}, {}, "max_estimated_usd"),
    ({"max_estimated_usd": None}, {}, "max_estimated_usd"),
    ({"input_price_per_mtok": "1.01"}, {}, "input_price_per_mtok"),
    ({"cached_input_price_per_mtok": "0.40"}, {}, "cached_input_price_per_mtok"),
    ({"output_price_per_mtok": "5"}, {}, "output_price_per_mtok"),
    ({"price_source_url": "https://elsewhere.invalid"}, {}, "price_source_url"),
    ({"price_observed_at": "2026-01-01"}, {}, "price_observed_at"),
    ({"max_output_tokens": 499}, {}, "max_output_tokens"),
    ({"max_request_cost_usd": "0.10"}, {}, "max_request_cost_usd"),
    ({"max_concurrency": 1}, {}, "max_concurrency"),
    ({"model": "other-model"}, {}, "model"),
])
def test_b3_authorization_must_match_the_configuration(auth_overrides, config_overrides, fragment):
    reason = smoke.execution_refusal(live_config(**config_overrides), authorization(**auth_overrides), RUN_ID)
    if fragment is None:
        assert reason is None
    else:
        assert reason is not None and fragment in reason


def test_b3_equal_prices_in_a_different_notation_are_accepted():
    assert smoke.execution_refusal(live_config(), authorization(input_price_per_mtok=1, output_price_per_mtok="4.0"),
                                   RUN_ID) is None


# ------------------------------------------------------------------------------------------- B-1 entrypoint (unit)
@pytest.fixture
def smoke_env(tmp_path, monkeypatch):
    key = tmp_path / "key.txt"
    key.write_text(TEST_OPENAI_KEY)
    auth_path = tmp_path / "AUTHORIZATION.json"
    auth_path.write_text(json.dumps(authorization()))
    for name, value in {"LBF_ASSISTANT_PROVIDER": "openai", "LBF_LIVE_INFERENCE": "true",
                        "OPENAI_MODEL": "gpt-6-luna", "OPENAI_API_KEY_FILE": str(key),
                        "LBF_OPENAI_MAX_OUTPUT_TOKENS": "500", "LBF_OPENAI_INPUT_PRICE_PER_MTOK": "1.00",
                        "LBF_OPENAI_CACHED_INPUT_PRICE_PER_MTOK": "0.50", "LBF_OPENAI_OUTPUT_PRICE_PER_MTOK": "4.00",
                        "LBF_OPENAI_PRICE_SOURCE": "test fixture (not a real price)",
                        "LBF_OPENAI_PRICE_OBSERVED_AT": "2026-11-22", "LBF_EVAL_AUTHORIZATION": str(auth_path)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LBF_O1A_EXECUTE", raising=False)
    calls: list[Any] = []
    monkeypatch.setattr(smoke, "execute", lambda config, auth, **kw: calls.append((config, auth)) or (0, {}))
    return calls


def _main(capsys) -> tuple[int, dict[str, Any]]:
    code = smoke.main()
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_b1_without_the_execute_flag_the_authorized_smoke_still_refuses(smoke_env, capsys):
    code, out = _main(capsys)
    assert code == 2 and out["status"] == "REFUSED" and "LBF_O1A_EXECUTE" in out["reason"]
    assert smoke_env == []


def test_b1_execute_flag_must_name_the_authorized_run(smoke_env, capsys, monkeypatch):
    monkeypatch.setenv("LBF_O1A_EXECUTE", "some-other-run")
    code, out = _main(capsys)
    assert code == 2 and out["error"] == "CONFIG_REJECTED" and smoke_env == []


def test_b1_mismatched_configuration_refuses_before_any_execution(smoke_env, capsys, monkeypatch):
    monkeypatch.setenv("LBF_O1A_EXECUTE", RUN_ID)
    monkeypatch.setenv("LBF_OPENAI_OUTPUT_PRICE_PER_MTOK", "4.50")
    code, out = _main(capsys)
    assert code == 2 and out["error"] == "CONFIG_REJECTED" and "output_price_per_mtok" in out["reason"]
    assert smoke_env == []


def test_b1_live_disabled_refuses_even_with_the_execute_flag(smoke_env, capsys, monkeypatch):
    monkeypatch.setenv("LBF_O1A_EXECUTE", RUN_ID)
    monkeypatch.setenv("LBF_LIVE_INFERENCE", "false")
    code, out = _main(capsys)
    assert code == 2 and out["error"] == "CONFIG_REJECTED" and smoke_env == []


def test_b1_complete_authorization_reaches_the_executor(smoke_env, capsys, monkeypatch):
    monkeypatch.setenv("LBF_O1A_EXECUTE", RUN_ID)
    code, _ = _main(capsys)
    assert code == 0 and len(smoke_env) == 1 and smoke_env[0][1]["run_id"] == RUN_ID


def test_f5_the_authorization_file_is_read_once_and_that_snapshot_is_executed(smoke_env, capsys, monkeypatch,
                                                                               tmp_path):
    monkeypatch.setenv("LBF_O1A_EXECUTE", RUN_ID)
    reads: list[Any] = []
    real_read, real_refusal = smoke.read_authorization, smoke.refusal

    def counting_read():
        reads.append(1)
        return real_read()

    def refusal_then_tamper(config=None, snapshot=None):
        reason = real_refusal(config, snapshot)
        (tmp_path / "AUTHORIZATION.json").write_text(json.dumps(authorization(max_requests=3, model="swapped")))
        return reason

    monkeypatch.setattr(smoke, "read_authorization", counting_read)
    monkeypatch.setattr(smoke, "refusal", refusal_then_tamper)
    code, _ = _main(capsys)
    assert code == 0 and len(reads) == 1 and smoke_env[0][1]["model"] == "gpt-6-luna"


def test_init_ledger_command_creates_once_and_never_replaces(capsys, monkeypatch, tmp_path):
    path = tmp_path / "o1a-ledger" / "ledger.sqlite3"
    monkeypatch.setattr(smoke, "LEDGER_PATH", path)
    assert smoke.main(["init-ledger"]) == 0 and path.is_file()
    assert json.loads(capsys.readouterr().out)["status"] == "INITIALIZED"
    assert smoke.main(["init-ledger"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "LEDGER_INIT_REFUSED"
    assert smoke.main(["something-else"]) == 2


# ------------------------------------------------------------------------------------------- execution (db, mocked)
@pytest.fixture(scope="module")
def eval_env():
    from evals.harness.env import EvalEnv

    env = EvalEnv().build()
    yield env
    env.close()


def factory(mock: MockOpenAI):
    def build(env: Any, config: Any, eval_budget: Decimal) -> OpenAIProvider:
        return OpenAIProvider(config, budget=BudgetService(env.engine, config, env.clock, eval_budget),
                              telemetry=Telemetry(env.engine, env.clock), clock=env.clock,
                              http_client=httpx2.Client(transport=httpx2.MockTransport(mock)), transport_label="mock",
                              api_key=TEST_OPENAI_KEY)
    return build


def ledger_file(tmp_path: Path) -> Path:
    path = tmp_path / "o1a-ledger" / "ledger.sqlite3"
    if not path.exists():
        init_ledger(path)
    return path


def run(eval_env, tmp_path, mock, *, run_id: str = RUN_ID, config: Any = None, out_root: Path | None = None,
        ledger_path: Path | None = None, build: Any = None, **auth_overrides: Any):
    return smoke.execute(config or live_config(), authorization(run_id=run_id, **auth_overrides), env=eval_env,
                         provider_factory=build or factory(mock), out_root=out_root or tmp_path,
                         ledger_path=ledger_path or ledger_file(tmp_path))


def three_requests() -> MockOpenAI:
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER), TOOL("search_customers", {"query": "Göta"}), FINAL(ANSWER)]
    return mock


@pytest.mark.db
def test_happy_path_three_requests_contract_checks_and_hashed_evidence(eval_env, tmp_path):
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER), TOOL("search_customers", {"query": "Göta"}), FINAL(ANSWER)]
    code, summary = run(eval_env, tmp_path, mock)
    assert code == 0 and summary["status"] == "PASS", summary
    assert len(mock.requests) == 3 and summary["requests_attempted"] == 3
    assert all(b["store"] is False and b["model"] == "gpt-6-luna" for b in mock.bodies())
    assert summary["scenarios"]["S1_connectivity_structured_output"]["passed"] is True
    s2 = summary["scenarios"]["S2_function_tool_round_trip"]
    assert s2["passed"] is True and s2["tool_calls"] == [{"name": "search_customers", "permitted": True,
                                                          "refused_reason": None}]
    assert summary["workflow_state_unchanged"] is True
    assert Decimal(summary["estimate"]["estimated_cost_usd"]) > 0
    evidence = tmp_path / RUN_ID / "summary.json"
    sidecar = (tmp_path / RUN_ID / "summary.json.sha256").read_text().split()
    assert sidecar == [hashlib.sha256(evidence.read_bytes()).hexdigest(), "summary.json"]
    text = evidence.read_text()
    assert TEST_OPENAI_KEY not in text and "Göta" not in text  # no key, no prompt content


@pytest.mark.db
def test_b2_hard_cap_holds_when_the_model_keeps_calling_tools(eval_env, tmp_path):
    mock = MockOpenAI()
    mock.queue = [TOOL("search_customers", {"query": "Göta"}, call_id=f"c{i}") for i in range(10)]
    code, summary = run(eval_env, tmp_path, mock, run_id="o1a-cap-tools")
    assert len(mock.requests) == 3 and summary["requests_attempted"] == 3 and code == 1
    assert summary["workflow_state_unchanged"] is True


@pytest.mark.db
@pytest.mark.parametrize("cap", [1, 2])
def test_b2_lower_authorized_cap_is_never_exceeded(eval_env, tmp_path, cap):
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER), TOOL("search_customers", {"query": "Göta"}), FINAL(ANSWER)]
    code, summary = run(eval_env, tmp_path, mock, run_id=f"o1a-cap-{cap}", max_requests=cap)
    assert len(mock.requests) == cap and summary["requests_attempted"] == cap and code == 1
    assert any(r["outcome"] == "CAP_DENIED" for r in summary["requests"])


@pytest.mark.db
def test_b2_cap_counts_the_wrapper_not_the_turn_loop(eval_env, tmp_path):
    mock = MockOpenAI()
    ledger = Ledger(ledger_file(tmp_path))
    assert ledger.bind(authorization(run_id="o1a-wrapper")) is None
    capped = smoke.CappedProvider(factory(mock)(eval_env, smoke.smoke_config(live_config()), Decimal("1")), 3,
                                  ledger, "o1a-wrapper", ledger.start_invocation("o1a-wrapper"))
    request = smoke.s1_request("o1a-wrapper")
    turns = [capped.run(request, max_attempts=6) for _ in range(5)]
    assert len(mock.requests) == 3 and capped.attempted == 3
    assert [t.error_category for t in turns[3:]] == [smoke.ErrorCategory.BUDGET_DENIED] * 2


@pytest.mark.db
def test_b2_retries_are_forced_off_and_a_timeout_consumes_a_slot(eval_env, tmp_path):
    mock = MockOpenAI()
    limited = httpx2.Response(429, json={"error": {"message": "slow"}}, headers={"retry-after": "1"})
    mock.queue = [httpx2.ReadTimeout("slow"), limited, limited, limited]
    code, summary = run(eval_env, tmp_path, mock, run_id="o1a-timeout", config=live_config(max_retries=2))
    assert code == 1 and len(mock.requests) == 2  # S1 timeout (1) + S2 one 429, never retried; cap not exceeded
    first = summary["requests"][0]
    assert first["error_category"] == "TIMEOUT" and first["outcome"] == "SENT_OR_UNKNOWN"
    assert summary["requests_attempted"] == 2


@pytest.mark.db
def test_eval_budget_denies_before_sending(eval_env, tmp_path):
    mock = MockOpenAI()
    code, summary = run(eval_env, tmp_path, mock, run_id="o1a-budget", max_estimated_usd="0.000001")
    assert code == 1 and len(mock.requests) == 0
    assert {r["error_category"] for r in summary["requests"]} == {"BUDGET_DENIED"}


@pytest.mark.db
def test_existing_evidence_is_never_overwritten(eval_env, tmp_path):
    (tmp_path / "o1a-exists").mkdir()
    mock = MockOpenAI()
    code, summary = run(eval_env, tmp_path, mock, run_id="o1a-exists")
    assert code == 2 and summary["status"] == "REFUSED" and len(mock.requests) == 0


# ------------------------------------------------------------------------------------------- durable consumption
@pytest.mark.db
def test_f1_r1_r3_r10_sequential_replay_with_a_new_evidence_root_is_refused(eval_env, tmp_path):
    first, second = three_requests(), three_requests()
    code, summary = run(eval_env, tmp_path, first, out_root=tmp_path / "root-a")
    assert code == 0 and len(first.requests) == 3
    assert summary["authorization_ledger"]["slots_reserved"] == 3
    code, refused = run(eval_env, tmp_path, second, out_root=tmp_path / "root-b")
    assert code == 2 and refused["error"] == "AUTHORIZATION_EXHAUSTED" and len(second.requests) == 0
    ledger = Ledger(ledger_file(tmp_path))
    assert ledger.consumed(RUN_ID) == 3 and ledger.summary(RUN_ID)["delegations_started"] == 3


@pytest.mark.db
def test_f1_r4_deleting_the_evidence_does_not_restore_capacity(eval_env, tmp_path):
    code, _ = run(eval_env, tmp_path, three_requests())
    assert code == 0
    shutil.rmtree(tmp_path / RUN_ID)
    again = three_requests()
    code, refused = run(eval_env, tmp_path, again)
    assert code == 2 and refused["error"] == "AUTHORIZATION_EXHAUSTED" and again.requests == []


@pytest.mark.db
def test_f1_r7_a_timeout_consumes_its_slot_across_invocations(eval_env, tmp_path):
    mock = MockOpenAI()
    limited = httpx2.Response(429, json={"error": {"message": "slow"}}, headers={"retry-after": "1"})
    mock.queue = [httpx2.ReadTimeout("slow"), limited, limited]
    code, summary = run(eval_env, tmp_path, mock, run_id="o1a-r7", out_root=tmp_path / "a")
    assert code == 1 and len(mock.requests) == 2 and summary["authorization_ledger"]["sent_or_unknown"] >= 1
    later = three_requests()
    code, summary = run(eval_env, tmp_path, later, run_id="o1a-r7", out_root=tmp_path / "b")
    assert len(later.requests) == 1  # only the one remaining slot
    assert Ledger(ledger_file(tmp_path)).consumed("o1a-r7") == 3
    assert any(r["outcome"] == "CAP_DENIED" for r in summary["requests"])


@pytest.mark.db
def test_f1_r5_rebuilding_lbf_eval_does_not_reset_consumption(eval_env, tmp_path):
    code, _ = run(eval_env, tmp_path, three_requests(), run_id="o1a-r5", out_root=tmp_path / "a")
    assert code == 0
    eval_env.engine.dispose()
    eval_env.build()  # DROP DATABASE lbf_eval WITH (FORCE) + fresh migration, as every real invocation does
    again = three_requests()
    code, refused = run(eval_env, tmp_path, again, run_id="o1a-r5", out_root=tmp_path / "b")
    assert code == 2 and refused["error"] == "AUTHORIZATION_EXHAUSTED" and again.requests == []


@pytest.mark.db
@pytest.mark.parametrize("change", [{"max_requests": 3}, {"expires_at": "2098-01-01T00:00:00Z"},
                                    {"max_estimated_usd": "0.40"}, {"approved_by": "someone else"}])
def test_f1_r8_a_changed_authorization_under_the_same_run_id_is_an_identity_conflict(eval_env, tmp_path, change):
    first = MockOpenAI()
    first.queue = [httpx2.ReadTimeout("slow")]
    run(eval_env, tmp_path, first, run_id="o1a-r8", out_root=tmp_path / "a", max_requests=1)
    assert len(first.requests) == 1
    again = three_requests()
    code, refused = run(eval_env, tmp_path, again, run_id="o1a-r8", out_root=tmp_path / "b",
                        **({"max_requests": 1} | change))
    assert code == 2 and refused["error"] == "AUTHORIZATION_IDENTITY_CONFLICT" and again.requests == []
    assert Ledger(ledger_file(tmp_path)).consumed("o1a-r8") == 1


@pytest.mark.db
def test_f1_r11_a_distinct_authorization_has_independent_capacity(eval_env, tmp_path):
    assert run(eval_env, tmp_path, three_requests(), run_id="o1a-r11-a")[0] == 0
    other = three_requests()
    code, summary = run(eval_env, tmp_path, other, run_id="o1a-r11-b")
    assert code == 0 and len(other.requests) == 3 and summary["authorization_ledger"]["slots_reserved"] == 3


@pytest.mark.db
def test_f1_r9_missing_ledger_refuses_before_any_side_effect(eval_env, tmp_path):
    built: list[Any] = []
    mock = MockOpenAI()
    code, refused = run(eval_env, tmp_path, mock, ledger_path=tmp_path / "absent" / "ledger.sqlite3",
                        build=lambda *a: built.append(a))
    assert code == 2 and refused["error"] == "LEDGER_UNAVAILABLE" and built == [] and mock.requests == []
    assert not (tmp_path / "absent").exists()  # never created implicitly


@pytest.mark.db
def test_f1_r9_corrupt_ledger_refuses(eval_env, tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a database" * 100)
    mock = MockOpenAI()
    code, refused = run(eval_env, tmp_path, mock, ledger_path=path)
    assert code == 2 and refused["error"] == "LEDGER_UNAVAILABLE" and mock.requests == []


@pytest.mark.db
def test_f1_r9_ledger_failure_mid_run_stops_all_further_delegation(eval_env, tmp_path, monkeypatch):
    real_reserve = Ledger.reserve
    calls: list[int] = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise LedgerError("disk full (simulated)")
        return real_reserve(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "reserve", flaky)
    mock = three_requests()
    code, summary = run(eval_env, tmp_path, mock, run_id="o1a-r9-mid")
    assert code == 1 and len(mock.requests) == 1 and summary["status"] == "FAIL"
    assert summary["authorization_ledger"]["ledger_failure"] == "disk full (simulated)"
    assert [r["outcome"] for r in summary["requests"]][1:] and all(
        r["outcome"] == "LEDGER_UNAVAILABLE" for r in summary["requests"][1:])
    assert len(calls) == 2  # after the failure the wrapper stops asking: no fallback counter


@pytest.mark.db
def test_f1_r12_ledger_and_evidence_hold_no_key_prompt_or_model_output(eval_env, tmp_path):
    marker = "model-output-marker-qxzv"
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER | {"summary": marker}), TOOL("search_customers", {"query": "Göta"}),
                  FINAL(ANSWER | {"summary": marker})]
    code, _ = run(eval_env, tmp_path, mock, run_id="o1a-r12")
    assert code == 0
    ledger_bytes = ledger_file(tmp_path).read_bytes()
    evidence = (tmp_path / "o1a-r12" / "summary.json").read_bytes()
    for blob in (ledger_bytes, evidence):
        for secret in (TEST_OPENAI_KEY, "Göta", smoke.S1_PROMPT[:40], marker):
            assert secret.encode() not in blob


@pytest.mark.db
def test_f1_summary_binds_the_ledger_identity(eval_env, tmp_path):
    code, summary = run(eval_env, tmp_path, three_requests(), run_id="o1a-bind")
    view = summary["authorization_ledger"]
    ledger = Ledger(ledger_file(tmp_path))
    assert code == 0 and view["ledger_id"] == ledger.ledger_id
    assert view["auth_sha256"] == ledger.bound_digest("o1a-bind") and view["invocations_unfinished"] == 1
    assert ledger.summary("o1a-bind")["invocations_unfinished"] == 0  # INVOCATION_FINISHED after the evidence
    assert [r["ledger_slot"] for r in summary["requests"]] == [1, 2, 3]
