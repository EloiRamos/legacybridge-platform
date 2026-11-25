"""Static, versioned authorization policy. Pure functions only.

The browser never sends a role; callers pass the server-side principal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from lbf.domain.enums import AvailabilityStatus, CreditHold, PolicyResult, Role
from lbf.domain.enums import Permission as P
from lbf.domain.models import PolicyCheck

POLICY_VERSION = "2026.11-1"
APPROVAL_VALUE_THRESHOLD = Decimal("2500.00")
VALIDATION_MAX_AGE = timedelta(minutes=10)
STEP_UP_MAX_AGE = timedelta(minutes=15)
OVERRIDE_REASON_MIN = 20
DECLINE_REASON_MIN = 10
ATTESTATION_NOTE_MIN = 20

_READS = {P.CUSTOMER_READ, P.INVENTORY_READ, P.OPERATION_READ}

ROLE_PERMISSIONS: dict[Role, frozenset[P]] = {
    Role.OPS_MANAGER: frozenset(
        _READS
        | {
            P.INVENTORY_ANNOTATE,
            P.DRAFT_CREATE,
            P.DRAFT_EDIT,
            P.DRAFT_SUBMIT,
            P.DRAFT_WITHDRAW,
            P.APPROVAL_GRANT_STANDARD,
            P.APPROVAL_GRANT_ANY,
            P.APPROVAL_OVERRIDE,
            P.RECONCILE_RECHECK,
            P.SKARVIK_WITHDRAW_AUTHORIZE,
            P.SKARVIK_REDELIVER_AUTHORIZE,
            P.ESCALATION_OPEN,
            P.ESCALATION_RESOLVE,
            P.AUDIT_READ,
            P.BACKEND_HEALTH_READ,
            P.ACKQUEUE_READ,
            P.ASSISTANT_USE,
        }
    ),
    Role.SALES_OPS: frozenset(
        _READS
        | {
            P.DRAFT_CREATE,
            P.DRAFT_EDIT,
            P.DRAFT_SUBMIT,
            P.DRAFT_WITHDRAW,
            P.APPROVAL_GRANT_STANDARD,
            P.RECONCILE_RECHECK,
            P.ESCALATION_OPEN,
            P.ASSISTANT_USE,
        }
    ),
    Role.INVENTORY_COORDINATOR: frozenset(_READS | {P.INVENTORY_ANNOTATE, P.ASSISTANT_USE}),
    Role.INTEGRATION_ENGINEER: frozenset(
        _READS
        | {
            P.RECONCILE_RECHECK,
            P.ESCALATION_OPEN,
            P.AUDIT_READ,
            P.BACKEND_HEALTH_READ,
            P.ACKQUEUE_READ,
            P.ASSISTANT_USE,
        }
    ),
    Role.COMPLIANCE_REVIEWER: frozenset(
        _READS | {P.AUDIT_READ, P.AUDIT_EXPORT, P.AUDIT_VERIFY, P.BACKEND_HEALTH_READ, P.ACKQUEUE_READ}
    ),
    Role.SYSADMIN: frozenset(
        {
            P.BACKEND_HEALTH_READ,
            P.ACKQUEUE_READ,
            P.ADMIN_USERS,
            P.ADMIN_ROLES,
            P.ADMIN_CONFIG_READ,
            P.DEMO_CONTROL,
        }
    ),
}

# Permissions granted only in the demo/test profile.
PROFILE_GATED = frozenset({P.DEMO_CONTROL})


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    username: str
    roles: frozenset[Role]
    authenticated_at: datetime | None = None
    on_behalf_of: UUID | None = None  # set when the assistant executes a tool for the human


def permissions_for(roles: Iterable[Role], *, profile: str = "default") -> frozenset[P]:
    perms: set[P] = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS[role]
    if profile not in ("demo", "test"):
        perms -= PROFILE_GATED
    return frozenset(perms)


def has_permission(principal: Principal, permission: P, *, profile: str = "default") -> bool:
    return permission in permissions_for(principal.roles, profile=profile)


def step_up_fresh(principal: Principal, now: datetime) -> bool:
    return principal.authenticated_at is not None and principal.authenticated_at >= now - STEP_UP_MAX_AGE


# --------------------------------------------------------------------------- segregation of duties


def sod1_violated(approver: UUID, creator: UUID, version_authors: Iterable[UUID]) -> bool:
    """SoD-1: approver ∉ {creator} ∪ {authors of any version}. SoD-2 is satisfied by recording the
    invoking human as author of assistant-prepared versions."""
    return approver == creator or approver in set(version_authors)


def sod3_violated(actor: UUID, target_user: UUID) -> bool:
    """SoD-3: nobody grants or revokes their own roles."""
    return actor == target_user


def sod4_violated(authorizer: UUID, version_authors: Iterable[UUID], creator: UUID) -> bool:
    """SoD-4: a redelivery authorizer is not the author of any version (nor the creator)."""
    return authorizer == creator or authorizer in set(version_authors)


def sod5_violated(resolver: UUID, approver_of_attempt: UUID | None) -> bool:
    """SoD-5: the resolver of an escalation is not the approver of the escalated attempt."""
    return approver_of_attempt is not None and resolver == approver_of_attempt


# --------------------------------------------------------------------------- approval policy


@dataclass(frozen=True)
class LineFacts:
    line_no: int
    quantity_base: int
    availability: AvailabilityStatus
    available_base: int | None


@dataclass(frozen=True)
class ApprovalFacts:
    target_hold: CreditHold
    linked_holds: Sequence[CreditHold]
    lines: Sequence[LineFacts]
    estimated_total: Decimal | None
    atomic_stock_allocation: bool  # capability: stock_allocation_on_accept == ATOMIC_ALLOCATION
    overlap_with: Sequence[str] = field(default_factory=tuple)  # correlation values of blocking operations
    validated_at: datetime | None = None


def evaluate_checks(
    facts: ApprovalFacts, *, now: datetime | None = None, include_overlap: bool = True
) -> list[PolicyCheck]:
    checks: list[PolicyCheck] = []
    if facts.target_hold is CreditHold.HOLD:
        checks.append(
            PolicyCheck(check="target_hold", result=PolicyResult.BLOCKING, detail="Target account is on credit hold.")
        )
    elif facts.target_hold is CreditHold.UNKNOWN:
        checks.append(
            PolicyCheck(
                check="target_hold", result=PolicyResult.OVERRIDE_REQUIRED, detail="Target account hold status unknown."
            )
        )
    elif CreditHold.HOLD in facts.linked_holds:
        checks.append(
            PolicyCheck(
                check="linked_account_hold",
                result=PolicyResult.OVERRIDE_REQUIRED,
                detail="A crosswalk-linked account in another backend is on credit hold.",
            )
        )
    else:
        checks.append(PolicyCheck(check="target_hold", result=PolicyResult.PASS))

    for line in facts.lines:
        if line.availability in (AvailabilityStatus.UNDETERMINED, AvailabilityStatus.STALE):
            checks.append(
                PolicyCheck(
                    check="line_availability",
                    result=PolicyResult.OVERRIDE_REQUIRED,
                    detail=f"Availability {line.availability.value}.",
                    line_no=line.line_no,
                )
            )
            continue
        short = line.available_base is not None and line.quantity_base > line.available_base
        if short and facts.atomic_stock_allocation:
            checks.append(
                PolicyCheck(
                    check="line_stock",
                    result=PolicyResult.BLOCKING,
                    detail=f"Known insufficient stock ({line.available_base} free, {line.quantity_base} requested).",
                    line_no=line.line_no,
                )
            )
        elif short:
            checks.append(
                PolicyCheck(
                    check="line_stock",
                    result=PolicyResult.WARNING,
                    detail=f"Short stock ({line.available_base} free); the backend accepts with backorder.",
                    line_no=line.line_no,
                )
            )
        else:
            checks.append(PolicyCheck(check="line_stock", result=PolicyResult.PASS, line_no=line.line_no))

    if facts.estimated_total is None or facts.estimated_total >= APPROVAL_VALUE_THRESHOLD:
        checks.append(
            PolicyCheck(
                check="value_threshold",
                result=PolicyResult.GRANT_ANY_REQUIRED,
                detail="Estimated total unknown or at least EUR 2,500.00.",
            )
        )
    else:
        checks.append(PolicyCheck(check="value_threshold", result=PolicyResult.PASS))

    if include_overlap:
        if facts.overlap_with:
            checks.append(
                PolicyCheck(
                    check="overlap_I5",
                    result=PolicyResult.OVERRIDE_REQUIRED,
                    detail="Overlapping unresolved operation(s): " + ", ".join(sorted(facts.overlap_with)),
                )
            )
        else:
            checks.append(PolicyCheck(check="overlap_I5", result=PolicyResult.PASS))

    if now is not None:
        if facts.validated_at is None or facts.validated_at < now - VALIDATION_MAX_AGE:
            checks.append(
                PolicyCheck(check="validation_age", result=PolicyResult.STALE_VALIDATION, detail="Re-validate first.")
            )
        else:
            checks.append(PolicyCheck(check="validation_age", result=PolicyResult.PASS))
    return checks


@dataclass(frozen=True)
class Requirements:
    blocking: bool
    stale: bool
    needs_override: bool
    needs_grant_any: bool
    has_overlap: bool


def requirements(checks: Iterable[PolicyCheck]) -> Requirements:
    checks = list(checks)
    return Requirements(
        blocking=any(c.result is PolicyResult.BLOCKING for c in checks),
        stale=any(c.result is PolicyResult.STALE_VALIDATION for c in checks),
        needs_override=any(c.result is PolicyResult.OVERRIDE_REQUIRED for c in checks),
        needs_grant_any=any(c.result is PolicyResult.GRANT_ANY_REQUIRED for c in checks),
        has_overlap=any(c.check == "overlap_I5" and c.result is PolicyResult.OVERRIDE_REQUIRED for c in checks),
    )


def approval_permitted(principal: Principal, req: Requirements, *, override_reason: str | None) -> tuple[bool, str]:
    """Whether ``principal`` may approve given the requirements. Returns ``(ok, reason)``."""
    if req.blocking:
        return False, "BLOCKING"
    if req.stale:
        return False, "STALE_VALIDATION"
    if req.needs_grant_any:
        if not has_permission(principal, P.APPROVAL_GRANT_ANY):
            return False, "GRANT_ANY_REQUIRED"
    elif not (has_permission(principal, P.APPROVAL_GRANT_STANDARD) or has_permission(principal, P.APPROVAL_GRANT_ANY)):
        return False, "FORBIDDEN"
    if req.needs_override:
        if not has_permission(principal, P.APPROVAL_OVERRIDE):
            return False, "OVERRIDE_REQUIRED"
        if override_reason is None or len(override_reason.strip()) < OVERRIDE_REASON_MIN:
            return False, "OVERRIDE_REASON_REQUIRED"
    return True, "OK"
