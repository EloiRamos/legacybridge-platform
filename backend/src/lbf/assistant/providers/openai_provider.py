"""``OpenAIProvider`` — the ONLY module that imports the ``openai`` SDK (B-8). Responses API.

Bindings (confirmed against the installed SDK 3.57.0 source; ADR-F10): ``openai.OpenAI(api_key, timeout,
max_retries=0, http_client)``; ``client.responses.create(model, instructions, input, tools, text, store=False,
max_output_tokens, parallel_tool_calls=False, tool_choice="auto")``; function tools ``{"type": "function", "name",
"description", "parameters", "strict": True}``; structured output ``text={"format": {"type": "json_schema", "name",
"schema", "strict": True}}``; output items ``function_call`` (``call_id``, ``name``, ``arguments``) and ``message``
(content ``output_text`` / ``refusal``); ``response.status`` / ``incomplete_details``; ``response.usage``
(``input_tokens``, ``output_tokens``, ``input_tokens_details.cached_tokens``); ``response._request_id``.

Guarantees: fail-closed configuration (no client is even constructed unless live inference is fully configured);
SDK retries disabled — every retry is decided, budgeted and recorded here; no custom base URL (official endpoint
only); no provider-hosted tools; no server-side storage or response chaining; exception text is never logged; every
request names the one authorized model, and a response that reports any other is recorded but never mapped or used.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import openai

from lbf.assistant.budget import BudgetService, Denial, Reservation
from lbf.assistant.config import AUTHORIZED_OPENAI_MODEL, AssistantConfig, read_key
from lbf.assistant.model_guard import ModelGuard, recordable
from lbf.assistant.provider import ErrorCategory, ProviderRequest, ProviderTurn, ToolCall, Usage
from lbf.assistant.telemetry import CallRecord, Telemetry

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
OFFICIAL_BASE_URL = "https://api.openai.com/v1"  # pinned: the SDK would otherwise honour OPENAI_BASE_URL
PROCESS_GUARD = ModelGuard()  # closed by the first unexpected reported model; only a restart opens it


def build_input(request: ProviderRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in request.messages:
        if m.tool_result is not None:
            items.append({"type": "function_call_output", "call_id": m.tool_result.call_id,
                          "output": m.tool_result.output_json})
        elif m.tool_calls:
            items.extend({"type": "function_call", "call_id": c.call_id, "name": c.name,
                          "arguments": c.arguments_json} for c in m.tool_calls)
        elif m.text is not None:
            items.append({"role": m.role, "content": m.text})
    return items


def build_body(request: ProviderRequest, max_output_tokens: int) -> dict[str, Any]:
    return {
        "model": AUTHORIZED_OPENAI_MODEL,
        "instructions": request.system_prompt,
        "input": build_input(request),
        "tools": [{"type": "function", "name": t.name, "description": t.description, "parameters": t.json_schema,
                   "strict": True} for t in request.tools],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "text": {"format": {"type": "json_schema", "name": "AssistantAnswer", "schema": request.output_schema,
                            "strict": True}},
        "max_output_tokens": max_output_tokens,
        "store": False,
    }


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def reported_usage(response: Any) -> Usage:
    u = getattr(response, "usage", None)
    details = getattr(u, "input_tokens_details", None)
    return Usage(input_tokens=_count(getattr(u, "input_tokens", None)),
                 output_tokens=_count(getattr(u, "output_tokens", None)),
                 cached_input_tokens=_count(getattr(details, "cached_tokens", None)))


def map_response(response: Any) -> tuple[str, ProviderTurn]:
    """(telemetry status, provider turn) from an SDK ``Response`` whose reported model was accepted."""
    common: dict[str, Any] = {"usage": reported_usage(response), "model_reported": getattr(response, "model", None),
              "provider_request_id": getattr(response, "_request_id", None)}
    if getattr(response, "status", None) == "incomplete":
        return "INCOMPLETE", ProviderTurn(kind="INCOMPLETE", error_category=ErrorCategory.INCOMPLETE, **common)
    calls, finals, refusals = [], [], []
    for item in getattr(response, "output", None) or []:
        kind = getattr(item, "type", None)
        if kind == "function_call":
            calls.append(ToolCall(call_id=str(item.call_id), name=str(item.name), arguments_json=str(item.arguments)))
        elif kind == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "output_text":
                    finals.append(str(part.text))
                elif getattr(part, "type", None) == "refusal":
                    refusals.append(str(part.refusal))
        # other item types (e.g. reasoning) are ignored
    if calls:
        return "TOOL_CALLS", ProviderTurn(kind="TOOL_CALLS", tool_calls=tuple(calls), **common)
    if refusals:
        return "REFUSED", ProviderTurn(kind="REFUSAL", refusal_text=refusals[0][:500],
                                       error_category=ErrorCategory.REFUSED, **common)
    if finals:
        return "COMPLETED", ProviderTurn(kind="FINAL", final_json=finals[-1], **common)
    return "FAILED", ProviderTurn(kind="ERROR", error_category=ErrorCategory.MALFORMED_RESPONSE, **common)


def categorize(exc: Exception) -> tuple[ErrorCategory, bool, float | None]:
    """(category, retry allowed, Retry-After seconds) — never inspects or returns the exception text."""
    if isinstance(exc, openai.APITimeoutError):
        return ErrorCategory.TIMEOUT, False, None  # may have been processed and billed: never retried
    if isinstance(exc, openai.APIConnectionError):
        cause = exc.__cause__
        before_send = cause is not None and type(cause).__name__ in ("ConnectError", "ConnectTimeout")
        return ErrorCategory.CONNECTION, before_send, None
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        retry_after = None
        try:
            raw = exc.response.headers.get("retry-after")
            retry_after = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            retry_after = None
        category = {429: ErrorCategory.RATE_LIMITED, 401: ErrorCategory.AUTHENTICATION,
                    403: ErrorCategory.PERMISSION, 404: ErrorCategory.NOT_FOUND, 400: ErrorCategory.BAD_REQUEST,
                    422: ErrorCategory.BAD_REQUEST}.get(status, ErrorCategory.SERVER_ERROR if status >= 500
                                                        else ErrorCategory.BAD_REQUEST)
        return category, status in RETRYABLE_STATUS, retry_after
    if isinstance(exc, (openai.APIResponseValidationError, ValueError, TypeError, KeyError, json.JSONDecodeError)):
        return ErrorCategory.MALFORMED_RESPONSE, False, None
    return ErrorCategory.MALFORMED_RESPONSE, False, None


class OpenAIProvider:
    name = "openai"

    def __init__(self, config: AssistantConfig, *, budget: BudgetService | None, telemetry: Telemetry,
                 clock: Callable[[], datetime], http_client: Any = None, transport_label: str = "live",
                 sleep: Callable[[float], None] = time.sleep, semaphore: threading.BoundedSemaphore | None = None,
                 api_key: str | None = None) -> None:
        self.config = config
        self.model = config.model if isinstance(config.model, str) else ""
        self.campaign: ModelGuard | None = None  # set by a capped run; closes with it
        self.budget = budget
        self.telemetry = telemetry
        self.clock = clock
        self.sleep = sleep
        self.transport_label = transport_label
        self.semaphore = semaphore or threading.BoundedSemaphore(config.max_concurrency)
        self.problems = config.live_problems()
        self._client: Any = None
        key = None if self.problems else (api_key if api_key is not None else read_key())
        if not self.problems and key and budget is not None:
            # http_client is injected ONLY by tests (mock transport); production uses the SDK default client.
            kwargs: dict[str, Any] = {"api_key": key, "timeout": config.timeout_s, "max_retries": 0,
                                      "base_url": OFFICIAL_BASE_URL}
            if http_client is not None:
                kwargs["http_client"] = http_client
            self._client = openai.OpenAI(**kwargs)
        elif not self.problems and not key:
            self.problems.append("OPENAI_API_KEY_FILE")

    @property
    def ready(self) -> bool:
        return self._client is not None

    @property
    def stopped(self) -> bool:
        """An unexpected model was reported: no further request until the process is restarted (R-4)."""
        return PROCESS_GUARD.closed or (self.campaign is not None and self.campaign.closed)

    def _usable(self, reported: Any) -> bool:
        """Both guards see every response; a campaign's version change also stops the process."""
        usable = PROCESS_GUARD.observe(reported)
        if self.campaign is not None and not self.campaign.observe(reported):
            PROCESS_GUARD.close(reported)
            usable = False
        return usable

    def run(self, request: ProviderRequest, max_attempts: int | None = None) -> ProviderTurn:
        if self._client is None or self.budget is None:
            return ProviderTurn(kind="ERROR", error_category=ErrorCategory.CONFIG_REJECTED)
        if not self.semaphore.acquire(timeout=5):
            return ProviderTurn(kind="ERROR", error_category=ErrorCategory.RATE_LIMITED)
        try:
            return self._run(request, max_attempts or (1 + self.config.max_retries))
        finally:
            self.semaphore.release()

    def _run(self, request: ProviderRequest, max_attempts: int) -> ProviderTurn:
        assert self.budget is not None
        body = build_body(request, self.config.max_output_tokens)
        serialized = json.dumps({k: body[k] for k in ("instructions", "input", "tools", "text")},
                                sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(serialized).hexdigest()
        attempts = min(max_attempts, 1 + self.config.max_retries)
        last = ProviderTurn(kind="ERROR", error_category=ErrorCategory.SERVER_ERROR)
        for attempt in range(attempts):
            if self.stopped:
                return ProviderTurn(kind="ERROR", error_category=ErrorCategory.MODEL_MISMATCH, retries=attempt)
            reservation = self.budget.reserve(len(serialized), turn_key=str(request.turn_id) if request.turn_id
                                              else None, eval_key=request.eval_run_id if request.scope == "EVAL"
                                              else None)
            record = CallRecord(id=uuid.uuid4(), provider="openai", started_at=self.clock(),
                                status="BUDGET_DENIED", correlation_id=request.correlation_id,
                                assistant_turn_id=request.turn_id, model_configured=AUTHORIZED_OPENAI_MODEL,
                                retry_no=attempt, request_sha256=digest, transport=self.transport_label)
            if isinstance(reservation, Denial):
                record.error_category = ErrorCategory.BUDGET_DENIED.value
                self.telemetry.record(record)
                return ProviderTurn(kind="ERROR", error_category=ErrorCategory.BUDGET_DENIED, retries=attempt)
            record.id = reservation.call_id
            record.reserved_usd = reservation.amount
            started = time.monotonic()
            if self.stopped:  # closed by another worker meanwhile: not sent, and the reservation is not refunded
                self._finish(record, reservation, started, "FAILED", ErrorCategory.MODEL_MISMATCH, None)
                return ProviderTurn(kind="ERROR", error_category=ErrorCategory.MODEL_MISMATCH, retries=attempt)
            try:
                response = self._client.responses.create(**body)
            except Exception as exc:  # mapped to a category; the exception text is never logged
                category, retry, retry_after = categorize(exc)
                self._finish(record, reservation, started, "FAILED", category, None)
                last = ProviderTurn(kind="ERROR", error_category=category, retries=attempt,
                                    latency_ms=record.latency_ms or 0)
                if retry and attempt + 1 < attempts:
                    wait = min(retry_after if retry_after is not None else float(attempt + 1),
                               self.config.retry_max_wait_s)
                    self.sleep(max(0.0, wait))
                    continue
                return last
            reported = getattr(response, "model", None)
            if not self._usable(reported):  # before anything of the response is mapped, executed, shown or repaired
                turn = ProviderTurn(kind="ERROR", error_category=ErrorCategory.MODEL_MISMATCH,
                                    usage=reported_usage(response), model_reported=recordable(reported),
                                    provider_request_id=getattr(response, "_request_id", None), retries=attempt)
                self._finish(record, reservation, started, "FAILED", ErrorCategory.MODEL_MISMATCH, turn)
                return turn
            try:
                status, turn = map_response(response)
            except Exception:  # an unmappable response is malformed
                status, turn = "FAILED", ProviderTurn(kind="ERROR", error_category=ErrorCategory.MALFORMED_RESPONSE)
            self._finish(record, reservation, started, status, turn.error_category, turn)
            return ProviderTurn(kind=turn.kind, tool_calls=turn.tool_calls, final_json=turn.final_json,
                                refusal_text=turn.refusal_text, usage=turn.usage, model_reported=turn.model_reported,
                                provider_request_id=turn.provider_request_id, latency_ms=record.latency_ms or 0,
                                error_category=turn.error_category, retries=attempt)
        return last

    def _finish(self, record: CallRecord, reservation: Reservation, started: float, status: str,
                category: ErrorCategory | None, turn: ProviderTurn | None) -> None:
        assert self.budget is not None
        record.latency_ms = int((time.monotonic() - started) * 1000)
        record.status = status
        record.error_category = category.value if category else None
        usage = turn.usage if turn is not None else Usage()
        record.usage_input_tokens = usage.input_tokens
        record.usage_cached_input_tokens = usage.cached_input_tokens
        record.usage_output_tokens = usage.output_tokens
        if turn is not None:
            record.model_reported = turn.model_reported
            record.provider_request_id = turn.provider_request_id
        amount, basis = self.budget.settle_amount(usage.input_tokens, usage.cached_input_tokens, usage.output_tokens,
                                                  reservation.amount)
        if category is ErrorCategory.MODEL_MISMATCH:  # never below the worst case: the prices are not that model's
            amount, basis = max(amount, reservation.amount), "WORST_CASE_RESERVATION"
        record.estimated_cost_usd = amount
        record.cost_basis = basis
        self.budget.settle(reservation, amount, basis)
        self.telemetry.record(record)


__all__ = ["OpenAIProvider", "build_body", "categorize", "map_response", "reported_usage"]
