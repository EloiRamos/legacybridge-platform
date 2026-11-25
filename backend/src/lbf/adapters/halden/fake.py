"""In-memory stand-in for the v1 ``LegacyClient`` with HALDEN ERP 3.1 semantics (offline tests only).

It returns the same raw shapes as the real client (E3–E8 per ``docs/contracts/HALDEN_LEGACY_API.md``), so the
``HaldenAdapter`` mapping, reconciliation and classification code run unchanged against it. Seed rows are parsed
from the frozen HALDEN ``seed.sql``. Fault modes model the simulator's control-plane faults and the wire-tap modes.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from lbf.adapters.halden.v1bridge.errors import LegacyUnavailable
from lbf.adapters.halden.v1bridge.models import E8Observation, RawStockRow
from lbf.adapters.halden.v1bridge.normalize import normalize_sku, parse_flag, parse_money, parse_quantity

INSERT_RE = re.compile(r"^INSERT INTO (\w+) VALUES \((.*)\);$")
TOKEN_RE = re.compile(r"'((?:[^']|'')*)'|(NULL)|(-?\d+)")

E8_MODES = ("normal", "refuse_440", "not_delivered", "commit_then_uncertain", "lost_before_backend", "error_500")


def parse_seed(path: Path) -> dict[str, list[list[Any]]]:
    tables: dict[str, list[list[Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = INSERT_RE.match(line.strip())
        if not m:
            continue
        values: list[Any] = []
        for s, null, num in TOKEN_RE.findall(m.group(2)):
            values.append(None if null else (int(num) if num else s.replace("''", "'")))
        tables.setdefault(m.group(1), []).append(values)
    return tables


@dataclass
class FakeOrder:
    order_no: str
    acct: str
    ref: str | None
    lines: list[tuple[str, int, str]]
    date: str = "21.11.2026"
    stat: str = "OP"


@dataclass
class FakeHalden:
    """The fake backend's state (its own 'database' and oracle view)."""

    customers: list[list[Any]]
    items: list[list[Any]]
    stock: list[list[Any]]
    orders: dict[str, FakeOrder] = field(default_factory=dict)
    next_no: int = 240111
    e8_received: int = 0
    e8_committed: int = 0
    read_count: int = 0
    next_e8_modes: list[str] = field(default_factory=list)
    read_failures: int = 0
    down: bool = False
    received_refs: list[str] = field(default_factory=list)  # oracle: every E8 that reached the backend
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_seed(cls, seed_sql: Path) -> FakeHalden:
        t = parse_seed(seed_sql)
        fake = cls(customers=t["CUSTMAST"], items=t["ITEMMAST"], stock=t["STOCKBAL"])
        lines: dict[str, list[tuple[str, int, str]]] = {}
        for order_no, _ln, item, qty, price in t["ORDLIN"]:
            lines.setdefault(order_no, []).append((item, int(qty), price))
        for order_no, acct, date, stat, _total, ref in t["ORDHDR"]:
            fake.orders[order_no] = FakeOrder(order_no, acct, ref, lines.get(order_no, []), date=date, stat=stat)
        return fake

    def orders_with_ref(self, ref: str) -> list[FakeOrder]:
        return [o for o in self.orders.values() if o.ref is not None and o.ref.strip() == ref]

    def book(self, acct: str, ref: str | None, lines: list[tuple[str, int]]) -> tuple[str, Any]:
        """E8 business logic (validation order of the API document); returns (kind, value)."""
        cust = next((c for c in self.customers if c[0] == str(acct).zfill(5)), None)
        if not lines or len(lines) > 20:
            return "rc", (40, "BAD_PAYLOAD")
        if cust is None:
            return "rc", (11, "UNKNOWN_ACCOUNT")
        if parse_flag(cust[5]) is True:
            return "rc", (12, "ACCOUNT_ON_HOLD")
        priced: list[tuple[str, int, Decimal]] = []
        for item, qty in lines:
            wanted = normalize_sku(item)
            record = next((i for i in sorted(self.items, key=lambda r: r[0]) if normalize_sku(i[1]) == wanted), None)
            if record is None:
                return "rc", (21, "UNKNOWN_ITEM")
            if parse_flag(record[7]) is True:
                return "rc", (22, "ITEM_DISCONTINUED")
            price = parse_money(record[4])
            if price is None:
                return "rc", (23, "NO_PRICE")
            if not 1 <= qty <= 999:
                return "rc", (30, "BAD_QTY")
            priced.append((normalize_sku(item), qty, price))
        need: dict[str, int] = {}
        for sku, qty, _ in priced:
            need[sku] = need.get(sku, 0) + qty
        for sku, qty in need.items():
            free = 0
            for row in self.stock:
                if normalize_sku(row[0]) == sku:
                    on_hand, alloc = parse_quantity(row[2]), parse_quantity(row[3])
                    if on_hand is not None and alloc is not None:
                        free += on_hand - alloc
            if qty > free:
                return "short", (sku, free)
        order_no = f"SO-{self.next_no:06d}"
        self.next_no += 1
        clean_ref = (ref or "").strip()[:12] or None
        self.orders[order_no] = FakeOrder(order_no, str(acct).zfill(5), clean_ref,
                                          [(sku, qty, str(price)) for sku, qty, price in priced])
        self.e8_committed += 1
        total = sum((q * p for _, q, p in priced), Decimal("0"))
        return "ok", (order_no, f"{total:.2f}")


class FakeLegacyClient:
    """Implements the subset of ``LegacyClient`` used by ``HaldenAdapter`` and ``HaldenReconciler``."""

    def __init__(self, backend: FakeHalden) -> None:
        self.backend = backend
        self.e8_request_count = 0

    def _read(self) -> None:
        b = self.backend
        b.read_count += 1
        if b.down:
            raise LegacyUnavailable("HALDEN ERP is not responding.")
        if b.read_failures > 0:
            b.read_failures -= 1
            raise LegacyUnavailable("HTTP 500")

    def ping(self) -> str:
        self._read()
        return "HALDEN-ERP 3.1 READY"

    def search_customers_raw(self, name: str | None = None, acct: int | str | None = None, offset: int = 0) -> dict:
        self._read()
        rows = []
        for c in self.backend.customers:
            if acct is not None and int(c[0]) != int(acct):
                continue
            if name is not None and name.casefold() not in (c[1] or "").casefold():
                continue
            rows.append({"acct": c[0], "nm": c[1], "contact": c[2], "mail": c[3], "tel": c[4], "hold": c[5],
                         "since": c[6], "city": c[7], "ctry": c[8]})
        return {"rows": rows, "next_offset": None}

    def list_items_raw(self, page: int = 1) -> tuple[list[dict[str, Any]], int]:
        self._read()
        if page != 1:
            return [], 1
        return [{"id": i[0], "item_no": i[1], "descr": i[2], "catg": i[3], "price": i[4], "uom": i[5], "avail": i[6],
                 "disc": i[7], "upd": i[8]} for i in self.backend.items], 1

    def stock_raw(self, item: str | None = None) -> list[RawStockRow]:
        self._read()
        return [RawStockRow(item=r[0], wh=r[1], on_hand=r[2], allocated=r[3]) for r in self.backend.stock
                if item is None or normalize_sku(r[0]) == normalize_sku(item)]

    def list_orders_raw(self, acct: int | str | None = None, cursor: str | None = None) -> dict:
        self._read()
        rows = [{"OrderNo": o.order_no, "Acct": str(int(o.acct)), "Date": o.date, "Stat": o.stat, "Total": "0.00",
                 "Ref": o.ref} for o in self.backend.orders.values() if acct is None or int(o.acct) == int(acct)]
        return {"data": {"orders": rows}, "continue": ""}

    def get_order_raw(self, order_no: str) -> dict:
        self._read()
        o = self.backend.orders.get(order_no.strip().upper())
        if o is None:
            return {"rc": 404, "msg": "ORDER NOT ON FILE"}
        return {"rc": 0, "order": {"OrderNo": o.order_no, "Acct": str(int(o.acct)), "Date": o.date, "Stat": o.stat,
                                   "Total": "0.00", "Ref": o.ref,
                                   "lines": [{"ln": n, "item": it, "qty": str(q), "price": p}
                                             for n, (it, q, p) in enumerate(o.lines, 1)]}}

    def prepare_write(self, account_id: str) -> dict:
        return self.search_customers_raw(acct=int(account_id))

    def post_order_once(self, acct: str, ref: str, lines: Iterable[tuple[str, int]]) -> E8Observation:
        b = self.backend
        self.e8_request_count += 1
        mode = b.next_e8_modes.pop(0) if b.next_e8_modes else "normal"
        if b.down or mode == "not_delivered":
            return E8Observation(kind="NOT_DELIVERED", detail="No connection to HALDEN ERP (fake).")
        with b.lock:
            b.e8_received += 1
            b.received_refs.append(ref)
            if mode == "refuse_440":
                return E8Observation(kind="PRE_HANDLER_REFUSAL", http_status=440, detail="HTTP 440 (fake).")
            if mode == "error_500":
                return E8Observation(kind="UNCERTAIN", http_status=500, detail="HTTP 500 (fake, pre-handler).")
            if mode == "lost_before_backend":
                b.e8_received -= 1
                b.received_refs.pop()
                return E8Observation(kind="UNCERTAIN", detail="Transport error after connecting (fake black hole).")
            kind, value = b.book(acct, ref, list(lines))
        if mode == "commit_then_uncertain" and kind == "ok":
            return E8Observation(kind="UNCERTAIN", http_status=500, detail="HTTP 500 after commit (fake).")
        if kind == "rc":
            return E8Observation(kind="REJECTED", http_status=200, rc=value[0], msg=value[1],
                                 detail=f"HTTP 200 rc {value[0]} {value[1]}")
        if kind == "short":
            return E8Observation(kind="SHORT", http_status=409, short_item=value[0], short_available=value[1],
                                 detail=f"HTTP 409 SHORT:{value[0]}:{value[1]}")
        return E8Observation(kind="ACCEPTED", http_status=200, rc=0, order_no=value[0], total=value[1],
                             detail=f"HTTP 200 rc 0 order_no {value[0]}")

    def close(self) -> None:
        return None
