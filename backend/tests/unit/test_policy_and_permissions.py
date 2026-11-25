"""Permission matrix, SoD, approval checks and permitted_actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from lbf.auth import policy
from lbf.auth.policy import ApprovalFacts, LineFacts, Principal
from lbf.domain.enums import Action, AvailabilityStatus, CreditHold, EvidenceClass, OperationState, PolicyResult, Role
from lbf.domain.enums import Permission as P
from lbf.ports.capabilities import HALDEN_CAPABILITIES, skarvik_capabilities
from lbf.workflow.permissions import OperationFacts, permitted_actions

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
CREATOR, MARTA, PRIYA, SANNA, RAFAEL, HELENA, TOMAS = (UUID(int=i) for i in range(1, 8))


def principal(uid, *roles):
    return Principal(user_id=uid, username=str(uid), roles=frozenset(roles), authenticated_at=NOW)


DANIEL_P = principal(CREATOR, Role.SALES_OPS)
MARTA_P = principal(MARTA, Role.OPS_MANAGER)
PRIYA_P = principal(PRIYA, Role.SALES_OPS)
SANNA_P = principal(SANNA, Role.INVENTORY_COORDINATOR)
RAFAEL_P = principal(RAFAEL, Role.INTEGRATION_ENGINEER)
HELENA_P = principal(HELENA, Role.COMPLIANCE_REVIEWER)
TOMAS_P = principal(TOMAS, Role.SYSADMIN)

# Expected matrix transcribed (independent of the implementation's table layout).
EXPECTED = {
    P.CUSTOMER_READ: {
        "ops_manager",
        "sales_ops",
        "inventory_coordinator",
        "integration_engineer",
        "compliance_reviewer",
    },
    P.INVENTORY_ANNOTATE: {"ops_manager", "inventory_coordinator"},
    P.DRAFT_CREATE: {"ops_manager", "sales_ops"},
    P.DRAFT_WITHDRAW: {"ops_manager", "sales_ops"},
    P.APPROVAL_GRANT_STANDARD: {"ops_manager", "sales_ops"},
    P.APPROVAL_GRANT_ANY: {"ops_manager"},
    P.APPROVAL_OVERRIDE: {"ops_manager"},
    P.RECONCILE_RECHECK: {"ops_manager", "sales_ops", "integration_engineer"},
    P.SKARVIK_REDELIVER_AUTHORIZE: {"ops_manager"},
    P.ESCALATION_OPEN: {"ops_manager", "sales_ops", "integration_engineer"},
    P.ESCALATION_RESOLVE: {"ops_manager"},
    P.AUDIT_READ: {"ops_manager", "integration_engineer", "compliance_reviewer"},
    P.AUDIT_EXPORT: {"compliance_reviewer"},
    P.BACKEND_HEALTH_READ: {"ops_manager", "integration_engineer", "compliance_reviewer", "sysadmin"},
    P.ASSISTANT_USE: {"ops_manager", "sales_ops", "inventory_coordinator", "integration_engineer"},
    P.ADMIN_USERS: {"sysadmin"},
    P.DEMO_CONTROL: {"sysadmin"},
}


@pytest.mark.parametrize("perm", list(EXPECTED))
def test_permission_matrix(perm):
    holders = {r.value for r in Role if perm in policy.permissions_for([r], profile="demo")}
    assert holders == EXPECTED[perm]


def test_demo_control_only_in_demo_or_test_profile():
    assert not policy.has_permission(TOMAS_P, P.DEMO_CONTROL, profile="default")
    assert policy.has_permission(TOMAS_P, P.DEMO_CONTROL, profile="test")


def test_sod_rules():
    assert policy.sod1_violated(CREATOR, CREATOR, [])
    assert policy.sod1_violated(MARTA, CREATOR, [MARTA])  # authored a later version
    assert not policy.sod1_violated(MARTA, CREATOR, [CREATOR])
    assert policy.sod3_violated(TOMAS, TOMAS)
    assert policy.sod4_violated(MARTA, [MARTA], CREATOR)
    assert policy.sod5_violated(MARTA, MARTA) and not policy.sod5_violated(MARTA, PRIYA)


def facts(**kw):
    base = {
        "target_hold": CreditHold.CLEAR,
        "linked_holds": (),
        "lines": (LineFacts(1, 10, AvailabilityStatus.IN_STOCK, 300),),
        "estimated_total": Decimal("48.50"),
        "atomic_stock_allocation": True,
        "validated_at": NOW,
    }
    base.update(kw)
    return ApprovalFacts(**base)


def results(f):
    return {c.check: c.result for c in policy.evaluate_checks(f, now=NOW)}


def test_policy_checks_table():
    assert results(facts(target_hold=CreditHold.HOLD))["target_hold"] is PolicyResult.BLOCKING
    assert results(facts(target_hold=CreditHold.UNKNOWN))["target_hold"] is PolicyResult.OVERRIDE_REQUIRED
    assert results(facts(linked_holds=(CreditHold.HOLD,)))["linked_account_hold"] is PolicyResult.OVERRIDE_REQUIRED
    short = (LineFacts(1, 10, AvailabilityStatus.IN_STOCK, 3),)
    assert results(facts(lines=short))["line_stock"] is PolicyResult.BLOCKING
    assert results(facts(lines=short, atomic_stock_allocation=False))["line_stock"] is PolicyResult.WARNING
    stale = (LineFacts(1, 10, AvailabilityStatus.STALE, None),)
    assert results(facts(lines=stale))["line_availability"] is PolicyResult.OVERRIDE_REQUIRED
    assert results(facts(estimated_total=None))["value_threshold"] is PolicyResult.GRANT_ANY_REQUIRED
    assert results(facts(estimated_total=Decimal("2500.00")))["value_threshold"] is PolicyResult.GRANT_ANY_REQUIRED
    assert results(facts(estimated_total=Decimal("2499.99")))["value_threshold"] is PolicyResult.PASS
    assert results(facts(overlap_with=("LB0000000001",)))["overlap_I5"] is PolicyResult.OVERRIDE_REQUIRED
    old = NOW - timedelta(minutes=10, seconds=1)
    assert results(facts(validated_at=old))["validation_age"] is PolicyResult.STALE_VALIDATION


def test_approval_permitted_thresholds_and_override():
    big = policy.requirements(policy.evaluate_checks(facts(estimated_total=Decimal("3000")), now=NOW))
    assert policy.approval_permitted(PRIYA_P, big, override_reason=None) == (False, "GRANT_ANY_REQUIRED")
    assert policy.approval_permitted(MARTA_P, big, override_reason=None)[0]
    ovr = policy.requirements(policy.evaluate_checks(facts(overlap_with=("LBX",)), now=NOW))
    assert policy.approval_permitted(MARTA_P, ovr, override_reason="short")[1] == "OVERRIDE_REASON_REQUIRED"
    assert policy.approval_permitted(MARTA_P, ovr, override_reason="Customer confirmed second order by phone")[0]
    assert policy.approval_permitted(PRIYA_P, ovr, override_reason="x" * 30)[1] == "OVERRIDE_REQUIRED"


# ---------------------------------------------------------------- permitted_actions


def op(state, **kw):
    base = {"state": state, "created_by": CREATOR, "version_authors": frozenset({CREATOR})}
    base.update(kw)
    return OperationFacts(**base)


OK_REQ = policy.requirements(policy.evaluate_checks(facts(), now=NOW))


def test_ready_approval_respects_sod1_and_roles():
    o = op(OperationState.READY, approval_requirements=OK_REQ)
    assert Action.APPROVE in permitted_actions(o, HALDEN_CAPABILITIES, PRIYA_P, NOW)
    assert Action.APPROVE not in permitted_actions(o, HALDEN_CAPABILITIES, DANIEL_P, NOW)  # own draft
    for p in (SANNA_P, RAFAEL_P, HELENA_P, TOMAS_P):
        assert not permitted_actions(o, HALDEN_CAPABILITIES, p, NOW) & {Action.APPROVE, Action.DECLINE}


def test_assistant_authored_version_blocks_invoker_sod2():
    # SoD-2: an ASSISTANT-origin version is authored by the invoking human (here PRIYA).
    o = op(OperationState.READY, version_authors=frozenset({CREATOR, PRIYA}), approval_requirements=OK_REQ)
    assert Action.APPROVE not in permitted_actions(o, HALDEN_CAPABILITIES, PRIYA_P, NOW)


def test_unknown_permits_only_read_only_and_capability_gated_actions():
    halden = permitted_actions(op(OperationState.UNKNOWN), HALDEN_CAPABILITIES, MARTA_P, NOW)
    assert halden == {Action.RECHECK, Action.ESCALATE}
    caps = skarvik_capabilities()
    skarvik = permitted_actions(
        op(OperationState.UNKNOWN, first_handoff_at=NOW - timedelta(minutes=5), message_id="LB0000000001-01"),
        caps,
        MARTA_P,
        NOW,
    )
    assert skarvik == {
        Action.RECHECK,
        Action.ESCALATE,
        Action.AUTHORIZE_WITHDRAW_UNCLAIMED,
        Action.AUTHORIZE_REDELIVERY,
    }


def test_redelivery_window_and_single_redelivery():
    caps = skarvik_capabilities(test_profile=True)  # window 120 s, margin 10 s
    late = op(OperationState.UNKNOWN, first_handoff_at=NOW - timedelta(seconds=111), message_id="m")
    assert Action.AUTHORIZE_REDELIVERY not in permitted_actions(late, caps, MARTA_P, NOW)
    inside = op(OperationState.UNKNOWN, first_handoff_at=NOW - timedelta(seconds=100), message_id="m")
    assert Action.AUTHORIZE_REDELIVERY in permitted_actions(inside, caps, MARTA_P, NOW)
    second = op(
        OperationState.UNKNOWN, first_handoff_at=NOW - timedelta(seconds=10), message_id="m", redeliveries_in_attempt=1
    )
    assert Action.AUTHORIZE_REDELIVERY not in permitted_actions(second, caps, MARTA_P, NOW)


def test_resolution_sod5_and_terminal_states():
    esc = op(OperationState.ESCALATED, approver_of_current_attempt=MARTA)
    assert Action.RESOLVE_VERIFIED not in permitted_actions(esc, HALDEN_CAPABILITIES, MARTA_P, NOW)
    for s in (
        OperationState.CONFIRMED,
        OperationState.REJECTED,
        OperationState.WITHDRAWN,
        OperationState.CLOSED_ATTESTED,
    ):
        assert permitted_actions(op(s), HALDEN_CAPABILITIES, MARTA_P, NOW) == frozenset()


def test_withdraw_own_vs_any_and_not_applied_resubmit():
    na = op(OperationState.NOT_APPLIED, last_evidence=EvidenceClass.NOT_SENT)
    assert {Action.SUBMIT_FOR_APPROVAL, Action.WITHDRAW, Action.REVISE} <= permitted_actions(
        na, HALDEN_CAPABILITIES, DANIEL_P, NOW
    )
    assert Action.WITHDRAW not in permitted_actions(na, HALDEN_CAPABILITIES, PRIYA_P, NOW)
    assert Action.WITHDRAW in permitted_actions(na, HALDEN_CAPABILITIES, MARTA_P, NOW)
    pending = op(OperationState.NOT_APPLIED, last_evidence=EvidenceClass.NOT_SENT, has_open_authorization=True)
    assert Action.WITHDRAW not in permitted_actions(pending, HALDEN_CAPABILITIES, MARTA_P, NOW)


def test_manual_recheck_rate_limit():
    recent = op(OperationState.UNKNOWN, last_manual_recheck_at=NOW - timedelta(seconds=5))
    assert Action.RECHECK not in permitted_actions(recent, HALDEN_CAPABILITIES, RAFAEL_P, NOW)
