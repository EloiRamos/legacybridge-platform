"""The model authorization contract: one authorized model, enforced in the provider, fail-closed on what is reported.

Acceptance criteria AC-01 … AC-13 and AC-16 of the model rule. Offline only: mocked transport,
synthetic key, session network guard active. "Zero requests" means no transport call, no reservation, no ledger slot."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from assistant_helpers import FINAL, REFUSAL, TEST_OPENAI_KEY, TOOL, MockOpenAI, R, live_config
from test_smoke_executor import authorization as smoke_authorization
from test_write_integrity_reproduction import args, p

from lbf.assistant import config as config_module
from lbf.assistant import model_guard, smoke
from lbf.assistant.budget import BudgetService
from lbf.assistant.config import AUTHORIZED_OPENAI_MODEL, AssistantConfig
from lbf.assistant.factory import build_assistant
from lbf.assistant.provider import ErrorCategory
from lbf.assistant.providers import openai_provider
from lbf.assistant.providers.openai_provider import OpenAIProvider
from lbf.assistant.smoke_ledger import Ledger, init_ledger
from lbf.assistant.telemetry import Telemetry

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from evals import live_assistant  # noqa: E402
from evals import run as evals  # noqa: E402
from evals.harness import authorization as eval_authorization  # noqa: E402
from evals.harness.spend import SpendBudget, SpendLedger, init_spend_ledger  # noqa: E402

pytestmark = pytest.mark.db
LUNA = "gpt-6-luna"
ANSWER = {"summary": "ok", "facts": [], "draft_operation_id": None, "warnings": [], "next_human_actions": ["none"]}
UNAUTHORIZED = ["gpt-6.1-terra", "gpt-5.4-mini-2026-03-17", "gpt-6.2-sol", "gpt-test-model", "GPT-6-LUNA",
                "gpt-6-luna-2026-11-01", "gpt-6-luna-mini", "gpt-6-lun", "ft:gpt-6-luna:x", "gpt-6-luna,gpt-6.1-terra",
                " gpt-6-luna", "gpt-6-luna "]
UNEXPECTED = [None, "", 7, True, ["gpt-6-luna"], {"id": "gpt-6-luna"}, "gpt-6-luna-mini", "gpt-6-luna2",
              "gpt-6-luna-latest", "gpt-6.1-luna", "GPT-6-LUNA", "gpt-6-luna-2026-02-30", "gpt-6-luna-0000-01-01",
              "gpt-6.1-terra", "gpt-6.2-sol", "gpt-5.4-mini-2026-03-17", "gpt-6-luna\n", " gpt-6-luna",
              "gpt-6-luna-2026-11-01\n", "gpt-6-luna-２０２６-11-01", "gpt-6-luna-2026-11-01-x", "x\x00y", "\ud800"]
READY = {"LBF_ASSISTANT_PROVIDER": "openai", "LBF_LIVE_INFERENCE": "true", "LBF_OPENAI_INPUT_PRICE_PER_MTOK": "1.00",
         "LBF_OPENAI_CACHED_INPUT_PRICE_PER_MTOK": "0.50", "LBF_OPENAI_OUTPUT_PRICE_PER_MTOK": "4.00",
         "LBF_OPENAI_PRICE_SOURCE": "test fixture (not a real price)", "LBF_OPENAI_PRICE_OBSERVED_AT": "2026-11-22",
         "LBF_OPENAI_MAX_OUTPUT_TOKENS": "500"}


@pytest.fixture
def ready_env(monkeypatch, tmp_path):
    for name in ("OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    key = tmp_path / "key.txt"
    key.write_text(TEST_OPENAI_KEY)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key))
    for name, value in READY.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def provider(h, mock, config=None, **kw):
    config = config or live_config()
    svc = h.api.svc
    return OpenAIProvider(config, budget=kw.pop("budget", None) or BudgetService(svc.engine, config, svc.clock),
                          telemetry=Telemetry(svc.engine, svc.clock), clock=svc.clock,
                          http_client=httpx2.Client(transport=httpx2.MockTransport(mock)), transport_label="mock",
                          sleep=h.sleeps.append, api_key=TEST_OPENAI_KEY, **kw)


def ledger_file(tmp_path, **kw):
    path = tmp_path / "ledger" / "l.sqlite3"
    if not path.exists():
        init_ledger(path, **kw)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reporting(body, model):
    body = json.loads(json.dumps(body))
    body["model"] = model
    return httpx2.Response(200, content=json.dumps(body).encode(), headers={"x-request-id": "req_test_1",
                                                                            "content-type": "application/json"})


# ---------------------------------------------------------------------------------------- AC-01, AC-11, AC-12
@pytest.mark.parametrize("value", [None, "", "   ", LUNA, f"  {LUNA}\t\r\n"])
def test_ac01_the_authorized_model_is_the_only_one_sent(ready_env, h, mock_openai, value):
    if value is not None:
        ready_env.setenv("OPENAI_MODEL", value)
    config = AssistantConfig.from_env()
    assert config.model == LUNA and config.live_problems() == []
    prov = provider(h, mock_openai, config)
    assert prov.ready and prov.model == LUNA
    mock_openai.queue = [TOOL("search_customers", {"query": "Alder"}), FINAL(ANSWER)]
    prov_service(h, prov, config).run_turn(p(h), "Find Alder")
    assert [b["model"] for b in mock_openai.bodies()] == [LUNA, LUNA]


def prov_service(h, prov, config):
    from lbf.assistant.service import AssistantService

    return AssistantService(h.api.svc, prov, config)


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_ac01_a_blank_model_in_a_configuration_object_is_the_authorized_one(h, mock_openai, blank):
    prov = provider(h, mock_openai, live_config(model=blank))
    assert prov.ready and prov.model == LUNA


def test_ac11_nothing_is_enabled_by_default(monkeypatch, h):
    for name in (*READY, "OPENAI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    reads = []
    monkeypatch.setattr(config_module, "read_key", lambda *a: reads.append(1) or "k")
    config = AssistantConfig.from_env()
    assert (config.provider, config.live_inference, config.model) == ("disabled", False, None)
    assert build_assistant(h.api.svc).provider.name == "disabled"
    assert reads == []  # a provider that is off does not even read the key
    compose = (REPO / "compose.yaml").read_text()
    assert "LBF_ASSISTANT_PROVIDER: ${LBF_ASSISTANT_PROVIDER:-disabled}" in compose
    assert 'LBF_LIVE_INFERENCE: "false"' in compose and "OPENAI_MODEL: ${OPENAI_MODEL:-gpt-6-luna}" in compose


@pytest.mark.parametrize("missing", ["LBF_ASSISTANT_PROVIDER", "LBF_LIVE_INFERENCE", "OPENAI_API_KEY_FILE",
                                     "LBF_OPENAI_INPUT_PRICE_PER_MTOK", "LBF_OPENAI_CACHED_INPUT_PRICE_PER_MTOK",
                                     "LBF_OPENAI_OUTPUT_PRICE_PER_MTOK", "LBF_OPENAI_PRICE_SOURCE",
                                     "LBF_OPENAI_PRICE_OBSERVED_AT"])
def test_ac12_every_element_of_the_opt_in_is_required(ready_env, h, missing):
    ready_env.setenv(missing, "/nonexistent" if missing == "OPENAI_API_KEY_FILE" else "")
    a = build_assistant(h.api.svc)
    assert a.provider.name == "disabled" and h.calls() == [] and h.ledger() == []


# ---------------------------------------------------------------------------------------- AC-02, AC-03
@pytest.mark.parametrize("model", UNAUTHORIZED)
def test_ac02_an_unauthorized_model_is_rejected_at_configuration(ready_env, h, monkeypatch, model):
    ready_env.setenv("OPENAI_MODEL", model)
    monkeypatch.setattr(openai_provider.openai, "OpenAI", lambda **k: pytest.fail("no client may be built"))
    a = build_assistant(h.api.svc)
    assert a.provider.name == "disabled" and a.config.live_problems() == ["OPENAI_MODEL"]
    assert h.api.svc.extras["assistant_status"] == "misconfigured"
    [event] = h.events("ASSISTANT_CONFIG_REJECTED")
    assert event.payload["missing"] == ["OPENAI_MODEL"]
    r = h.api.anon().get("/readyz")
    assert r.status_code == 200 and r.json()["assistant"] == "misconfigured"
    assert a.run_turn(p(h), "Find Alder").fallback_used and h.calls() == [] and h.ledger() == []


@pytest.mark.parametrize("model", [*UNAUTHORIZED, 7, ["gpt-6-luna"]])
def test_ac03_the_provider_itself_rejects_it(h, mock_openai, monkeypatch, model):
    monkeypatch.setattr(openai_provider.openai, "OpenAI", lambda **k: pytest.fail("no client may be built"))
    config = live_config(model=model)
    prov = provider(h, mock_openai, config)
    assert not prov.ready
    out = prov_service(h, prov, config).run_turn(p(h), "Find Alder")
    assert out.fallback_reason == "CONFIG_REJECTED"
    assert mock_openai.requests == [] and h.calls() == [] and h.ledger() == []


def test_ac03_changing_a_live_provider_afterwards_does_not_change_the_request(h, mock_openai):
    prov = provider(h, mock_openai)
    prov.model = "gpt-6.1-terra"
    prov_service(h, prov, live_config()).run_turn(p(h), "Find Alder")
    assert {b["model"] for b in mock_openai.bodies()} <= {LUNA}


# ---------------------------------------------------------------------------------------- AC-04
PAIRS = [(m, m) for m in UNAUTHORIZED] + [(LUNA, "gpt-6.1-terra"), ("gpt-6.1-terra", LUNA)]


@pytest.mark.parametrize(("configured", "authorized"), PAIRS)
def test_ac04_smoke_refuses(tmp_path, ready_env, configured, authorized):
    ready_env.setenv("OPENAI_MODEL", configured)
    path = ledger_file(tmp_path)
    before = sha(path)
    auth = smoke_authorization(model=authorized)
    config = AssistantConfig.from_env()
    assert smoke.refusal(config, (auth, None))
    assert smoke.execution_refusal(config, auth, auth["run_id"])
    code, out = smoke.execute(config, auth, out_root=tmp_path, ledger_path=path,
                              provider_factory=lambda *a: pytest.fail("no provider may be built"))
    assert code == 2 and out["error"] == "CONFIG_REJECTED" and sha(path) == before


@pytest.mark.parametrize(("configured", "authorized"), PAIRS)
def test_ac04_live_validation_refuses(tmp_path, ready_env, configured, authorized):
    from test_live_validation import COMMIT, auth
    from test_live_validation import config as live_run_config

    path = ledger_file(tmp_path, max_cap=8)
    before = sha(path)
    a = auth(model=authorized, ledger_id=Ledger(path).ledger_id)
    cfg = live_run_config(model=configured)
    assert live_assistant.refusal(cfg, a, commit=COMMIT, clean=True)
    code, out = live_assistant.execute(cfg, a, ledger_path=path, out_root=tmp_path / "o", static_dir=tmp_path,
                                       driver=lambda *x: pytest.fail("must not run"),
                                       provider_factory=lambda *x: pytest.fail("no provider may be built"))
    assert code == 2 and out["error"] == "CONFIG_REJECTED" and sha(path) == before


@pytest.mark.parametrize(("configured", "authorized"), PAIRS)
def test_ac04_evaluation_refuses(tmp_path, ready_env, configured, authorized):
    ready_env.setenv("OPENAI_MODEL", configured)
    path = ledger_file(tmp_path, max_cap=200)
    before = sha(path)
    auth = evaluation_authorization(model=authorized)
    file = tmp_path / "a.json"
    file.write_text(json.dumps(auth))
    ready_env.setenv("LBF_EVAL_AUTHORIZATION", str(file))
    assert eval_authorization.check("evaluation")[0] is None
    code = evals.run("live", tmp_path / "o", authorization=auth, ledger_path=path,
                     provider_factory=lambda *a: pytest.fail("no provider may be built"))
    assert code == 2 and sha(path) == before


@pytest.mark.parametrize("value", [None, "", "  "])
def test_ac04_a_run_needs_the_model_set_explicitly(ready_env, tmp_path, value):
    if value is not None:
        ready_env.setenv("OPENAI_MODEL", value)
    config = AssistantConfig.from_env()
    assert config.model == LUNA and "explicit" in (smoke.refusal(config, (smoke_authorization(), None)) or "")
    file = tmp_path / "a.json"
    file.write_text(json.dumps(evaluation_authorization()))
    ready_env.setenv("LBF_EVAL_AUTHORIZATION", str(file))
    assert eval_authorization.check("evaluation")[0] is None


def evaluation_authorization(**kw):
    return smoke_authorization(**({"purpose": "evaluation", "run_id": "ev-test", "max_requests": 40,
                                   "max_trials": 40, "max_concurrency": 1, "max_output_tokens": 500} | kw))


@pytest.mark.parametrize("change", [{"purpose": "smoke"}, {"purpose": "live_validation"}, {"provider": "other"},
                                    {"expires_at": "2020-01-01T00:00:00Z"}, {"expires_at": "2099-01-01T00:00:00"},
                                    {"expires_at": None}, {"approved_by": ""}, {"approved_at": "x"},
                                    {"approved_at": "2098-01-01T00:00:00Z"}, {"max_estimated_usd": None},
                                    {"max_requests": 0}, {"max_concurrency": 2}, {"input_price_per_mtok": "0.10"},
                                    {"output_price_per_mtok": "-1"}, {"max_request_cost_usd": "0.10"},
                                    {"run_id": "../x"}, {"max_trials": "many"}])
def test_ac12_an_incomplete_or_foreign_authorization_is_refused(tmp_path, ready_env, change):
    ready_env.setenv("OPENAI_MODEL", LUNA)
    file = tmp_path / "a.json"
    ready_env.setenv("LBF_EVAL_AUTHORIZATION", str(file))
    assert eval_authorization.check("evaluation")[0] is None  # missing
    file.write_text(json.dumps(evaluation_authorization()))
    assert eval_authorization.check("evaluation") == (evaluation_authorization(), "")
    file.write_text(json.dumps(evaluation_authorization(**change)))
    assert eval_authorization.check("evaluation")[0] is None
    path = ledger_file(tmp_path, max_cap=200)
    before = sha(path)
    assert evals.run("live", tmp_path / "o", authorization=evaluation_authorization(**change), ledger_path=path,
                     provider_factory=lambda *a: pytest.fail("no provider may be built")) == 2
    assert sha(path) == before


def test_ac12_a_live_run_needs_its_ledger(tmp_path, ready_env):
    ready_env.setenv("OPENAI_MODEL", LUNA)
    for ledger in (None, tmp_path / "missing.sqlite3"):
        assert evals.run("live", tmp_path / "o", authorization=evaluation_authorization(), ledger_path=ledger,
                         provider_factory=lambda *a: pytest.fail("no provider may be built")) == 2


# ---------------------------------------------------------------------------------------- AC-05, AC-06
def status(code):
    return lambda request: httpx2.Response(code, json={"error": {"message": "x"}}, headers={"retry-after": "0"})


@pytest.mark.parametrize("item", [status(404), status(429), status(500), httpx2.ReadTimeout("slow"),
                                  httpx2.ConnectError("refused"), REFUSAL(), R([], status="incomplete"),
                                  FINAL("not json"), FINAL({"summary": "x"})])
def test_ac05_errors_retries_and_repairs_never_change_the_model(h, mock_openai, item):
    mock_openai.queue = [item] * 10
    out = h.openai(mock_openai, live_config(max_retries=2)).run_turn(p(h), "Find Alder")
    assert out.fallback_used and 1 <= len(mock_openai.requests) <= 6
    assert {b["model"] for b in mock_openai.bodies()} == {LUNA}
    assert not any("models" in b for b in mock_openai.bodies())
    assert {c.model_configured for c in h.calls()} == {LUNA}


def test_ac06_one_constant_and_no_path_around_it():
    source = REPO / "backend/src"
    literals = [(f, node.value) for f in [*source.rglob("*.py"), *(REPO / "evals").rglob("*.py")]
                for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("gpt-")]
    assert [(f.name, v) for f, v in literals] == [("config.py", LUNA)]
    assert AUTHORIZED_OPENAI_MODEL == LUNA
    text = "\n".join(f.read_text(encoding="utf-8") for f in source.rglob("*.py"))
    assert text.count('"OPENAI_MODEL"') <= 4 and "AUTHORIZED_OPENAI_MODEL =" in text
    for name in (".env.example", "compose.yaml"):
        assert "terra" not in (REPO / name).read_text().lower()


# ---------------------------------------------------------------------------------------- AC-07, AC-08
@pytest.mark.parametrize("reported", [LUNA, "gpt-6-luna-2026-11-01", "gpt-6-luna-2028-02-29"])
def test_ac07_accepted_identifiers_are_recorded_verbatim(h, mock_openai, reported):
    mock_openai.queue = [reporting(FINAL(ANSWER), reported)]
    out = h.openai(mock_openai).run_turn(p(h), "Find Alder")
    assert out.fallback_used is False
    assert [(c.model_configured, c.model_reported) for c in h.calls()] == [(LUNA, reported)]


VARIANTS = {"final": FINAL(ANSWER), "tool": TOOL("prepare_draft", args(3)), "refusal": REFUSAL(),
            "incomplete": R([], status="incomplete")}


@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("reported", UNEXPECTED, ids=[ascii(x) for x in UNEXPECTED])
def test_ac08_an_unexpected_identifier_is_never_used(h, mock_openai, reported, variant):
    mock_openai.queue = [reporting(VARIANTS[variant], reported) for _ in range(5)]
    before = h.counts()
    a = h.openai(mock_openai, live_config(max_retries=2))
    out = a.run_turn(p(h), "Prepare a HALDEN draft for account 00417: 3 x BW-1001.", session_key="s")
    assert out.fallback_reason == "MODEL_MISMATCH" and out.notice and "model" in out.notice.lower()
    assert out.tool_calls == [] and out.proposal is None and out.receipts == []
    assert h.counts() == before and len(mock_openai.requests) == 1
    [call] = h.calls()
    recorded = reported if isinstance(reported, str) and reported not in ("x\x00y", "\ud800") else (
        None if reported is None else json.dumps(reported))
    assert (call.status, call.error_category, call.model_reported) == ("FAILED", "MODEL_MISMATCH", recorded)
    assert call.provider_request_id == "req_test_1" and call.usage_input_tokens == 120
    assert call.cost_basis == "WORST_CASE_RESERVATION" and call.estimated_cost_usd >= call.reserved_usd > 0
    assert {e.entry for e in h.ledger()} == {"RESERVE", "SETTLE"}
    assert all(e.amount_usd >= call.reserved_usd for e in h.ledger())
    # the latch is the process's, not the provider object's
    again = h.openai(mock_openai).run_turn(p(h), "Find Alder")
    assert again.fallback_reason == "MODEL_MISMATCH" and len(mock_openai.requests) == 1 and len(h.calls()) == 1


def test_ac08_a_response_without_the_field_or_not_an_object_is_a_mismatch(h, mock_openai):
    body = FINAL(ANSWER)
    del body["model"]
    mock_openai.queue = [body]
    assert h.openai(mock_openai).run_turn(p(h), "Find Alder").fallback_reason == "MODEL_MISMATCH"


def test_ac08_usage_above_the_reservation_settles_at_the_usage(h, mock_openai):
    mock_openai.queue = [reporting(FINAL(ANSWER, usage={"input_tokens": 90000, "output_tokens": 9000,
                                                        "total_tokens": 99000}), "gpt-6.1-terra")]
    h.openai(mock_openai).run_turn(p(h), "Find Alder")
    [call] = h.calls()
    assert call.estimated_cost_usd == Decimal("0.126") and call.cost_basis == "WORST_CASE_RESERVATION"


def test_r7_error_responses_carry_no_model_report(h, mock_openai):
    mock_openai.queue = [status(500), status(404), httpx2.ReadTimeout("slow")]
    a = h.openai(mock_openai, live_config(max_retries=0))
    reasons = [a.run_turn(p(h), "Find Alder").fallback_reason for _ in range(3)]
    assert reasons == ["SERVER_ERROR", "NOT_FOUND", "TIMEOUT"]
    assert a.run_turn(p(h), "Find Alder").fallback_used is False  # nothing latched


def test_a_response_in_flight_when_the_latch_closes_is_recorded_but_not_used(h):
    arrived, release = threading.Event(), threading.Event()

    class Slow(MockOpenAI):
        def __call__(self, request):
            if b"slow question" in request.content:
                with self.lock:
                    self.requests.append(request)
                arrived.set()
                release.wait(10)
                return httpx2.Response(200, json=FINAL(ANSWER), headers={"x-request-id": "req_slow"})
            return super().__call__(request)

    mock = Slow()
    mock.queue = [reporting(FINAL(ANSWER), "gpt-6.1-terra")]
    a = h.openai(mock)
    outs: list = []
    t = threading.Thread(target=lambda: outs.append(a.run_turn(p(h), "slow question")))
    t.start()
    assert arrived.wait(10)
    assert a.run_turn(p(h), "fast question").fallback_reason == "MODEL_MISMATCH"
    release.set()
    t.join(10)
    assert outs[0].fallback_reason == "MODEL_MISMATCH" and len(mock.requests) == 2
    assert sorted((c.model_reported, c.error_category) for c in h.calls()) == [
        ("gpt-6-luna", "MODEL_MISMATCH"), ("gpt-6.1-terra", "MODEL_MISMATCH")]


def test_the_latch_is_checked_again_after_the_reservation(h, mock_openai):
    prov = provider(h, mock_openai)
    real = prov.budget.reserve

    def reserve(*a, **k):
        out = real(*a, **k)
        openai_provider.PROCESS_GUARD.close("gpt-6.1-terra")
        return out

    prov.budget.reserve = reserve
    out = prov_service(h, prov, live_config()).run_turn(p(h), "Find Alder")
    assert out.fallback_reason == "MODEL_MISMATCH" and mock_openai.requests == []
    assert [c.cost_basis for c in h.calls()] == ["WORST_CASE_RESERVATION"]  # never refunded


# ---------------------------------------------------------------------------------------- AC-09, AC-10
@pytest.fixture(scope="module")
def eval_env():
    from evals.harness.env import EvalEnv

    env = EvalEnv().build()
    yield env
    env.close()


def campaign(tmp_path, env, mock, *, run_id="ev-test", tasks=None, spend=None, out="o", **kw):
    def factory(e, auth):
        config = live_config(max_retries=0)
        budget = SpendBudget(e.engine, config, e.clock, Decimal("1"), spend=spend, campaign=run_id) if spend else \
            BudgetService(e.engine, config, e.clock, Decimal("1"))
        return OpenAIProvider(config, budget=budget, telemetry=Telemetry(e.engine, e.clock), clock=e.clock,
                              http_client=httpx2.Client(transport=httpx2.MockTransport(mock)), transport_label="mock",
                              api_key=TEST_OPENAI_KEY)
    return evals.run("live", tmp_path / out, tasks=tasks or evals.load_tasks("T0*.yaml"), provider_factory=factory,
                     authorization=evaluation_authorization(run_id=run_id, **kw),
                     ledger_path=ledger_file(tmp_path, max_cap=200), env=env)


@pytest.mark.parametrize("n", [1, 3])
def test_ac09_a_mismatch_ends_the_campaign_and_its_authorization(tmp_path, eval_env, ready_env, n):
    ready_env.setenv("OPENAI_MODEL", LUNA)
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER)] * (n - 1) + [reporting(FINAL(ANSWER), "gpt-6.1-terra")]
    assert campaign(tmp_path, eval_env, mock) not in (0, 2)
    s = json.loads((tmp_path / "o/summary.json").read_text())
    assert s["status"] == "ABORTED_MODEL_MISMATCH" and s["tasks"] == n and len(mock.requests) == n
    assert s["model_configured"] == LUNA and "gpt-6.1-terra" in json.dumps(s["model_reported"])
    openai_provider.PROCESS_GUARD = model_guard.ModelGuard()  # a new process
    again = MockOpenAI()
    assert campaign(tmp_path, eval_env, again, out="o2") == 2 and again.requests == []
    ledger = Ledger(ledger_file(tmp_path))
    assert ledger.remaining("ev-test") == 0 and ledger.reserve("ev-test", "x") is None
    assert [e["kind"] for e in ledger.events("ev-test")].count("CAMPAIGN_ABORTED") == 1


def test_ac09_concurrent_workers_stop_after_the_first_mismatch(tmp_path, h):
    started: list[float] = []
    lock = threading.Lock()

    class Transport(MockOpenAI):
        def __call__(self, request):
            with lock:
                started.append(time.monotonic())
                me = len(started)
            time.sleep(0.02)
            return reporting(FINAL(ANSWER), "gpt-6.1-terra" if me == 3 else LUNA)

    mock = Transport()
    config = live_config(max_retries=0, max_concurrency=4)
    path = ledger_file(tmp_path, max_cap=200)
    ledger = Ledger(path)
    assert ledger.bind(evaluation_authorization()) is None
    invocation = ledger.start_invocation("ev-test")
    capped = smoke.CappedProvider(provider(h, mock, config), 40, ledger, "ev-test", invocation)
    request = smoke.s1_request("ev-test", config)
    request = dataclasses.replace(request, scope="TURN", eval_run_id=None)
    turns: list = []
    workers = [threading.Thread(target=lambda: turns.extend(capped.run(request) for _ in range(5))) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(20)
    assert len(turns) == 20 and capped.guard.closed
    assert len(started) <= 8  # at most the four in flight beyond the third
    # every response is recorded; a reservation overtaken by the abort is recorded as not sent
    assert len(started) == sum(c.provider_request_id is not None for c in h.calls()) <= len(h.calls())
    after = [t for t in turns if t.error_category is not ErrorCategory.MODEL_MISMATCH]
    assert all(t.kind == "FINAL" for t in after) and len(after) < len(started)
    assert Ledger(path).reserve("ev-test", "again") is None


@pytest.mark.parametrize("first,second", [(LUNA, "gpt-6-luna-2026-11-01"),
                                          ("gpt-6-luna-2026-11-01", "gpt-6-luna-2026-11-02")])
def test_ac10_two_accepted_versions_in_one_campaign_abort_it(tmp_path, eval_env, ready_env, first, second):
    ready_env.setenv("OPENAI_MODEL", LUNA)
    mock = MockOpenAI()
    mock.queue = [reporting(FINAL(ANSWER), first), reporting(FINAL(ANSWER), second)]
    assert campaign(tmp_path, eval_env, mock, run_id="ev-ver") not in (0, 2)
    s = json.loads((tmp_path / "o/summary.json").read_text())
    assert s["status"] == "ABORTED_MODEL_MISMATCH" and len(mock.requests) == 2
    assert s["observed_version"] == first


def test_ac10_the_observed_version_survives_a_restart_of_the_process(tmp_path, h):
    path = ledger_file(tmp_path)
    ledger = Ledger(path)
    assert ledger.bind(smoke_authorization()) is None
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER), reporting(FINAL(ANSWER), "gpt-6-luna-2026-11-01")]
    config = live_config(max_retries=0)
    kinds = []
    for _ in range(2):
        capped = smoke.CappedProvider(provider(h, mock, config), 3, ledger, "o1a-test-run",
                                      ledger.start_invocation("o1a-test-run"))
        kinds.append(capped.run(dataclasses.replace(smoke.s1_request("o1a-test-run", config), scope="TURN",
                                                    eval_run_id=None)).error_category)
        capped.finish("FAIL")
        openai_provider.PROCESS_GUARD = model_guard.ModelGuard()
    assert kinds == [None, ErrorCategory.MODEL_MISMATCH]


def test_r6_does_not_apply_to_interactive_use(h, mock_openai):
    mock_openai.queue = [FINAL(ANSWER), reporting(FINAL(ANSWER), "gpt-6-luna-2026-11-01")]
    a = h.openai(mock_openai)
    assert [a.run_turn(p(h), "Find Alder").fallback_used for _ in range(2)] == [False, False]


def test_ac09_smoke_and_live_validation_report_the_abort(tmp_path, eval_env):
    from test_smoke_executor import run as smoke_run

    mock = MockOpenAI()
    mock.queue = [reporting(FINAL(ANSWER), "gpt-6.2-sol")]
    code, s = smoke_run(eval_env, tmp_path, mock, run_id="sm-mismatch")
    assert code not in (0, 2) and s["status"] == "ABORTED_MODEL_MISMATCH" and len(mock.requests) == 1
    assert s["requests"][0]["model_reported"] == "gpt-6.2-sol"
    openai_provider.PROCESS_GUARD = model_guard.ModelGuard()
    again = MockOpenAI()
    code, s = smoke_run(eval_env, tmp_path, again, run_id="sm-mismatch", out_root=tmp_path / "b")
    assert code == 2 and again.requests == []


# ---------------------------------------------------------------------------------------- AC-13
def test_ac13_the_shared_expenditure_ceiling_holds_across_campaigns(tmp_path, eval_env, ready_env):
    ready_env.setenv("OPENAI_MODEL", LUNA)
    spend = SpendLedger(init_spend_ledger(tmp_path / "spend.sqlite3", Decimal("0.10")))
    mock = MockOpenAI()
    mock.queue = [FINAL(ANSWER, usage={"input_tokens": 9000, "output_tokens": 500, "total_tokens": 9500})] * 60
    codes = [campaign(tmp_path, eval_env, mock, run_id=f"ev-s{i}", out=f"o{i}", spend=spend,
                      tasks=evals.load_tasks("*.yaml")[:8]) for i in range(3)]
    view = spend.summary()
    assert 3 in codes and view["settled_usd"] + view["outstanding_usd"] <= Decimal("0.10")
    assert len(mock.requests) == view["requests"] < 24
    assert SpendLedger(tmp_path / "spend.sqlite3").summary() == view  # durable


def test_ac13_the_expenditure_ledger_fails_closed(tmp_path):
    import uuid

    with pytest.raises(ValueError):
        SpendLedger(tmp_path / "missing.sqlite3")
    path = init_spend_ledger(tmp_path / "s.sqlite3", Decimal("0.50"))
    with pytest.raises(ValueError):
        init_spend_ledger(path, Decimal("5"))
    s = SpendLedger(path)
    a, b = uuid.uuid4(), uuid.uuid4()
    assert s.reserve(a, Decimal("0.3"), "c1") and not s.reserve(b, Decimal("0.3"), "c2")
    s.settle(a, Decimal("0.1"), "REPORTED_USAGE")
    assert s.reserve(b, Decimal("0.4"), "c2") and not s.reserve(uuid.uuid4(), Decimal("0.000001"), "c2")
    with pytest.raises(ValueError):
        s.settle(a, Decimal("0"), "REPORTED_USAGE")  # once only
    with pytest.raises(ValueError):
        s.settle(uuid.uuid4(), Decimal("0"), "REPORTED_USAGE")  # never reserved
    import sqlite3

    with sqlite3.connect(path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM entries")


# ---------------------------------------------------------------------------------------- AC-16
def test_ac16_identifier_grammar():
    for ok in (LUNA, "gpt-6-luna-2026-11-01"):
        assert model_guard.accepted(ok)
    for bad in UNEXPECTED:
        assert not model_guard.accepted(bad)
    assert LUNA == AUTHORIZED_OPENAI_MODEL


# ---------------------------------------------------------------------------------------- readiness after the latch
def test_a_stopped_model_is_reported_and_never_makes_the_application_unready(h, mock_openai):
    config = live_config()
    a = prov_service(h, provider(h, mock_openai, config), config)
    a.provider.transport_label = "live"  # as in the process that serves people; still the mocked transport
    h.api.svc.extras["assistant"] = a
    h.api.svc.extras["assistant_status"] = "openai"
    c = h.api.as_("daniel")
    before = h.api.anon().get("/readyz")
    assert before.json()["assistant"] == "openai" and c.get("/api/v1/assistant/status").json()["live_inference"] is True
    mock_openai.queue = [reporting(FINAL(ANSWER), "gpt-6.1-terra")]
    assert a.run_turn(p(h), "Find Alder").fallback_reason == "MODEL_MISMATCH"
    after = h.api.anon().get("/readyz")
    assert (after.status_code, after.json()["status"]) == (before.status_code, before.json()["status"])
    assert after.json()["assistant"] == "stopped:model-mismatch"
    status = c.get("/api/v1/assistant/status").json()
    assert (status["mode"], status["live_inference"]) == ("openai live", False)
