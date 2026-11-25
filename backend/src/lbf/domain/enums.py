"""Closed enumerations of the canonical contract ``lbf-canonical/1.0``."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

CANONICAL_SCHEMA = "lbf-canonical/1.0"
AUDIT_SCHEMA = "lbf-audit/1.0"

BackendId = Literal["halden", "skarvik"]
BACKEND_IDS: tuple[BackendId, ...] = ("halden", "skarvik")


class OperationState(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    DECLINED = "DECLINED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    AWAITING_ACK = "AWAITING_ACK"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    ESCALATED = "ESCALATED"
    NOT_APPLIED = "NOT_APPLIED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED_ATTESTED = "CLOSED_ATTESTED"


TERMINAL_STATES = frozenset(
    {OperationState.CONFIRMED, OperationState.REJECTED, OperationState.WITHDRAWN, OperationState.CLOSED_ATTESTED}
)
# Overlap guard I5: states that block a second approval for overlapping goods.
OVERLAP_BLOCKING_STATES = frozenset(
    {
        OperationState.APPROVED,
        OperationState.SUBMITTING,
        OperationState.AWAITING_ACK,
        OperationState.UNKNOWN,
        OperationState.RECONCILING,
        OperationState.ESCALATED,
    }
)
# Demo reset guard.
RESET_BLOCKING_STATES = frozenset(
    {OperationState.APPROVED, OperationState.SUBMITTING, OperationState.AWAITING_ACK, OperationState.RECONCILING}
)


class Outcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    ESCALATED = "ESCALATED"
    CLOSED_ATTESTED = "CLOSED_ATTESTED"


class EvidenceClass(StrEnum):
    # CONFIRMED
    READ_BACK = "READ_BACK"
    ACKNOWLEDGED_ONLY = "ACKNOWLEDGED_ONLY"
    RECONCILED = "RECONCILED"
    DEDUP_CONFIRMED = "DEDUP_CONFIRMED"
    MANUAL_VERIFIED = "MANUAL_VERIFIED"
    # REJECTED
    BACKEND_REJECTION = "BACKEND_REJECTION"
    # NOT_APPLIED
    NOT_SENT = "NOT_SENT"
    PRE_PROCESSING_REFUSAL = "PRE_PROCESSING_REFUSAL"
    NEVER_DELIVERED = "NEVER_DELIVERED"
    WITHDRAWN_BEFORE_PICKUP = "WITHDRAWN_BEFORE_PICKUP"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    # UNKNOWN
    NO_MATCH_AFTER_UNCERTAIN = "NO_MATCH_AFTER_UNCERTAIN"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    ACK_OVERDUE = "ACK_OVERDUE"
    INTERRUPTED = "INTERRUPTED"
    SOURCE_STALE = "SOURCE_STALE"
    # ESCALATED
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    CONTRADICTION = "CONTRADICTION"
    RECHECK_LIMIT = "RECHECK_LIMIT"
    MANUAL = "MANUAL"
    # CLOSED_ATTESTED
    ATTESTED_BOOKED = "ATTESTED_BOOKED"
    ATTESTED_NOT_BOOKED = "ATTESTED_NOT_BOOKED"


class VerificationLevel(StrEnum):
    VERIFIED_BY_READ = "VERIFIED_BY_READ"
    ACKNOWLEDGED_ONLY = "ACKNOWLEDGED_ONLY"
    ATTESTED = "ATTESTED"
    UNRESOLVED = "UNRESOLVED"


class TransportObservationKind(StrEnum):
    NOT_DELIVERED = "NOT_DELIVERED"
    REFUSED_PRE_PROCESSING = "REFUSED_PRE_PROCESSING"
    REJECTED = "REJECTED"
    ACCEPTED = "ACCEPTED"
    DELIVERED_PENDING_ACK = "DELIVERED_PENDING_ACK"
    UNCERTAIN = "UNCERTAIN"
    INTERRUPTED = "INTERRUPTED"


class ReconciliationVerdict(StrEnum):
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    MISMATCH = "MISMATCH"
    SOURCE_STALE = "SOURCE_STALE"
    READ_FAILED = "READ_FAILED"
    CONTRADICTION = "CONTRADICTION"


class AvailabilityStatus(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNDETERMINED = "UNDETERMINED"
    STALE = "STALE"


class CreditHold(StrEnum):
    HOLD = "HOLD"
    CLEAR = "CLEAR"
    UNKNOWN = "UNKNOWN"


class AuthorizationKind(StrEnum):
    SUBMIT = "SUBMIT"
    REDELIVER = "REDELIVER"
    WITHDRAW = "WITHDRAW"


class AuthorizationState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


class TransmissionState(StrEnum):
    SENDING = "SENDING"
    OBSERVED = "OBSERVED"
    INTERRUPTED = "INTERRUPTED"


class DraftOrigin(StrEnum):
    UI = "UI"
    ASSISTANT = "ASSISTANT"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"


class ReconciliationTrigger(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    ACK = "ACK"
    RECOVERY = "RECOVERY"


class ResolutionKind(StrEnum):
    VERIFIED = "VERIFIED"
    ATTESTED_BOOKED = "ATTESTED_BOOKED"
    ATTESTED_NOT_BOOKED = "ATTESTED_NOT_BOOKED"


class Role(StrEnum):
    OPS_MANAGER = "ops_manager"
    SALES_OPS = "sales_ops"
    INVENTORY_COORDINATOR = "inventory_coordinator"
    INTEGRATION_ENGINEER = "integration_engineer"
    COMPLIANCE_REVIEWER = "compliance_reviewer"
    SYSADMIN = "sysadmin"


class Permission(StrEnum):
    CUSTOMER_READ = "customer.read"
    INVENTORY_READ = "inventory.read"
    OPERATION_READ = "operation.read"
    INVENTORY_ANNOTATE = "inventory.annotate"
    DRAFT_CREATE = "draft.create"
    DRAFT_EDIT = "draft.edit"
    DRAFT_SUBMIT = "draft.submit"
    DRAFT_WITHDRAW = "draft.withdraw"
    APPROVAL_GRANT_STANDARD = "approval.grant.standard"
    APPROVAL_GRANT_ANY = "approval.grant.any"
    APPROVAL_OVERRIDE = "approval.override"
    RECONCILE_RECHECK = "reconcile.recheck"
    SKARVIK_WITHDRAW_AUTHORIZE = "skarvik.withdraw.authorize"
    SKARVIK_REDELIVER_AUTHORIZE = "skarvik.redeliver.authorize"
    ESCALATION_OPEN = "escalation.open"
    ESCALATION_RESOLVE = "escalation.resolve"
    AUDIT_READ = "audit.read"
    AUDIT_EXPORT = "audit.export"
    AUDIT_VERIFY = "audit.verify"
    BACKEND_HEALTH_READ = "backend.health.read"
    ACKQUEUE_READ = "ackqueue.read"
    ASSISTANT_USE = "assistant.use"
    ADMIN_USERS = "admin.users"
    ADMIN_ROLES = "admin.roles"
    ADMIN_CONFIG_READ = "admin.config.read"
    DEMO_CONTROL = "demo.control"


class Action(StrEnum):
    """Operation-level actions a human may take; rendered by the UI only when permitted."""

    REVISE = "revise"
    SUBMIT_FOR_APPROVAL = "submit_for_approval"
    WITHDRAW = "withdraw"
    APPROVE = "approve"
    DECLINE = "decline"
    RECHECK = "recheck"
    ESCALATE = "escalate"
    RESOLVE_VERIFIED = "resolve_verified"
    RESOLVE_ATTESTED = "resolve_attested"
    AUTHORIZE_WITHDRAW_UNCLAIMED = "authorize_withdraw_unclaimed"
    AUTHORIZE_REDELIVERY = "authorize_redelivery"


class PolicyResult(StrEnum):
    PASS = "PASS"  # noqa: S105 - policy result, not a credential
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"
    OVERRIDE_REQUIRED = "OVERRIDE_REQUIRED"
    GRANT_ANY_REQUIRED = "GRANT_ANY_REQUIRED"
    STALE_VALIDATION = "STALE_VALIDATION"
