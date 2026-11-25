"""HALDEN reconciliation — refactored from v1 ``service.py`` (``_matches_approved``, ``_same_account``,
``_read_back``, ``_reconcile``). Traceability: ``docs/contracts/HALDEN_ADAPTER.md``.

Rule R-2: a match requires the account, the correlation value in ``Ref`` (``None`` is a non-match, tested before
trimming) and the line multiset after native-item normalization; price, total, date and status are not compared.
Rule R-3: an anchored reconcile reads only the anchor order and never confirms a different order.
HALDEN can never prove absence: ``absence_proven`` is always ``False``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from lbf.adapters.halden.v1bridge.errors import BridgeError
from lbf.adapters.halden.v1bridge.normalize import normalize_account_id, normalize_sku, parse_quantity
from lbf.domain.enums import ReconciliationVerdict as V
from lbf.domain.models import ReconciliationQuery, ReconciliationResult, SourceRead

MAX_ORDER_PAGES = 50


def body_sha256(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def same_account(raw_acct: Any, account_id: str) -> bool:
    try:
        return normalize_account_id(raw_acct) == normalize_account_id(account_id)
    except ValueError:
        return False


def ref_matches(raw_ref: Any, correlation_value: str) -> bool:
    if raw_ref is None or not isinstance(raw_ref, str):  # null is a non-match, tested before trimming
        return False
    return raw_ref.strip() == correlation_value


def matches_approved(raw_order: Mapping[str, Any], query: ReconciliationQuery) -> bool:
    if not ref_matches(raw_order.get("Ref"), query.correlation_value):
        return False
    if not same_account(raw_order.get("Acct"), query.native_customer_id):
        return False
    try:
        booked = Counter(
            (normalize_sku(str(line.get("item") or "")), parse_quantity(line.get("qty")))
            for line in raw_order.get("lines") or []
        )
    except AttributeError:
        return False
    expected = Counter((normalize_sku(line.native_item), line.quantity_native) for line in query.expected_lines)
    return booked == expected


class HaldenReconciler:
    def __init__(self, client: Any, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._clock = clock

    def _read(self, source: str, fn: Callable[[], Any], summary: Callable[[Any], str]) -> tuple[Any, SourceRead]:
        body = fn()
        return body, SourceRead(source=source, read_at=self._clock(), response_sha256=body_sha256(body),
                                summary=summary(body))

    def order_rows(self, acct: str) -> tuple[list[Mapping[str, Any]], list[SourceRead]]:
        rows: list[Mapping[str, Any]] = []
        reads: list[SourceRead] = []
        cursor: str | None = None
        for _ in range(MAX_ORDER_PAGES):
            body, read = self._read(
                "halden:E6",
                lambda c=cursor: self._client.list_orders_raw(acct=int(acct), cursor=c),
                lambda b: f"{len(b['data']['orders'])} orders for account {acct}",
            )
            reads.append(read)
            rows.extend(body["data"]["orders"])
            cursor = body["continue"]
            if cursor == "":
                return rows, reads
        raise BridgeError(message="HALDEN ERP order paging did not terminate.")

    def _detail(self, order_ref: str) -> tuple[Mapping[str, Any], SourceRead]:
        body, read = self._read(
            "halden:E7", lambda: self._client.get_order_raw(order_ref),
            lambda b: f"E7 {order_ref}: rc {b.get('rc')}",
        )
        if body["rc"] not in (0, 404):
            raise BridgeError(message=f"HALDEN ERP answered rc {body['rc']} for order {order_ref}.")
        return body, read

    def reconcile(self, query: ReconciliationQuery) -> ReconciliationResult:
        reads: list[SourceRead] = []
        try:
            if query.anchor_order_ref:
                body, read = self._detail(query.anchor_order_ref)
                reads.append(read)
                if body["rc"] == 0 and matches_approved(body["order"], query):
                    return ReconciliationResult(verdict=V.MATCHED, matched_order_ref=query.anchor_order_ref,
                                                candidates=[query.anchor_order_ref], source_reads=reads,
                                                detail=f"Anchored order {query.anchor_order_ref} read back; matches.")
                detail = ("is not on file (E7 rc 404)" if body["rc"] == 404
                          else "differs from the approved content (account, reference or lines)")
                return ReconciliationResult(verdict=V.MISMATCH, candidates=[query.anchor_order_ref],
                                            source_reads=reads,
                                            detail=f"Anchored order {query.anchor_order_ref} {detail}.")
            rows, page_reads = self.order_rows(query.native_customer_id)
            reads.extend(page_reads)
            candidates = [str(r.get("OrderNo") or "").strip() for r in rows
                          if ref_matches(r.get("Ref"), query.correlation_value)]
            if not candidates:
                return ReconciliationResult(
                    verdict=V.NO_MATCH, source_reads=reads,
                    detail=f"No order with reference {query.correlation_value} is visible for account "
                           f"{query.native_customer_id}. This does not prove it was not created.")
            if len(candidates) >= 2:
                return ReconciliationResult(
                    verdict=V.AMBIGUOUS, candidates=candidates, source_reads=reads,
                    detail=f"{len(candidates)} orders carry reference {query.correlation_value}: "
                           + ", ".join(candidates))
            body, read = self._detail(candidates[0])
            reads.append(read)
            if body["rc"] == 0 and matches_approved(body["order"], query):
                return ReconciliationResult(verdict=V.MATCHED, matched_order_ref=candidates[0], candidates=candidates,
                                            source_reads=reads,
                                            detail=f"Reconciled to order {candidates[0]}: account, reference and "
                                                   "lines match the approval.")
            return ReconciliationResult(
                verdict=V.MISMATCH, candidates=candidates, source_reads=reads,
                detail=f"Candidate {candidates[0]} carries the reference but its detail is missing or differs.")
        except (BridgeError, ValueError, TypeError, KeyError) as exc:
            message = getattr(exc, "message", None) or exc.__class__.__name__
            return ReconciliationResult(verdict=V.READ_FAILED, source_reads=reads,
                                        detail=f"HALDEN could not be read: {message}")
