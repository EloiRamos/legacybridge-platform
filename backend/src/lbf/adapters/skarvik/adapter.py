"""``SkarvikAdapter`` — SKARVIK OES 4 behind ``LegacyBackendPort``.

Reads come from snapshot extracts; writes are file drops on the exchange share. The adapter never reads simulator
internals or the control plane (B-5).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from lbf.adapters.skarvik import acks as ack_files
from lbf.adapters.skarvik import exchange, extracts
from lbf.adapters.skarvik import formats as fmt
from lbf.adapters.skarvik import reconcile as skarvik_reconcile
from lbf.domain.enums import TransportObservationKind
from lbf.domain.errors import DomainError, ErrorCode, IntegrationError
from lbf.domain.inventory import snapshot_age_seconds
from lbf.domain.models import (
    BackendAccount,
    CanonicalItem,
    ItemMapping,
    ReconciliationQuery,
    ReconciliationResult,
    RenderedRequest,
    SubmissionRequest,
    TransportObservation,
)
from lbf.ports.backend import (
    AckObservation,
    BackendHealth,
    BackendOrder,
    BackendOrderLine,
    BackendOrderSummary,
    CatalogueEntry,
    CatalogueSnapshot,
    PreflightResult,
    StockSnapshot,
    WithdrawResult,
)
from lbf.ports.capabilities import BackendCapabilities, skarvik_capabilities

MAX_SEARCH_RESULTS = 50


class SkarvikAdapter:
    def __init__(
        self,
        exchange_dir: Path | str,
        *,
        mappings: Iterable[ItemMapping],
        capabilities: BackendCapabilities | None = None,
        clock: Callable[[], datetime] | None = None,
        before_publish: Callable[[RenderedRequest], None] | None = None,
    ) -> None:
        self.share = exchange.Share(Path(exchange_dir))
        self._caps = capabilities or skarvik_capabilities()
        self._mappings = {m.native_item: m for m in mappings if m.backend_id == "skarvik"}
        self._clock = clock or (lambda: datetime.now(UTC))
        self.before_publish = before_publish or (lambda rendered: None)

    def capabilities(self) -> BackendCapabilities:
        return self._caps

    # ------------------------------------------------------------------ reads (snapshots)

    def health(self) -> BackendHealth:
        if not self.share.writable():
            return BackendHealth(backend_id="skarvik", reachable=False, detail="exchange share not writable")
        stock = extracts.newest(self.share, "STOCK", required=False)
        if stock is None:
            return BackendHealth(backend_id="skarvik", reachable=False, detail="no complete STOCK extract")
        age = snapshot_age_seconds(stock.snapshot_at, self._clock())
        return BackendHealth(backend_id="skarvik", reachable=True, detail=f"newest snapshot {stock.file_name}",
                             last_successful_read_at=stock.snapshot_at, snapshot_age_seconds=age)

    def _customers(self) -> list[BackendAccount]:
        extract = extracts.newest(self.share, "CUST")
        assert extract is not None
        return extracts.customers(extract)

    def search_customers(self, query: str) -> list[BackendAccount]:
        q = (query or "").strip().casefold()
        if not q:
            return []
        hits = [a for a in self._customers() if q in a.ref.native_id.casefold() or q in a.name_raw.casefold()]
        return hits[:MAX_SEARCH_RESULTS]

    def get_customer(self, native_id: str) -> BackendAccount:
        for account in self._customers():
            if account.ref.native_id == native_id:
                return account
        raise DomainError(ErrorCode.NOT_FOUND, f"No SKARVIK customer {native_id}")

    def _orders(self) -> list[extracts.ExtractOrder]:
        extract = extracts.newest(self.share, "ORDERS")
        assert extract is not None
        return extracts.orders(extract)

    @staticmethod
    def _summary(o: extracts.ExtractOrder) -> BackendOrderSummary:
        return BackendOrderSummary(backend_id="skarvik", order_ref=o.orderno, native_customer_id=o.custno,
                                   reference=o.ordref or None, message_id=o.msgid or None, status=o.status,
                                   order_date=o.orddate or None)

    def list_orders(self, native_customer_id: str) -> list[BackendOrderSummary]:
        return [self._summary(o) for o in self._orders() if o.custno == native_customer_id]

    def get_order(self, backend_order_ref: str) -> BackendOrder:
        for o in self._orders():
            if o.orderno == backend_order_ref:
                s = self._summary(o)
                return BackendOrder(**s.model_dump(), lines=[
                    BackendOrderLine(line_no=ln.lineno, native_item=ln.itemno, quantity_native=ln.qty)
                    for ln in o.lines])
        raise DomainError(ErrorCode.NOT_FOUND, f"SKARVIK order {backend_order_ref} not in the latest snapshot")

    def _items(self) -> tuple[datetime, dict[str, extracts.ItemRow]]:
        extract = extracts.newest(self.share, "ITEMS")
        assert extract is not None
        return extract.snapshot_at, extracts.items(extract)

    def catalogue(self) -> CatalogueSnapshot:
        as_of, rows = self._items()
        entries = []
        for itemno, row in sorted(rows.items()):
            mapping = self._mappings.get(itemno)
            entries.append(CatalogueEntry(
                native_item=itemno, name=row.descr, native_uom=row.uom, unit_price=row.price,
                discontinued=row.disc, pack_size=row.pack,
                canonical=CanonicalItem(canonical_sku=mapping.canonical_sku, name=row.descr, category="",
                                        base_uom=mapping.native_uom) if mapping else None,
                issues=[] if row.price is not None else ["MISSING_PRICE"]))
        return CatalogueSnapshot(backend_id="skarvik", as_of=as_of, entries=entries)

    def stock(self) -> StockSnapshot:
        stock = extracts.newest(self.share, "STOCK")
        assert stock is not None
        _, rows = self._items()
        availability = extracts.availability(stock, rows, self._mappings, now=self._clock(),
                                             max_age_seconds=self._caps.inventory_max_age_seconds or 900)
        return StockSnapshot(backend_id="skarvik", as_of=stock.snapshot_at, freshness="SNAPSHOT",
                             availability=availability)

    # ------------------------------------------------------------------ write path

    def preflight(self, request: SubmissionRequest) -> PreflightResult:
        if not self.share.writable():
            return PreflightResult(ok=False, detail="SKARVIK exchange share is not writable")
        try:
            cust_extract = extracts.newest(self.share, "CUST")
        except IntegrationError as exc:
            return PreflightResult(ok=False, detail=exc.message)
        assert cust_extract is not None
        warnings = []
        age = snapshot_age_seconds(cust_extract.snapshot_at, self._clock())
        if age > (self._caps.inventory_max_age_seconds or 900):
            warnings.append(f"customer snapshot is {age} s old")
        match = [a for a in extracts.customers(cust_extract) if a.ref.native_id == request.native_customer_id]
        if not match:
            return PreflightResult(ok=False, detail=f"customer {request.native_customer_id} not in the latest extract")
        if match[0].credit_hold.value == "HOLD":
            return PreflightResult(ok=False, detail=f"customer {request.native_customer_id} is blocked (SPARR=J)")
        return PreflightResult(ok=True, detail="exchange writable; customer present", warnings=warnings)

    def _uoms(self) -> dict[str, str]:
        return {item: m.native_uom for item, m in self._mappings.items()}

    def render(self, request: SubmissionRequest) -> RenderedRequest:
        return fmt.rendered(request, self._uoms(), str(self.share.root))

    def handoff(self, rendered: RenderedRequest) -> TransportObservation:
        try:
            return exchange.deliver(self.share, rendered, clock=self._clock, before_publish=self.before_publish)
        except Exception as exc:
            return TransportObservation(kind=TransportObservationKind.UNCERTAIN, observed_at=self._clock(),
                                        detail=f"adapter exception during delivery ({exc.__class__.__name__})")

    def poll_acknowledgements(self) -> list[AckObservation]:
        return ack_files.poll(self.share)

    def archive_acknowledgement(self, ack: AckObservation) -> None:
        ack_files.archive(self.share, ack)

    def reconcile(self, query: ReconciliationQuery) -> ReconciliationResult:
        return skarvik_reconcile.reconcile(self.share, query, self._caps, self._clock)

    def withdraw_unclaimed(self, message_id: str) -> WithdrawResult:
        return exchange.withdraw(self.share, message_id)

    def redeliver(self, original: RenderedRequest) -> TransportObservation:
        """Byte-identical stored payload under the same message id (window enforced by the workflow)."""
        try:
            return exchange.deliver(self.share, original, clock=self._clock, before_publish=self.before_publish,
                                    redelivery=True)
        except Exception as exc:
            return TransportObservation(kind=TransportObservationKind.UNCERTAIN, observed_at=self._clock(),
                                        detail=f"adapter exception during redelivery ({exc.__class__.__name__})")
