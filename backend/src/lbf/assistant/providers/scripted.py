"""``ScriptedProvider`` — deterministic, fixture-driven model turns (tests, offline demos, scripted evaluation).
No network. Each step is either a fixed turn or a function of the request (so scripts can cite tool results)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from lbf.assistant.provider import ErrorCategory, ProviderRequest, ProviderTurn, ToolCall, Usage

Step = dict[str, Any] | Callable[[ProviderRequest], dict[str, Any]]


def turn_from(step: dict[str, Any]) -> ProviderTurn:
    usage = Usage(input_tokens=step.get("input_tokens", 100), output_tokens=step.get("output_tokens", 50),
                  cached_input_tokens=0)
    if "tool_calls" in step:
        calls = tuple(ToolCall(call_id=c.get("call_id", f"call-{i}"), name=c["name"],
                               arguments_json=c["arguments"] if isinstance(c["arguments"], str)
                               else json.dumps(c["arguments"]))
                      for i, c in enumerate(step["tool_calls"], 1))
        return ProviderTurn(kind="TOOL_CALLS", tool_calls=calls, usage=usage, model_reported="scripted")
    if "final" in step:
        final = step["final"] if isinstance(step["final"], str) else json.dumps(step["final"])
        return ProviderTurn(kind="FINAL", final_json=final, usage=usage, model_reported="scripted")
    if "refusal" in step:
        return ProviderTurn(kind="REFUSAL", refusal_text=step["refusal"], error_category=ErrorCategory.REFUSED,
                            usage=usage)
    if "incomplete" in step:
        return ProviderTurn(kind="INCOMPLETE", error_category=ErrorCategory.INCOMPLETE, usage=usage)
    return ProviderTurn(kind="ERROR", error_category=ErrorCategory(step.get("error", "SERVER_ERROR")))


class ScriptedProvider:
    name = "scripted"
    model = "scripted"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[ProviderRequest] = []

    def run(self, request: ProviderRequest, max_attempts: int | None = None) -> ProviderTurn:
        started = time.monotonic()
        self.requests.append(request)
        if not self.steps:
            return ProviderTurn(kind="ERROR", error_category=ErrorCategory.MALFORMED_RESPONSE)
        step = self.steps.pop(0)
        data = step(request) if callable(step) else step
        turn = turn_from(data)
        return ProviderTurn(**{**turn.__dict__, "latency_ms": int((time.monotonic() - started) * 1000)})
