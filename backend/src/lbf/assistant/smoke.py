"""``make openai-smoke`` (optional). Refuses — with zero network calls — unless LBF_LIVE_INFERENCE=true,
the configuration is complete and ``governance/eval/AUTHORIZATION.json`` is valid, unexpired, purpose ``smoke`` and
names the configured model.

A valid authorization alone still refuses. The live smoke executes only when, in addition, ``LBF_O1A_EXECUTE`` equals
the authorization's ``run_id`` (the owner's approval of that specific run) and the authorization's prices and caps
match the configuration the provider will actually use (B-1, B-3). Execution:

* a hard cap of ``max_requests`` (≤ 3) on model requests per AUTHORIZATION, across all invocations:
  ``CappedProvider`` around the unchanged ``OpenAIProvider`` delegates only after a slot is reserved and committed in
  the durable ``smoke_ledger`` (outside ``lbf_eval`` and the evidence directories); retries are forced to 0, so one
  ``run()`` sends at most one request; a reserved slot is never refunded (timeout, crash, unknown outcome); a
  missing or failing ledger means zero delegations (B-2);
* the authorization file is read once; that one snapshot is validated, bound to the ledger by ``run_id`` + content
  digest, and executed against (F-5); the same ``run_id`` with changed content is refused;
* the existing budget algorithm in ``EVAL`` scope keyed by ``run_id`` with ``max_estimated_usd`` as the run budget;
* the offline evaluation environment (fresh ``lbf_eval`` database, fake backends; no real backend, no operational data);
* scope: S1 connectivity/authentication/model acceptance/structured output (1 request), S2 one strict
  function-tool round trip through the unchanged assistant service and guard (≤ 2 requests). The live injection probe
  is not part of this smoke;
* evidence ``governance/eval/openai-smoke/<run_id>/summary.json`` + ``.sha256`` without prompts, outputs or the key;
  an existing evidence directory is never overwritten.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import os
import re
import sys
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from lbf.assistant.config import AUTHORIZED_OPENAI_MODEL, AssistantConfig
from lbf.assistant.model_guard import ModelGuard, recordable
from lbf.assistant.provider import (
    ErrorCategory,
    ProviderMessage,
    ProviderRequest,
    ProviderTurn,
    TurnLimits,
)
from lbf.assistant.smoke_ledger import Ledger, LedgerError, NotAdmitted, init_ledger

REPO = Path(__file__).resolve().parents[4]
# Durable per-authorization consumption ledger. Deliberately not overridable from the environment.
LEDGER_PATH = REPO / "governance" / "eval" / "smoke-ledger" / "authorization_ledger.sqlite3"
SMOKE_MAX_REQUESTS = 3
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
S1_PROMPT = ("Connectivity check. Do not call any tool. Return an AssistantAnswer with summary \"connectivity check\", "
             "no facts, no warnings, draft_operation_id null and next_human_actions [\"none\"].")
S2_PROMPT = "Find Göta Hamnservice in SKARVIK."  # read-only; evaluation task T02's prompt
S2_USER = "daniel"  # evaluation persona (SALES_OPS: assistant.use, customer.read)
S2_TURN_REQUESTS = 2  # tool call + final answer
ABORTED = "ABORTED_MODEL_MISMATCH"
ABORT_EXIT = 4
NOT_SENT = {ErrorCategory.BUDGET_DENIED, ErrorCategory.CONFIG_REJECTED}
UNKNOWN = {ErrorCategory.TIMEOUT, ErrorCategory.CONNECTION}


def _authorization_path() -> Path:
    return Path(os.environ.get("LBF_EVAL_AUTHORIZATION", "governance/eval/AUTHORIZATION.json"))


Snapshot = tuple[dict[str, Any] | None, str | None]


def read_authorization() -> Snapshot:
    """Read the authorization file ONCE: ``(snapshot, None)`` or ``(None, reason)`` (F-5)."""
    path = _authorization_path()
    if not path.is_file():
        return None, f"no owner authorization file at {path}"
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "authorization unreadable"
    return (auth, None) if isinstance(auth, dict) else (None, "authorization unreadable")


def refusal(config: AssistantConfig | None = None, snapshot: Snapshot | None = None) -> str | None:
    config = config or AssistantConfig.from_env()
    problems = config.live_problems()
    if problems:
        return "configuration incomplete: " + ", ".join(problems)
    if not config.model_explicit:
        return "OPENAI_MODEL must be set explicitly for a run"
    auth, problem = snapshot if snapshot is not None else read_authorization()
    if auth is None:
        return problem or "authorization unreadable"
    try:
        expires = datetime.fromisoformat(str(auth["expires_at"]).replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return "authorization unreadable"
    if auth.get("purpose") != "smoke" or auth.get("provider") != "openai" \
            or model_refusal(config, auth):
        return "authorization does not cover a smoke test of the authorized model"
    if expires <= datetime.now(UTC):
        return "authorization expired"
    if int(auth.get("max_requests", 0)) > 3:
        return "a smoke test is limited to 3 requests"
    return None


# ------------------------------------------------------------------------------------------- B-3 cross-checks
def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def model_refusal(config: AssistantConfig, auth: dict[str, Any]) -> str | None:
    """Equality is not enough: both the authorization and the configuration must name the authorized model."""
    if auth.get("model") != AUTHORIZED_OPENAI_MODEL or not config.model_authorized:
        return "the authorization model and OPENAI_MODEL must both be the authorized model"
    return None


def execution_refusal(config: AssistantConfig, auth: dict[str, Any], requested_run_id: str | None,
                      request_cap: int = SMOKE_MAX_REQUESTS) -> str | None:
    """Why the authorized run may not execute with this configuration (``None`` ⇒ it may). Never sends anything."""
    run_id = auth.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        return "authorization run_id missing or invalid ([A-Za-z0-9_.-], ≤ 64 characters)"
    if requested_run_id != run_id:
        return "LBF_O1A_EXECUTE does not name the authorized run_id"
    if (reason := model_refusal(config, auth)) is not None:
        return reason
    max_requests = auth.get("max_requests")
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or not 1 <= max_requests <= request_cap:
        return f"authorization max_requests must be an integer 1..{request_cap}"
    budget = _dec(auth.get("max_estimated_usd"))
    if budget is None or budget <= 0:
        return "authorization max_estimated_usd must be a positive number"
    for name, configured in (("input_price_per_mtok", config.input_price_per_mtok),
                             ("cached_input_price_per_mtok", config.cached_input_price_per_mtok),
                             ("output_price_per_mtok", config.output_price_per_mtok)):
        authorized = _dec(auth.get(name))
        if authorized is None or configured is None or authorized != configured:
            return f"authorization {name} differs from the configured price"
    if auth.get("price_source_url") != config.price_source:
        return "authorization price_source_url differs from LBF_OPENAI_PRICE_SOURCE"
    if auth.get("price_observed_at") != config.price_observed_at:
        return "authorization price_observed_at differs from LBF_OPENAI_PRICE_OBSERVED_AT"
    max_output = auth.get("max_output_tokens")
    if not isinstance(max_output, int) or isinstance(max_output, bool) or config.max_output_tokens > max_output:
        return "LBF_OPENAI_MAX_OUTPUT_TOKENS exceeds authorization max_output_tokens"
    request_cost = _dec(auth.get("max_request_cost_usd"))
    if request_cost is None or config.max_request_cost_usd > request_cost:
        return "LBF_OPENAI_MAX_REQUEST_COST_USD exceeds authorization max_request_cost_usd"
    concurrency = auth.get("max_concurrency")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or config.max_concurrency > concurrency:
        return "LBF_OPENAI_MAX_CONCURRENCY exceeds authorization max_concurrency"
    return None


# ------------------------------------------------------------------------------------------- request cap
class CappedProvider:
    """Hard cap per AUTHORIZATION around an unchanged provider (B-2). Each delegation first reserves and commits
    a slot in the durable ledger, then commits ``PROVIDER_DELEGATION_STARTED``, then calls the inner provider (retries
    forced to 0 ⇒ at most one request). No slot ⇒ no delegation. Any ledger failure stops all further delegations of
    this invocation; there is no fallback to an in-memory counter. ``attempted`` only reports this invocation."""

    def __init__(self, inner: Any, limit: int, ledger: Ledger, run_id: str, invocation_id: str) -> None:
        if getattr(getattr(inner, "config", None), "max_retries", 0) != 0:
            raise ValueError("the O1a cap requires max_retries = 0")
        self.inner = inner
        self.limit = limit
        self.ledger = ledger
        self.run_id = run_id
        self.invocation_id = invocation_id
        self.attempted = 0
        self.ledger_failure: str | None = None
        self.scenario = ""
        self.log: list[dict[str, Any]] = []
        self.name = inner.name
        self.model = inner.model
        self.transport_label = getattr(inner, "transport_label", "")  # what the console says about this run
        self._lock = threading.RLock()  # the counter, the log and the ledger connection; never held while delegating
        self.guard = ModelGuard(pin_version=True, observed=ledger.observed_model(run_id), on_abort=self._abort)
        inner.campaign = self.guard

    def _deny(self, outcome: str, slot: int | None = None,
              category: ErrorCategory = ErrorCategory.BUDGET_DENIED) -> ProviderTurn:
        self.log.append({"scenario": self.scenario, "request_no": None, "ledger_slot": slot, "outcome": outcome,
                         "error_category": category.value})
        return ProviderTurn(kind="ERROR", error_category=category)

    def _abort(self, reported: object) -> None:
        """Durable end of this authorization, recorded before the response's outcome and whichever thread saw it."""
        try:
            with closing(Ledger(self.ledger.path)) as ledger:
                ledger.record(self.run_id, self.invocation_id, "CAMPAIGN_ABORTED", scenario=self.scenario,
                              detail={"status": ABORTED, "model_reported": recordable(reported)})
        except LedgerError as exc:
            self._ledger_failed(exc)

    def finish(self, status: str) -> None:
        """The end of this invocation, as far as it can be proven. Not recorded ⇒ the authorization never resumes."""
        abort = {"status": ABORTED, "model_reported": recordable(self.guard.unexpected)} if self.guard.closed else None
        try:
            with closing(Ledger(self.ledger.path)) as ledger:
                ledger.finish_invocation(self.run_id, self.invocation_id, {"status": status}, abort=abort,
                                         failed=self.ledger_failure is not None)
        except LedgerError as exc:
            self._ledger_failed(exc)

    def _ledger_failed(self, exc: LedgerError) -> None:
        self.ledger_failure = self.ledger_failure or str(exc)

    def run(self, request: ProviderRequest, max_attempts: int | None = None) -> ProviderTurn:
        if self.guard.closed:
            return self._deny(ABORTED, category=ErrorCategory.MODEL_MISMATCH)
        try:
            with closing(Ledger(self.ledger.path)) as ledger:  # SQLite connections belong to one thread
                return self._capped(ledger, request)
        except LedgerError as exc:
            self._ledger_failed(exc)
            return self._deny("LEDGER_UNAVAILABLE")

    def _capped(self, ledger: Ledger, request: ProviderRequest) -> ProviderTurn:
        if self.ledger_failure is not None:
            return self._deny("LEDGER_UNAVAILABLE")
        try:
            slot = ledger.reserve(self.run_id, self.invocation_id, self.scenario)
        except LedgerError as exc:
            self._ledger_failed(exc)
            return self._deny("LEDGER_UNAVAILABLE")
        if slot is None:
            return self._deny("CAP_DENIED")
        with self._lock:
            self.attempted += 1
            request_no = self.attempted
        try:
            ledger.record(self.run_id, self.invocation_id, "PROVIDER_DELEGATION_STARTED", slot_no=slot,
                               scenario=self.scenario)
        except LedgerError as exc:  # the slot stays consumed; nothing is delegated
            self._ledger_failed(exc)
            return self._deny("LEDGER_UNAVAILABLE", slot)
        turn: ProviderTurn = self.inner.run(request, max_attempts=1)
        if turn.kind != "ERROR" and not self.guard.observe(turn.model_reported):
            turn = ProviderTurn(kind="ERROR", error_category=ErrorCategory.MODEL_MISMATCH, usage=turn.usage,
                                model_reported=turn.model_reported, provider_request_id=turn.provider_request_id,
                                latency_ms=turn.latency_ms)
        category = turn.error_category
        unsent = category is ErrorCategory.MODEL_MISMATCH and turn.provider_request_id is None
        outcome = ("NOT_SENT_LOCAL" if category in NOT_SENT else "SENT_OR_UNKNOWN" if category in UNKNOWN or unsent
                   else "RESPONSE_OBSERVED")
        entry: dict[str, Any] = {
            "scenario": self.scenario, "request_no": request_no, "ledger_slot": slot, "outcome": outcome,
            "kind": turn.kind, "error_category": category.value if category else None,
            "model_reported": turn.model_reported, "provider_request_id": turn.provider_request_id,
            "latency_ms": turn.latency_ms,
            "usage_reported": {"input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens,
                               "cached_input_tokens": turn.usage.cached_input_tokens},
            "final_sha256": hashlib.sha256(turn.final_json.encode()).hexdigest() if turn.final_json else None,
            "tool_calls": [c.name[:80] for c in turn.tool_calls]}
        try:
            ledger.record(self.run_id, self.invocation_id, outcome, slot_no=slot, scenario=self.scenario,
                               detail={"kind": turn.kind, "error_category": entry["error_category"],
                                       "provider_request_id": turn.provider_request_id,
                                       "model_reported": turn.model_reported})
        except LedgerError as exc:  # unresolved in the ledger ⇒ read as sent-or-unknown; no further delegation
            self._ledger_failed(exc)
            entry["ledger_outcome_recorded"] = False
        self.log.append(entry)
        return turn


def smoke_config(config: AssistantConfig) -> AssistantConfig:
    """The configuration actually used: retries forced off; one tool round trip per turn."""
    return dataclasses.replace(config, max_retries=0, max_requests_per_turn=S2_TURN_REQUESTS)


def s1_request(run_id: str, config: AssistantConfig | None = None) -> ProviderRequest:
    from lbf.assistant.answer import output_schema
    from lbf.assistant.prompt import PROMPT_VERSION, SYSTEM_PROMPT
    from lbf.assistant.service import MAX_TOOL_CALLS
    from lbf.assistant.tools import REGISTRY

    c = config or AssistantConfig()
    # The production request shape (all allowlisted strict tools offered); the prompt asks for no tool use.
    return ProviderRequest(system_prompt=SYSTEM_PROMPT, prompt_version=PROMPT_VERSION,
                           messages=(ProviderMessage(role="user", text=S1_PROMPT),),
                           tools=tuple(t.spec() for t in REGISTRY.values()), output_schema=output_schema(),
                           limits=TurnLimits(timeout_s=c.timeout_s, max_output_tokens=c.max_output_tokens,
                                             max_requests=1, max_tool_calls=MAX_TOOL_CALLS),
                           scope="EVAL", eval_run_id=run_id)


def _default_env() -> Any:
    """The offline evaluation environment (fresh ``lbf_eval`` database, in-process fake backends)."""
    if not (REPO / "evals" / "harness" / "env.py").is_file():
        raise RuntimeError("offline evaluation environment unavailable (run from the repository checkout)")
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    env_module = importlib.import_module("evals.harness.env")  # repository-level package, not part of ``lbf``
    return env_module.EvalEnv().build()


def _live_provider(env: Any, config: AssistantConfig, eval_budget: Decimal) -> Any:
    from lbf.assistant.budget import BudgetService
    from lbf.assistant.providers.openai_provider import OpenAIProvider
    from lbf.assistant.telemetry import Telemetry

    return OpenAIProvider(config, budget=BudgetService(env.engine, config, env.clock, eval_budget),
                          telemetry=Telemetry(env.engine, env.clock), clock=env.clock)


def _workflow_counts(engine: Any) -> dict[str, int]:
    from sqlalchemy import func, select

    from lbf.persistence import tables as t

    with engine.connect() as conn:
        return {tb.name: int(conn.execute(select(func.count()).select_from(tb)).scalar() or 0)
                for tb in (t.operations, t.approvals, t.authorizations, t.transmissions, t.escalations,
                           t.draft_versions)}


def _ledger(engine: Any, run_id: str) -> dict[str, str]:
    from sqlalchemy import func, select

    from lbf.assistant.tables import inference_budget_ledger as L

    with engine.connect() as conn:
        def total(entry: str) -> Decimal:
            return Decimal(conn.execute(select(func.coalesce(func.sum(L.c.amount_usd), 0)).where(
                L.c.scope == "EVAL", L.c.scope_key == run_id, L.c.entry == entry)).scalar())
        return {"reserved_usd": str(total("RESERVE")), "estimated_cost_usd": str(total("SETTLE"))}


def _write_evidence(out_dir: Path, summary: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True)
    data = (json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    (out_dir / "summary.json").write_bytes(data)
    (out_dir / "summary.json.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  summary.json\n",
                                                 encoding="utf-8")


def finish_run(capped: CappedProvider | None, ledger: Ledger, run_id: str, invocation_id: str, status: str) -> None:
    """A missing INVOCATION_FINISHED marks the invocation as unresolved (conservative): nothing further is admitted."""
    if capped is not None:
        capped.finish(status)
        return
    try:
        ledger.finish_invocation(run_id, invocation_id, {"status": status})
    except LedgerError:
        pass


def _open_ledger(path: Path, auth: dict[str, Any]) -> tuple[Ledger | None, str | None, dict[str, Any] | None]:
    """Open the ledger, bind the authorization identity and require remaining capacity — before any side effect.
    Returns ``(ledger, invocation_id, None)`` or ``(None, None, refusal)``."""
    run_id = str(auth["run_id"])
    try:
        ledger = Ledger(path)
    except LedgerError as exc:
        return None, None, {"status": "REFUSED", "error": "LEDGER_UNAVAILABLE", "reason": str(exc)}
    try:
        conflict = ledger.bind(auth)
        if conflict is not None:
            ledger.close()
            return None, None, {"status": "REFUSED", "error": "AUTHORIZATION_IDENTITY_CONFLICT", "reason": conflict}
        if ledger.consumed(run_id) >= int(auth["max_requests"]):
            ledger.record(run_id, "-", "INVOCATION_REFUSED", detail={"reason": "exhausted"})
            ledger.close()
            return None, None, {"status": "REFUSED", "error": "AUTHORIZATION_EXHAUSTED",
                                "reason": f"every request slot of run {run_id} is already consumed (durable ledger); "
                                          "a further run needs a new owner-approved run_id"}
        return ledger, ledger.start_invocation(run_id), None
    except NotAdmitted as exc:
        ledger.close()
        return None, None, {"status": "REFUSED", "error": "AUTHORIZATION_UNRESOLVED", "reason": str(exc)}
    except (LedgerError, ValueError) as exc:
        ledger.close()
        return None, None, {"status": "REFUSED", "error": "LEDGER_UNAVAILABLE", "reason": str(exc)}


def execute(config: AssistantConfig, auth: dict[str, Any], *, env: Any = None,
            provider_factory: Callable[[Any, AssistantConfig, Decimal], Any] | None = None,
            out_root: Path | None = None, ledger_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Run the authorized smoke. Exit code 0 all contract checks passed, 1 a check failed, 2 refused (nothing sent).
    Callers must have checked ``refusal()`` and ``execution_refusal()`` first, on the same snapshot ``auth``."""
    from lbf.assistant.answer import validate
    from lbf.assistant.service import AssistantService
    from lbf.assistant.tools import REGISTRY

    if (problem := model_refusal(config, auth)) is not None:
        return 2, {"status": "REFUSED", "error": "CONFIG_REJECTED", "reason": problem}
    run_id = str(auth["run_id"])
    out_dir = (out_root or REPO / "governance" / "eval" / "openai-smoke") / run_id
    if out_dir.exists():
        return 2, {"status": "REFUSED", "reason": f"evidence for run {run_id} already exists; never overwritten"}
    cap = int(auth["max_requests"])
    eval_budget = Decimal(str(auth["max_estimated_usd"]))
    cfg = smoke_config(config)
    ledger, invocation_id, refused = _open_ledger(ledger_path or LEDGER_PATH, auth)
    if ledger is None or invocation_id is None:
        return 2, refused or {"status": "REFUSED", "error": "LEDGER_UNAVAILABLE"}
    own_env = env is None
    started = datetime.now(UTC)
    status = "CRASHED"
    capped: CappedProvider | None = None
    try:
        if own_env:
            env = _default_env()
        inner = (provider_factory or _live_provider)(env, cfg, eval_budget)
        if not getattr(inner, "ready", False):
            status = "REFUSED"
            return 2, {"status": "REFUSED", "error": "CONFIG_REJECTED", "reason": "provider not ready"}
        capped = CappedProvider(inner, cap, ledger, run_id, invocation_id)
        before = _workflow_counts(env.engine)

        capped.scenario = "S1"
        turn = capped.run(s1_request(run_id, cfg))
        s1_valid = turn.kind == "FINAL" and turn.final_json is not None and validate(turn.final_json, {})[0] is not None
        s1 = {"passed": s1_valid, "kind": turn.kind,
              "error_category": turn.error_category.value if turn.error_category else None,
              "structured_output_valid": s1_valid}

        capped.scenario = "S2"
        outcome = AssistantService(env.svc, capped, cfg).run_turn(env.principal(S2_USER), S2_PROMPT,
                                                                   eval_run_id=run_id)
        calls = [{"name": c["name"][:80], "permitted": c["permitted"],
                  "refused_reason": next((r["reason"] for r in outcome.refused_calls
                                          if r["args_sha256"] == c["args_sha256"]), None)}
                 for c in outcome.tool_calls]
        s2_valid = (not outcome.fallback_used and bool(calls) and all(c["permitted"] for c in calls)
                    and all(c["name"] in REGISTRY for c in calls))
        s2 = {"passed": s2_valid, "fallback_used": outcome.fallback_used, "fallback_reason": outcome.fallback_reason,
              "validation_failure": outcome.validation_failure, "tool_calls": calls,
              "draft_operation_id": None if outcome.answer.draft_operation_id is None
              else str(outcome.answer.draft_operation_id)}

        unchanged = _workflow_counts(env.engine) == before
        passed = s1_valid and s2_valid and unchanged and capped.attempted <= cap and capped.ledger_failure is None
        try:
            ledger_view: dict[str, Any] = ledger.summary(run_id)
        except LedgerError as exc:
            ledger_view = {"error": str(exc)}
        summary = {
            "status": ABORTED if capped.guard.closed else "PASS" if passed else "FAIL", "run_id": run_id,
            "plan": "§17.9 O1a", "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(), "provider": inner.name,
            "model_configured": cfg.model, "model_observed": capped.guard.observed,
            "transport": getattr(inner, "transport_label", None),
            "authorization": {k: auth.get(k) for k in ("approved_by", "approved_at", "expires_at", "max_requests",
                                                       "max_output_tokens", "max_estimated_usd",
                                                       "max_request_cost_usd", "max_concurrency",
                                                       "input_price_per_mtok", "cached_input_price_per_mtok",
                                                       "output_price_per_mtok", "price_source_url",
                                                       "price_observed_at")},
            "effective_limits": {"request_cap": cap, "max_retries": cfg.max_retries,
                                 "max_requests_per_turn": cfg.max_requests_per_turn,
                                 "max_output_tokens": cfg.max_output_tokens, "timeout_s": cfg.timeout_s},
            "requests_attempted": capped.attempted,
            "requests_possibly_sent": sum(1 for r in capped.log if r["outcome"] in ("RESPONSE_OBSERVED",
                                                                                     "SENT_OR_UNKNOWN")),
            "requests": capped.log,
            "scenarios": {"S1_connectivity_structured_output": s1, "S2_function_tool_round_trip": s2},
            "workflow_state_unchanged": unchanged,
            "estimate": _ledger(env.engine, run_id) | {
                "note": "local estimate from configured prices; the OpenAI usage dashboard and invoice are "
                        "authoritative"},
            "not_in_scope": "live injection/authority probe (O1b, plan R17)",
            "authorization_ledger": ledger_view | {
                "invocation_id": invocation_id, "ledger_created_at": ledger.created_at,
                "slots_reserved_this_invocation": capped.attempted, "ledger_failure": capped.ledger_failure,
                "note": "slots reserved across ALL invocations of this authorization; reserved != sent != observed "
                        "!= billed"},
        }
        _write_evidence(out_dir, summary)
        status = summary["status"]
        return (ABORT_EXIT if capped.guard.closed else 0 if passed else 1), summary
    finally:
        finish_run(capped, ledger, run_id, invocation_id, status)
        ledger.close()
        if own_env and env is not None:
            env.close()


def _init_ledger_command() -> int:
    try:
        ledger_id = init_ledger(LEDGER_PATH)
    except LedgerError as exc:
        print(json.dumps({"status": "REFUSED", "error": "LEDGER_INIT_REFUSED", "reason": str(exc)}))
        return 2
    print(json.dumps({"status": "INITIALIZED", "ledger_id": ledger_id, "path": str(LEDGER_PATH)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv or []
    if args == ["init-ledger"]:
        return _init_ledger_command()
    if args:
        print(json.dumps({"status": "REFUSED", "reason": "usage: python -m lbf.assistant.smoke [init-ledger]"}))
        return 2
    config = AssistantConfig.from_env()
    snapshot = read_authorization()  # the single read of the authorization file (F-5)
    reason = refusal(config, snapshot)
    if reason is not None:
        print(json.dumps({"status": "REFUSED", "error": "CONFIG_REJECTED", "reason": reason}))
        return 2
    requested = os.environ.get("LBF_O1A_EXECUTE") or None
    if requested is None:
        print(json.dumps({"status": "REFUSED", "reason": "authorized, but not executed: set LBF_O1A_EXECUTE to the "
                          "authorization's run_id only with the owner's explicit approval of that specific run"}))
        return 2
    auth = snapshot[0]
    if auth is None:  # unreachable: refusal() rejects a missing snapshot
        return 2
    problem = execution_refusal(config, auth, requested)
    if problem is not None:
        print(json.dumps({"status": "REFUSED", "error": "CONFIG_REJECTED", "reason": problem}))
        return 2
    code, summary = execute(config, auth)
    print(json.dumps({k: summary.get(k) for k in ("status", "error", "run_id", "reason", "requests_attempted",
                                                   "requests_possibly_sent", "workflow_state_unchanged")}))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
