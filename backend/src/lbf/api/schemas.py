"""API request/response schemas (OpenAPI 3.1 source of the generated TypeScript client).

Requests: ``extra="forbid"``, length limits (free text ≤ 500, delivery note ≤ 60), quantities 1..999, enum-only
filters. Responses expose evidence, never secrets or payload bytes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lbf.domain.enums import (
    Action,
    AvailabilityStatus,
    CreditHold,
    EvidenceClass,
    OperationState,
    Outcome,
    Permission,
    ResolutionKind,
    Role,
)


class In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- auth


class LoginIn(In):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class ReauthIn(In):
    password: str = Field(min_length=1, max_length=200)


class SessionOut(Out):
    user_id: UUID
    username: str
    roles: list[Role]
    permissions: list[Permission]
    authenticated_at: datetime | None
    csrf_token: str
    profile: str


# --------------------------------------------------------------------------- customers / inventory


class AccountOut(Out):
    backend_id: str
    native_id: str
    name_raw: str
    name_display: str
    credit_hold: CreditHold
    city: str | None = None
    country_code: str | None = None
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    customer_since: date | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)
    untrusted_text: dict[str, str] = Field(default_factory=dict)


class PartyOut(Out):
    party_id: UUID
    display_name: str
    accounts: list[AccountOut]
    conflicts: list[dict[str, Any]]
    crosswalk_basis: Literal["CURATED_MASTER_DATA"] = "CURATED_MASTER_DATA"


class CustomerSearchOut(Out):
    query: str
    accounts: list[AccountOut]
    parties: list[PartyOut]
    ambiguity_groups: list[list[str]]
    backend_errors: dict[str, str] = Field(default_factory=dict)


class CustomerCardOut(Out):
    account: AccountOut
    linked_accounts: list[AccountOut]
    orders: list[dict[str, Any]]
    open_operations: list[OperationSummaryOut]
    backend_errors: dict[str, str] = Field(default_factory=dict)


class AvailabilityOut(Out):
    backend_id: str
    native_item: str
    canonical_sku: str | None
    status: AvailabilityStatus
    available_base: int | None
    as_of: datetime
    freshness: str
    snapshot_age_seconds: int | None
    explanation: str
    issues: list[dict[str, Any]]
    warehouses: list[dict[str, Any]]
    legacy_available_flag: bool | None


class InventoryItemOut(Out):
    canonical_sku: str
    name: str
    per_backend: list[AvailabilityOut]


class InventoryOut(Out):
    items: list[InventoryItemOut]
    unmapped: list[AvailabilityOut]
    backend_errors: dict[str, str] = Field(default_factory=dict)


class AnnotationIn(In):
    canonical_sku: str | None = Field(default=None, max_length=16)
    backend_id: Literal["halden", "skarvik"] | None = None
    native_item: str | None = Field(default=None, max_length=16)
    body: str = Field(min_length=1, max_length=500)


# --------------------------------------------------------------------------- operations


class LineIn(In):
    canonical_sku: str | None = Field(default=None, max_length=16)
    native_item: str | None = Field(default=None, max_length=16)
    quantity: int = Field(ge=1, le=999)


class DraftIn(In):
    backend_id: Literal["halden", "skarvik"]
    native_customer_id: str = Field(min_length=1, max_length=16)
    lines: list[LineIn] = Field(min_length=1, max_length=50)
    customer_po: str | None = Field(default=None, max_length=20)
    requested_date: date | None = None
    delivery_note: str | None = Field(default=None, max_length=60)


class ReviseIn(DraftIn):
    expected_version: int = Field(ge=1)


class VersionRefIn(In):
    expected_version: int = Field(ge=1)


class ApproveIn(In):
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    override_reason: str | None = Field(default=None, max_length=500)


class DeclineIn(In):
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class EscalateIn(In):
    note: str = Field(default="", max_length=500)


class ResolveIn(In):
    kind: ResolutionKind
    external_reference: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=500)


class DraftCreatedOut(Out):
    operation_id: UUID
    version: int
    content_hash: str
    state: OperationState


class CommandOut(Out):
    operation_id: UUID
    state: OperationState
    detail: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)


class OperationSummaryOut(Out):
    operation_id: UUID
    correlation_value: str
    backend_id: str
    native_customer_id: str
    state: OperationState
    last_outcome: Outcome | None
    last_evidence: EvidenceClass | None
    verification_level: str | None
    current_version: int
    attempt_count: int
    origin: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    estimated_total: str | None = None
    anchor_order_ref: str | None = None


class OperationDetailOut(Out):
    operation: OperationSummaryOut
    versions: list[dict[str, Any]]
    validation: dict[str, Any] | None
    approvals: list[dict[str, Any]]
    authorizations: list[dict[str, Any]]
    transmissions: list[dict[str, Any]]
    acknowledgements: list[dict[str, Any]]
    reconciliation_runs: list[dict[str, Any]]
    escalations: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    permitted_actions: list[Action]
    capabilities: dict[str, Any]
    schedule: dict[str, Any]


class QueueOut(Out):
    items: list[OperationSummaryOut]


class DashboardOut(Out):
    counts_by_state: dict[str, int]
    pending_approvals: int
    unknown: int
    escalated: int
    overdue_acks: int
    recent_outcomes: list[OperationSummaryOut]
    backends: list[dict[str, Any]]
    data_quality_issues: dict[str, int]


# --------------------------------------------------------------------------- audit / admin


class AuditEventOut(Out):
    seq: int
    event_type: str
    occurred_at: datetime
    actor_type: str
    actor_username: str | None
    on_behalf_of: str | None
    operation_id: UUID | None
    correlation: dict[str, Any]
    state: dict[str, Any] | None
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str


class AuditPageOut(Out):
    events: list[AuditEventOut]
    head_seq: int


class VerifyOut(Out):
    valid: bool
    count: int
    head_seq: int
    head_hash: str
    first_broken_seq: int | None
    reason: str


class UserOut(Out):
    user_id: UUID
    username: str
    display_name: str
    is_active: bool
    roles: list[Role]
    locked_until: datetime | None


class UserCreateIn(In):
    username: str = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=200)
    roles: list[Role] = Field(default_factory=list, max_length=6)


class RoleIn(In):
    role: Role


CustomerCardOut.model_rebuild()
