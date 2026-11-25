"""``HaldenAdapter`` — HALDEN ERP 3.1 behind ``LegacyBackendPort``.

One ``LegacyClient`` instance = one HALDEN ticket (ADR-F04). Reads use the v1 client and v1 normalizers
unchanged; ``handoff`` calls v1 ``post_order_once`` exactly once through a capturing, retry-free transport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from lbf.adapters.halden import render as halden_render
from lbf.adapters.halden.mapping import account_from_row, item_mappings, to_integration_error
from lbf.adapters.halden.reconcile import MAX_ORDER_PAGES, HaldenReconciler
from lbf.adapters.halden.transport import CapturingTransport
from lbf.adapters.halden.v1bridge.client import LegacyClient
from lbf.adapters.halden.v1bridge.errors import BridgeError
from lbf.adapters.halden.v1bridge.models import E8Observation, StockStatus
from lbf.adapters.halden.v1bridge.normalize import (
    assess_stock,
    build_order,
    build_order_summary,
    build_products,
    normalize_account_id,
)
from lbf.domain.enums import AvailabilityStatus, TransportObservationKind
from lbf.domain.errors import DomainError, ErrorCode, IntegrationError, capability_unsupported
from lbf.domain.models import (
    Availability,
    BackendAccount,
    CanonicalItem,
    DataIssue,
    ItemMapping,
    ReconciliationQuery,
    ReconciliationResult,
    RenderedRequest,
    SubmissionRequest,
    TransportObservation,
    WarehouseQty,
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
from lbf.ports.capabilities import HALDEN_CAPABILITIES, BackendCapabilities

MAX_CUSTOMER_PAGES = 50
MAX_ITEM_PAGES = 50

E8_KIND_MAP = {
    "NOT_DELIVERED": TransportObservationKind.NOT_DELIVERED,
    "PRE_HANDLER_REFUSAL": TransportObservationKind.REFUSED_PRE_PROCESSING,
    "REJECTED": TransportObservationKind.REJECTED,
    "SHORT": TransportObservationKind.REJECTED,
    "ACCEPTED": TransportObservationKind.ACCEPTED,
    "UNCERTAIN": TransportObservationKind.UNCERTAIN,
}


class HaldenAdapter:
    def __init__(
        self,
        base_url: str,
        login: str,
        secret: str,
        *,
        mappings: Iterable[ItemMapping],
        timeout: float = 5.0,
        max_retries: int = 2,
        inner_transport: Any = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        before_publish: Callable[[RenderedRequest], None] | None = None,
        client: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = CapturingTransport(inner_transport)
        if client is not None:  # offline tests: lbf.adapters.halden.fake.FakeLegacyClient
            self.client = client
        else:
            kwargs: dict[str, Any] = {"timeout": timeout, "max_retries": max_retries, "transport": self.transport}
            if sleep is not None:
                kwargs["sleep"] = sleep
            self.client = LegacyClient(self.base_url, login, secret, **kwargs)
        self._mappings = item_mappings(mappings)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reconciler = HaldenReconciler(self.client, self._clock)
        self._last_read_at: datetime | None = None
        # Engine hook invoked at the last moment before bytes can reach HALDEN (test-profile pause point
        # ``before_handoff``); a no-op in other profiles.
        self.before_publish = before_publish or (lambda rendered: None)

    # ------------------------------------------------------------------ static

    def capabilities(self) -> BackendCapabilities:
        return HALDEN_CAPABILITIES

    def _read(self, fn: Callable[[], Any]) -> Any:
        try:
            result = fn()
        except BridgeError as exc:
            raise to_integration_error(exc) from exc
        self._last_read_at = self._clock()
        return result

    def health(self) -> BackendHealth:
        try:
            self._read(self.client.ping)
            return BackendHealth(backend_id="halden", reachable=True, detail="ping ok",
                                 last_successful_read_at=self._last_read_at)
        except IntegrationError as exc:
            return BackendHealth(backend_id="halden", reachable=False, detail=exc.message,
                                 last_successful_read_at=self._last_read_at)

    # ------------------------------------------------------------------ customers

    def _customer_rows(self, *, name: str | None = None, acct: int | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_CUSTOMER_PAGES):
            body = self._read(lambda o=offset: self.client.search_customers_raw(name=name, acct=acct, offset=o))
            rows.extend(body["rows"])
            if body.get("next_offset") is None:
                return rows
            offset = body["next_offset"]
        raise IntegrationError(ErrorCode.BACKEND_MALFORMED, "HALDEN customer paging did not terminate")

    def search_customers(self, query: str) -> list[BackendAccount]:
        text = (query or "").strip()
        if text.isdigit() and 1 <= len(text) <= 5:
            rows = self._customer_rows(acct=int(text))
        else:
            rows = self._customer_rows(name=text)
        return [account_from_row(r) for r in rows]

    def get_customer(self, native_id: str) -> BackendAccount:
        try:
            account = normalize_account_id(native_id)
        except ValueError as exc:
            raise IntegrationError(ErrorCode.BACKEND_MALFORMED, "invalid HALDEN account number") from exc
        matches = [a for a in (account_from_row(r) for r in self._customer_rows(acct=int(account)))
                   if a.ref.native_id == account]
        if not matches:
            raise DomainError(ErrorCode.NOT_FOUND, f"No HALDEN customer {account}")
        return matches[0]

    # ------------------------------------------------------------------ orders

    def list_orders(self, native_customer_id: str) -> list[BackendOrderSummary]:
        try:
            rows, _ = self._reconciler.order_rows(normalize_account_id(native_customer_id))
        except BridgeError as exc:
            raise to_integration_error(exc) from exc
        out = []
        for row in rows:
            s = build_order_summary(row)
            out.append(BackendOrderSummary(backend_id="halden", order_ref=s.order_no, native_customer_id=s.account_id,
                                           reference=row.get("Ref") if isinstance(row.get("Ref"), str) else None,
                                           status=s.status.value, order_date=s.order_date.isoformat()
                                           if s.order_date else None, total=s.total))
        return out

    def get_order(self, backend_order_ref: str) -> BackendOrder:
        body = self._read(lambda: self.client.get_order_raw(backend_order_ref))
        if body["rc"] != 0:
            raise DomainError(ErrorCode.NOT_FOUND, f"HALDEN order {backend_order_ref} not found")
        raw = body["order"]
        order = build_order(raw)
        return BackendOrder(
            backend_id="halden", order_ref=order.order_no, native_customer_id=order.account_id,
            reference=raw.get("Ref") if isinstance(raw.get("Ref"), str) else None, status=order.status.value,
            order_date=order.order_date.isoformat() if order.order_date else None, total=order.total,
            lines=[BackendOrderLine(line_no=ln.line_no, native_item=ln.sku, quantity_native=ln.quantity)
                   for ln in order.lines],
        )

    # ------------------------------------------------------------------ catalogue and stock (rule I-1)

    def _products(self) -> list[Any]:
        items: list[dict[str, Any]] = []
        page, pages = 1, 1
        while page <= pages and page <= MAX_ITEM_PAGES:
            rows, pages = self._read(lambda p=page: self.client.list_items_raw(p))
            items.extend(rows)
            page += 1
        return build_products(items)

    def catalogue(self) -> CatalogueSnapshot:
        products = self._products()
        entries = []
        for p in products:
            mapping = self._mappings.get(p.sku)
            entries.append(CatalogueEntry(native_item=p.sku, name=p.name, native_uom=p.unit_of_measure or "",
                                          unit_price=p.unit_price, discontinued=p.discontinued, pack_size=1,
                                          canonical=CanonicalItem(canonical_sku=mapping.canonical_sku, name=p.name,
                                                                  category=p.category, base_uom=mapping.native_uom)
                                          if mapping else None,
                                          issues=[i.code.value for i in p.issues]))
        return CatalogueSnapshot(backend_id="halden", as_of=self._clock(), entries=entries)

    def stock(self) -> StockSnapshot:
        products = self._products()
        rows = self._read(self.client.stock_raw)
        now = self._clock()
        result = []
        for product in products:
            report = assess_stock(product, rows)
            mapping = self._mappings.get(product.sku)
            status = AvailabilityStatus(report.status.value)
            result.append(Availability(
                canonical_sku=mapping.canonical_sku if mapping else None,
                backend_id="halden", native_item=product.sku, status=status,
                available_base=report.available_quantity if report.status != StockStatus.UNDETERMINED else None,
                as_of=now, freshness="LIVE", snapshot_age_seconds=None,
                warehouses=[WarehouseQty(warehouse=w.warehouse, raw_on_hand=w.raw_on_hand,
                                         raw_allocated=w.raw_allocated, on_hand=w.on_hand, allocated=w.allocated,
                                         available_native=w.available, available_base=w.available)
                            for w in report.warehouses],
                issues=[DataIssue(code=i.code.value, field=i.field, raw_value=i.raw_value, message=i.message)
                        for i in report.issues],
                explanation=report.explanation,
                legacy_available_flag=report.legacy_available_flag,
            ))
        return StockSnapshot(backend_id="halden", as_of=now, freshness="LIVE", availability=result)

    # ------------------------------------------------------------------ write path

    def preflight(self, request: SubmissionRequest) -> PreflightResult:
        """v1 ``prepare_write``: ticket assurance + one authenticated E3 account read (reads only)."""
        try:
            body = self.client.prepare_write(normalize_account_id(request.native_customer_id))
            present = any(str(r.get("acct", "")).strip().zfill(5) == normalize_account_id(request.native_customer_id)
                          for r in body.get("rows", []))
        except (BridgeError, ValueError) as exc:
            return PreflightResult(ok=False, detail=f"HALDEN preflight failed: {getattr(exc, 'message', exc)}")
        if not present:
            return PreflightResult(ok=False, detail=f"HALDEN account {request.native_customer_id} not found")
        return PreflightResult(ok=True, detail="ticket valid; account present")

    def render(self, request: SubmissionRequest) -> RenderedRequest:
        return halden_render.render(request, self.base_url)

    def handoff(self, rendered: RenderedRequest) -> TransportObservation:
        """Exactly one E8 via v1 ``post_order_once``; never retries; never raises."""
        req = rendered.request
        self.transport.reset_capture()
        try:
            # Engine pause point ``before_handoff``. A hook that fails with an ordinary exception has sent nothing:
            # no request object was created yet, so the truthful observation is NOT_DELIVERED. A process kill
            # (or a BaseException modelling one in tests) propagates: the transmission row stays SENDING and the
            # sweeper later records INTERRUPTED -> UNKNOWN (HALDEN can never prove absence).
            self.before_publish(rendered)
        except Exception as exc:
            return TransportObservation(kind=TransportObservationKind.NOT_DELIVERED, observed_at=self._clock(),
                                        detail=f"pre-handoff hook failed before any request ({exc.__class__.__name__})")
        try:
            obs: E8Observation = self.client.post_order_once(
                req.native_customer_id, req.correlation_value,
                [(line.native_item, line.quantity_native) for line in req.lines])
        except Exception as exc:
            return TransportObservation(kind=TransportObservationKind.UNCERTAIN,
                                        detail=f"adapter exception after handoff start ({exc.__class__.__name__})",
                                        observed_at=self._clock(),
                                        payload_sha256_on_wire=self.transport.last_request_sha256)
        wire = self.transport.last_request_sha256
        kind = E8_KIND_MAP[obs.kind]
        detail = obs.detail
        if wire is not None and wire != rendered.payload_sha256:
            kind = TransportObservationKind.UNCERTAIN
            detail = f"INTEGRITY_ALERT: bytes on the wire differ from the rendered payload; {detail}"
        code = None
        if obs.kind == "SHORT":
            code = f"SHORT:{obs.short_item}:{obs.short_available}"
        elif obs.rc is not None:
            code = f"rc={obs.rc}"
        return TransportObservation(
            kind=kind, backend_order_ref=obs.order_no, backend_code=code, http_status=obs.http_status,
            detail=detail, response_sha256=self.transport.last_response_sha256,
            response_excerpt=obs.body_excerpt, observed_at=self._clock(), payload_sha256_on_wire=wire,
        )

    def reconcile(self, query: ReconciliationQuery) -> ReconciliationResult:
        return self._reconciler.reconcile(query)

    # ------------------------------------------------------------------ capability-gated (unsupported)

    def poll_acknowledgements(self) -> list[AckObservation]:
        raise capability_unsupported("poll_acknowledgements", "halden")

    def archive_acknowledgement(self, ack: AckObservation) -> None:
        raise capability_unsupported("archive_acknowledgement", "halden")

    def withdraw_unclaimed(self, message_id: str) -> WithdrawResult:
        raise capability_unsupported("withdraw_unclaimed", "halden")

    def redeliver(self, original: RenderedRequest) -> TransportObservation:
        raise capability_unsupported("redeliver", "halden")

    def close(self) -> None:
        self.client.close()


__all__ = ["MAX_ORDER_PAGES", "HaldenAdapter"]
