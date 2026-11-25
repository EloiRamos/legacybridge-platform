"""Deterministic guided-intent router: the fallback and the evaluation baseline. No model involved.

Grammar: ``find customer <text>`` · ``stock <sku>`` · ``explain <sku>`` · ``status <operation id|correlation>`` ·
``draft <backend> <customer> <sku>x<qty>[, <sku>x<qty>…]`` · ``unresolved``. It calls the same tools through the same
guard, so permissions and caps are identical.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lbf.assistant.answer import AssistantAnswer, Fact, Source
from lbf.assistant.guard import GuardOutcome
from lbf.assistant.provider import ToolCall

GRAMMAR = ("find customer <text>", "stock <sku>", "explain <sku>", "status <operation id|correlation>",
           "draft <halden|skarvik> <customer> <sku>x<qty>[, …]", "unresolved")


@dataclass(frozen=True)
class Routed:
    answer: AssistantAnswer
    calls: list[tuple[ToolCall, GuardOutcome]]
    understood: bool


def _call(execute: Callable[[ToolCall], GuardOutcome], n: int, name: str, args: dict[str, Any]
          ) -> tuple[ToolCall, GuardOutcome]:
    call = ToolCall(f"router-{n}", name, json.dumps(args))
    return call, execute(call)


def route(text: str, execute: Callable[[ToolCall], GuardOutcome]) -> Routed:
    t = " ".join(text.strip().split())
    low = t.casefold()
    calls: list[tuple[ToolCall, GuardOutcome]] = []
    facts: list[Fact] = []
    warnings: list[str] = []
    draft_id = None
    actions: list[Any] = []
    summary = ""

    def fact(claim: str, call: ToolCall, path: str) -> None:
        facts.append(Fact(claim=claim, source=Source(tool_call_id=call.call_id, field_path=path)))

    if m := re.fullmatch(r"find customer (.{2,60})", t, re.IGNORECASE):
        call, out = _call(execute, 1, "search_customers", {"query": m.group(1)})
        calls.append((call, out))
        accounts = out.payload.get("accounts", []) if isinstance(out.payload, dict) else []
        for i, a in enumerate(accounts[:15]):
            fact(f"{a['backend_id'].upper()} {a['native_id']} {a['name_display']} (hold {a['credit_hold']})", call,
                 f"accounts[{i}].native_id")
        groups = out.payload.get("ambiguity_groups", []) if isinstance(out.payload, dict) else []
        if groups:
            warnings.append("Several accounts share a similar name in the same system; choose explicitly.")
        summary = f"{len(accounts)} account(s) match '{m.group(1)}'." if out.permitted else "Search refused."
        actions = ["search_customers"]
    elif m := re.fullmatch(r"(stock|explain) ([A-Za-z0-9-]{2,16})", t, re.IGNORECASE):
        call, out = _call(execute, 1, "explain_stock_discrepancy", {"canonical_sku": m.group(2).upper()})
        calls.append((call, out))
        rows = out.payload.get("per_backend", []) if isinstance(out.payload, dict) else []
        for i, r in enumerate(rows):
            qty = "unknown" if r["available_base"] is None else str(r["available_base"])
            fact(f"{r['backend_id'].upper()} {r['native_item']}: {r['status']} ({qty} available)", call,
                 f"per_backend[{i}]")
        summary = (f"{m.group(2).upper()} per backend (never summed across backends)." if rows
                   else f"No availability data for {m.group(2).upper()}.")
        actions = ["check_inventory"]
    elif m := re.fullmatch(r"status (\S{6,40})", t, re.IGNORECASE):
        ref = m.group(1)
        if re.fullmatch(r"[0-9a-f-]{36}", ref):
            op_id = ref
        else:
            call, out = _call(execute, 1, "list_operations", {"state": None, "backend_id": None, "customer": None})
            calls.append((call, out))
            ops = out.payload.get("operations", []) if isinstance(out.payload, dict) else []
            op_id = next((o["operation_id"] for o in ops if o["correlation_value"] == ref.upper()), "")
        if op_id:
            call, out = _call(execute, 2, "get_operation_timeline", {"operation_id": op_id})
            calls.append((call, out))
            if out.permitted and isinstance(out.payload, dict) and "state" in out.payload:
                fact(f"State {out.payload['state']} (evidence {out.payload['last_evidence']})", call, "state")
                summary = f"Operation {out.payload['correlation_value']} is {out.payload['state']}."
                if out.payload["state"] == "UNKNOWN":
                    warnings.append("Outcome unknown: do not re-enter this order. Automatic checks only read; the "
                                    "order page shows the actions available to you.")
                actions = ["open_operation"]
        if not summary:
            summary = f"No operation found for {ref}."
    elif m := re.fullmatch(r"draft (halden|skarvik) (\S{1,16}) (.+)", t, re.IGNORECASE):
        lines: list[dict[str, Any]] = []
        for part in m.group(3).split(","):
            lm = re.fullmatch(r"\s*([A-Za-z0-9-]{2,16})\s*x\s*(\d{1,3})\s*", part)
            if lm is None:
                lines = []
                break
            lines.append({"canonical_sku": lm.group(1).upper(), "native_item": None, "quantity": int(lm.group(2))})
        if lines:
            call, out = _call(execute, 1, "prepare_draft", {
                "backend_id": m.group(1).lower(), "native_customer_id": m.group(2), "lines": lines,
                "customer_po": None, "requested_date": None, "delivery_note": None})
            calls.append((call, out))
            if out.permitted and isinstance(out.payload, dict) and "operation_id" in out.payload:
                from uuid import UUID

                draft_id = UUID(out.payload["operation_id"])
                fact(f"Draft created in state {out.payload['state']}", call, "state")
                summary = "Draft created. Review it and submit it for approval yourself; another person approves."
                actions = ["open_operation", "submit_for_approval"]
            else:
                summary = "The draft could not be created."
        else:
            summary = "Could not read the lines; use <sku>x<qty>, separated by commas."
    elif low == "unresolved":
        call, out = _call(execute, 1, "list_operations", {"state": "UNKNOWN", "backend_id": None, "customer": None})
        calls.append((call, out))
        call2, out2 = _call(execute, 2, "list_operations", {"state": "ESCALATED", "backend_id": None, "customer": None})
        calls.append((call2, out2))
        ops = (out.payload.get("operations", []) if isinstance(out.payload, dict) else []) + \
              (out2.payload.get("operations", []) if isinstance(out2.payload, dict) else [])
        summary = f"{len(ops)} unresolved operation(s)."
        actions = ["recheck_in_ui"] if ops else ["none"]
    elif re.match(r"(?:please )?(?:approve|decline|submit|send|withdraw|redeliver|escalate|resolve|re-?check)\b", low):
        return Routed(AssistantAnswer(
            summary="The assistant cannot do that and nothing was done. A person with the required authority does it "
            "on the order page.", facts=[], draft_operation_id=None, warnings=[],
            next_human_actions=["open_operation"]), [], True)
    else:
        return Routed(AssistantAnswer(summary="Not understood. Commands: " + "; ".join(GRAMMAR), facts=[],
                                      draft_operation_id=None, warnings=[], next_human_actions=["none"]), [], False)
    refused = [c for c, o in calls if not o.permitted]
    if refused:
        warnings.append("Some information is not available to your role.")
    return Routed(AssistantAnswer(summary=summary, facts=facts[:15], draft_operation_id=draft_id,
                                  warnings=warnings[:10], next_human_actions=actions or ["none"]), calls, True)
