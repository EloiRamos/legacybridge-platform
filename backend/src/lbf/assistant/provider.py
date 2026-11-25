"""Provider-neutral types. No SDK types here; consumed only by ``lbf.assistant.service`` (B-8)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


class ErrorCategory(StrEnum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    SERVER_ERROR = "SERVER_ERROR"
    CONNECTION = "CONNECTION"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    REFUSED = "REFUSED"
    INCOMPLETE = "INCOMPLETE"
    BUDGET_DENIED = "BUDGET_DENIED"
    CONFIG_REJECTED = "CONFIG_REJECTED"
    MODEL_MISMATCH = "MODEL_MISMATCH"  # the response did not report the authorized model; nothing of it is used


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    json_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output_json: str


@dataclass(frozen=True)
class ProviderMessage:
    role: Literal["user", "assistant"]
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None


@dataclass(frozen=True)
class TurnLimits:
    timeout_s: float
    max_output_tokens: int
    max_requests: int
    max_tool_calls: int


@dataclass(frozen=True)
class ProviderRequest:
    system_prompt: str
    prompt_version: str
    messages: tuple[ProviderMessage, ...]
    tools: tuple[ToolSpec, ...]
    output_schema: dict[str, Any]
    limits: TurnLimits
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    turn_id: uuid.UUID | None = None
    scope: Literal["TURN", "EVAL"] = "TURN"
    eval_run_id: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ProviderTurn:
    kind: Literal["TOOL_CALLS", "FINAL", "REFUSAL", "INCOMPLETE", "ERROR"]
    tool_calls: tuple[ToolCall, ...] = ()
    final_json: str | None = None
    refusal_text: str | None = None
    usage: Usage = Usage()
    model_reported: str | None = None
    provider_request_id: str | None = None
    latency_ms: int = 0
    error_category: ErrorCategory | None = None
    retries: int = 0


class AssistantProvider(Protocol):
    name: str
    model: str

    def run(self, request: ProviderRequest) -> ProviderTurn:
        """One model request; never raises past the service."""
        ...
