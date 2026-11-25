"""Tool execution guard: allowlist → strict argument validation (no coercion) → the human's permission
→ for the drafting tools, the intent gate (``lbf.assistant.intent``) → execution through the application services →
capped, untrusted-text-wrapped JSON result."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from lbf.api.services import AppServices
from lbf.assistant.provider import ToolCall, ToolResult
from lbf.assistant.tools import MAX_ROWS, REGISTRY, Committed
from lbf.auth.policy import Principal, has_permission
from lbf.domain.errors import DomainError, ErrorCode
from lbf.workflow.readback import EventIdentity

MAX_RESULT_BYTES = 8 * 1024
MUTATING = frozenset({"prepare_draft", "revise_draft"})
log = logging.getLogger("lbf.assistant")


class IntentRefused(Exception):
    """The intent gate refused a drafting call before anything was written."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason)
        self.reason, self.message = reason, message


@dataclass(frozen=True)
class GuardOutcome:
    result: ToolResult
    permitted: bool
    refused_reason: str | None  # UNKNOWN_TOOL | MALFORMED_ARGUMENTS | FORBIDDEN | INTENT_* | None
    args_sha256: str
    result_sha256: str
    payload: Any  # the (capped) result object, for grounding
    outcome: str = "SUCCEEDED"  # REFUSED (never executed) | SUCCEEDED | FAILED (nothing changed) | UNKNOWN
    error_code: str | None = None
    event: EventIdentity | None = None  # a drafting call's own audit event, as appended in its transaction


def _cap(value: Any) -> Any:
    if isinstance(value, list):
        return [_cap(v) for v in value[:MAX_ROWS]]
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    return value


def _dump(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text.encode("utf-8")) <= MAX_RESULT_BYTES:
        return text
    return json.dumps({"truncated": True, "note": "result exceeded 8 KiB; narrow the query",
                       "preview": text.encode("utf-8")[: MAX_RESULT_BYTES - 200].decode("utf-8", "ignore")})


def execute(svc: AppServices, principal: Principal, call: ToolCall,
            gate: Callable[[ToolCall, dict[str, Any]], None] | None = None) -> GuardOutcome:
    """``gate`` is consulted only for a drafting call that is otherwise about to execute; it raises
    :class:`IntentRefused` to refuse it."""
    args_digest = hashlib.sha256(call.arguments_json.encode("utf-8", "replace")).hexdigest()

    def refuse(reason: str, message: str) -> GuardOutcome:
        body = json.dumps({"error": reason, "message": message})
        return GuardOutcome(ToolResult(call.call_id, body), False, reason, args_digest,
                            hashlib.sha256(body.encode()).hexdigest(),
                            {"error": reason, "message": message}, "REFUSED", reason)

    tool = REGISTRY.get(call.name)
    if tool is None:
        return refuse("UNKNOWN_TOOL", f"'{call.name}' is not an available tool. Approvals, submissions, "
                      "re-checks, withdrawals, redeliveries, escalations and resolutions are human actions in the UI.")
    try:
        args = tool.args.model_validate_json(call.arguments_json)
    except (ValidationError, ValueError) as exc:
        return refuse("MALFORMED_ARGUMENTS", f"arguments do not match the schema ({exc.__class__.__name__})")
    if not has_permission(principal, tool.permission):
        return refuse("FORBIDDEN", f"you do not have the permission {tool.permission.value}")
    if call.name in MUTATING and gate is not None:
        try:
            gate(call, args.model_dump(mode="json"))
        except IntentRefused as exc:
            return refuse(exc.reason, exc.message)
    outcome, code, event = "SUCCEEDED", None, None
    try:
        result: Any = tool.handler(svc, principal, args)
        if isinstance(result, Committed):
            result, event = result.payload, result.event
        payload = _cap(result)
        body = _dump(payload)
    except DomainError as exc:
        payload = {"error": exc.code.value, "message": exc.message}
        body = json.dumps(payload)
        outcome, code = "FAILED", exc.code.value
        if call.name in MUTATING and exc.code in (ErrorCode.DATABASE_UNAVAILABLE, ErrorCode.INTERNAL):
            outcome = "UNKNOWN"
    except Exception as exc:
        if call.name not in MUTATING:
            raise
        # The command may or may not have committed (e.g. the connection dropped at COMMIT): never a success and
        # never "nothing happened".
        log.error("drafting tool %s ended with an unknown outcome (%s)", call.name, exc.__class__.__name__)
        payload = {"error": "OUTCOME_UNKNOWN", "message": "It is not known whether the draft was stored. Check Orders "
                   "before preparing it again."}
        body = json.dumps(payload)
        outcome, code = "UNKNOWN", "OUTCOME_UNKNOWN"
    return GuardOutcome(ToolResult(call.call_id, body), True, None, args_digest,
                        hashlib.sha256(body.encode()).hexdigest(), payload, outcome, code, event)
