"""Draft intent normalization and validation (T01–T04; T31).

All backend reads happen here, **outside** any database transaction; the commands then persist the result in
one short transaction under the operation row lock.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lbf.auth.policy import ApprovalFacts, LineFacts, evaluate_checks, requirements
from lbf.domain.enums import AvailabilityStatus, CreditHold, PolicyResult
from lbf.domain.errors import DomainError, ErrorCode, IntegrationError
from lbf.domain.models import (
    Availability,
    DraftContent,
    DraftContentLine,
    ItemMapping,
    LineValidation,
    PolicyCheck,
    ValidationSnapshot,
    quantize_money,
)
from lbf.ports.backend import LegacyBackendPort
from lbf.ports.capabilities import CANONICAL_QUANTITY_RANGE, BackendCapabilities


class LineRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_sku: str | None = Field(default=None, max_length=16)
    native_item: str | None = Field(default=None, max_length=16)
    quantity: int = Field(ge=CANONICAL_QUANTITY_RANGE[0], le=CANONICAL_QUANTITY_RANGE[1])  # base units

    @model_validator(mode="after")
    def _one_item(self) -> LineRequest:
        if not (self.canonical_sku or self.native_item):
            raise ValueError("a line needs canonical_sku or native_item")
        return self


class DraftRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(max_length=16)
    native_customer_id: str = Field(min_length=1, max_length=16)
    lines: list[LineRequest] = Field(min_length=1, max_length=50)
    customer_po: str | None = Field(default=None, max_length=20)
    requested_date: date | None = None
    delivery_note: str | None = Field(default=None, max_length=60)


class DraftPatch(BaseModel):
    """A partial revision. A field that is not set keeps its value; a header field set to ``None`` is cleared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[LineRequest] | None = Field(default=None, min_length=1, max_length=50)
    customer_po: str | None = Field(default=None, max_length=20)
    requested_date: date | None = None
    delivery_note: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _something(self) -> DraftPatch:
        if not self.model_fields_set:
            raise ValueError("an empty revision")
        if "lines" in self.model_fields_set and self.lines is None:
            raise ValueError("the lines of a draft cannot be cleared")
        return self

    def apply(self, base: DraftContent) -> DraftRequest:
        kept = {name: getattr(self if name in self.model_fields_set else base, name)
                for name in ("customer_po", "requested_date", "delivery_note")}
        lines = self.lines if self.lines is not None else [
            LineRequest(canonical_sku=ln.canonical_sku, native_item=ln.native_item, quantity=ln.quantity_base)
            for ln in base.lines]
        return DraftRequest(backend_id=base.backend_id, native_customer_id=base.native_customer_id, lines=lines, **kept)


def _mapping_for(line: LineRequest, mappings: dict[str, ItemMapping]) -> ItemMapping:
    if line.native_item:
        m = mappings.get(line.native_item.strip())
        if m is None:
            raise DomainError(ErrorCode.VALIDATION, f"item {line.native_item} has no crosswalk mapping")
        if line.canonical_sku and m.canonical_sku != line.canonical_sku:
            raise DomainError(ErrorCode.VALIDATION, f"item {line.native_item} is not {line.canonical_sku}")
        return m
    candidates = [m for m in mappings.values() if m.canonical_sku == line.canonical_sku]
    if len(candidates) != 1:
        what = "no" if not candidates else "an ambiguous"
        raise DomainError(ErrorCode.VALIDATION, f"{line.canonical_sku} has {what} item mapping in this backend")
    return candidates[0]


def build_content(req: DraftRequest, caps: BackendCapabilities, mappings: dict[str, ItemMapping],
                  prices: dict[str, Decimal | None]) -> DraftContent:
    """Normalize the requested intent into hashed draft content (base → native units, merged lines)."""
    if req.backend_id != caps.backend_id:
        raise DomainError(ErrorCode.VALIDATION, "backend mismatch")
    for name, value, supported in (("customer_po", req.customer_po, caps.supports_customer_po),
                                   ("requested_date", req.requested_date, caps.supports_requested_date),
                                   ("delivery_note", req.delivery_note, caps.supports_delivery_note)):
        if value not in (None, "") and not supported:
            raise DomainError(ErrorCode.VALIDATION, f"{name} is not supported by {caps.display_name}")
    merged: dict[str, tuple[ItemMapping, int]] = {}
    for line in req.lines:
        m = _mapping_for(line, mappings)
        previous = merged.get(m.native_item, (m, 0))[1]
        merged[m.native_item] = (m, previous + line.quantity)
    if len(merged) > caps.max_lines_per_order:
        raise DomainError(ErrorCode.VALIDATION, f"at most {caps.max_lines_per_order} lines")
    lines = []
    for n, (m, base) in enumerate(merged.values(), start=1):
        if base > CANONICAL_QUANTITY_RANGE[1]:
            raise DomainError(ErrorCode.VALIDATION, f"merged quantity of {m.native_item} exceeds 999")
        numerator = base * m.factor_den
        if numerator % m.factor_num:
            raise DomainError(ErrorCode.VALIDATION,
                              f"{base} base units of {m.canonical_sku} are not a whole number of {m.native_uom}")
        native = numerator // m.factor_num
        lo, hi = caps.quantity_range
        if not lo <= native <= hi:
            raise DomainError(ErrorCode.VALIDATION, f"quantity {native} outside {lo}..{hi}")
        price = prices.get(m.native_item)
        lines.append(DraftContentLine(
            line_no=n, canonical_sku=m.canonical_sku, native_item=m.native_item, quantity_base=base,
            quantity_native=native, native_uom=m.native_uom, unit_price_estimate=price,
            line_estimate=quantize_money(price * native) if price is not None else None))
    return DraftContent(backend_id=caps.backend_id, native_customer_id=req.native_customer_id, lines=lines,
                        customer_po=req.customer_po or None, requested_date=req.requested_date,
                        delivery_note=req.delivery_note or None)


def catalogue_prices(port: LegacyBackendPort) -> dict[str, Decimal | None]:
    return {e.native_item: e.unit_price for e in port.catalogue().entries}


def encoding_problems(content: DraftContent, caps: BackendCapabilities) -> list[str]:
    """Free-text fields must be representable in the backend's encoding (SKARVIK: ISO-8859-1, T03)."""
    problems = []
    for name in ("customer_po", "delivery_note"):
        value = getattr(content, name)
        if value is None:
            continue
        try:
            value.encode(caps.text_encoding)
        except UnicodeEncodeError:
            problems.append(f"{name} contains characters outside {caps.text_encoding}")
        if any(ch in value for ch in ";\r\n"):
            problems.append(f"{name} must not contain ';', CR or LF")
    return problems


def validate(*, operation_id: UUID, version: int, content: DraftContent, port: LegacyBackendPort,
             linked: Sequence[tuple[LegacyBackendPort, str]], now: datetime) -> ValidationSnapshot:
    """Live preflight reads (customer hold, linked holds, stock) → policy checks without overlap/age."""
    caps = port.capabilities()
    extra: list[PolicyCheck] = []
    try:
        target_hold = port.get_customer(content.native_customer_id).credit_hold
    except DomainError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            raise
        target_hold = CreditHold.UNKNOWN
        extra.append(PolicyCheck(check="customer_read", result=PolicyResult.WARNING, detail=exc.message))
    linked_holds = []
    for other, native in linked:
        try:
            linked_holds.append(other.get_customer(native).credit_hold)
        except DomainError as exc:
            extra.append(PolicyCheck(check="linked_account_read", result=PolicyResult.WARNING,
                                     detail=f"linked account {native}: {exc.message}"))
    try:
        snapshot = port.stock()
        by_item: dict[str, Availability] = {a.native_item: a for a in snapshot.availability}
    except IntegrationError as exc:
        by_item = {}
        extra.append(PolicyCheck(check="stock_read", result=PolicyResult.WARNING, detail=exc.message))
    line_facts, line_validation, availability = [], [], []
    for line in content.lines:
        a = by_item.get(line.native_item)
        status = a.status if a is not None else AvailabilityStatus.UNDETERMINED
        available = a.available_base if a is not None else None
        line_facts.append(LineFacts(line.line_no, line.quantity_base, status, available))
        if a is not None:
            availability.append(a)
        line_validation.append(LineValidation(line_no=line.line_no, availability_status=status))
    for problem in encoding_problems(content, caps):
        extra.append(PolicyCheck(check="encoding", result=PolicyResult.BLOCKING, detail=problem))
    facts = ApprovalFacts(target_hold=target_hold, linked_holds=linked_holds, lines=line_facts,
                          estimated_total=content.estimated_total(),
                          atomic_stock_allocation=caps.stock_allocation_on_accept == "ATOMIC_ALLOCATION")
    checks = evaluate_checks(facts, include_overlap=False) + extra
    blocking = requirements(checks).blocking
    by_line: dict[int, list[PolicyCheck]] = {}
    for c in checks:
        if c.line_no is not None:
            by_line.setdefault(c.line_no, []).append(c)
    line_validation = [
        lv.model_copy(update={
            "blocking_reasons": [c.detail for c in by_line.get(lv.line_no, []) if c.result is PolicyResult.BLOCKING],
            "warnings": [c.detail for c in by_line.get(lv.line_no, [])
                         if c.result in (PolicyResult.WARNING, PolicyResult.OVERRIDE_REQUIRED)],
        })
        for lv in line_validation
    ]
    return ValidationSnapshot(operation_id=operation_id, version=version, validated_at=now, checks=checks,
                              availability=availability, lines=line_validation,
                              estimated_total=content.estimated_total(), blocking=blocking)
