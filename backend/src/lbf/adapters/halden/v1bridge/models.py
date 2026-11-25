"""Typed domain models."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CENT = Decimal("0.01")


def quantize_money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- enums


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    ALLOCATED = "ALLOCATED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    ON_HOLD = "ON_HOLD"
    UNKNOWN = "UNKNOWN"


class StockStatus(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNDETERMINED = "UNDETERMINED"


class IssueCode(str, Enum):
    FLAG_MISMATCH = "FLAG_MISMATCH"
    DUPLICATE_SKU = "DUPLICATE_SKU"
    UNKNOWN_QUANTITY = "UNKNOWN_QUANTITY"
    NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
    MISSING_PRICE = "MISSING_PRICE"
    CONFLICTING_PRICE = "CONFLICTING_PRICE"
    MISSING_CATEGORY = "MISSING_CATEGORY"
    INVALID_EMAIL = "INVALID_EMAIL"
    UNPARSEABLE_DATE = "UNPARSEABLE_DATE"
    UNKNOWN_STATUS_CODE = "UNKNOWN_STATUS_CODE"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"


class DraftLifecycle(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    SUBMITTING = "SUBMITTING"
    CONFIRMED = "CONFIRMED"
    REJECTED_BY_ERP = "REJECTED_BY_ERP"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    REJECTED_BY_USER = "REJECTED_BY_USER"


class SubmissionOutcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


class OutcomeEvidence(str, Enum):
    READ_BACK = "READ_BACK"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RECONCILED = "RECONCILED"
    LEGACY_REJECTION = "LEGACY_REJECTION"
    NOT_SENT = "NOT_SENT"
    PRE_HANDLER_REJECTION = "PRE_HANDLER_REJECTION"
    NO_MATCH_AFTER_UNCERTAIN = "NO_MATCH_AFTER_UNCERTAIN"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


class ResultKind(str, Enum):
    SUBMIT = "SUBMIT"
    RECHECK = "RECHECK"


OUTSTANDING_STATUSES = frozenset(
    {OrderStatus.OPEN, OrderStatus.ALLOCATED, OrderStatus.SHIPPED, OrderStatus.ON_HOLD}
)

EVIDENCE_FOR_OUTCOME: dict[SubmissionOutcome, frozenset[OutcomeEvidence]] = {
    SubmissionOutcome.CONFIRMED: frozenset(
        {OutcomeEvidence.READ_BACK, OutcomeEvidence.ACKNOWLEDGED, OutcomeEvidence.RECONCILED}
    ),
    SubmissionOutcome.REJECTED: frozenset({OutcomeEvidence.LEGACY_REJECTION}),
    SubmissionOutcome.NOT_APPLIED: frozenset(
        {OutcomeEvidence.NOT_SENT, OutcomeEvidence.PRE_HANDLER_REJECTION}
    ),
    SubmissionOutcome.UNKNOWN: frozenset(
        {
            OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN,
            OutcomeEvidence.AMBIGUOUS_MATCH,
            OutcomeEvidence.READBACK_MISMATCH,
            OutcomeEvidence.RECONCILIATION_FAILED,
        }
    ),
}

LIFECYCLE_FOR_OUTCOME: dict[SubmissionOutcome, DraftLifecycle] = {
    SubmissionOutcome.CONFIRMED: DraftLifecycle.CONFIRMED,
    SubmissionOutcome.REJECTED: DraftLifecycle.REJECTED_BY_ERP,
    SubmissionOutcome.NOT_APPLIED: DraftLifecycle.NOT_APPLIED,
    SubmissionOutcome.UNKNOWN: DraftLifecycle.UNKNOWN,
}


class DraftAction(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    DISCARD = "DISCARD"
    RECHECK = "RECHECK"
    VIEW_ORDER = "VIEW_ORDER"


# Permitted UI actions per lifecycle (the UI renders buttons strictly from this table).
PERMITTED_ACTIONS: dict[DraftLifecycle, tuple[DraftAction, ...]] = {
    DraftLifecycle.READY: (DraftAction.APPROVE, DraftAction.REJECT),
    DraftLifecycle.BLOCKED: (DraftAction.DISCARD,),
    DraftLifecycle.SUBMITTING: (),
    DraftLifecycle.CONFIRMED: (DraftAction.VIEW_ORDER,),
    DraftLifecycle.REJECTED_BY_ERP: (),
    DraftLifecycle.NOT_APPLIED: (DraftAction.APPROVE, DraftAction.REJECT),
    DraftLifecycle.UNKNOWN: (DraftAction.RECHECK,),
    DraftLifecycle.REJECTED_BY_USER: (),
}

E8_KINDS = ("NOT_DELIVERED", "PRE_HANDLER_REFUSAL", "REJECTED", "SHORT", "ACCEPTED", "UNCERTAIN")

ORDER_NO_RE = re.compile(r"^SO-\d{6}$")
DRAFT_ID_RE = re.compile(r"^LB-[0-9A-F]{8}$")


# --------------------------------------------------------------------------- adapter-level records


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class RawStockRow(Frozen):
    """One E5 data row, values untrimmed exactly as transmitted (empty field = "")."""

    item: str
    wh: str
    on_hand: str
    allocated: str


class E8Observation(Frozen):
    """The single observation of one E8 request handed to the transport."""

    kind: str
    http_status: int | None = None
    rc: int | None = None
    order_no: str | None = None
    total: str | None = None
    msg: str | None = None
    short_item: str | None = None
    short_available: int | None = None
    detail: str = ""
    body_excerpt: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in E8_KINDS:
            raise ValueError(f"unknown E8 observation kind {value!r}")
        return value


# --------------------------------------------------------------------------- entities


class DataIssue(Frozen):
    code: IssueCode
    field: str
    raw_value: str | None
    message: str


class Customer(Frozen):
    account_id: str = Field(pattern=r"^\d{5}$")
    name: str
    contact_name: str | None
    email: str | None
    phone: str | None
    credit_hold: bool
    customer_since: date | None
    city: str | None
    country: str | None
    issues: list[DataIssue] = Field(default_factory=list)


class OrderSummary(Frozen):
    order_no: str = Field(pattern=r"^SO-\d{6}$")
    account_id: str
    order_date: date | None
    status: OrderStatus
    raw_status: str
    total: Decimal | None
    customer_reference: str | None
    issues: list[DataIssue] = Field(default_factory=list)

    @property
    def is_outstanding(self) -> bool:
        return self.status in OUTSTANDING_STATUSES


class OrderLine(Frozen):
    line_no: int
    sku: str
    quantity: int = Field(ge=1)
    unit_price: Decimal | None
    line_total: Decimal | None


class Order(OrderSummary):
    lines: list[OrderLine]
    lines_total: Decimal | None


class Product(Frozen):
    sku: str
    record_ids: list[int]
    record_descriptions: list[str]
    name: str
    category: str
    unit_price: Decimal | None
    unit_of_measure: str | None
    legacy_available_flag: bool | None
    discontinued: bool
    last_updated: date | None
    issues: list[DataIssue] = Field(default_factory=list)

    def has_issue(self, code: IssueCode) -> bool:
        return any(issue.code == code for issue in self.issues)


class WarehouseStock(Frozen):
    warehouse: str
    raw_on_hand: str
    raw_allocated: str
    on_hand: int | None
    allocated: int | None
    available: int | None


class AvailabilityReport(Frozen):
    sku: str
    product_name: str | None
    status: StockStatus
    available_quantity: int | None
    known_minimum: int | None
    warehouses: list[WarehouseStock]
    legacy_available_flag: bool | None
    issues: list[DataIssue] = Field(default_factory=list)
    explanation: str

    @model_validator(mode="after")
    def _undetermined(self) -> AvailabilityReport:
        if (self.available_quantity is None) != (self.status == StockStatus.UNDETERMINED):
            raise ValueError("available_quantity is None iff status is UNDETERMINED")
        return self


class DraftLine(Frozen):
    sku: str
    product_name: str | None
    quantity: int = Field(ge=1, le=999)
    unit_price: Decimal | None
    line_total: Decimal | None
    availability: StockStatus | None
    available_quantity: int | None
    blocking_reasons: list[str] = Field(default_factory=list)


class Approval(Frozen):
    draft_id: str
    attempt_no: int = Field(ge=1)
    approved_at: datetime


class SubmissionResult(Frozen):
    draft_id: str
    attempt_no: int
    kind: ResultKind
    outcome: SubmissionOutcome
    evidence: OutcomeEvidence
    evidence_detail: str
    e8_requests: int
    order_no: str | None = None
    legacy_code: str | None = None
    erp_booked_total: Decimal | None = None
    message: str
    order: Order | None = None
    recorded_at: datetime

    @model_validator(mode="after")
    def _consistency(self) -> SubmissionResult:
        if self.evidence not in EVIDENCE_FOR_OUTCOME[self.outcome]:
            raise ValueError(f"evidence {self.evidence} is not valid for outcome {self.outcome}")
        if self.kind == ResultKind.SUBMIT and self.e8_requests not in (0, 1):
            raise ValueError("a SUBMIT result records 0 or 1 E8 requests")
        if self.kind == ResultKind.RECHECK and self.e8_requests != 0:
            raise ValueError("a RECHECK result records 0 E8 requests")
        if self.outcome == SubmissionOutcome.CONFIRMED and not self.order_no:
            raise ValueError("CONFIRMED requires order_no")
        if self.order is not None and not (
            self.outcome == SubmissionOutcome.CONFIRMED
            and self.evidence in (OutcomeEvidence.READ_BACK, OutcomeEvidence.RECONCILED)
        ):
            raise ValueError("an attached order requires CONFIRMED with READ_BACK or RECONCILED")
        if not self.evidence_detail.strip():
            raise ValueError("evidence_detail must be non-empty")
        return self


class SubmissionAttempt(BaseModel):
    """Mutable; mutated only under the DraftStore lock."""

    attempt_no: int
    approved_at: datetime
    started_at: datetime
    e8_requests: int = 0
    e8_observation_kind: str | None = None
    acknowledged_order_no: str | None = None
    result: SubmissionResult | None = None


class OrderDraft(BaseModel):
    """Mutable; mutated only under the DraftStore lock."""

    draft_id: str = Field(pattern=r"^LB-[0-9A-F]{8}$")
    account_id: str
    customer_name: str | None
    lines: list[DraftLine]
    estimated_total: Decimal | None
    eligible: bool
    blocking_reasons: list[str]
    lifecycle: DraftLifecycle
    created_at: datetime
    attempts: list[SubmissionAttempt] = Field(default_factory=list)
    history: list[SubmissionResult] = Field(default_factory=list)

    @property
    def submission(self) -> SubmissionResult | None:
        return self.history[-1] if self.history else None

    @property
    def status(self) -> str:
        """R2 public field: business eligibility at preparation (never changes)."""
        return DraftLifecycle.READY.value if self.eligible else DraftLifecycle.BLOCKED.value

    @property
    def next_attempt_no(self) -> int:
        return len(self.attempts) + 1


class CustomerOverview(Frozen):
    customer: Customer
    orders: list[OrderSummary]
    outstanding: list[OrderSummary]

    @model_validator(mode="after")
    def _subset(self) -> CustomerOverview:
        numbers = {o.order_no for o in self.orders}
        if any(o.order_no not in numbers for o in self.outstanding):
            raise ValueError("outstanding must be a subset of orders")
        return self


class OverviewMetrics(Frozen):
    customer_count: int
    credit_hold_count: int
    order_count: int
    outstanding_order_count: int
    outstanding_order_value: Decimal
    needs_review_order_count: int
    product_count: int
    stock_status_counts: dict[StockStatus, int]
    issue_counts: dict[IssueCode, int]
    outstanding_orders: list[OrderSummary]


def json_safe(value: Any) -> Any:
    """Helper for rendering arbitrary results in JSON mode."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k.value if isinstance(k, Enum) else k): json_safe(v) for k, v in value.items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
