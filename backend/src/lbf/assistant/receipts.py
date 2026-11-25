"""Mutation receipts: what the application itself recorded about a drafting call.

Built from the tool's own result and verified by reading back the stored version and the audit event appended by the
same transaction. ``STORED`` means operation, version, content hash and event identity all matched; anything less is
``UNVERIFIED``. A receipt never depends on the model's answer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from lbf.assistant.guard import GuardOutcome
from lbf.assistant.provider import ToolCall
from lbf.workflow.readback import verify_version


@dataclass(frozen=True)
class Receipt:
    call_id: str
    tool: str
    status: str  # STORED (verified) | UNVERIFIED (committed, not verified) | UNKNOWN (outcome not observed)
    operation_id: str | None
    correlation_value: str | None
    version: int | None
    content_hash: str | None
    state: str | None
    lines: list[dict[str, Any]]
    audit_seq: int | None  # the DRAFT_VERSION_CREATED event
    audit_event_hash: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build(engine: Engine, call: ToolCall, out: GuardOutcome) -> Receipt:
    if out.outcome != "SUCCEEDED" or not isinstance(out.payload, dict):
        args = json.loads(call.arguments_json)
        return Receipt(call.call_id, call.name, "UNKNOWN", args.get("operation_id"), None, None, None, None, [], None,
                       None)
    p = out.payload
    op_id, version, digest = str(p["operation_id"]), int(p["version"]), str(p["content_hash"])
    verified = None
    try:
        verified = verify_version(engine, UUID(op_id), version, digest, out.event)
    except Exception:  # noqa: S110 - the write is committed; only its read-back failed (status says so)
        pass
    if verified is None:
        # nothing that was not verified is shown as if it were: no reference, no lines, no audit event
        return Receipt(call.call_id, call.name, "UNVERIFIED", op_id, None, version, digest, p["state"], [], None, None)
    lines = [{"canonical_sku": ln.canonical_sku, "native_item": ln.native_item, "quantity_base": ln.quantity_base}
             for ln in verified.version.content.lines]
    return Receipt(call.call_id, call.name, "STORED", op_id, verified.correlation_value, version, digest, p["state"],
                   lines, verified.event.seq, verified.event.event_hash)
