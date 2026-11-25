"""Port <-> v1 bridge mapping for HALDEN ("reads ... -> canonical models via mapping.py")."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from lbf.adapters.halden.v1bridge.errors import (
    BridgeError,
    LegacyAuthError,
    LegacyMalformedResponse,
    LegacyRateLimited,
    LegacyUnavailable,
)
from lbf.adapters.halden.v1bridge.normalize import build_customer, normalize_company_name, parse_flag
from lbf.domain.enums import CreditHold
from lbf.domain.errors import ErrorCode, IntegrationError
from lbf.domain.models import BackendAccount, DataIssue, ItemMapping, NativeRef


def item_mappings(rows: Iterable[ItemMapping]) -> dict[str, ItemMapping]:
    """The HALDEN rows of the curated item crosswalk, keyed by native item (adapters stay persistence-free)."""
    return {m.native_item: m for m in rows if m.backend_id == "halden"}


def to_integration_error(exc: BridgeError) -> IntegrationError:
    if isinstance(exc, LegacyAuthError):
        return IntegrationError(ErrorCode.BACKEND_AUTH, exc.message)
    if isinstance(exc, LegacyRateLimited):
        return IntegrationError(ErrorCode.BACKEND_THROTTLED, exc.message, retryable_read=True)
    if isinstance(exc, LegacyMalformedResponse):
        return IntegrationError(ErrorCode.BACKEND_MALFORMED, exc.message)
    if isinstance(exc, LegacyUnavailable):
        return IntegrationError(ErrorCode.BACKEND_UNAVAILABLE, exc.message, retryable_read=True)
    return IntegrationError(ErrorCode.BACKEND_UNAVAILABLE, getattr(exc, "message", "HALDEN read failed"))


def credit_hold(raw: Any) -> CreditHold:
    """v1 ``parse_flag``: J/Y/1/true -> HOLD; N/nein/0 -> CLEAR; anything else (incl. empty) -> UNKNOWN."""
    flag = parse_flag(raw)
    if flag is True:
        return CreditHold.HOLD
    if flag is False:
        return CreditHold.CLEAR
    return CreditHold.UNKNOWN


def account_from_row(row: dict[str, Any]) -> BackendAccount:
    customer = build_customer(row)
    return BackendAccount(
        ref=NativeRef(backend_id="halden", native_id=customer.account_id),
        name_raw=str(row.get("nm") or ""),
        name_display=normalize_company_name(row.get("nm") or ""),
        credit_hold=credit_hold(row.get("hold")),
        contact_name=customer.contact_name,
        email=customer.email,
        phone=customer.phone,
        city=customer.city,
        country_code=customer.country,
        customer_since=customer.customer_since,
        issues=[DataIssue(code=i.code.value, field=i.field, raw_value=i.raw_value, message=i.message)
                for i in customer.issues],
        untrusted_text_fields=[],
    )
