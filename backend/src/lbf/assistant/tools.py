"""The assistant's tool registry — exactly nine read/draft tools (AI-SEC-01).

Each tool executes through the same application services the API uses, with the **invoking human's** principal
(marked ``on_behalf_of``). There is no tool that submits for approval, approves, declines, re-checks, withdraws,
redelivers, escalates, resolves, administers, runs demo controls, SQL, files or HTTP — they do not exist here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lbf.api import readmodels
from lbf.api.routers import customers as customer_api
from lbf.api.routers import inventory as inventory_api
from lbf.api.services import AppServices
from lbf.assistant.provider import ToolSpec
from lbf.auth.policy import Principal
from lbf.domain.enums import DraftOrigin, OperationState
from lbf.domain.enums import Permission as P
from lbf.domain.errors import DomainError, ErrorCode
from lbf.workflow.commands import create_draft, revise_draft
from lbf.workflow.drafts import DraftPatch, DraftRequest, LineRequest
from lbf.workflow.readback import EventIdentity

MAX_ROWS = 25


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SearchCustomersArgs(Args):
    query: str = Field(min_length=2, max_length=60)


class CustomerOverviewArgs(Args):
    backend_id: Literal["halden", "skarvik"]
    native_id: str = Field(min_length=1, max_length=16)


class SearchItemsArgs(Args):
    query: str | None = Field(max_length=60)
    category: str | None = Field(max_length=40)


class AvailabilityArgs(Args):
    canonical_sku: str | None = Field(max_length=16)
    backend_id: Literal["halden", "skarvik"]
    native_item: str | None = Field(max_length=16)


class DiscrepancyArgs(Args):
    canonical_sku: str = Field(min_length=1, max_length=16)


class ListOperationsArgs(Args):
    state: OperationState | None
    backend_id: Literal["halden", "skarvik"] | None
    customer: str | None = Field(max_length=16)


class TimelineArgs(Args):
    operation_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class DraftLineArgs(Args):
    native_item: str | None = Field(max_length=16, description="The backend's own item code (e.g. NVB-6205-2RS); null "
                                    "when a group SKU is given.")
    canonical_sku: str | None = Field(max_length=16, description="Group SKU such as BW-1001, valid in both backends.")
    quantity: int = Field(ge=1, le=999)


class PrepareDraftArgs(Args):
    backend_id: Literal["halden", "skarvik"]
    native_customer_id: str = Field(min_length=1, max_length=16)
    lines: list[DraftLineArgs] = Field(min_length=1, max_length=10)
    customer_po: str | None = Field(max_length=20)
    requested_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    delivery_note: str | None = Field(max_length=60)


class ReviseDraftArgs(Args):
    operation_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_version: int = Field(ge=1)
    lines: list[DraftLineArgs] = Field(min_length=1, max_length=10)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[Args]
    permission: P
    handler: Callable[[AppServices, Principal, Any], Any]

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, strict_schema(self.args))


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for strict function tools: ``additionalProperties: false``, every property required, optional
    values nullable."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            out = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = sorted(out["properties"])
            return out
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return dict(fix(schema))


# --------------------------------------------------------------------------- handlers


def _untrusted(account: dict[str, Any]) -> dict[str, Any]:
    account = dict(account)
    account["untrusted_text"] = {k: {"untrusted_text": v} for k, v in (account.get("untrusted_text") or {}).items()}
    return account


def h_search_customers(svc: AppServices, _p: Principal, a: SearchCustomersArgs) -> Any:
    out = customer_api.search(svc, a.query).model_dump(mode="json")
    out["accounts"] = [_untrusted(x) for x in out["accounts"][:MAX_ROWS]]
    for party in out["parties"]:
        party["accounts"] = [_untrusted(x) for x in party["accounts"]]
    return out


def h_customer_overview(svc: AppServices, p: Principal, a: CustomerOverviewArgs) -> Any:
    card = customer_api.get_customer(a.backend_id, a.native_id, None, svc).model_dump(mode="json")  # type: ignore[arg-type]
    card["account"] = _untrusted(card["account"])
    card["orders"] = card["orders"][-10:]
    return card


def h_search_items(svc: AppServices, _p: Principal, a: SearchItemsArgs) -> Any:
    items: dict[str, dict[str, Any]] = {}
    for backend_id, port in svc.workflow.backends.items():
        for e in port.catalogue().entries:
            if e.canonical is None:
                continue
            text = f"{e.canonical.canonical_sku} {e.name} {e.native_item}".casefold()
            if a.query and a.query.casefold() not in text:
                continue
            if a.category and a.category.casefold() not in (e.canonical.category or "").casefold():
                continue
            item = items.setdefault(e.canonical.canonical_sku, {"canonical_sku": e.canonical.canonical_sku,
                                                                 "names": {}, "native_items": {}})
            item["names"][backend_id] = e.name
            item["native_items"][backend_id] = e.native_item
    return {"items": sorted(items.values(), key=lambda x: x["canonical_sku"])[:MAX_ROWS]}


def _inventory(svc: AppServices) -> dict[str, Any]:
    return inventory_api.inventory(svc).model_dump(mode="json")


def h_availability(svc: AppServices, _p: Principal, a: AvailabilityArgs) -> Any:
    inv = _inventory(svc)
    rows = [r for item in inv["items"] for r in item["per_backend"]] + inv["unmapped"]
    rows = [r for r in rows if r["backend_id"] == a.backend_id
            and (a.canonical_sku is None or r["canonical_sku"] == a.canonical_sku)
            and (a.native_item is None or r["native_item"] == a.native_item)]
    return {"availability": rows[:MAX_ROWS], "backend_errors": inv["backend_errors"]}


def h_discrepancy(svc: AppServices, _p: Principal, a: DiscrepancyArgs) -> Any:
    inv = _inventory(svc)
    item = next((i for i in inv["items"] if i["canonical_sku"] == a.canonical_sku), None)
    if item is None:
        raise DomainError(ErrorCode.NOT_FOUND, f"no inventory data for {a.canonical_sku}")
    return {"canonical_sku": a.canonical_sku, "name": item["name"],
            "per_backend": [{"backend_id": r["backend_id"], "native_item": r["native_item"], "status": r["status"],
                             "available_base": r["available_base"], "explanation": r["explanation"],
                             "issues": [i["code"] for i in r["issues"]],
                             "legacy_available_flag": r["legacy_available_flag"],
                             "freshness": r["freshness"], "snapshot_age_seconds": r["snapshot_age_seconds"]}
                            for r in item["per_backend"]],
            "rule": "Backends are different legal entities; stock is never summed across them (I-3)."}


def h_list_operations(svc: AppServices, _p: Principal, a: ListOperationsArgs) -> Any:
    with svc.engine.connect() as conn:
        items = readmodels.list_operations(conn, states=[a.state.value] if a.state else [], backend=a.backend_id,
                                           customer=a.customer, limit=MAX_ROWS)
    return {"operations": [dict(i, operation_id=str(i["operation_id"])) for i in items]}


def h_timeline(svc: AppServices, p: Principal, a: TimelineArgs) -> Any:
    with svc.engine.connect() as conn:
        from sqlalchemy import select

        from lbf.persistence import tables as t

        backend = conn.execute(select(t.operations.c.backend_id).where(
            t.operations.c.id == UUID(a.operation_id))).scalar()
        if backend is None:
            raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
        d = readmodels.detail(conn, UUID(a.operation_id), svc.workflow.caps(backend), p, svc.clock(),
                              svc.workflow.settings)
    assert d is not None
    op = d["operation"]
    return {"operation_id": a.operation_id, "correlation_value": op["correlation_value"], "state": op["state"],
            "last_evidence": op["last_evidence"], "verification_level": op["verification_level"],
            "backend_order_ref": op["anchor_order_ref"], "attempt_count": op["attempt_count"],
            "permitted_actions_for_you": d["permitted_actions"],
            "timeline": [{"seq": e["seq"], "event_type": e["event_type"], "state": e["state"]}
                         for e in d["timeline"]][-MAX_ROWS:]}


def _lines(lines: list[DraftLineArgs]) -> list[LineRequest]:
    return [LineRequest(native_item=x.native_item, canonical_sku=x.canonical_sku, quantity=x.quantity) for x in lines]


@dataclass(frozen=True)
class Committed:
    """A drafting tool's result for the model, and the identity of the audit event its transaction appended. The
    identity is for the application's receipt only; the model never sees it."""

    payload: dict[str, Any]
    event: EventIdentity | None


def _stored(r: Any, backend_id: str, customer: str, lines: list[DraftLineArgs]) -> Committed:
    return Committed({"operation_id": str(r.operation_id), "version": r.version, "state": r.state.value,
            "content_hash": r.content_hash, "console_path": f"/operations/{r.operation_id}", "backend_id": backend_id,
            "native_customer_id": customer, "lines": [x.model_dump(mode="json") for x in lines],
            "note": "Draft only. A human must submit it for approval and a different human must approve it."}, r.event)


def h_prepare_draft(svc: AppServices, p: Principal, a: PrepareDraftArgs) -> Any:
    req = DraftRequest(backend_id=a.backend_id, native_customer_id=a.native_customer_id, lines=_lines(a.lines),
                       customer_po=a.customer_po, delivery_note=a.delivery_note,
                       requested_date=date.fromisoformat(a.requested_date) if a.requested_date else None)
    r = create_draft(svc.workflow, p, req, origin=DraftOrigin.ASSISTANT,
                     raw_request={"channel": "assistant", "arguments": a.model_dump(mode="json")})
    return _stored(r, a.backend_id, a.native_customer_id, a.lines)


def h_revise_draft(svc: AppServices, p: Principal, a: ReviseDraftArgs) -> Any:
    with svc.engine.connect() as conn:
        from sqlalchemy import select

        from lbf.persistence import tables as t

        op = conn.execute(select(t.operations).where(t.operations.c.id == UUID(a.operation_id))).first()
    if op is None:
        raise DomainError(ErrorCode.NOT_FOUND, "operation not found")
    r = revise_draft(svc.workflow, p, UUID(a.operation_id), a.expected_version, DraftPatch(lines=_lines(a.lines)),
                     origin=DraftOrigin.ASSISTANT)  # lines only: the other fields keep their values
    return _stored(r, op.backend_id, op.native_customer_id, a.lines)


REGISTRY: dict[str, Tool] = {t.name: t for t in (
    Tool("search_customers", "Search customer accounts in both ERPs by name or account number. Never merges "
         "accounts; parties come only from curated master data.",
         SearchCustomersArgs, P.CUSTOMER_READ, h_search_customers),
    Tool("get_customer_overview", "Open ONE account whose backend and native account number are known: recorded "
         "identifier, hold status, linked accounts, open operations, last 10 orders.",
         CustomerOverviewArgs, P.CUSTOMER_READ, h_customer_overview),
    Tool("search_items", "Search canonical group items (SKU, name, category).", SearchItemsArgs, P.INVENTORY_READ,
         h_search_items),
    Tool("get_availability", "Availability of an item in ONE backend, with status, freshness and raw rows.",
         AvailabilityArgs, P.INVENTORY_READ, h_availability),
    Tool("explain_stock_discrepancy", "Per-backend comparison and explanations for one group SKU.",
         DiscrepancyArgs, P.INVENTORY_READ, h_discrepancy),
    Tool("list_operations", "List order operations (≤ 25) by state, backend or customer.", ListOperationsArgs,
         P.OPERATION_READ, h_list_operations),
    Tool("get_operation_timeline", "States, evidence and the actions available to the HUMAN for one operation.",
         TimelineArgs, P.OPERATION_READ, h_timeline),
    Tool("prepare_draft", "Create a NEW order DRAFT for the user (never submits, never approves). Only with the "
         "account, items and whole quantities the user stated; never infer, convert or correct a quantity.",
         PrepareDraftArgs, P.DRAFT_CREATE, h_prepare_draft),
    Tool("revise_draft", "Replace the LINES of an existing draft in a new version; its other fields stay (never "
         "submits, never approves). Only with the quantities the user stated; never infer, convert or correct one.",
         ReviseDraftArgs, P.DRAFT_EDIT, h_revise_draft),
)}

ALLOWLIST = frozenset(REGISTRY)
