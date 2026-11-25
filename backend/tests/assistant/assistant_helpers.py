"""Assistant test helpers: the real assistant service over the workflow harness, with the scripted provider or the
OpenAI provider on a mocked ``httpx2`` transport (no network; the session network guard is active)."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from typing import Any

import httpx2
from api_support import Api
from sqlalchemy import func, select

from lbf.assistant.budget import BudgetService
from lbf.assistant.config import AssistantConfig
from lbf.assistant.providers.openai_provider import OpenAIProvider
from lbf.assistant.providers.scripted import ScriptedProvider
from lbf.assistant.service import AssistantService
from lbf.assistant.tables import inference_budget_ledger, inference_calls
from lbf.assistant.telemetry import Telemetry
from lbf.persistence import tables as t

TEST_OPENAI_KEY = "test-key-not-a-secret"


def live_config(**overrides: Any) -> AssistantConfig:
    base = {"provider": "openai", "live_inference": True, "model": "gpt-6-luna", "timeout_s": 5.0,
            "max_output_tokens": 500, "max_retries": 1, "retry_max_wait_s": 10.0, "max_concurrency": 2,
            "max_requests_per_turn": 6, "input_price_per_mtok": Decimal("1.00"),
            "cached_input_price_per_mtok": Decimal("0.50"), "output_price_per_mtok": Decimal("4.00"),
            "price_source": "test fixture (not a real price)", "price_observed_at": "2026-11-22",
            "key_present": True, "model_explicit": True}
    base.update(overrides)
    return AssistantConfig(**base)


class MockOpenAI:
    """Serves queued Responses-API payloads (or exceptions/handlers) and records every request."""

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.requests: list[httpx2.Request] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        with self.lock:
            self.requests.append(request)
            item = self.queue.pop(0) if self.queue else _final({"summary": "ok", "facts": [],
                                                               "draft_operation_id": None, "warnings": [],
                                                               "next_human_actions": ["none"]})
        if callable(item) and not isinstance(item, httpx2.Response):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx2.Response):
            return item
        return httpx2.Response(200, json=item, headers={"x-request-id": "req_test_1"})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _usage(i: int = 120, o: int = 30, c: int = 20) -> dict[str, Any]:
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o,
            "input_tokens_details": {"cached_tokens": c}, "output_tokens_details": {"reasoning_tokens": 0}}


def _response(output: list[dict[str, Any]], status: str = "completed", usage: dict[str, Any] | None = None) -> dict:
    body: dict[str, Any] = {"id": "resp_test", "object": "response", "created_at": 1767225600, "status": status,
                            "model": "gpt-6-luna", "output": output, "parallel_tool_calls": False,
                            "tool_choice": "auto", "tools": [], "error": None,
                            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None}
    if usage is not False:
        body["usage"] = usage or _usage()
    return body


def _tool_call(name: str, args: dict[str, Any] | str, call_id: str = "call_1") -> dict:
    return _response([{"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
                       "arguments": args if isinstance(args, str) else json.dumps(args), "status": "completed"}])


def _final(answer: dict[str, Any] | str, usage: dict[str, Any] | None = None) -> dict:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return _response([{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]}], usage=usage)


def _refusal(text: str = "I can't help with that.") -> dict:
    return _response([{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "refusal", "refusal": text}]}])


class Harness:
    def __init__(self, api: Api) -> None:
        self.api = api
        self.sleeps: list[float] = []

    def scripted(self, steps: list[Any]) -> AssistantService:
        return AssistantService(self.api.svc, ScriptedProvider(steps), AssistantConfig(provider="scripted"))

    def openai(self, mock: MockOpenAI, config: AssistantConfig | None = None, *, eval_budget: Decimal | None = None,
               semaphore: threading.BoundedSemaphore | None = None) -> AssistantService:
        config = config or live_config()
        svc = self.api.svc
        provider = OpenAIProvider(config, budget=BudgetService(svc.engine, config, svc.clock, eval_budget),
                                  telemetry=Telemetry(svc.engine, svc.clock), clock=svc.clock,
                                  http_client=httpx2.Client(transport=httpx2.MockTransport(mock)),
                                  transport_label="mock", sleep=self.sleeps.append, semaphore=semaphore,
                                  api_key=TEST_OPENAI_KEY)
        return AssistantService(svc, provider, config)

    def counts(self) -> dict[str, int]:
        with self.api.wf.engine.connect() as conn:
            return {tb.name: conn.execute(select(func.count()).select_from(tb)).scalar()
                    for tb in (t.operations, t.approvals, t.authorizations, t.transmissions, t.escalations,
                               t.draft_versions)}

    def ledger(self) -> list[Any]:
        with self.api.wf.engine.connect() as conn:
            return list(conn.execute(select(inference_budget_ledger)).all())

    def calls(self) -> list[Any]:
        with self.api.wf.engine.connect() as conn:
            return list(conn.execute(select(inference_calls).order_by(inference_calls.c.started_at)).all())

    def events(self, event_type: str) -> list[Any]:
        with self.api.wf.engine.connect() as conn:
            return list(conn.execute(select(t.audit_events).where(t.audit_events.c.event_type == event_type)).all())


R = _response
TOOL = _tool_call
FINAL = _final
REFUSAL = _refusal
USAGE = _usage
