"""Bounded two-turn live validation of the AI assistant (purpose ``live_validation``).

The committed application, unchanged, over the offline evaluation environment: real FastAPI server on loopback, real
built console, real session, the production ``AssistantService`` and ``OpenAIProvider``. Two questions are asked in the
drawer by a browser; the second only makes sense with the first one's conversation.

Nothing is sent unless every precondition holds (owner authorization read once, exact commit, bound ledger, the prices
the authorization names, unexpired window). Hard limits, checked before every request: 8 requests, 0 retries, 6 000
output tokens, USD 0.10 estimated, 15 minutes. A tool outside the seven read tools is never executed: the run stops.
One invocation per run id; a failure is a failure and is never rerun from here.

    PYTHONPATH=.. python -m evals.live_assistant preflight|run --ledger ABS --out ABS
    PYTHONPATH=.. python -m evals.live_assistant init-ledger --ledger ABS
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from evals.harness.spend import SpendBudget, SpendLedger
from lbf.assistant.config import AssistantConfig
from lbf.assistant.provider import ErrorCategory, ProviderRequest, ProviderTurn
from lbf.assistant.service import AssistantService, TurnOutcome
from lbf.assistant.smoke import (
    ABORT_EXIT,
    ABORTED,
    RUN_ID,
    CappedProvider,
    _dec,
    _workflow_counts,
    finish_run,
    model_refusal,
    read_authorization,
)
from lbf.assistant.smoke_ledger import Ledger, LedgerError, NotAdmitted, init_ledger

REPO = Path(__file__).resolve().parents[1]
MAX_REQUESTS, MAX_PER_TURN, MAX_OUTPUT_TOTAL, MAX_OUTPUT_REQUEST = 8, 4, 6000, 1500
MAX_USD, MAX_REQUEST_USD, MAX_SECONDS = Decimal("0.10"), Decimal("0.06"), 900.0
PRICES = ("input_price_per_mtok", "cached_input_price_per_mtok", "output_price_per_mtok")
READ_TOOLS = frozenset({"search_customers", "get_customer_overview", "search_items", "get_availability",
                        "explain_stock_discrepancy", "list_operations", "get_operation_timeline"})
USER, ACCOUNT = "daniel", "KND-0190"
QUESTIONS = ("Find the SKARVIK customer Kiruna Gruvteknik. What is its account number, and is it on credit hold?",
             "In which city and country is that customer located? Look it up for the same account.")
Driver = Callable[[str, Path], list[dict[str, Any]]]


# --------------------------------------------------------------------------------------------- preconditions
def commit_state() -> tuple[str, bool]:
    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(REPO), *a], check=True, capture_output=True,  # noqa: S603, S607
                              text=True).stdout.strip()
    return git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=no") == ""


def refusal(config: AssistantConfig, auth: dict[str, Any] | None, *, commit: str, clean: bool,
            now: datetime | None = None) -> str | None:
    """Why this run may not start (``None`` ⇒ it may). Sends nothing."""
    now = now or datetime.now(UTC)
    if config.live_problems():
        return "configuration incomplete: " + ", ".join(config.live_problems())
    if auth is None:
        return "no readable owner authorization"
    if auth.get("purpose") != "live_validation" or auth.get("provider") != "openai":
        return "authorization is not for a live_validation with openai"
    if not isinstance(auth.get("run_id"), str) or not RUN_ID.fullmatch(auth["run_id"]):
        return "run_id missing or invalid"
    if (reason := model_refusal(config, auth)) is not None:
        return reason
    if not config.model_explicit:
        return "OPENAI_MODEL must be set explicitly for a run"
    if auth.get("commit") != commit or not clean:
        return "authorization names another commit, or tracked files are modified"
    try:
        start, end = (datetime.fromisoformat(str(auth[k]).replace("Z", "+00:00"))
                      for k in ("approved_at", "expires_at"))
        if not start <= now < end or end - start > timedelta(seconds=MAX_SECONDS):
            return "outside the authorized window, or the window exceeds 15 minutes"
    except (KeyError, ValueError, TypeError):
        return "authorization window unreadable"
    for name, want in (("max_requests", MAX_REQUESTS), ("max_output_tokens", MAX_OUTPUT_TOTAL), ("max_concurrency", 1)):
        if type(auth.get(name)) is not int or not 1 <= auth[name] <= want:
            return f"authorization {name} must be an integer 1..{want}"
    for name, limit in (("max_estimated_usd", MAX_USD), ("max_request_cost_usd", MAX_REQUEST_USD)):
        value = _dec(auth.get(name))
        if value is None or not 0 < value <= limit:
            return f"authorization {name} must be in (0, {limit}]"
    for name in PRICES:  # entered by the owner for this run, with their source; none carries over from another model
        value = _dec(auth.get(name))
        if value is None or value <= 0 or value != getattr(config, name):
            return f"{name} must be positive and equal the configured price"
    if auth.get("price_source_url") != config.price_source or auth.get("price_observed_at") != config.price_observed_at:
        return "price source or date differs from the configuration"
    return None


def run_config(config: AssistantConfig, auth: dict[str, Any]) -> AssistantConfig:
    """Ceilings forced to the authorized values. The DAILY budget of the fresh database is the run budget."""
    usd = Decimal(str(auth["max_estimated_usd"]))
    return dataclasses.replace(
        config, max_retries=0, max_concurrency=1, max_requests_per_turn=MAX_PER_TURN,
        max_output_tokens=MAX_OUTPUT_REQUEST, timeout_s=min(config.timeout_s, 30.0), max_turn_cost_usd=usd,
        daily_budget_usd=usd, max_request_cost_usd=Decimal(str(auth["max_request_cost_usd"])))


# --------------------------------------------------------------------------------------------- hard limits
class LiveProvider(CappedProvider):
    """Durable request slots (``CappedProvider``) plus output-token, time and read-tool limits. Refusals are local."""

    def __init__(self, inner: Any, limit: int, ledger: Ledger, run_id: str, invocation_id: str, *,
                 output_limit: int = MAX_OUTPUT_TOTAL, deadline: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        super().__init__(inner, limit, ledger, run_id, invocation_id)
        self.monotonic = monotonic
        self.deadline = deadline if deadline is not None else monotonic() + MAX_SECONDS
        self.output_limit, self.output_used = output_limit, 0
        self.stop: str | None = None
        self.context: list[dict[str, Any]] = []
        self.tool_results: dict[str, Any] = {}

    def run(self, request: ProviderRequest, max_attempts: int | None = None) -> ProviderTurn:
        texts = [(m.role, m.text) for m in request.messages if m.text is not None]
        self.context.append({"scenario": self.scenario, "messages": texts})
        for m in request.messages:
            if m.tool_result is not None:
                self.tool_results[m.tool_result.call_id] = json.loads(m.tool_result.output_json)
        if self.stop is None and self.monotonic() + self.inner.config.timeout_s > self.deadline:
            self.stop = "TIME_LIMIT"
        if self.stop is None and self.output_used + self.inner.config.max_output_tokens > self.output_limit:
            self.stop = "OUTPUT_TOKEN_LIMIT"
        if self.stop is not None:
            return self._deny(self.stop)
        before = self.attempted
        turn = super().run(request, max_attempts=1)
        if self.attempted > before:
            entry = self.log[-1]
            if entry["outcome"] != "NOT_SENT_LOCAL":
                reported = turn.usage.output_tokens
                self.output_used += self.inner.config.max_output_tokens if reported is None else reported
            if self.guard.closed:
                self.stop = "MODEL_MISMATCH"
            forbidden = [c.name for c in turn.tool_calls if c.name not in READ_TOOLS]
            if forbidden:
                self.stop = "FORBIDDEN_TOOL"
            entry["tool_arguments"] = [c.arguments_json[:300] for c in turn.tool_calls]
            if self.stop is not None:  # nothing of this response is executed or shown
                return ProviderTurn(kind="ERROR", error_category=ErrorCategory.MODEL_MISMATCH if self.guard.closed
                                    else ErrorCategory.BAD_REQUEST)
        return turn


class RecordingService(AssistantService):
    """The unchanged service; only keeps the outcome objects of this run (the API does not expose the branch code)."""

    def __init__(self, svc: Any, provider: Any, config: AssistantConfig) -> None:
        super().__init__(svc, provider, config)
        self.outcomes: list[TurnOutcome] = []

    def run_turn(self, *a: Any, **kw: Any) -> TurnOutcome:
        self.provider.scenario = f"T{len(self.outcomes) + 1}"  # type: ignore[attr-defined]
        out = super().run_turn(*a, **kw)
        self.outcomes.append(out)
        return out


def browser_driver(origin: str, out: Path) -> list[dict[str, Any]]:
    """Signs in and asks both questions in the assistant drawer (Playwright)."""
    from evals.harness.env import PASSWORD

    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI", "LBF_"))}
    r = subprocess.run([node, str(REPO / "evals/harness/live_drawer.mjs"), origin, str(out), USER,  # noqa: S603
                        json.dumps(QUESTIONS)], cwd=REPO, capture_output=True, text=True, timeout=MAX_SECONDS,
                       env={**env, "LBF_DRIVER_PASSWORD": PASSWORD}, check=False)
    if r.returncode != 0:
        raise RuntimeError("drawer driver failed: " + r.stderr[-1500:])
    return list(json.loads(r.stdout.strip().splitlines()[-1]))


def _live_inner(env: Any, config: AssistantConfig, spend: SpendLedger | None = None, run_id: str = "") -> Any:
    """``spend``: the expenditure ceiling this run shares with every other paid run under the same ceiling."""
    from lbf.assistant.budget import BudgetService
    from lbf.assistant.providers.openai_provider import OpenAIProvider
    from lbf.assistant.telemetry import Telemetry

    budget = BudgetService(env.engine, config, env.clock) if spend is None else SpendBudget(
        env.engine, config, env.clock, spend=spend, campaign=run_id)
    return OpenAIProvider(config, budget=budget,
                          telemetry=Telemetry(env.engine, env.clock), clock=env.clock)


# --------------------------------------------------------------------------------------------- acceptance
def facts_text(outcome: TurnOutcome) -> str:
    return " ".join([outcome.answer.summary, *(f.claim for f in outcome.answer.facts)])


def judge(outcomes: list[TurnOutcome], provider: LiveProvider, shown: list[dict[str, Any]], *, unchanged: bool,
          handoffs: int) -> dict[str, bool]:
    from lbf.assistant.answer import resolve

    seed = json.loads((REPO / "legacy-systems/skarvik-oes/seed/seed.json").read_text(encoding="utf-8"))
    truth = next(c for c in seed["customers"] if c["custno"] == ACCOUNT)
    checks: dict[str, bool] = {"two_turns": len(outcomes) == 2 and len(shown) == 2}
    for i, o in enumerate(outcomes[:2], 1):
        calls = {c["name"] for c in o.tool_calls}
        checks[f"t{i}_not_fallback"] = not o.fallback_used and o.fallback_reason is None
        checks[f"t{i}_validation_ok"] = o.validation_failure is None
        checks[f"t{i}_read_tools_only"] = bool(calls) and calls <= READ_TOOLS and not o.refused_calls \
            and all(c["permitted"] for c in o.tool_calls)
        checks[f"t{i}_tools_succeeded"] = all("error" not in provider.tool_results.get(c["call_id"], {"error": 1})
                                               for c in o.tool_calls)
        checks[f"t{i}_cited_facts"] = bool(o.answer.facts) and all(
            resolve(provider.tool_results.get(f.source.tool_call_id), f.source.field_path) is not None
            for f in o.answer.facts)
        checks[f"t{i}_no_draft"] = o.answer.draft_operation_id is None
        checks[f"t{i}_shown_in_drawer"] = len(shown) >= i and " ".join(o.answer.summary.split()) in " ".join(
            str(shown[i - 1].get("text", "")).split()) \
            and shown[i - 1].get("question") == QUESTIONS[i - 1]
    if len(outcomes) == 2:
        def cited(o: TurnOutcome, value: str) -> bool:
            return any(value.casefold() in json.dumps(resolve(provider.tool_results.get(f.source.tool_call_id),
                                                              f.source.field_path), ensure_ascii=False).casefold()
                       and value.casefold() in f.claim.casefold() for f in o.answer.facts)
        t1, t2 = facts_text(outcomes[0]).casefold(), facts_text(outcomes[1]).casefold()
        checks["t1_account_cited"] = cited(outcomes[0], ACCOUNT)
        checks["t1_credit_hold_correct"] = truth["sparr"] == "J" and "hold" in t1 and not re.search(
            r"\b(not|isn.t|no)\b[^.]{0,20}\bhold\b", t1)
        checks["t2_city_cited"] = cited(outcomes[1], truth["city"])
        checks["t2_country"] = bool(re.search(r"\bse\b|sweden", t2))
        t2_requests = [c["messages"] for c in provider.context if c["scenario"] == "T2"]
        checks["t2_has_session_context"] = bool(t2_requests) and all(
            m[0] == ("user", QUESTIONS[0]) and m[1][0] == "assistant" and ACCOUNT in (m[1][1] or "")
            and m[2] == ("user", QUESTIONS[1]) for m in t2_requests)
        t2_args = " ".join(a for r in provider.log if r["scenario"] == "T2" for a in r.get("tool_arguments", []))
        checks["t2_tool_used_context"] = ACCOUNT in t2_args or truth["name"].split()[0].casefold() in t2_args.casefold()
        checks["t1_started_without_context"] = all(len(c["messages"]) >= 1 and c["messages"][0] == (
            "user", QUESTIONS[0]) for c in provider.context if c["scenario"] == "T1")
    checks["workflow_unchanged"] = unchanged
    checks["no_handoffs"] = handoffs == 0
    checks["within_requests"] = 0 < provider.attempted <= MAX_REQUESTS
    checks["within_output_tokens"] = provider.output_used <= provider.output_limit
    checks["no_stop_or_ledger_failure"] = provider.stop is None and provider.ledger_failure is None
    checks["all_responses_observed"] = all(r["outcome"] == "RESPONSE_OBSERVED" for r in provider.log)
    return checks


def execute(config: AssistantConfig, auth: dict[str, Any], *, ledger_path: Path, out_root: Path,
            driver: Driver = browser_driver, env: Any = None, provider_factory: Callable[[Any, AssistantConfig], Any]
            | None = None, static_dir: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Exit code 0 every criterion holds, 1 LIVE FAIL, 2 refused (nothing sent)."""
    import uvicorn

    from lbf.api.app import create_app
    from lbf.api.services import AppConfig, AppServices
    from lbf.assistant.tables import inference_budget_ledger as budget_rows

    if (problem := model_refusal(config, auth)) is not None:
        return 2, {"status": "REFUSED", "error": "CONFIG_REJECTED", "reason": problem}
    run_id = str(auth["run_id"])
    out = out_root / run_id
    if out.exists():
        return 2, {"status": "REFUSED", "reason": "evidence for this run already exists"}
    static = static_dir or REPO / "frontend" / "dist"
    if driver is browser_driver and not (static / "index.html").is_file():
        return 2, {"status": "REFUSED", "reason": "console not built"}
    try:
        ledger = Ledger(ledger_path)
    except LedgerError as exc:
        return 2, {"status": "REFUSED", "error": "LEDGER_UNAVAILABLE", "reason": str(exc)}
    own_env, server, status = env is None, None, "CRASHED"
    invocation = "-"
    provider: LiveProvider | None = None
    try:
        if auth.get("ledger_id") != ledger.ledger_id:
            return 2, {"status": "REFUSED", "error": "LEDGER_UNAVAILABLE",
                       "reason": "authorization names another ledger"}
        conflict = ledger.bind(auth)
        try:
            invocation = ledger.start_invocation(run_id, only=True) if conflict is None else "-"
        except NotAdmitted as exc:
            conflict = str(exc)
        if conflict is not None:
            return 2, {"status": "REFUSED", "error": "AUTHORIZATION_CONSUMED", "reason": conflict}
        started = datetime.now(UTC)
        if own_env:
            from evals.harness.env import EvalEnv

            env = EvalEnv().build()
        cfg = run_config(config, auth)
        inner = (provider_factory or _live_inner)(env, cfg)
        if not getattr(inner, "ready", False):
            status = "REFUSED"
            return 2, {"status": "REFUSED", "error": "CONFIG_REJECTED", "reason": "provider not ready"}
        provider = LiveProvider(inner, int(auth["max_requests"]), ledger, run_id, invocation,
                                output_limit=int(auth["max_output_tokens"]))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        svc = AppServices(config=AppConfig(profile="test", public_origin=origin, engines_enabled=False,
                                           static_dir=static, assistant_provider="openai"),
                          engine=env.engine, audit_engine=None, auth=env.svc.auth, workflow=env.workflow)
        service = RecordingService(svc, provider, cfg)
        svc.extras["assistant"] = service
        server = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        before = _workflow_counts(env.engine)
        sent_before = len(env.halden.received_refs) + len(env.skarvik.received())
        out.mkdir(parents=True)
        shown: list[dict[str, Any]] = []
        driver_error: str | None = None
        try:
            shown = driver(origin, out)
        except Exception as exc:  # recorded as a failure; whatever was sent is in the ledger
            driver_error = f"{type(exc).__name__}: {str(exc)[-600:]}"
        unchanged = _workflow_counts(env.engine) == before
        handoffs = len(env.halden.received_refs) + len(env.skarvik.received()) - sent_before
        checks = judge(service.outcomes, provider, shown, unchanged=unchanged, handoffs=handoffs)
        checks["driver_ok"] = driver_error is None
        with env.engine.connect() as conn:
            rows = [r for r in conn.execute(budget_rows.select()).all() if r.scope == "DAILY"]
        estimate = sum((r.amount_usd for r in rows if r.entry == "SETTLE"), Decimal(0))
        checks["within_cost"] = estimate <= Decimal(str(auth["max_estimated_usd"])) and len(
            [r for r in rows if r.entry == "SETTLE"]) == len([r for r in rows if r.entry == "RESERVE"])
        passed = all(checks.values())
        status = ABORTED if provider.guard.closed else "LIVE PASS" if passed else "LIVE FAIL"
        summary = {
            "status": status if getattr(inner, "transport_label", "") == "live" else status.replace("LIVE", "MOCK"),
            "run_id": run_id, "contract": "two-turn live validation", "commit": auth.get("commit"),
            "transport": getattr(inner, "transport_label", None), "provider": inner.name, "model": cfg.model,
            "model_observed": provider.guard.observed,
            "started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(), "checks": checks,
            "driver_error": driver_error, "stop": provider.stop, "requests_attempted": provider.attempted,
            "requests": provider.log, "output_tokens_counted": provider.output_used,
            "estimate_usd": str(estimate),
            "limits": {"requests": auth["max_requests"], "per_turn": cfg.max_requests_per_turn, "retries": 0,
                       "output_total": auth["max_output_tokens"], "output_request": cfg.max_output_tokens,
                       "usd": auth["max_estimated_usd"], "usd_request": auth["max_request_cost_usd"]},
            "turns": [{"question": q, "fallback_used": o.fallback_used, "fallback_reason": o.fallback_reason,
                       "validation_failure": o.validation_failure, "answer": o.answer.model_dump(mode="json"),
                       "tool_calls": [{"call_id": c["call_id"], "name": c["name"], "permitted": c["permitted"],
                                       "result": provider.tool_results.get(c["call_id"])} for c in o.tool_calls]}
                      for q, o in zip(QUESTIONS, service.outcomes, strict=False)],
            "shown_in_drawer": shown, "workflow_counts": before, "handoffs": handoffs,
            "ledger": ledger.summary(run_id), "note": "fictional data; estimate only, the invoice is authoritative"}
        _write_summary(out, summary)
        return (ABORT_EXIT if provider.guard.closed else 0 if passed else 1), summary
    finally:
        if server is not None:
            server.should_exit = True
        if invocation != "-":
            finish_run(provider, ledger, run_id, invocation, status)
        ledger.close()
        if own_env and env is not None:
            env.close()


def _write_summary(out: Path, summary: dict[str, Any]) -> None:
    data = (json.dumps(summary, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode()
    (out / "summary.json").write_bytes(data)
    (out / "summary.json.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  summary.json\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bounded live assistant validation")
    parser.add_argument("command", choices=["init-ledger", "preflight", "run"])
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--spend-ledger", type=Path, help="required by run")
    args = parser.parse_args(argv)
    if args.command == "init-ledger":
        try:
            print(json.dumps({"status": "INITIALIZED", "ledger_id": init_ledger(
                args.ledger, max_cap=MAX_REQUESTS, purpose="live_validation")}))
        except LedgerError as exc:
            print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
            return 2
        return 0
    config = AssistantConfig.from_env()
    auth, problem = read_authorization()
    commit, clean = commit_state()
    reason = problem or refusal(config, auth, commit=commit, clean=clean)
    if reason is not None or auth is None:
        print(json.dumps({"status": "REFUSED", "reason": reason}))
        return 2
    if os.environ.get("LBF_LIVE_VALIDATION_EXECUTE") != auth["run_id"] or args.command != "run" or args.out is None \
            or args.spend_ledger is None:
        print(json.dumps({"status": "AUTHORIZED_NOT_EXECUTED", "run_id": auth["run_id"], "commit": commit}))
        return 0 if args.command == "preflight" else 2
    try:
        spend = SpendLedger(args.spend_ledger)
    except ValueError as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}))
        return 2
    code, summary = execute(config, auth, ledger_path=args.ledger, out_root=args.out,
                            provider_factory=lambda env, cfg: _live_inner(env, cfg, spend, str(auth["run_id"])))
    print(json.dumps({k: summary.get(k) for k in ("status", "reason", "run_id", "requests_attempted", "stop",
                                                  "estimate_usd", "driver_error")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
