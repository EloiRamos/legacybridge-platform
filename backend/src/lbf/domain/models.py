"""Canonical models of contract ``lbf-canonical/1.0``.

Pydantic v2, frozen, ``extra="forbid"``. Money is ``Decimal`` quantized to 0.01 and serialized as a
string. Timestamps are timezone-aware UTC. Framework-neutral: no FastAPI/SQLAlchemy/httpx/openai.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from lbf.domain.canonical import content_hash
from lbf.domain.enums import (
    AvailabilityStatus,
    BackendId,
    CreditHold,
    DraftOrigin,
    EvidenceClass,
    OperationState,
    Outcome,
    PolicyResult,
    ReconciliationVerdict,
    TransportObservationKind,
)
from lbf.domain.evidence import NOT_APPLIED_EVIDENCE, require_allowed

CENT = Decimal("0.01")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


Money = Annotated[Decimal, PlainSerializer(lambda v: format(quantize_money(v), "f"), return_type=str)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- identity


class NativeRef(Frozen):
    backend_id: BackendId
    native_id: str = Field(min_length=1, max_length=40)


class DataIssue(Frozen):
    code: str
    field: str
    raw_value: str | None = None
    message: str


# --------------------------------------------------------------------------- customers


class BackendAccount(Frozen):
    ref: NativeRef
    name_raw: str
    name_display: str
    credit_hold: CreditHold
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    country_code: str | None = None
    customer_since: date | None = None
    issues: list[DataIssue] = Field(default_factory=list)
    untrusted_text_fields: list[str] = Field(default_factory=list)
    notes: str | None = None  # free text (SKARVIK NOTE); untrusted


class Party(Frozen):
    party_id: UUID
    display_name: str
    accounts: list[BackendAccount] = Field(min_length=1)
    crosswalk_basis: Literal["CURATED_MASTER_DATA"] = "CURATED_MASTER_DATA"
    conflicts: list[DataIssue] = Field(default_factory=list)


class CustomerSearchResult(Frozen):
    accounts: list[BackendAccount]
    parties: list[Party]
    ambiguity_groups: list[list[NativeRef]]


# --------------------------------------------------------------------------- inventory


class CanonicalItem(Frozen):
    canonical_sku: str
    name: str
    category: str
    base_uom: str


class ItemMapping(Frozen):
    canonical_sku: str
    backend_id: BackendId
    native_item: str
    native_uom: str
    factor_num: int = Field(gt=0)
    factor_den: int = Field(gt=0)


class WarehouseQty(Frozen):
    warehouse: str
    raw_on_hand: str
    raw_allocated: str | None = None
    on_hand: int | None = None
    allocated: int | None = None
    available_native: int | None = None
    available_base: int | None = None


class Availability(Frozen):
    canonical_sku: str | None
    backend_id: BackendId
    native_item: str
    status: AvailabilityStatus
    available_base: int | None
    as_of: datetime
    freshness: Literal["LIVE", "SNAPSHOT"]
    snapshot_age_seconds: int | None = None
    warehouses: list[WarehouseQty] = Field(default_factory=list)
    issues: list[DataIssue] = Field(default_factory=list)
    explanation: str
    legacy_available_flag: bool | None = None

    @field_validator("as_of")
    @classmethod
    def _as_of_utc(cls, v: datetime) -> datetime:
        return _utc(v)

    @model_validator(mode="after")
    def _quantity_rule(self) -> Availability:
        undetermined = self.status in (AvailabilityStatus.UNDETERMINED, AvailabilityStatus.STALE)
        if undetermined != (self.available_base is None):
            raise ValueError("available_base must be None iff status is UNDETERMINED or STALE")
        return self


# --------------------------------------------------------------------------- drafts


class DraftContentLine(Frozen):
    """A draft line as hashed business content. Availability, blocking reasons and warnings are
    validation data and live in :class:`ValidationSnapshot`, not here."""

    line_no: int = Field(ge=1, le=99)
    canonical_sku: str | None = None
    native_item: str = Field(min_length=1, max_length=16)
    quantity_base: int = Field(ge=1, le=999)
    quantity_native: int = Field(ge=1, le=99999)
    native_uom: str = Field(min_length=1, max_length=3)
    unit_price_estimate: Money | None = None
    line_estimate: Money | None = None


class DraftContent(Frozen):
    backend_id: BackendId
    native_customer_id: str = Field(min_length=1, max_length=16)
    lines: list[DraftContentLine] = Field(min_length=1, max_length=50)
    customer_po: str | None = Field(default=None, max_length=20)
    requested_date: date | None = None
    delivery_note: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _line_numbers(self) -> DraftContent:
        numbers = [line.line_no for line in self.lines]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("line numbers must be 1..n in order")
        return self

    def estimated_total(self) -> Decimal | None:
        totals = [line.line_estimate for line in self.lines]
        if any(t is None for t in totals):
            return None
        return quantize_money(sum((t for t in totals if t is not None), Decimal("0")))


class DraftVersion(Frozen):
    operation_id: UUID
    version: int = Field(ge=1)
    content: DraftContent
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: UUID
    origin: DraftOrigin
    created_at: datetime

    @model_validator(mode="after")
    def _hash_matches(self) -> DraftVersion:
        expected = content_hash(self.operation_id, self.version, self.content.model_dump(mode="python"))
        if expected != self.content_hash:
            raise ValueError("content_hash does not match content")
        return self

    @classmethod
    def build(
        cls,
        *,
        operation_id: UUID,
        version: int,
        content: DraftContent,
        created_by: UUID,
        origin: DraftOrigin,
        created_at: datetime,
    ) -> DraftVersion:
        return cls(
            operation_id=operation_id,
            version=version,
            content=content,
            content_hash=content_hash(operation_id, version, content.model_dump(mode="python")),
            created_by=created_by,
            origin=origin,
            created_at=created_at,
        )


class PolicyCheck(Frozen):
    check: str
    result: PolicyResult
    detail: str = ""
    line_no: int | None = None


class LineValidation(Frozen):
    line_no: int
    availability_status: AvailabilityStatus
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ValidationSnapshot(Frozen):
    operation_id: UUID
    version: int
    validated_at: datetime
    checks: list[PolicyCheck]
    availability: list[Availability]
    lines: list[LineValidation] = Field(default_factory=list)
    estimated_total: Money | None
    blocking: bool


# --------------------------------------------------------------------------- operation


class Operation(Frozen):
    operation_id: UUID
    correlation_value: str = Field(pattern=r"^LB[0-9A-HJKMNP-TV-Z]{10}$")
    backend_id: BackendId
    native_customer_id: str
    state: OperationState
    current_version: int = Field(ge=1)
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    row_version: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    last_outcome: Outcome | None = None
    last_evidence: EvidenceClass | None = None
    escalation_id: UUID | None = None


# --------------------------------------------------------------------------- submission


class SubmissionLine(Frozen):
    native_item: str
    quantity_native: int = Field(ge=1)


class SubmissionRequest(Frozen):
    operation_id: UUID
    attempt_no: int = Field(ge=1, le=99)
    transmission_no: int = Field(ge=1)
    authorization_id: UUID
    backend_id: BackendId
    native_customer_id: str
    lines: list[SubmissionLine] = Field(min_length=1)
    correlation_value: str
    message_id: str | None = None
    customer_po: str | None = None
    requested_date: date | None = None
    delivery_note: str | None = None


class RenderedRequest(Frozen):
    request: SubmissionRequest
    payload: bytes
    payload_sha256: str
    transport_descriptor: str


# --------------------------------------------------------------------------- observation


_KEEP = {"\t", "\n", "\r"}


def storable_text(value: str | None) -> str | None:
    """Untrusted backend text made storable and visible: C0 controls (except tab/newline/CR) and DEL become ``\\xNN``.
    PostgreSQL ``text``/``jsonb`` reject U+0000; a corrupted response must still be recorded as an observation (R05).
    The exact bytes remain bound by the accompanying SHA-256."""
    if value is None:
        return None
    return "".join(f"\\x{ord(ch):02x}" if (ord(ch) < 32 and ch not in _KEEP) or ord(ch) == 127 else ch
                   for ch in value)


class TransportObservation(Frozen):
    kind: TransportObservationKind
    backend_order_ref: str | None = None
    backend_code: str | None = None
    http_status: int | None = None
    detail: str = ""
    response_sha256: str | None = None
    response_excerpt: str | None = Field(default=None, max_length=512)
    observed_at: datetime
    payload_sha256_on_wire: str | None = None

    @field_validator("response_excerpt", mode="before")
    @classmethod
    def _cap_excerpt(cls, v: Any) -> Any:
        if isinstance(v, str):
            return (storable_text(v) or "")[:512]
        return v

    @field_validator("backend_order_ref", "backend_code", "detail", mode="before")
    @classmethod
    def _storable(cls, v: Any) -> Any:
        return storable_text(v) if isinstance(v, str) else v


# --------------------------------------------------------------------------- reconciliation


class ReconciliationQuery(Frozen):
    operation_id: UUID
    backend_id: BackendId
    native_customer_id: str
    correlation_value: str
    message_id: str | None = None
    expected_lines: list[SubmissionLine]
    anchor_order_ref: str | None = None
    first_handoff_at: datetime
    attempt_no: int


class SourceRead(Frozen):
    source: str
    read_at: datetime
    snapshot_at: datetime | None = None
    response_sha256: str
    summary: str

    @field_validator("summary", mode="before")
    @classmethod
    def _storable(cls, v: Any) -> Any:
        return storable_text(v) if isinstance(v, str) else v


class ReconciliationResult(Frozen):
    verdict: ReconciliationVerdict
    matched_order_ref: str | None = None
    candidates: list[str] = Field(default_factory=list)
    source_reads: list[SourceRead] = Field(default_factory=list)
    absence_proven: bool = False
    absence_evidence: EvidenceClass | None = None
    detail: str = ""

    @field_validator("detail", "matched_order_ref", mode="before")
    @classmethod
    def _storable(cls, v: Any) -> Any:
        return storable_text(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _rules(self) -> ReconciliationResult:
        # Rule R-1: absence only from an evidence class of the NOT_APPLIED set.
        if self.absence_proven != (self.absence_evidence is not None):
            raise ValueError("absence_proven requires absence_evidence and vice versa")
        if self.absence_evidence is not None and self.absence_evidence not in NOT_APPLIED_EVIDENCE:
            raise ValueError("absence_evidence must belong to the NOT_APPLIED evidence set (rule R-1)")
        if self.verdict is ReconciliationVerdict.MATCHED and not self.matched_order_ref:
            raise ValueError("MATCHED requires matched_order_ref")
        if self.verdict is not ReconciliationVerdict.MATCHED and self.matched_order_ref:
            raise ValueError("matched_order_ref only with MATCHED")
        if self.absence_proven and self.verdict is ReconciliationVerdict.MATCHED:
            raise ValueError("a matched order cannot prove absence")
        return self


# --------------------------------------------------------------------------- recorded outcome


class RecordedOutcome(Frozen):
    outcome: Outcome
    evidence: EvidenceClass
    backend_order_ref: str | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _consistency(self) -> RecordedOutcome:
        require_allowed(self.outcome, self.evidence)
        if self.outcome is Outcome.CONFIRMED and not self.backend_order_ref:
            raise ValueError("CONFIRMED requires the backend order reference")
        return self
