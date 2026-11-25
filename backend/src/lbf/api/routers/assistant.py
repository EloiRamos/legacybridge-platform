"""Assistant turns (``assistant.use``). The conversation belongs to the server session; no client-supplied
history is accepted."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from lbf.api.deps import Auth, require, services
from lbf.api.services import AppServices
from lbf.domain.enums import Permission
from lbf.domain.errors import DomainError, ErrorCode

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


class TurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class ReceiptOut(BaseModel):
    """What the application stored for one drafting call; independent of the model's answer."""

    model_config = ConfigDict(extra="forbid")
    call_id: str
    tool: Literal["prepare_draft", "revise_draft"]
    status: Literal["STORED", "UNVERIFIED", "UNKNOWN"]
    operation_id: UUID | None
    correlation_value: str | None
    version: int | None
    content_hash: str | None
    state: str | None
    lines: list[dict[str, Any]]
    audit_seq: int | None
    audit_event_hash: str | None


class ProposalOut(BaseModel):
    """A drafting call that was NOT executed; sending ``confirm <code>`` executes exactly this proposal once."""

    model_config = ConfigDict(extra="forbid")
    code: str
    tool: Literal["prepare_draft", "revise_draft"]
    description: str
    reasons: list[str]
    expires_at: datetime


class TurnOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: UUID
    mode: str  # disabled | scripted | openai live | openai mock | application
    answer: dict[str, Any]
    fallback_used: bool
    fallback_reason: str | None
    notice: str | None
    tool_calls: list[dict[str, Any]]
    refused_calls: list[dict[str, Any]]
    receipts: list[ReceiptOut] = Field(default_factory=list)
    proposal: ProposalOut | None = None


class StatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    live_inference: bool
    grammar: list[str]


def _assistant(svc: AppServices) -> Any:
    assistant = svc.extras.get("assistant")
    if assistant is None:
        raise DomainError(ErrorCode.ASSISTANT_UNAVAILABLE, "assistant not configured in this process")
    return assistant


def _mode(assistant: Any) -> str:
    name = assistant.provider.name
    if name != "openai":
        return str(name)
    return "openai live" if getattr(assistant.provider, "transport_label", "") == "live" else "openai mock"


@router.get("/status", response_model=StatusOut, operation_id="assistantStatus")
def status(_: Auth = Depends(require(Permission.ASSISTANT_USE)), svc: AppServices = Depends(services)) -> StatusOut:
    from lbf.assistant.router import GRAMMAR

    a = _assistant(svc)
    return StatusOut(mode=_mode(a), live_inference=a.config.live_inference and _mode(a) == "openai live"
                     and not getattr(a.provider, "stopped", False),
                     grammar=list(GRAMMAR))


def _in_words(receipts: list[ReceiptOut], proposal: ProposalOut | None) -> list[str]:
    """Version 1: the same receipts and proposal as sentences of `notice`, so that nothing that was written is hidden
    from a client that does not know the typed fields."""
    said = []
    for r in receipts:
        if r.status == "STORED":
            said.append(f"Stored draft {r.operation_id} ({r.correlation_value}), version {r.version}, {r.state}, "
                        f"content {r.content_hash}, audit event {r.audit_seq}: read back and verified.")
        elif r.status == "UNVERIFIED":
            said.append(f"A draft was written to order {r.operation_id}, version {r.version}, but it was not verified. "
                        "Open the order before doing anything else.")
        else:
            said.append("A drafting call has an unknown outcome: it may or may not have been stored"
                        + (f" in order {r.operation_id}" if r.operation_id else "")
                        + ". Check the drafts before asking again.")
    if proposal is not None:
        said.append(f"Proposed, and nothing was stored: {proposal.description} To store exactly this before "
                    f"{proposal.expires_at.isoformat()}, send: confirm {proposal.code}")
    return said


# Version 1 (the default) has the eight fields a strict client of the accepted contract knows. Version 2 adds `receipts`
# and `proposal`, left out when empty.
@router.post("/turns", response_model=TurnOut, response_model_exclude_defaults=True, operation_id="assistantTurn")
def turn(body: TurnIn, version: Literal["1", "2"] = Header("1", alias="LBF-Assistant-Version"),
         auth: Auth = Depends(require(Permission.ASSISTANT_USE)), svc: AppServices = Depends(services)) -> TurnOut:
    a = _assistant(svc)
    out = a.run_turn(auth.principal, body.text, session_key=auth.session.token_sha256)
    receipts = [ReceiptOut(**r.as_dict()) for r in out.receipts]
    proposal = ProposalOut(code=out.proposal.code, tool=out.proposal.tool, description=out.proposal.description,
                           reasons=list(out.proposal.reasons),
                           expires_at=out.proposal.expires_at) if out.proposal else None
    answer, notice = out.answer.model_dump(mode="json"), out.notice
    if version == "1":
        notice = " ".join([*([notice] if notice else []), *_in_words(receipts, proposal)]) or None
        if answer["draft_operation_id"] is None:
            answer["draft_operation_id"] = next((str(r.operation_id) for r in reversed(receipts)
                                                 if r.status != "UNKNOWN"), None)
        receipts, proposal = [], None
    return TurnOut(turn_id=out.turn_id, mode=out.provider if out.provider == "application" else _mode(a),
                   answer=answer,
                   fallback_used=out.fallback_used, fallback_reason=out.fallback_reason, notice=notice,
                   tool_calls=out.tool_calls, refused_calls=out.refused_calls,
                   receipts=receipts, proposal=proposal)
