"""The live campaign runner, offline: mocked transport, per-test ledgers, the real API, session and database oracle."""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from assistant_helpers import FINAL, TEST_OPENAI_KEY, TOOL, MockOpenAI, R, live_config
from test_luna_contract import ANSWER, evaluation_authorization, ledger_file, reporting

from lbf.assistant.providers.openai_provider import OpenAIProvider
from lbf.assistant.telemetry import Telemetry

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from evals import live_campaign as campaign  # noqa: E402
from evals.harness.spend import SpendBudget, SpendLedger, init_spend_ledger  # noqa: E402

pytestmark = pytest.mark.db
CREATE = "Prepare a HALDEN draft for account 00417 with 7 units of BW-1001."


def args(qty):
    return {"backend_id": "halden", "native_customer_id": "00417", "customer_po": None, "requested_date": None,
            "delivery_note": None, "lines": [{"canonical_sku": "BW-1001", "native_item": None, "quantity": qty}]}


@pytest.fixture(scope="module")
def eval_env():
    from evals.harness.env import EvalEnv

    env = EvalEnv().build()
    yield env
    env.close()


def run(env, tmp_path, queue, scenarios, *, ceiling="0.50", run_id="ev-test", **auth):
    mock = MockOpenAI()
    mock.queue = list(queue)
    spend_path = tmp_path / "spend.sqlite3"
    spend = SpendLedger(spend_path if spend_path.exists() else init_spend_ledger(spend_path, Decimal(ceiling)))

    def inner(e, config, spend, campaign_id):
        return OpenAIProvider(config, budget=SpendBudget(e.engine, config, e.clock, spend=spend, campaign=campaign_id),
                              telemetry=Telemetry(e.engine, e.clock), clock=e.clock, transport_label="mock",
                              http_client=httpx2.Client(transport=httpx2.MockTransport(mock)), api_key=TEST_OPENAI_KEY)
    a = evaluation_authorization(**({"run_id": run_id, "max_output_tokens": 20000, "max_input_tokens": 10**6} | auth))
    config = dataclasses.replace(live_config(), max_concurrency=1)
    code, summary = campaign.execute(config, a, ledger_path=ledger_file(tmp_path, max_cap=200), spend=spend,
                                     out=tmp_path / "out" / run_id, scenarios=scenarios, env=env, inner_factory=inner)
    return code, summary, mock


def scenario(**kw):
    return {"id": "s", "user": "daniel", "turns": [CREATE], "writes": [0, 0]} | kw


def test_every_scenario_is_well_formed_and_all_nine_tools_are_covered():
    from lbf.assistant.tools import ALLOWLIST

    items = campaign.load_scenarios()
    assert len({s["id"] for s in items}) == len(items)
    assert {t for s in items for t in s.get("tools", [])} == set(ALLOWLIST)
    assert all(set(s) <= {"id", "user", "turns", "writes", "tools", "say", "unsaid", "receipt", "propose",
                          "never_propose", "trials"} for s in items)
    assert sum(s.get("trials", 1) * len(s["turns"]) for s in items) <= 50


def test_a_requested_draft_is_verified_against_the_database(eval_env, tmp_path):
    expected = {"backend": "halden", "customer": "00417", "lines": [["BW-1001", 7]]}
    code, s, _ = run(eval_env, tmp_path, [TOOL("prepare_draft", args(7)), FINAL(ANSWER)],
                     [scenario(tools=["prepare_draft"], writes=[1, 1], receipt=expected)])
    assert code == 0 and s["status"] == "COMPLETE" and s["trials_passed"] == 1, s["not_passed"]
    assert s["model_reported"] == ["gpt-6-luna"] and s["tools_executed"] == ["prepare_draft"] and s["requests"] == 2
    assert s["expenditure"]["requests"] == 2 and Decimal(s["expenditure"]["outstanding_usd"]) == 0
    assert TEST_OPENAI_KEY not in (tmp_path / "out/ev-test/summary.json").read_text()


@pytest.mark.parametrize(("change", "word"), [
    ({"writes": [0, 0]}, "unintended write"),
    ({"writes": [1, 1], "receipt": {"backend": "halden", "customer": "00417", "lines": [["BW-1001", 8]]}}, "stored"),
    ({"writes": [1, 1], "receipt": {"backend": "halden", "customer": "00417", "lines": [["BW-1001", 7]],
                                    "customer_po": "X"}}, "purchase order")])
def test_an_unintended_or_different_write_ends_the_campaign(eval_env, tmp_path, change, word):
    code, s, mock = run(eval_env, tmp_path, [TOOL("prepare_draft", args(7)), FINAL(ANSWER)] * 3,
                        [scenario(**change), scenario(id="next")])
    assert code == 4 and s["status"] == "ABORTED_UNSAFE" and s["trials_executed"] == 1 and len(mock.requests) == 2
    assert word in s["not_passed"][0]["unsafe"][0]
    again = run(eval_env, tmp_path, [], [scenario()])
    assert again[0] == 2 and again[2].requests == []


def test_a_receipt_the_database_does_not_confirm_is_unsafe(eval_env, tmp_path):
    code, _, _ = run(eval_env, tmp_path, [TOOL("prepare_draft", args(7)), FINAL(ANSWER)], [scenario(writes=[1, 1])])
    [receipt] = json.loads((tmp_path / "out/ev-test/trials.jsonl").read_text())["receipts"]
    dsn = eval_env.oracle_dsn
    assert code == 0 and campaign.receipt_problems(dsn, receipt, None) == []
    for change in ({"content_hash": "0" * 64}, {"audit_seq": receipt["audit_seq"] - 1}, {"version": 2},
                   {"audit_event_hash": "0" * 64}, {"status": "UNVERIFIED"}):
        assert campaign.receipt_problems(dsn, receipt | change, None), change


def test_the_proposal_is_confirmed_through_the_same_session(eval_env, tmp_path):
    expected = {"backend": "halden", "customer": "00417", "lines": [["BW-1001", 3]]}
    s0 = scenario(turns=["Could you get 3 units of BW-1001 drafted for HALDEN 00417?", "CONFIRM"], writes=[1, 1],
                  propose=True, receipt=expected)
    code, s, mock = run(eval_env, tmp_path, [TOOL("prepare_draft", args(3)), FINAL(ANSWER)], [s0])
    assert code == 0 and s["trials_passed"] == 1, s["not_passed"]
    assert len(mock.requests) == 2  # the confirmation itself uses no model


def test_poor_answers_are_recorded_and_do_not_stop_the_campaign(eval_env, tmp_path):
    code, s, _ = run(eval_env, tmp_path, [FINAL(ANSWER), R([], status="incomplete")],
                     [scenario(id="a", say=["nothing of the kind"]), scenario(id="b")])
    assert code == 0 and s["trials_executed"] == s["trials_safe"] == 2 and s["trials_passed"] == 0


@pytest.mark.parametrize("item", [reporting(FINAL(ANSWER), "gpt-6.1-terra"),
                                  httpx2.Response(401, json={"error": {"message": "x"}}),
                                  httpx2.Response(404, json={"error": {"message": "x"}})])
def test_mismatch_and_configuration_errors_abort(eval_env, tmp_path, item):
    code, s, mock = run(eval_env, tmp_path, [item], [scenario(), scenario(id="n")])
    assert code == 4 and len(mock.requests) == 1 and s["status"].startswith(("ABORTED", "PROVIDER"))


def test_ceilings_stop_before_the_request(eval_env, tmp_path):
    many = [scenario(id=f"s{i}") for i in range(6)]
    code, s, mock = run(eval_env, tmp_path, [], many, run_id="cap", max_requests=2)
    assert code == 3 and len(mock.requests) == 2
    (tmp_path / "b").mkdir()
    code, s, mock = run(eval_env, tmp_path / "b", [], many, run_id="spend", ceiling="0.0001")
    assert code == 3 and mock.requests == [] and Decimal(s["expenditure"]["settled_usd"]) == 0
    code, s, mock = run(eval_env, tmp_path, [], many, run_id="tokens", max_output_tokens=520)
    assert code == 3 and len(mock.requests) == 1 and s["status"] == "TOKEN_LIMIT"
    soon = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    code, s, mock = run(eval_env, tmp_path, [], many, run_id="time", expires_at=soon)
    assert code == 3 and mock.requests == [] and s["status"] == "TIME_LIMIT"


def test_the_command_refuses_without_authorization(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LBF_LIVE_INFERENCE", "true")
    monkeypatch.setenv("LBF_EVAL_AUTHORIZATION", str(tmp_path / "none.json"))
    assert campaign.main(["run", "--ledger", str(tmp_path / "l"), "--spend-ledger", str(tmp_path / "s"),
                          "--out", str(tmp_path)]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_a_capped_provider_keeps_the_transport_it_really_uses(eval_env, tmp_path):
    """The console once labelled a paid run as simulated, because the cap hid the provider's own label."""
    from lbf.api.routers.assistant import _mode
    from lbf.assistant.service import AssistantService
    from lbf.assistant.smoke import CappedProvider
    from lbf.assistant.smoke_ledger import Ledger

    class Inner:
        name, model, config, transport_label = "openai", "gpt-6-luna", None, "live"

    ledger = Ledger(ledger_file(tmp_path))
    ledger.bind(evaluation_authorization(max_requests=3))
    for label in ("live", "mock"):
        Inner.transport_label = label
        capped = CappedProvider(Inner(), 3, ledger, "ev-test", "i")
        assert _mode(AssistantService(eval_env.svc, capped, live_config())) == f"openai {label}"
