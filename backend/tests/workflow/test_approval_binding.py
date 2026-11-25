"""F2-04 version-bound approval (G3), SoD-1/SoD-2, step-up, value threshold, holds and the I5 overlap guard."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from workflow_support import WF

from lbf.auth.policy import Principal
from lbf.domain.enums import DraftOrigin, OperationState, Role
from lbf.domain.errors import DomainError, ErrorCode
from lbf.persistence import tables as t
from lbf.workflow import commands

pytestmark = pytest.mark.db
S = OperationState


@pytest.fixture
def wf(app_engine, tmp_path):
    return WF(app_engine, tmp_path)


def content_hash(wf, op, version):
    with wf.engine.connect() as conn:
        return conn.execute(select(t.draft_versions.c.content_hash).where(
            t.draft_versions.c.operation_id == op, t.draft_versions.c.version == version)).scalar()


def counts(wf, op):
    with wf.engine.connect() as conn:
        a = conn.execute(select(func.count()).select_from(t.approvals).where(
            t.approvals.c.operation_id == op, t.approvals.c.decision == "APPROVED")).scalar()
        z = conn.execute(select(func.count()).select_from(t.authorizations).where(
            t.authorizations.c.operation_id == op)).scalar()
    return a, z


def ready(wf, **kw):
    op = wf.draft("halden", **kw)
    assert wf.submit(op).blocking is False
    return op


def test_stale_version_wrong_hash_and_revised_under_race_all_refused(wf):
    op = ready(wf)
    h1 = content_hash(wf, op, 1)
    with pytest.raises(DomainError) as wrong_hash:
        commands.approve(wf.ctx, wf.p("priya"), op, version=1, content_hash="f" * 64)
    assert wrong_hash.value.code is ErrorCode.CONFLICT_VERSION
    commands.revise_draft(wf.ctx, wf.p("daniel"), op, 1, wf.request("halden", qty=12))
    wf.submit(op)
    with pytest.raises(DomainError) as stale:  # the approver saw version 1; version 2 is current
        commands.approve(wf.ctx, wf.p("priya"), op, version=1, content_hash=h1)
    assert stale.value.code is ErrorCode.CONFLICT_VERSION
    assert counts(wf, op) == (0, 0) and wf.state(op)[0] == S.READY
    result = commands.approve(wf.ctx, wf.p("priya"), op, version=2, content_hash=content_hash(wf, op, 2))
    with pytest.raises(DomainError) as replay:  # replayed approval of the consumed READY state
        commands.approve(wf.ctx, wf.p("priya"), op, version=2, content_hash=content_hash(wf, op, 2))
    assert replay.value.code is ErrorCode.CONFLICT_STATE
    assert counts(wf, op) == (1, 1) and result.attempt_no == 1


def test_sod1_creator_and_any_version_author_cannot_approve(wf):
    op = ready(wf)
    with pytest.raises(DomainError) as own:
        wf.approve(op, by="daniel")
    assert own.value.code is ErrorCode.SOD_VIOLATION
    commands.revise_draft(wf.ctx, wf.p("priya"), op, 1, wf.request("halden", qty=9))  # priya authors v2
    wf.submit(op)
    with pytest.raises(DomainError) as author:
        wf.approve(op, by="priya")
    assert author.value.code is ErrorCode.SOD_VIOLATION
    wf.approve(op, by="marta")
    assert wf.state(op)[0] == S.APPROVED


def test_sod2_assistant_prepared_draft_is_authored_by_the_invoking_human(wf):
    human = wf.p("daniel")
    via_assistant = Principal(user_id=human.user_id, username=human.username, roles=human.roles,
                              authenticated_at=human.authenticated_at, on_behalf_of=human.user_id)
    op = commands.create_draft(wf.ctx, via_assistant, wf.request("halden"), origin=DraftOrigin.ASSISTANT).operation_id
    with wf.engine.connect() as conn:
        v = conn.execute(select(t.draft_versions).where(t.draft_versions.c.operation_id == op)).one()
        ev = conn.execute(select(t.audit_events).where(t.audit_events.c.operation_id == op)
                          .order_by(t.audit_events.c.seq)).first()
    assert v.created_by == human.user_id and v.origin == "ASSISTANT"
    assert ev.actor_type == "ASSISTANT" and ev.on_behalf_of == str(human.user_id)
    wf.submit(op)
    with pytest.raises(DomainError) as sod2:
        wf.approve(op, by="daniel")
    assert sod2.value.code is ErrorCode.SOD_VIOLATION


def test_step_up_required_for_approve_and_decline(wf):
    op = ready(wf)
    with pytest.raises(DomainError) as approve:
        commands.approve(wf.ctx, wf.p("priya", fresh=False), op, version=1, content_hash=content_hash(wf, op, 1))
    with pytest.raises(DomainError) as decline:
        commands.decline(wf.ctx, wf.p("priya", fresh=False), op, version=1, content_hash=content_hash(wf, op, 1),
                         reason="not needed any more")
    assert approve.value.code is decline.value.code is ErrorCode.REAUTH_REQUIRED


def test_value_threshold_requires_grant_any(wf):
    op = ready(wf, sku="BW-1004", qty=15)  # 15 × 86.20 = 1,293.00 < 2,500
    big = ready(wf, sku="BW-1003", qty=3)  # 3 × 1,249.00 = 3,747.00 ≥ 2,500 (HALDEN price "1.249,00")
    with wf.engine.connect() as conn:
        total = conn.execute(select(t.validation_snapshots.c.estimated_total).where(
            t.validation_snapshots.c.operation_id == big)).scalar()
    assert str(total) == "3747.00"
    wf.approve(op, by="priya")
    with pytest.raises(DomainError) as grant_any:
        wf.approve(big, by="priya")
    assert grant_any.value.code is ErrorCode.FORBIDDEN
    wf.approve(big, by="marta")
    assert wf.state(big)[0] == S.APPROVED and wf.state(op)[0] == S.APPROVED


def test_target_hold_blocks_and_linked_hold_requires_override(wf):
    held = wf.draft("halden", customer="10871")
    assert wf.submit(held).blocking is True and wf.state(held)[0] == S.DRAFT
    linked = wf.draft("skarvik", customer="KND-0114")  # Vantaa: CLEAR in SKARVIK, HOLD in HALDEN (P5)
    assert wf.submit(linked).blocking is False
    with pytest.raises(DomainError) as std:
        wf.approve(linked, by="priya")
    assert std.value.code is ErrorCode.FORBIDDEN
    with pytest.raises(DomainError) as no_reason:
        wf.approve(linked, by="marta")
    assert no_reason.value.code is ErrorCode.VALIDATION
    wf.approve(linked, by="marta", override_reason="HALDEN hold is for the DE entity; Nordic account cleared")
    assert wf.state(linked)[0] == S.APPROVED
    assert "OVERRIDE_RECORDED" in wf.events(linked)


def test_i5_overlap_refused_then_audited_override(wf):
    first = wf.approved("halden")
    wf.halden.next_e8_modes.append("lost_before_backend")
    wf.step()
    assert wf.state(first)[0] == S.UNKNOWN
    second = ready(wf, qty=4)
    with pytest.raises(DomainError) as overlap:
        wf.approve(second, by="priya")
    assert overlap.value.code is ErrorCode.CONFLICT_OVERLAP
    with pytest.raises(DomainError) as short_reason:
        wf.approve(second, by="marta", override_reason="too short")
    assert short_reason.value.code is ErrorCode.CONFLICT_OVERLAP
    wf.approve(second, by="marta", override_reason="first order confirmed lost by phone with HALDEN desk")
    assert wf.state(second)[0] == S.APPROVED and "OVERRIDE_RECORDED" in wf.events(second)


def test_validation_older_than_ten_minutes_is_refused(wf):
    op = ready(wf)
    wf.clock.advance(11 * 60)
    with pytest.raises(DomainError) as stale:
        wf.approve(op)
    assert stale.value.code is ErrorCode.APPROVAL_STALE_VALIDATION
    wf.engines.refresher.run_once()
    wf.approve(op)
    assert wf.state(op)[0] == S.APPROVED


@pytest.mark.parametrize("who", ["sanna", "rafael", "helena", "tomas"])
def test_roles_without_draft_or_approval_permissions_are_refused(wf, who):
    with pytest.raises(DomainError) as create:
        wf.draft("halden", by=who)
    assert create.value.code is ErrorCode.FORBIDDEN
    op = ready(wf)
    with pytest.raises(DomainError) as approve:
        wf.approve(op, by=who)
    assert approve.value.code is ErrorCode.FORBIDDEN


def test_unknown_customer_and_unmapped_item_are_refused_before_any_record(wf):
    with pytest.raises(DomainError) as cust:
        wf.draft("halden", customer="99999")
    assert cust.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as item:
        wf.draft("halden", sku="BW-9999")
    assert item.value.code is ErrorCode.VALIDATION
    with wf.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.operations)).scalar() == 0


def test_latin1_and_capability_fields_are_validated(wf):
    with pytest.raises(DomainError) as po:
        wf.draft("halden", customer_po="PO-1")  # HALDEN declares no customer PO
    assert po.value.code is ErrorCode.VALIDATION
    op = wf.draft("skarvik", delivery_note="Kaj 7 ✓")
    result = wf.submit(op)
    assert result.blocking is True and any(c.check == "encoding" for c in result.checks)


def test_idempotency_key_returns_the_same_operation(wf):
    a = commands.create_draft(wf.ctx, wf.p("daniel"), wf.request("halden"), idempotency_key="k-1")
    b = commands.create_draft(wf.ctx, wf.p("daniel"), wf.request("halden"), idempotency_key="k-1")
    assert a.operation_id == b.operation_id


def test_principal_roles_are_server_side_only(wf):
    forged = Principal(user_id=wf.users["sanna"], username="sanna", roles=frozenset({Role.INVENTORY_COORDINATOR}),
                       authenticated_at=wf.clock() + timedelta(seconds=1))
    with pytest.raises(DomainError):
        commands.create_draft(wf.ctx, forged, wf.request("halden"))
