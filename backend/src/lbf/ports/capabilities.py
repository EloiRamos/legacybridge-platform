"""Backend capability declarations (normative values)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from lbf.domain.enums import BackendId


class BackendCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: BackendId
    display_name: str
    contract_version: str
    write_mode: Literal["SYNC_REQUEST_RESPONSE", "ASYNC_FILE_DROP"]
    dedup: Literal["NONE", "MESSAGE_ID_WINDOW"]
    dedup_window_seconds: int | None
    dedup_safety_margin_seconds: int | None
    correlation_field: str
    correlation_max_length: int
    correlation_unique_in_backend: bool
    supports_customer_po: bool
    supports_requested_date: bool
    supports_delivery_note: bool
    pre_processing_refusal_signals: bool
    can_withdraw_before_pickup: bool
    ack_deadline_seconds: int | None
    read_after_write: Literal["IMMEDIATE", "SNAPSHOT"]
    snapshot_max_lag_seconds: int | None
    inventory_freshness: Literal["LIVE", "SNAPSHOT"]
    inventory_max_age_seconds: int | None
    stock_allocation_on_accept: Literal["ATOMIC_ALLOCATION", "ACCEPT_WITH_BACKORDER"]
    text_encoding: str
    max_lines_per_order: int
    quantity_range: tuple[int, int]

    @property
    def can_prove_absence_after_handoff(self) -> bool:
        """Derived predicate (not a declared field): an asynchronous file-drop backend can prove that a
        handed-off message was never processed (withdrawal before pickup, never-delivered temp file);
        a synchronous request/response backend cannot (HALDEN ``absence_proven`` is always False)."""
        return self.write_mode == "ASYNC_FILE_DROP" and self.can_withdraw_before_pickup

    @property
    def is_async(self) -> bool:
        return self.write_mode == "ASYNC_FILE_DROP"

    @property
    def supports_redelivery(self) -> bool:
        return self.dedup == "MESSAGE_ID_WINDOW"


HALDEN_CAPABILITIES = BackendCapabilities(
    backend_id="halden",
    display_name="HALDEN ERP 3.1",
    contract_version="3.1",
    write_mode="SYNC_REQUEST_RESPONSE",
    dedup="NONE",
    dedup_window_seconds=None,
    dedup_safety_margin_seconds=None,
    correlation_field="ref",
    correlation_max_length=12,
    correlation_unique_in_backend=False,
    supports_customer_po=False,
    supports_requested_date=False,
    supports_delivery_note=False,
    pre_processing_refusal_signals=True,
    can_withdraw_before_pickup=False,
    ack_deadline_seconds=None,
    read_after_write="IMMEDIATE",
    snapshot_max_lag_seconds=None,
    inventory_freshness="LIVE",
    inventory_max_age_seconds=None,
    stock_allocation_on_accept="ATOMIC_ALLOCATION",
    text_encoding="utf-8",
    max_lines_per_order=20,
    quantity_range=(1, 999),
)


def skarvik_capabilities(*, test_profile: bool = False) -> BackendCapabilities:
    """SKARVIK OES 4. Test-profile values (dedup 120 s / margin 10 s, ack 20 s,
    snapshot lag 6 s, inventory max age 30 s)."""
    return BackendCapabilities(
        backend_id="skarvik",
        display_name="SKARVIK OES 4",
        contract_version="skarvik-exchange/1.0",
        write_mode="ASYNC_FILE_DROP",
        dedup="MESSAGE_ID_WINDOW",
        dedup_window_seconds=120 if test_profile else 259_200,
        dedup_safety_margin_seconds=10 if test_profile else 3_600,
        correlation_field="ORDREF",
        correlation_max_length=20,
        correlation_unique_in_backend=False,
        supports_customer_po=True,
        supports_requested_date=True,
        supports_delivery_note=True,
        pre_processing_refusal_signals=False,
        can_withdraw_before_pickup=True,
        ack_deadline_seconds=20 if test_profile else 300,
        read_after_write="SNAPSHOT",
        snapshot_max_lag_seconds=6 if test_profile else 60,
        inventory_freshness="SNAPSHOT",
        inventory_max_age_seconds=30 if test_profile else 900,
        stock_allocation_on_accept="ACCEPT_WITH_BACKORDER",
        text_encoding="iso-8859-1",
        max_lines_per_order=50,
        quantity_range=(1, 99_999),
    )


SKARVIK_CAPABILITIES = skarvik_capabilities()
CANONICAL_QUANTITY_RANGE = (1, 999)
