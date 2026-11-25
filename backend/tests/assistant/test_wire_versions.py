"""The two representations of an assistant turn (wire contract of 2026-11-25). A client that predates receipts and
proposals, rejects unknown fields and sends no header decodes every turn, and still learns of every stored, unverified,
unknown or proposed draft. ``LBF-Assistant-Version: 2`` carries the typed fields."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from test_write_integrity import OPERATIVE, call, final

pytestmark = pytest.mark.db
URL = "/api/v1/assistant/turns"
V2 = {"LBF-Assistant-Version": "2"}
PROPOSED = "HALDEN 00417: two boxes of BW-1001"


class PriorTurn(BaseModel):
    """The accepted contract before receipts and proposals existed, as a strict consumer decodes it."""

    model_config = ConfigDict(extra="forbid")
    turn_id: UUID
    mode: str
    answer: dict[str, Any]
    fallback_used: bool
    fallback_reason: str | None
    notice: str | None
    tool_calls: list[dict[str, Any]]
    refused_calls: list[dict[str, Any]]


def post(client, text, **headers):
    r = client.post(URL, json={"text": text}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_an_unmodified_strict_client_decodes_a_turn_that_stored_a_draft_and_is_told(h):
    h.api.svc.extras["assistant"] = h.scripted([call(3), final(), call(3), final()])
    c = h.api.as_("daniel")
    old = PriorTurn.model_validate(post(c, OPERATIVE[0]))
    stored = h.api.detail(old.answer["draft_operation_id"])
    version = stored["versions"][-1]
    op = stored["operation"]
    for value in (op["operation_id"], op["correlation_value"], version["content_hash"], "version 1", "DRAFT"):
        assert str(value) in (old.notice or ""), old.notice
    new = post(c, OPERATIVE[0], **V2)
    assert new["receipts"][0]["status"] == "STORED" and new["notice"] is None and new["mode"] == old.mode


def test_an_unmodified_strict_client_sees_a_proposal_and_its_confirmation(h):
    h.api.svc.extras["assistant"] = h.scripted([call(2), final()])
    c = h.api.as_("daniel")
    proposed = PriorTurn.model_validate(post(c, PROPOSED))
    assert proposed.notice and "nothing was stored" in proposed.notice and "confirm " in proposed.notice
    code = proposed.notice.split("confirm ", 1)[1][:6]
    done = PriorTurn.model_validate(post(c, f"confirm {code}"))
    assert done.mode == "application" and "Stored draft" in (done.notice or "")  # who acted stays visible
    assert h.api.detail(done.answer["draft_operation_id"])["operation"]["state"] == "DRAFT"


def test_a_write_that_could_not_be_verified_is_not_reported_as_verified(h, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("read-back")

    monkeypatch.setattr("lbf.assistant.receipts.verify_version", broken)
    h.api.svc.extras["assistant"] = h.scripted([call(3), final()])
    old = PriorTurn.model_validate(post(h.api.as_("daniel"), OPERATIVE[0]))
    assert "not verified" in (old.notice or "") and "Stored draft" not in (old.notice or "")


def test_an_unknown_outcome_tells_an_old_client_not_to_repeat_it(h, monkeypatch):
    from lbf.assistant import receipts
    from lbf.assistant.service import AssistantService

    real = AssistantService.run_turn

    def unknown(self, *a, **kw):
        out = real(self, *a, **kw)
        out.receipts = [receipts.Receipt("c1", "revise_draft", "UNKNOWN", str(UUID(int=7)), None, None, None, None, [],
                                         None, None)]
        return out

    monkeypatch.setattr(AssistantService, "run_turn", unknown)
    h.api.svc.extras["assistant"] = h.scripted([final()])
    old = PriorTurn.model_validate(post(h.api.as_("daniel"), "hello"))
    assert "may or may not" in (old.notice or "") and str(UUID(int=7)) in (old.notice or "")
    assert old.answer["draft_operation_id"] is None  # an unobserved outcome is never shown as a draft


def test_an_existing_notice_is_kept_and_a_turn_without_a_write_is_unchanged(h):
    unusable = [{"final": {"summary": "not an answer"}}, {"final": {"summary": "nor this"}}]
    h.api.svc.extras["assistant"] = h.scripted([call(3), *unusable, final()])
    c = h.api.as_("daniel")
    old = PriorTurn.model_validate(post(c, OPERATIVE[0]))
    assert old.fallback_used and "Stored draft" in (old.notice or "")
    plain = PriorTurn.model_validate(post(c, "hello"))
    assert "draft" not in (plain.notice or "").casefold()


def test_no_header_and_version_one_are_the_same_and_anything_else_is_refused(h):
    h.api.svc.extras["assistant"] = h.scripted([call(3), final()])
    c = h.api.as_("daniel")
    assert "receipts" not in post(c, OPERATIVE[0], **{"LBF-Assistant-Version": "1"})
    for value in ("3", "", "2.0", "two"):
        assert c.post(URL, json={"text": "hello"}, headers={"LBF-Assistant-Version": value}).status_code == 422, value


def test_the_contract_states_the_header_and_still_has_41_operations():
    from lbf.api.openapi import document

    doc = document()
    [param] = [p for p in doc["paths"][URL]["post"]["parameters"] if p["in"] == "header"
               and p["name"] == "LBF-Assistant-Version"]
    assert param["required"] is False and param["schema"]["enum"] == ["1", "2"] and param["schema"]["default"] == "1"
    assert sum(m in ("get", "post", "put", "patch", "delete") for ops in doc["paths"].values() for m in ops) == 41


@pytest.mark.parametrize("source", ["frontend/src/components/AssistantPanel.tsx", "tools/demo_check.py",
                                    "evals/live_campaign.py"])
def test_the_current_consumers_ask_for_the_typed_fields(source):
    from pathlib import Path

    assert "LBF-Assistant-Version" in (Path(__file__).resolve().parents[3] / source).read_text(encoding="utf-8")
