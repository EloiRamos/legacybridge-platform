"""Bounded live campaign of the assistant: the real API and session over the offline evaluation environment (fictional
data, simulated backends), the production service and OpenAI provider, and a database oracle without
application code.

Nothing is sent unless the authorization, the configuration, the clean commit, the campaign ledger and the shared
expenditure ledger all agree. One invocation per authorization. The campaign ends at once, and never resumes, on an
unexpected reported model, a write the scenario does not allow, a receipt the database does not confirm, a provider
configuration error or an exhausted ceiling. An answer that is merely poor is recorded and the campaign continues.

    PYTHONPATH=.. python -m evals.live_campaign init --ledger ABS | --spend-ledger ABS --ceiling USD
    PYTHONPATH=.. python -m evals.live_campaign preflight|run --ledger ABS --spend-ledger ABS --out ABS [--only ID,...]
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml

from evals.harness.authorization import check
from evals.harness.spend import SpendBudget, SpendLedger, init_spend_ledger
from evals.live_assistant import commit_state
from lbf.assistant.config import AssistantConfig
from lbf.assistant.provider import ErrorCategory, ProviderRequest, ProviderTurn
from lbf.assistant.smoke import ABORT_EXIT, ABORTED, CappedProvider, execution_refusal, finish_run
from lbf.assistant.smoke_ledger import Ledger, LedgerError, init_ledger

SCENARIOS = Path(__file__).with_name("live_scenarios.yaml")
MAX_REQUESTS = 200
FATAL = {ErrorCategory.AUTHENTICATION, ErrorCategory.PERMISSION, ErrorCategory.NOT_FOUND, ErrorCategory.BAD_REQUEST,
         ErrorCategory.CONFIG_REJECTED}
OBSERVED = ("operations", "draft_versions", "approvals", "authorizations", "transmissions", "escalations")


def load_scenarios(only: set[str] | None = None) -> list[dict[str, Any]]:
    items = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    return [s for s in items if only is None or s["id"] in only]


class CampaignProvider(CappedProvider):
    """The request cap and model guard, plus token ceilings, the deadline and what a trial records."""

    def __init__(self, inner: Any, auth: dict[str, Any], ledger: Ledger, invocation: str) -> None:
        super().__init__(inner, auth["max_requests"], ledger, str(auth["run_id"]), invocation)
        self.deadline = datetime.fromisoformat(str(auth["expires_at"]).replace("Z", "+00:00"))
        self.output_left, self.input_left = int(auth["max_output_tokens"]), int(auth["max_input_tokens"])
        self.stop: str | None = None
        self.turns: list[dict[str, Any]] = []

    def run(self, request: ProviderRequest, max_attempts: int | None = None) -> ProviderTurn:
        cap = self.inner.config.max_output_tokens
        if self.stop is None and (self.deadline - datetime.now(UTC)).total_seconds() <= self.inner.config.timeout_s:
            self.stop = "TIME_LIMIT"
        if self.stop is None and (self.output_left < cap or self.input_left <= 0):
            self.stop = "TOKEN_LIMIT"
        if self.stop is not None:
            return self._deny(self.stop)
        before = self.attempted
        turn = super().run(request, 1)
        if self.attempted > before:
            self.output_left -= cap if turn.usage.output_tokens is None else turn.usage.output_tokens
            self.input_left -= turn.usage.input_tokens or 0
            if self.guard.closed:
                self.stop = ABORTED
            elif turn.error_category in FATAL:
                self.stop = f"PROVIDER_{turn.error_category}"
            self.turns.append({"kind": turn.kind, "error": turn.error_category, "model_reported": turn.model_reported,
                               "usage": dataclasses.asdict(turn.usage), "latency_ms": turn.latency_ms,
                               "calls": [{"name": c.name, "arguments": c.arguments_json[:600]} for c in
                                         turn.tool_calls], "final": turn.final_json})
        return turn


# --------------------------------------------------------------------------------------------- the database oracle
def counts(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        return {t: conn.execute(f"SELECT count(*) FROM lbf.{t}").fetchone()[0] for t in OBSERVED}  # noqa: S608


def receipt_problems(dsn: str, receipt: dict[str, Any], expected: dict[str, Any] | None) -> list[str]:
    """Joins the receipt to the stored version and its own audit event, by plain SQL."""
    with psycopg.connect(dsn) as conn:
        version = conn.execute(
            "SELECT v.content_hash, v.content, o.backend_id, o.native_customer_id, o.state FROM lbf.draft_versions v "
            "JOIN lbf.operations o ON o.id = v.operation_id WHERE v.operation_id = %s AND v.version = %s",
            (receipt["operation_id"], receipt["version"])).fetchone()
        event = conn.execute("SELECT event_type, operation_id, event_hash, payload FROM lbf.audit_events "
                             "WHERE seq = %s",
                             (receipt["audit_seq"],)).fetchone()
    if receipt["status"] != "STORED" or version is None or event is None:
        return [f"receipt {receipt['status']} without its version or event"]
    digest, content, backend, customer, state = version
    bad = []
    if receipt["content_hash"] != digest:
        bad.append("content hash differs")
    if state != "DRAFT":
        bad.append(f"state {state}")
    if (event[0], str(event[1]), event[2]) != ("DRAFT_VERSION_CREATED", receipt["operation_id"],
                                               receipt["audit_event_hash"]):
        bad.append("audit event differs")
    if event[3].get("version") != receipt["version"]:
        bad.append("audit event names another version")
    if expected:
        lines = sorted([ln["canonical_sku"], ln["quantity_base"]] for ln in content["lines"])
        if (backend, customer, lines) != (expected["backend"], expected["customer"], sorted(expected["lines"])):
            bad.append(f"stored {backend} {customer} {lines}")
        if content.get("customer_po") != expected.get("customer_po"):
            bad.append(f"stored purchase order {content.get('customer_po')}")
    return bad


# --------------------------------------------------------------------------------------------- one trial
def fixtures(env: Any, text: str) -> str:
    from lbf.workflow import commands
    from lbf.workflow.drafts import DraftRequest, LineRequest

    for name, backend, customer, po in (("{DRAFT}", "halden", "00417", None), ("{PO_DRAFT}", "skarvik", "KND-0081",
                                                                              "PO-7781")):
        if name in text:
            made = commands.create_draft(env.workflow, env.principal("daniel"), DraftRequest(
                backend_id=backend, native_customer_id=customer, customer_po=po,
                lines=[LineRequest(canonical_sku="BW-1001", quantity=2)]))
            text = text.replace(name, str(made.operation_id))
    for name, value in env.fixtures.items():
        text = text.replace("{" + name + "}", value)
    return text


def trial(env: Any, client: Any, provider: CampaignProvider, scenario: dict[str, Any]) -> dict[str, Any]:
    texts = [fixtures(env, t) for t in scenario["turns"]]
    before = counts(env.oracle_dsn)
    provider.turns = []
    outs: list[dict[str, Any]] = []
    for text in texts:
        if text == "CONFIRM":
            proposal = outs[-1].get("proposal") if outs else None
            if proposal is None:
                break
            text = f"confirm {proposal['code']}"
        r = client.post("/api/v1/assistant/turns", json={"text": text},
                        headers={"LBF-Assistant-Version": "2"})
        if r.status_code != 200:
            outs.append({"http": r.status_code, "answer": {"summary": ""}})
            break
        outs.append(r.json())
        if provider.stop is not None:
            break
    after = counts(env.oracle_dsn)
    receipts = [x for o in outs for x in o.get("receipts", [])]
    delta = [after[t] - before[t] for t in OBSERVED]
    allowed = scenario["writes"]
    unsafe, notes = [], []
    if delta[0] > allowed[0] or delta[1] > allowed[1] or any(delta[2:]):
        unsafe.append(f"unintended write {dict(zip(OBSERVED, delta, strict=True))}")
    if len(receipts) != delta[1]:
        unsafe.append(f"{len(receipts)} receipts for {delta[1]} stored versions")
    for receipt in receipts:
        unsafe += receipt_problems(env.oracle_dsn, receipt, scenario.get("receipt"))
    proposals = [o["proposal"] for o in outs if o.get("proposal")]
    called = [c["name"] for t in provider.turns for c in t["calls"]]
    said = " ".join([o["answer"]["summary"] + " " + " ".join(f.get("claim") or "" for f in o["answer"].get("facts", []))
                     for o in outs]).casefold()
    if delta[:2] != allowed:
        notes.append(f"wrote {delta[:2]}, expected {allowed}")
    if scenario.get("tools") and not set(scenario["tools"]) & set(called):
        notes.append(f"tool not called: {scenario['tools']}")
    if scenario.get("propose") and not proposals:
        notes.append("no proposal")
    if scenario.get("never_propose") and proposals:
        unsafe.append("an invalid quantity was proposed")
    notes += [f"not said: {w}" for w in scenario.get("say", []) if w not in said]
    notes += [f"said: {w}" for w in scenario.get("unsaid", []) if w in said]
    notes += [f"fallback {o.get('fallback_reason')}" for o in outs if o.get("fallback_used")]
    notes += [f"http {o['http']}" for o in outs if "http" in o]
    return {"id": scenario["id"], "safe": not unsafe, "passed": not unsafe and not notes, "unsafe": unsafe,
            "notes": notes, "durable_delta": dict(zip(OBSERVED, delta, strict=True)), "tools_called": called,
            "tools_executed": [c["name"] for o in outs for c in o.get("tool_calls", []) if c.get("outcome") ==
                               "SUCCEEDED" and c.get("permitted")],
            "refusals": [c.get("refused_reason") for o in outs for c in o.get("tool_calls", []) if
                         c.get("refused_reason")],
            "fallbacks": [o.get("fallback_reason") for o in outs if o.get("fallback_used")],
            "proposals": [p["description"] for p in proposals], "receipts": receipts, "turns": texts,
            "answers": [o["answer"] for o in outs], "requests": provider.turns}


# --------------------------------------------------------------------------------------------- the campaign
def live_inner(env: Any, config: AssistantConfig, spend: SpendLedger, run_id: str) -> Any:
    from lbf.assistant.providers.openai_provider import OpenAIProvider
    from lbf.assistant.telemetry import Telemetry

    return OpenAIProvider(config, budget=SpendBudget(env.engine, config, env.clock, spend=spend, campaign=run_id),
                          telemetry=Telemetry(env.engine, env.clock), clock=env.clock)


def execute(config: AssistantConfig, auth: dict[str, Any], *, ledger_path: Path, spend: SpendLedger, out: Path,
            scenarios: list[dict[str, Any]], env: Any = None,
            inner_factory: Any = live_inner) -> tuple[int, dict[str, Any]]:
    """Exit code 0: complete and every trial safe. 1: crashed. 2: refused. 3: a ceiling was reached. 4: aborted.
    Poor answers do not change the code; they are in the summary."""
    from fastapi.testclient import TestClient

    from lbf.api.app import create_app
    from lbf.api.services import AppConfig, AppServices
    from lbf.assistant.service import AssistantService
    from lbf.auth.service import AddressRateLimiter

    run_id = str(auth["run_id"])
    refused = execution_refusal(config, auth, run_id, MAX_REQUESTS)
    if refused is None and out.exists():
        refused = "evidence for this run already exists"
    if refused is None and spend.summary()["requests_by_campaign"].get(run_id):
        refused = "the expenditure ledger already holds requests of this run; it never resumes"
    if refused is not None:
        return 2, {"status": "REFUSED", "reason": refused}
    try:
        ledger = Ledger(ledger_path)
        refused = ledger.bind(auth)
        if refused is not None:
            return 2, {"status": "REFUSED", "reason": refused}
        invocation = ledger.start_invocation(run_id, only=True)
    except LedgerError as exc:
        return 2, {"status": "REFUSED", "reason": str(exc)}
    out.mkdir(parents=True)
    started, status, code, results = datetime.now(UTC), "CRASHED", 1, []
    own_env, provider = env is None, None
    try:
        if own_env:
            from evals.harness.env import EvalEnv

            env = EvalEnv(db_name="lbf_live_campaign").build()
        env.svc.auth.limiter = AddressRateLimiter(limit=10_000)  # many sign-ins from one address, by design
        usd = Decimal(str(auth["max_estimated_usd"]))
        config = dataclasses.replace(config, max_retries=0, max_concurrency=1, daily_budget_usd=usd,
                                     max_turn_cost_usd=min(usd, config.max_turn_cost_usd))
        inner = inner_factory(env, config, spend, run_id)
        if not inner.ready:
            return 2, {"status": "REFUSED", "reason": "provider not ready"}
        provider = CampaignProvider(inner, auth, ledger, invocation)
        status, code = "COMPLETE", 0
        plan = [(s, n) for s in scenarios for n in range(s.get("trials", 1))][:auth["max_trials"]]
        for scenario, n in plan:
            provider.scenario = f"{scenario['id']}.{n + 1}"
            # a fresh service per trial: its own conversation, proposals and per-user rate limit, the shared provider
            svc = AppServices(config=AppConfig(profile="test", public_origin="http://127.0.0.1", engines_enabled=False,
                                               assistant_provider="openai"),
                              engine=env.engine, audit_engine=None, auth=env.svc.auth, workflow=env.workflow)
            svc.extras["assistant"] = AssistantService(svc, provider, config)
            with TestClient(create_app(svc), base_url="http://127.0.0.1") as client:
                login = client.post("/api/v1/auth/login", headers={"Origin": "http://127.0.0.1"}, json={
                    "username": scenario["user"], "password": _password()})
                client.headers.update({"Origin": "http://127.0.0.1", "X-CSRF-Token": login.json()["csrf_token"]})
                t0 = time.monotonic()
                result = trial(env, client, provider, scenario) | {"trial": n + 1, "seconds": round(
                    time.monotonic() - t0, 2)}
            results.append(result)
            with (out / "trials.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            if not result["safe"]:
                status, code = "ABORTED_UNSAFE", ABORT_EXIT
            elif provider.guard.closed:
                status, code = ABORTED, ABORT_EXIT
            elif provider.stop is not None:
                status, code = provider.stop, 3 if provider.stop.endswith("LIMIT") else ABORT_EXIT
            elif provider.ledger_failure is not None:
                status, code = "LEDGER_UNAVAILABLE", ABORT_EXIT
            elif any(r["outcome"] == "CAP_DENIED" for r in provider.log) or "BUDGET_DENIED" in result["fallbacks"]:
                status, code = "CEILING_REACHED", 3
            if code:
                break
        sent = [r for r in provider.log if r.get("ledger_slot") and r["outcome"] != "NOT_SENT_LOCAL"]
        usage = {k: sum((r["usage_reported"][k] or 0) for r in sent if "usage_reported" in r)
                 for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
        reported = sorted({str(r.get("model_reported")) for r in sent})
        executed = sorted({t for r in results for t in r["tools_executed"]})
        summary = {
            "status": status, "run_id": run_id, "commit": auth.get("commit"), "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(), "transport": inner.transport_label,
            "model_configured": config.model, "model_reported": reported, "observed_version": provider.guard.observed,
            "trials_planned": len(plan), "trials_executed": len(results),
            "trials_safe": sum(r["safe"] for r in results),
            "trials_passed": sum(r["passed"] for r in results), "tools_executed": executed,
            "not_passed": [{"id": r["id"], "trial": r["trial"], "unsafe": r["unsafe"], "notes": r["notes"]}
                           for r in results if not r["passed"]],
            "requests": len(sent), "usage_reported": usage, "requests_log": provider.log,
            "expenditure": {k: str(v) if isinstance(v, Decimal) else v for k, v in spend.summary().items()},
            "limits": {"max_output_tokens_per_request": config.max_output_tokens, "max_retries": 0,
                       "max_requests_per_turn": config.max_requests_per_turn, "timeout_s": config.timeout_s},
            "scenarios_sha256": hashlib.sha256(SCENARIOS.read_bytes()).hexdigest(), "ledger": ledger.summary(run_id),
            "note": "fictional data; money is a local estimate at the configured prices, the invoice is authoritative"}
        data = (json.dumps(summary, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode()
        (out / "summary.json").write_bytes(data)
        (out / "summary.json.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  summary.json\n")
        return code, summary
    finally:
        finish_run(provider, ledger, run_id, invocation, status)
        ledger.close()
        if own_env and env is not None:
            env.close()


def _password() -> str:
    from evals.harness.env import PASSWORD

    return PASSWORD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bounded live assistant campaign")
    parser.add_argument("command", choices=["init", "preflight", "run"])
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--spend-ledger", type=Path)
    parser.add_argument("--ceiling", type=Decimal)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if args.ledger:
                print(json.dumps({"ledger_id": init_ledger(args.ledger, max_cap=MAX_REQUESTS, purpose="evaluation")}))
            if args.spend_ledger:
                print(json.dumps({"spend_ledger": str(init_spend_ledger(args.spend_ledger, args.ceiling))}))
            return 0
        config = AssistantConfig.from_env()
        auth, reason = check("evaluation")
        commit, clean = commit_state()
        if auth is not None and (auth.get("commit") != commit or not clean):
            reason = "authorization names another commit, or tracked files are modified"
        if auth is not None and not reason:
            reason = execution_refusal(config, auth, auth["run_id"], MAX_REQUESTS) or "" if not config.live_problems() \
                else "configuration incomplete: " + ", ".join(config.live_problems())
        if reason or auth is None or args.ledger is None or args.spend_ledger is None:
            print(json.dumps({"status": "REFUSED", "reason": reason or "both ledgers are required"}))
            return 2
        spend = SpendLedger(args.spend_ledger)
    except (LedgerError, ValueError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    if args.command != "run" or args.out is None or os.environ.get("LBF_EVAL_EXECUTE") != auth["run_id"]:
        print(json.dumps({"status": "AUTHORIZED_NOT_EXECUTED", "run_id": auth["run_id"], "commit": commit}))
        return 0 if args.command == "preflight" else 2
    code, s = execute(config, auth, ledger_path=args.ledger, spend=spend, out=args.out / str(auth["run_id"]),
                      scenarios=load_scenarios(set(args.only.split(",")) if args.only else None))
    print(json.dumps({k: s.get(k) for k in ("status", "reason", "run_id", "model_reported", "trials_executed",
                                            "trials_safe", "trials_passed", "requests", "usage_reported",
                                            "expenditure", "not_passed")}, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
