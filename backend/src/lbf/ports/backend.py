"""The single legacy backend port and its DTOs.

Workflow code depends only on this protocol and on :class:`BackendCapabilities`; it never branches
on backend identity.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lbf.domain.enums import BackendId, EvidenceClass
from lbf.domain.models import (
    Availability,
    BackendAccount,
    CanonicalItem,
    ReconciliationQuery,
    ReconciliationResult,
    RenderedRequest,
    SubmissionRequest,
    TransportObservation,
    storable_text,
)
from lbf.ports.capabilities import BackendCapabilities


class _Dto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BackendHealth(_Dto):
    backend_id: BackendId
    reachable: bool
    detail: str
    last_successful_read_at: datetime | None = None
    snapshot_age_seconds: int | None = None


class BackendOrderLine(_Dto):
    line_no: int
    native_item: str
    quantity_native: int | None
    raw_quantity: str | None = None


class BackendOrderSummary(_Dto):
    backend_id: BackendId
    order_ref: str
    native_customer_id: str
    reference: str | None  # correlation field value as stored by the backend (raw, may be None)
    message_id: str | None = None
    status: str
    order_date: str | None = None
    total: Decimal | None = None


class BackendOrder(BackendOrderSummary):
    lines: list[BackendOrderLine]


class CatalogueEntry(_Dto):
    native_item: str
    name: str
    native_uom: str
    unit_price: Decimal | None
    discontinued: bool
    pack_size: int | None = None
    canonical: CanonicalItem | None = None
    issues: list[str] = Field(default_factory=list)


class CatalogueSnapshot(_Dto):
    backend_id: BackendId
    as_of: datetime
    entries: list[CatalogueEntry]


class StockSnapshot(_Dto):
    backend_id: BackendId
    as_of: datetime
    freshness: Literal["LIVE", "SNAPSHOT"]
    availability: list[Availability]


class PreflightResult(_Dto):
    ok: bool
    detail: str
    warnings: list[str] = Field(default_factory=list)


class AckObservation(_Dto):
    message_id: str | None
    seq: int | None
    status: Literal["ACCEPTED", "REJECTED", "DUPLICATE", "MALFORMED"]
    order_ref: str | None
    code: str | None
    ts_utc: datetime | None
    file_sha256: str
    file_name: str
    detail: str = ""

    @field_validator("message_id", "order_ref", "code", "detail", mode="before")
    @classmethod
    def _storable(cls, v: Any) -> Any:  # untrusted file content: storable in text/jsonb (see storable_text)
        return storable_text(v) if isinstance(v, str) else v


class WithdrawResult(_Dto):
    withdrawn: bool
    absence_evidence: EvidenceClass | None
    detail: str


@runtime_checkable
class LegacyBackendPort(Protocol):
    def capabilities(self) -> BackendCapabilities: ...

    def health(self) -> BackendHealth: ...

    def search_customers(self, query: str) -> list[BackendAccount]: ...

    def get_customer(self, native_id: str) -> BackendAccount: ...

    def list_orders(self, native_customer_id: str) -> list[BackendOrderSummary]: ...

    def get_order(self, backend_order_ref: str) -> BackendOrder: ...

    def catalogue(self) -> CatalogueSnapshot: ...

    def stock(self) -> StockSnapshot: ...

    def preflight(self, request: SubmissionRequest) -> PreflightResult: ...

    def render(self, request: SubmissionRequest) -> RenderedRequest: ...

    def handoff(self, rendered: RenderedRequest) -> TransportObservation:
        """Exactly one transport handoff; never retries; never raises."""
        ...

    def poll_acknowledgements(self) -> list[AckObservation]: ...

    def archive_acknowledgement(self, ack: AckObservation) -> None:
        """Move a recorded acknowledgement to the archive (called only after the DB record commits)."""
        ...

    def reconcile(self, query: ReconciliationQuery) -> ReconciliationResult: ...

    def withdraw_unclaimed(self, message_id: str) -> WithdrawResult: ...

    def redeliver(self, original: RenderedRequest) -> TransportObservation: ...
