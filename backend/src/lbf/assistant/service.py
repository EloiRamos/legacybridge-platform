"""Assistant service — owns the tool-call loop (no SDK agent/runner abstraction).

Per turn: ≤ 8 tool calls, ≤ ``LBF_OPENAI_MAX_REQUESTS_PER_TURN`` model requests (retries and the single repair
request included), ≤ 60 s wall clock, budgets enforced before every request by the provider. Per user: ≤ 20 turns per
10 min. Every outcome that is not a validated, grounded answer falls back to the deterministic router or a template
of the raw tool results, with a user-visible notice naming the category. Nothing here can approve, submit,
redeliver, withdraw, resolve or administer: the only write tools are draft create/revise (registry, AI-SEC-01).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from lbf.api.services import AppServices
from lbf.assistant import guard, intent, receipts, router
from lbf.assistant.answer import AssistantAnswer, Evidence, Fact, Source, check, failure_code, output_schema, render
from lbf.assistant.config import AssistantConfig
from lbf.assistant.conversation import ConversationStore, remembered
from lbf.assistant.prompt import PROMPT_VERSION, SYSTEM_PROMPT
from lbf.assistant.provider import (
    AssistantProvider,
    ErrorCategory,
    ProviderMessage,
    ProviderRequest,
    ToolCall,
    ToolResult,
    TurnLimits,
)
from lbf.assistant.tools import REGISTRY
from lbf.auth.policy import Principal, has_permission
from lbf.domain.enums import Permission
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import repositories as repo
from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow.records import system_event, user_event

log = logging.getLogger("lbf.assistant")
APPLICATION = "application"  # a turn answered by the application itself, without a model
MAX_TOOL_CALLS = 8
PROPOSAL_TTL = timedelta(minutes=10)
CONFIRM = re.compile(r"\s*confirm\s+([0-9A-F]{6})\s*\.?\s*", re.IGNORECASE)
TURN_SECONDS = 60.0
USER_TURNS = 20
USER_WINDOW = 600.0
NOTICES = {
    "DISABLED": "The AI assistant is disabled; deterministic commands answered your request.",
    "BUDGET_DENIED": "AI assistant budget for today is exhausted; deterministic commands remain available",
    "CONFIG_REJECTED": "The AI assistant is not configured for live use; deterministic commands answered.",
    "MODEL_MISMATCH": "The provider did not report the authorized model; its output was discarded and the AI assistant "
                      "is stopped. Deterministic commands remain available.",
    "GROUNDING_FAILED": "The model's answer could not be verified against the tool results; showing the raw results.",
    "SCHEMA_INVALID": "The model's answer was not in the expected format; showing the raw results.",
    "LIMIT": "The assistant reached its per-turn limit; showing what it found.",
    "UNVERIFIED": "The outcome of the draft could not be verified. Check Orders before preparing it again.",
    "RECORD_FAILED": "The draft below was stored, but this turn could not be recorded. Do not prepare it again.",
    "RECORD_FAILED_UNVERIFIED": "This turn could not be recorded and the draft could not be verified. Check Orders "
                                "before preparing it again.",
}


@dataclass
class TurnOutcome:
    turn_id: uuid.UUID
    provider: str
    answer: AssistantAnswer
    fallback_used: bool
    fallback_reason: str | None
    notice: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    refused_calls: list[dict[str, Any]] = field(default_factory=list)
    validation_failure: str | None = None  # content-free branch code of the last rejected final answer
    receipts: list[receipts.Receipt] = field(default_factory=list)  # application-made, independent of the model
    proposal: Proposal | None = None  # a drafting call the person must confirm before it is executed


@dataclass(frozen=True)
class Proposal:
    """A structured draft the model proposed but the person's words did not establish. Nothing was written."""

    code: str
    tool: str
    description: str
    reasons: tuple[str, ...]
    expires_at: datetime
    arguments_json: str
    order: int = 0


@dataclass
class _Turn:
    """Mutable state of one running turn."""

    text: str
    results: list[tuple[ToolCall, guard.GuardOutcome]] = field(default_factory=list)
    receipts: list[receipts.Receipt] = field(default_factory=list)
    known_drafts: dict[str, int] = field(default_factory=dict)
    proposal: Proposal | None = None
    invalid: bool = False
    order: int = 0  # arrival order of the turn, so that a slow older turn cannot replace a newer proposal
    prior: Proposal | None = None  # the proposal that was pending when the turn began


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _plain(summary: str) -> AssistantAnswer:
    return AssistantAnswer(summary=summary, facts=[], draft_operation_id=None, warnings=[],
                           next_human_actions=["none"])


class TurnRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[uuid.UUID, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, user: uuid.UUID) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[user]
            while q and now - q[0] >= USER_WINDOW:
                q.popleft()
            if len(q) >= USER_TURNS:
                return False
            q.append(now)
            return True


def citable(call_id: str, output_json: str) -> str:
    """The tool result as the model reads it, prefixed with the ``tool_call_id`` its facts must cite. The
    grounding payload (and its field paths) and the guard's ``result_sha256`` are unchanged."""
    try:
        body = json.loads(output_json)
    except ValueError:
        return output_json
    if not isinstance(body, dict) or "tool_call_id" in body:
        return output_json
    return json.dumps({"tool_call_id": call_id, **body}, ensure_ascii=False)


def _leaf_paths(value: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    """Scalar fields of a tool result, top level first, for the deterministic fallback."""
    out: list[tuple[str, Any]] = []
    nested: list[tuple[str, Any]] = []
    items = value.items() if isinstance(value, dict) else [(f"[{i}]", v) for i, v in enumerate(value)]
    for key, v in items:
        path = f"{prefix}{key}" if str(key).startswith("[") else (f"{prefix}.{key}" if prefix else str(key))
        if isinstance(v, (dict, list)):
            if v:
                nested.append((path, v))
            else:
                out.append((path, v))
        elif v is not None:
            out.append((path, v))
    if depth < 2:
        for path, v in nested:
            out += _leaf_paths(v, path, depth + 1)
    return out


def template_answer(results: list[tuple[ToolCall, guard.GuardOutcome]], summary: str,
                    verified: tuple[Fact, ...] = ()) -> AssistantAnswer:
    """Deterministic rendering after a failed model answer: the facts that did verify, else observed fields."""
    facts: list[Fact] = list(verified)
    if not facts:
        share = max(3, 15 // max(1, len(results)))
        for call, out in results:
            if isinstance(out.payload, dict) and out.payload:
                for path, value in _leaf_paths(out.payload)[:share]:
                    facts.append(Fact(claim=render(path, value), source=Source(tool_call_id=call.call_id,
                                                                               field_path=path[:200])))
    return AssistantAnswer(summary=summary[:1200], facts=facts[:15], draft_operation_id=None, warnings=[],
                           next_human_actions=["none"])


class AssistantService:
    def __init__(self, svc: AppServices, provider: AssistantProvider, config: AssistantConfig) -> None:
        self.svc = svc
        self.provider = provider
        self.config = config
        self.limiter = TurnRateLimiter()
        self.conversations = ConversationStore(svc.clock)
        self._budget_notified: set[tuple[uuid.UUID, str]] = set()
        self._session_lock = threading.Lock()
        self._proposals: dict[tuple[uuid.UUID, str], Proposal] = {}
        self._drafted: dict[tuple[uuid.UUID, str], dict[str, int]] = {}  # verified drafts of this session → version
        self._turns = 0

    # ------------------------------------------------------------------ audit helpers
    def _event(self, principal: Principal, event_type: str, payload: dict[str, Any]) -> Any:
        """Actor ASSISTANT acting on behalf of the human (``on_behalf_of`` set)."""
        actor = Principal(user_id=principal.user_id, username=principal.username, roles=principal.roles,
                          authenticated_at=principal.authenticated_at, on_behalf_of=principal.user_id)
        return user_event(actor, event_type, payload=payload)

    def record_config(self) -> None:
        """``ASSISTANT_CONFIG_LOADED`` (non-secret digest) or ``ASSISTANT_CONFIG_REJECTED`` (missing elements)."""
        with UnitOfWork(self.svc.engine, clock=self.svc.clock) as uow:
            if self.config.provider == "openai" and self.config.live_problems():
                uow.append_event(system_event("assistant", "ASSISTANT_CONFIG_REJECTED", payload={
                    "missing": self.config.live_problems(), "provider": "openai"}))
            uow.append_event(system_event("assistant", "ASSISTANT_CONFIG_LOADED", payload={
                "provider": self.config.provider, "live_inference": self.config.live_inference,
                "model": self.config.model, "config_digest": self.config.digest()}))

    # ------------------------------------------------------------------ the turn
    def run_turn(self, principal: Principal, text: str, *, session_key: str | None = None,
                 eval_run_id: str | None = None) -> TurnOutcome:
        """``session_key`` names the server session whose conversation the model may see. Without it, and in
        evaluation runs, the turn stands alone."""
        if not has_permission(principal, Permission.ASSISTANT_USE):
            raise DomainError(ErrorCode.FORBIDDEN, "assistant.use required")
        if eval_run_id is None and not self.limiter.allow(principal.user_id):
            raise DomainError(ErrorCode.RATE_LIMITED, "at most 20 assistant turns per 10 minutes")
        text = (text or "").strip()[:2000]
        if not text:
            raise DomainError(ErrorCode.VALIDATION, "empty question")
        actor = Principal(user_id=principal.user_id, username=principal.username, roles=principal.roles,
                          authenticated_at=principal.authenticated_at, on_behalf_of=principal.user_id)
        turn_id = uuid.uuid4()
        started = datetime.now(UTC)
        t0 = time.monotonic()
        turn = _Turn(text=text)
        conversation = (principal.user_id, session_key) if session_key and eval_run_id is None else None
        if conversation:
            with self._session_lock:
                turn.known_drafts = dict(self._drafted.get(conversation, {}))
                turn.prior = self._proposals.get(conversation)
                self._turns += 1
                turn.order = self._turns

        def run(call: ToolCall, gate: Any) -> guard.GuardOutcome:
            out = guard.execute(self.svc, actor, call, gate)
            turn.results.append((call, out))
            if call.name in guard.MUTATING and out.permitted and out.outcome in ("SUCCEEDED", "UNKNOWN"):
                turn.receipts.append(receipts.build(self.svc.engine, call, out))
            return out

        def execute(call: ToolCall) -> guard.GuardOutcome:
            return run(call, lambda c, args: self._gate(turn, c, args))

        def execute_typed(call: ToolCall) -> guard.GuardOutcome:
            """The deterministic grammar and a confirmed proposal: the person's input *is* the structured call."""
            return run(call, None)

        history = self.conversations.history(conversation) if conversation else []
        confirmed = CONFIRM.fullmatch(text)
        if confirmed:
            outcome = self._confirm(conversation, confirmed.group(1).upper(), turn_id, execute_typed, turn)
        else:
            outcome = self._loop(actor, text, turn_id, execute, execute_typed, turn, eval_run_id, history)
        self._attach(outcome, turn, conversation)
        try:
            self._persist(principal, outcome, turn.results, started, int((time.monotonic() - t0) * 1000))
        except Exception as exc:
            if not outcome.receipts:
                raise
            # The draft is committed with its own audit events; hiding it behind an error would invite a duplicate.
            log.error("assistant turn %s could not be recorded (%s)", turn_id, exc.__class__.__name__)
            stored = all(r.status == "STORED" for r in outcome.receipts)
            outcome.notice = NOTICES["RECORD_FAILED" if stored else "RECORD_FAILED_UNVERIFIED"]
        if conversation and self.provider.name != "disabled":  # no model, nothing to remember
            self.conversations.add(conversation, text, remembered(outcome.answer))
        return outcome

    # ------------------------------------------------------------------ intent gate, proposals and receipts
    def _gate(self, turn: _Turn, call: ToolCall, args: dict[str, Any]) -> None:
        """Refuses a drafting call unless the person's words of this turn establish it. At most one draft is
        written per turn: a repeated or second call needs its own request."""
        if turn.receipts:
            raise guard.IntentRefused("INTENT_ALREADY_SERVED", "this request already produced a draft version; a "
                                      "further draft needs a separate request from the user")
        verdict = intent.assess(turn.text, call.name, args, turn.known_drafts)
        base = self._revision_base(args["operation_id"]) if call.name == "revise_draft" else None
        if verdict.verdict == "ESTABLISHED" and base is not None and (gone := base.removed(args)):
            names = ", ".join(str(ln.canonical_sku or ln.native_item) for ln in gone)
            verdict = intent.Assessment("UNCONFIRMED", (f"it would remove {names} from the draft",))
        if verdict.verdict == "INVALID":
            turn.proposal = None
            turn.invalid = True
            raise guard.IntentRefused("INTENT_INVALID_QUANTITY", "; ".join(verdict.problems) + ". Nothing was written. "
                                      "Ask the user to state a valid quantity; do not correct it yourself.")
        if verdict.verdict == "UNCONFIRMED":
            if call.name == "revise_draft" and (base is None or base.version != args["expected_version"]):
                raise guard.IntentRefused("INTENT_UNCONFIRMED", "nothing was written: " + (
                    "there is no such draft." if base is None else f"the draft is at version {base.version}."))
            if not turn.invalid:
                turn.proposal = Proposal(secrets.token_hex(3).upper(), call.name,
                                         intent.describe(call.name, args, base), verdict.problems,
                                         self.svc.clock() + PROPOSAL_TTL, call.arguments_json, turn.order)
            raise guard.IntentRefused("INTENT_UNCONFIRMED", "the user's words do not establish this draft: " +
                                      "; ".join(verdict.problems) + ". Nothing was written. The console shows the "
                                      "exact proposal and asks the user to confirm it; do not call the tool again.")

    def _revision_base(self, operation_id: str) -> intent.RevisionBase | None:
        with self.svc.engine.connect() as conn:
            version = conn.execute(select(t.operations.c.current_version).where(
                t.operations.c.id == uuid.UUID(operation_id))).scalar()
            stored = repo.load_draft_version(conn, uuid.UUID(operation_id), version) if version else None
        return intent.RevisionBase(stored.version, stored.content) if stored else None

    def _confirm(self, conversation: tuple[uuid.UUID, str] | None, code: str, turn_id: uuid.UUID, execute_typed: Any,
                 turn: _Turn) -> TurnOutcome:
        """Executes exactly the stored proposal with the person's current permissions. No model is involved.

        At most once: the matching, unexpired proposal is taken under the lock *before* the command runs, so a replay
        or a second tab finds nothing, and a refused or conflicting command needs a new proposal. A code that does not
        match leaves the pending proposal alone. A revision additionally carries its exact base version, which the
        command checks under the operation's row lock."""
        proposal = None
        with self._session_lock:
            pending = self._proposals.get(conversation) if conversation else None
            if conversation and pending is not None and pending.expires_at <= self.svc.clock():
                del self._proposals[conversation]
            elif conversation and pending is not None and secrets.compare_digest(pending.code, code):
                proposal = self._proposals.pop(conversation)
        if proposal is None:
            return TurnOutcome(turn_id, APPLICATION, _plain(
                "There is no pending proposal with that code in this session (it may have expired or been replaced). "
                "Nothing was written."), False, None, None)
        out = execute_typed(ToolCall("confirmed-1", proposal.tool, proposal.arguments_json))
        if out.outcome == "SUCCEEDED" and out.permitted:
            summary = ("Confirmed. The receipt shows what was stored. Review it and submit it for approval "
                       "yourself." if turn.receipts and turn.receipts[-1].status == "STORED" else
                       "The draft was sent to storage but could not be read back. Check Orders before preparing it "
                       "again.")
        elif out.outcome == "UNKNOWN":
            summary = "The outcome is unknown. Check Orders before preparing this draft again."
        else:
            summary = f"Nothing was written: {out.error_code or out.refused_reason}."
        return TurnOutcome(turn_id, APPLICATION, _plain(summary), False, None, None)

    def _attach(self, outcome: TurnOutcome, turn: _Turn, conversation: tuple[uuid.UUID, str] | None) -> None:
        """Receipts and proposals come from the application's own records, whatever the model answered."""
        outcome.receipts = list(turn.receipts)
        stored = [r for r in turn.receipts if r.status == "STORED" and r.operation_id]
        link = uuid.UUID(stored[-1].operation_id) if stored and stored[-1].operation_id else None
        update: dict[str, Any] = {"draft_operation_id": link}
        if any(r.status != "STORED" for r in turn.receipts):
            update["warnings"] = [*outcome.answer.warnings, NOTICES["UNVERIFIED"]][-10:]
        outcome.answer = outcome.answer.model_copy(update=update)
        proposal = None if turn.receipts else turn.proposal
        if conversation:
            with self._session_lock:
                if proposal is not None:
                    newer = self._proposals.get(conversation)
                    if newer is None or newer.order <= proposal.order:
                        self._proposals[conversation] = proposal
                    else:
                        proposal = None  # a later turn already shows its own proposal; only that one can be confirmed
                elif (turn.receipts or turn.invalid) and turn.prior is not None and \
                        self._proposals.get(conversation) is turn.prior:
                    del self._proposals[conversation]
                if stored:
                    versions = {r.operation_id: r.version for r in stored if r.operation_id and r.version}
                    self._drafted[conversation] = {**self._drafted.get(conversation, {}), **versions}
                while len(self._proposals) > 200:
                    del self._proposals[next(iter(self._proposals))]
                while len(self._drafted) > 200:
                    del self._drafted[next(iter(self._drafted))]
        outcome.proposal = proposal

    def _fallback(self, text: str, turn_id: uuid.UUID, reason: str, execute_typed: Any, turn: _Turn,
                  verified: tuple[Fact, ...] = ()) -> TurnOutcome:
        notice = NOTICES.get(reason, f"The AI assistant could not answer ({reason}); deterministic fallback used.")
        if turn.results:
            if turn.receipts:
                notice += " The draft receipt below is the application's own record and is not affected."
            answer = template_answer(turn.results, notice, verified)
        else:
            routed = router.route(text, execute_typed)
            # the command grammar is offered only where commands are the interface (no model)
            answer = routed.answer if routed.understood or reason == "DISABLED" else _plain(notice)
        return TurnOutcome(turn_id, self.provider.name, answer, True, reason, notice)

    def _loop(self, actor: Principal, text: str, turn_id: uuid.UUID, execute: Any, execute_typed: Any,
              turn_state: _Turn, eval_run_id: str | None, history: list[ProviderMessage]) -> TurnOutcome:
        results = turn_state.results
        args_by_id: dict[str, Any] = {}

        def fallback(reason: str, verified: tuple[Fact, ...] = ()) -> TurnOutcome:
            return self._fallback(text, turn_id, reason, execute_typed, turn_state, verified)

        if self.provider.name == "disabled":
            return fallback("DISABLED")
        messages: list[ProviderMessage] = [*history, ProviderMessage(role="user", text=text)]
        tools = tuple(t.spec() for t in REGISTRY.values())
        schema = output_schema()
        requests_left = self.config.max_requests_per_turn
        deadline = time.monotonic() + TURN_SECONDS
        repaired = False
        by_id: dict[str, Any] = {}
        context = "\n".join(m.text for m in history if m.text)
        while True:
            if requests_left <= 0 or time.monotonic() > deadline:
                return fallback("LIMIT")
            request = ProviderRequest(
                system_prompt=SYSTEM_PROMPT, prompt_version=PROMPT_VERSION, messages=tuple(messages), tools=tools,
                output_schema=schema, limits=TurnLimits(timeout_s=self.config.timeout_s,
                                                        max_output_tokens=self.config.max_output_tokens,
                                                        max_requests=requests_left, max_tool_calls=MAX_TOOL_CALLS),
                turn_id=turn_id, scope="EVAL" if eval_run_id else "TURN", eval_run_id=eval_run_id)
            try:
                turn = self.provider.run(request, max_attempts=requests_left)  # type: ignore[call-arg]
            except Exception as exc:
                if not turn_state.receipts:
                    raise
                log.error("provider failed after a stored draft (%s)", exc.__class__.__name__)
                return fallback("PROVIDER_FAILED")
            requests_left -= 1 + turn.retries
            if turn.kind == "TOOL_CALLS":
                messages.append(ProviderMessage(role="assistant", tool_calls=turn.tool_calls))
                for call in turn.tool_calls:  # sequential, in the returned order
                    if len(results) >= MAX_TOOL_CALLS:
                        return fallback("LIMIT")
                    out = execute(call)
                    by_id[call.call_id] = out.payload
                    args_by_id[call.call_id] = _json_object(call.arguments_json)
                    messages.append(ProviderMessage(role="user", tool_result=ToolResult(
                        call.call_id, citable(call.call_id, out.result.output_json))))
                continue
            if turn.kind == "FINAL" and turn.final_json is not None:
                checked = check(turn.final_json, by_id, Evidence(
                    user_text=text, context_text=context, call_args=args_by_id,
                    receipts=sum(r.status == "STORED" for r in turn_state.receipts)))
                answer, problem = checked.answer, checked.problem
                if answer is not None:
                    return TurnOutcome(turn_id, self.provider.name, answer, False, None, None)
                if not repaired and requests_left > 0 and time.monotonic() <= deadline:
                    repaired = True
                    messages.append(ProviderMessage(role="assistant", text=turn.final_json[:4000]))
                    messages.append(ProviderMessage(role="user", text=(
                        f"Your answer was rejected ({problem}). Return a corrected AssistantAnswer: cite only tool "
                        "call ids from this turn, one field per fact with its exact value, and in the summary only "
                        "numbers and identifiers that a tool returned in this turn or the user wrote.")))
                    continue
                # a violation (schema or grounding) not corrected by the single repair — because the repair
                # failed or because no request was left to send it — is GROUNDING_FAILED, never masked as LIMIT.
                failed = fallback("GROUNDING_FAILED", checked.verified_facts)
                failed.validation_failure = failure_code(problem)
                return failed
            category = turn.error_category or ErrorCategory.MALFORMED_RESPONSE
            if category is ErrorCategory.BUDGET_DENIED:
                self._budget_exhausted(actor)
            reason = {"REFUSAL": "REFUSED", "INCOMPLETE": "INCOMPLETE"}.get(turn.kind, category.value)
            return fallback(reason)

    def _budget_exhausted(self, actor: Principal) -> None:
        key = (actor.user_id, datetime.now(UTC).date().isoformat())
        if key in self._budget_notified:
            return
        self._budget_notified.add(key)
        with UnitOfWork(self.svc.engine, clock=self.svc.clock) as uow:
            uow.append_event(self._event(actor, "ASSISTANT_BUDGET_EXHAUSTED", {"day": key[1]}))

    # ------------------------------------------------------------------ persistence
    def _persist(self, principal: Principal, outcome: TurnOutcome,
                 results: list[tuple[ToolCall, guard.GuardOutcome]], started: datetime, latency_ms: int) -> None:
        calls: list[dict[str, Any]] = [{"name": c.name, "args_sha256": o.args_sha256, "permitted": o.permitted,
                  "result_sha256": o.result_sha256, "call_id": c.call_id, "outcome": o.outcome,
                  "error_code": o.error_code, "refused_reason": o.refused_reason} for c, o in results]
        for receipt in outcome.receipts:
            for entry in calls:
                if entry["call_id"] == receipt.call_id and entry["name"] == receipt.tool:
                    entry["receipt"] = receipt.as_dict()
        refused = [{"name": c.name, "reason": o.refused_reason, "args_sha256": o.args_sha256}
                   for c, o in results if not o.permitted]
        outcome.tool_calls, outcome.refused_calls = calls, refused
        final = json.loads(outcome.answer.model_dump_json())
        with UnitOfWork(self.svc.engine, clock=self.svc.clock) as uow:
            uow.conn.execute(t.assistant_turns.insert().values(
                id=outcome.turn_id, user_id=principal.user_id, provider=outcome.provider,
                model=self.provider.model or None, prompt_version=PROMPT_VERSION, started_at=started,
                latency_ms=latency_ms, input_tokens=None, output_tokens=None, tool_calls=calls,
                refused_calls=refused, final_answer=final, fallback_used=outcome.fallback_used,
                fallback_reason=outcome.fallback_reason))
            for c, o in results:
                if not o.permitted:
                    uow.append_event(self._event(principal, "ASSISTANT_TOOL_REFUSED", {
                        "assistant_turn_id": str(outcome.turn_id), "tool": c.name[:80], "reason": o.refused_reason,
                        "args_sha256": o.args_sha256}))
            uow.append_event(self._event(principal, "ASSISTANT_TURN", {
                "assistant_turn_id": str(outcome.turn_id), "provider": outcome.provider,
                "fallback_used": outcome.fallback_used, "fallback_reason": outcome.fallback_reason,
                "tool_calls": [c["name"] for c in calls], "refused": len(refused),
                "draft_operation_id": str(outcome.answer.draft_operation_id)
                if outcome.answer.draft_operation_id else None}))
