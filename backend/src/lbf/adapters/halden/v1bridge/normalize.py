"""Pure normalization of raw HALDEN values into typed models.

Raw values are interpreted only here. Nothing in this module performs I/O.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from .models import (
    CENT,
    AvailabilityReport,
    Customer,
    DataIssue,
    IssueCode,
    Order,
    OrderLine,
    OrderStatus,
    OrderSummary,
    Product,
    RawStockRow,
    StockStatus,
    WarehouseStock,
)

# --------------------------------------------------------------------------- scalar parsers

_DEC_COMMA = re.compile(r"^\d+,\d{2}$")
_DEC_DOT = re.compile(r"^\d+\.\d{1,2}$")
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(\.\d{3})+$")
_DIGITS = re.compile(r"^\d+$")
_PLAIN_DECIMAL = re.compile(r"^\d+(\.\d+)?$")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.upper() == "N/A":
        return None
    text = text.replace("€", "").replace("EUR", "").strip()
    if not text:
        return None
    if "." in text and "," in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
        if not _PLAIN_DECIMAL.match(text):
            return None
    elif "," in text:
        if not _DEC_COMMA.match(text):
            return None
        text = text.replace(",", ".")
    elif "." in text:
        if _DEC_DOT.match(text):
            pass
        elif _THOUSANDS_DOT.match(text):
            text = text.replace(".", "")
        else:
            return None
    elif not _DIGITS.match(text):
        return None
    try:
        return _q(Decimal(text))
    except InvalidOperation:
        return None


def parse_quantity(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if re.match(r"^-?\d+$", text):
        return int(text)
    if _THOUSANDS_DOT.match(text):
        return int(text.replace(".", ""))
    return None


_TRUE = {"y", "yes", "j", "ja", "1", "true"}
_FALSE = {"n", "no", "nein", "0", "false"}


def parse_flag(raw: str | None) -> bool | None:
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def parse_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    text = raw.strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        if re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
            return date(int(text[6:10]), int(text[3:5]), int(text[0:2]))
        if re.match(r"^\d{8}$", text):
            return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None
    return None


def normalize_account_id(raw: Any) -> str:
    text = str(raw).strip()
    if not re.match(r"^\d{1,5}$", text):
        raise ValueError(f"invalid account id {raw!r}")
    return text.zfill(5)


def normalize_sku(raw: str) -> str:
    return raw.strip().upper()


def normalize_company_name(raw: str) -> str:
    text = raw.strip()
    if text.isupper():
        text = text.title()
    text = re.sub(r"\bAs$", "AS", text)
    text = re.sub(r"\bGmbh$", "GmbH", text)
    return text


def normalize_person_name(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if "," in text:
        last, first = (part.strip() for part in text.split(",", 1))
        if last and first:
            text = f"{first} {last}"
    if text.isupper():
        text = text.title()
    return text


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip().lower()
    return text if _EMAIL.match(text) else None


CANONICAL_CATEGORIES = (
    "Bearings",
    "Hydraulics",
    "Valves",
    "PPE",
    "Power Tools",
    "Hand Tools",
    "Lubricants",
)
_CATEGORY_KEYS = {"".join(c for c in name.lower() if c.isalpha()): name for name in CANONICAL_CATEGORIES}
UNCATEGORIZED = "Uncategorized"


def normalize_category(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return UNCATEGORIZED
    key = "".join(c for c in raw.lower() if c.isalpha())
    return _CATEGORY_KEYS.get(key, raw.strip().title())


_STATUS_MAP = {
    "OP": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "AL": OrderStatus.ALLOCATED,
    "ALLOC": OrderStatus.ALLOCATED,
    "SH": OrderStatus.SHIPPED,
    "SHP": OrderStatus.SHIPPED,
    "SHIPPED": OrderStatus.SHIPPED,
    "DL": OrderStatus.DELIVERED,
    "DLV": OrderStatus.DELIVERED,
    "DELIVERED": OrderStatus.DELIVERED,
    "CX": OrderStatus.CANCELLED,
    "CAN": OrderStatus.CANCELLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "HD": OrderStatus.ON_HOLD,
    "HOLD": OrderStatus.ON_HOLD,
}


def map_order_status(raw: str | None) -> OrderStatus:
    if raw is None:
        return OrderStatus.UNKNOWN
    return _STATUS_MAP.get(raw.strip().upper(), OrderStatus.UNKNOWN)


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _issue(code: IssueCode, field: str, raw: str | None, message: str) -> DataIssue:
    return DataIssue(code=code, field=field, raw_value=raw, message=message)


# --------------------------------------------------------------------------- customers and orders


def build_customer(row: Mapping[str, Any]) -> Customer:
    issues: list[DataIssue] = []
    account_id = normalize_account_id(row.get("acct"))
    raw_mail = row.get("mail")
    email = normalize_email(raw_mail)
    if email is None and raw_mail is not None and raw_mail.strip():
        issues.append(
            _issue(IssueCode.INVALID_EMAIL, "email", raw_mail, f"Email address {raw_mail.strip()!r} is not valid.")
        )
    raw_since = row.get("since")
    since = parse_date(raw_since)
    if since is None and raw_since is not None and raw_since.strip():
        issues.append(
            _issue(IssueCode.UNPARSEABLE_DATE, "customer_since", raw_since, f"Date {raw_since!r} cannot be parsed.")
        )
    return Customer(
        account_id=account_id,
        name=normalize_company_name(row.get("nm") or ""),
        contact_name=normalize_person_name(row.get("contact")),
        email=email,
        phone=_clean(row.get("tel")),
        credit_hold=parse_flag(row.get("hold")) is True,
        customer_since=since,
        city=_clean(row.get("city")),
        country=_clean(row.get("ctry")),
        issues=issues,
    )


def _summary_fields(row: Mapping[str, Any]) -> tuple[dict[str, Any], list[DataIssue]]:
    issues: list[DataIssue] = []
    raw_status = row.get("Stat")
    status = map_order_status(raw_status)
    if status == OrderStatus.UNKNOWN:
        issues.append(
            _issue(
                IssueCode.UNKNOWN_STATUS_CODE,
                "status",
                raw_status,
                f"Status code {raw_status!r} is not a known HALDEN status.",
            )
        )
    raw_date = row.get("Date")
    order_date = parse_date(raw_date)
    if order_date is None and raw_date is not None and raw_date.strip():
        issues.append(_issue(IssueCode.UNPARSEABLE_DATE, "order_date", raw_date, f"Date {raw_date!r} cannot be parsed."))
    ref = row.get("Ref")
    fields = dict(
        order_no=str(row.get("OrderNo") or "").strip().upper(),
        account_id=normalize_account_id(row.get("Acct")),
        order_date=order_date,
        status=status,
        raw_status=raw_status if raw_status is not None else "",
        total=parse_money(row.get("Total")),
        customer_reference=_clean(ref) if isinstance(ref, str) else None,
    )
    return fields, issues


def build_order_summary(row: Mapping[str, Any]) -> OrderSummary:
    fields, issues = _summary_fields(row)
    return OrderSummary(**fields, issues=issues)


def build_order(raw_order: Mapping[str, Any]) -> Order:
    """Build an ``Order`` from the E7 ``order`` object. Raises ValueError on an unusable line."""
    fields, issues = _summary_fields(raw_order)
    lines: list[OrderLine] = []
    for raw_line in raw_order.get("lines") or []:
        quantity = parse_quantity(raw_line.get("qty"))
        if quantity is None or quantity < 1:
            raise ValueError(f"order line quantity {raw_line.get('qty')!r} is not usable")
        unit_price = parse_money(raw_line.get("price"))
        lines.append(
            OrderLine(
                line_no=int(raw_line.get("ln")),
                sku=normalize_sku(str(raw_line.get("item") or "")),
                quantity=quantity,
                unit_price=unit_price,
                line_total=None if unit_price is None else _q(unit_price * quantity),
            )
        )
    lines_total = None
    if lines and all(line.line_total is not None for line in lines):
        lines_total = _q(sum((line.line_total for line in lines), Decimal("0")))
    total = fields["total"]
    if total is not None and lines_total is not None and total != lines_total:
        issues.append(
            _issue(
                IssueCode.TOTAL_MISMATCH,
                "total",
                str(raw_order.get("Total")),
                f"Header total {total} differs from the sum of the lines {lines_total}.",
            )
        )
    return Order(**fields, issues=issues, lines=lines, lines_total=lines_total)


# --------------------------------------------------------------------------- products


def build_products(rows: Iterable[Mapping[str, Any]]) -> list[Product]:
    """Group raw item records by canonical SKU and build one Product per group."""
    groups: "OrderedDict[str, list[Mapping[str, Any]]]" = OrderedDict()
    for row in rows:
        groups.setdefault(normalize_sku(row.get("item_no") or ""), []).append(row)
    return sorted((_build_product(sku, records) for sku, records in groups.items()), key=lambda p: p.sku)


def _build_product(sku: str, records: list[Mapping[str, Any]]) -> Product:
    records = sorted(records, key=lambda r: int(r["id"]))
    first = records[0]
    issues: list[DataIssue] = []
    if len(records) > 1:
        issues.append(
            _issue(
                IssueCode.DUPLICATE_SKU,
                "sku",
                ", ".join(repr(r.get("item_no")) for r in records),
                f"SKU {sku} exists as {len(records)} item records (IDs {', '.join(str(r['id']) for r in records)}).",
            )
        )
        prices = [parse_money(r.get("price")) for r in records]
        if all(p is not None for p in prices) and len(set(prices)) == 1:
            unit_price = prices[0]
        elif all(p is None for p in prices):
            unit_price = None
            issues.append(_issue(IssueCode.MISSING_PRICE, "unit_price", first.get("price"), "No reliable price on file."))
        else:
            unit_price = None
            issues.append(
                _issue(
                    IssueCode.CONFLICTING_PRICE,
                    "unit_price",
                    " | ".join(str(r.get("price")) for r in records),
                    f"Item records for {sku} carry different prices ({', '.join(str(r.get('price')) for r in records)}).",
                )
            )
    else:
        unit_price = parse_money(first.get("price"))
        if unit_price is None:
            issues.append(
                _issue(
                    IssueCode.MISSING_PRICE,
                    "unit_price",
                    first.get("price"),
                    f"Price {first.get('price')!r} is not a usable price.",
                )
            )
    raw_cat = first.get("catg")
    if raw_cat is None or not raw_cat.strip():
        issues.append(_issue(IssueCode.MISSING_CATEGORY, "category", raw_cat, "No category on file."))
    raw_upd = first.get("upd")
    last_updated = parse_date(raw_upd)
    if last_updated is None and raw_upd is not None and raw_upd.strip():
        issues.append(_issue(IssueCode.UNPARSEABLE_DATE, "last_updated", raw_upd, f"Date {raw_upd!r} cannot be parsed."))
    return Product(
        sku=sku,
        record_ids=[int(r["id"]) for r in records],
        record_descriptions=[r.get("descr") if r.get("descr") is not None else "" for r in records],
        name=(first.get("descr") or "").strip(),
        category=normalize_category(raw_cat),
        unit_price=unit_price,
        unit_of_measure=_clean(first.get("uom")),
        legacy_available_flag=parse_flag(first.get("avail")),
        discontinued=any(parse_flag(r.get("disc")) is True for r in records),
        last_updated=last_updated,
        issues=issues,
    )


# --------------------------------------------------------------------------- availability


def _warehouse(row: RawStockRow) -> WarehouseStock:
    on_hand = parse_quantity(row.on_hand)
    allocated = parse_quantity(row.allocated)
    available = on_hand - allocated if on_hand is not None and allocated is not None else None
    return WarehouseStock(
        warehouse=row.wh.strip(),
        raw_on_hand=row.on_hand,
        raw_allocated=row.allocated,
        on_hand=on_hand,
        allocated=allocated,
        available=available,
    )


def rows_for_sku(sku: str, stock_rows: Iterable[RawStockRow]) -> list[RawStockRow]:
    return [row for row in stock_rows if normalize_sku(row.item) == sku]


def assess_availability(product: Product, stock_rows: Iterable[RawStockRow]) -> AvailabilityReport:
    """The five per-SKU availability rules plus FLAG_MISMATCH."""
    rows = rows_for_sku(product.sku, stock_rows)
    warehouses = [_warehouse(row) for row in rows]
    flag = product.legacy_available_flag
    issues: list[DataIssue] = []

    def report(status, available, minimum, explanation):
        if status != StockStatus.UNDETERMINED and flag is not None and flag != (status == StockStatus.IN_STOCK):
            issues.append(
                _issue(
                    IssueCode.FLAG_MISMATCH,
                    "legacy_available_flag",
                    "true" if flag else "false",
                    f"The legacy availability flag says {'available' if flag else 'not available'}, "
                    f"but the stock extract shows {available} free units.",
                )
            )
        return AvailabilityReport(
            sku=product.sku,
            product_name=product.name,
            status=status,
            available_quantity=available,
            known_minimum=minimum,
            warehouses=warehouses,
            legacy_available_flag=flag,
            issues=issues,
            explanation=explanation,
        )

    if product.has_issue(IssueCode.DUPLICATE_SKU):
        issues.append(
            _issue(IssueCode.DUPLICATE_SKU, "sku", product.sku, f"SKU {product.sku} has {len(product.record_ids)} item records.")
        )
        return report(
            StockStatus.UNDETERMINED,
            None,
            None,
            f"Stock cannot be determined: SKU {product.sku} exists as {len(product.record_ids)} item records, "
            "so warehouse quantities cannot be attributed reliably.",
        )
    unknown = [w for w in warehouses if w.on_hand is None or w.allocated is None]
    if unknown:
        known_minimum = sum(w.available for w in warehouses if w.available is not None)
        for w in unknown:
            raw = w.raw_on_hand if w.on_hand is None else w.raw_allocated
            issues.append(
                _issue(
                    IssueCode.UNKNOWN_QUANTITY,
                    "on_hand" if w.on_hand is None else "allocated",
                    raw,
                    f"Warehouse {w.warehouse} reports an unreadable quantity {raw!r} for {product.sku}.",
                )
            )
        names = ", ".join(w.warehouse for w in unknown)
        return report(
            StockStatus.UNDETERMINED,
            None,
            known_minimum,
            f"Stock cannot be determined: warehouse {names} reports an unreadable quantity for {product.sku}; "
            f"at least {known_minimum} units are known to be free elsewhere.",
        )
    negative = [w for w in warehouses if w.on_hand is not None and w.on_hand < 0]
    if negative:
        for w in negative:
            issues.append(
                _issue(
                    IssueCode.NEGATIVE_QUANTITY,
                    "on_hand",
                    w.raw_on_hand,
                    f"Warehouse {w.warehouse} reports a negative on-hand quantity ({w.on_hand}) for {product.sku}.",
                )
            )
        w = negative[0]
        return report(
            StockStatus.UNDETERMINED,
            None,
            None,
            f"Stock cannot be determined: warehouse {w.warehouse} reports a negative on-hand quantity "
            f"({w.on_hand}) for {product.sku}.",
        )
    if not warehouses:
        return report(StockStatus.OUT_OF_STOCK, 0, 0, f"No stock balance is on file for {product.sku}.")
    available = sum(w.available for w in warehouses)  # type: ignore[misc]
    pooled = " + ".join(f"{w.warehouse} {w.available}" for w in warehouses)
    if available > 0:
        return report(
            StockStatus.IN_STOCK,
            available,
            available,
            f"{available} units are free in HALDEN's pooled allocation ({pooled}).",
        )
    return report(
        StockStatus.OUT_OF_STOCK,
        available,
        available,
        f"No free units in HALDEN's pooled allocation ({pooled}).",
    )


def assess_stock(product: Product, stock_rows: Iterable[RawStockRow]) -> AvailabilityReport:
    """Row validation before aggregation: a negative ALLOCATED value makes the SKU UNDETERMINED
    (field ``allocated``) and ``assess_availability`` is not invoked for that SKU."""
    rows = rows_for_sku(product.sku, stock_rows)
    for row in rows:
        allocated = parse_quantity(row.allocated)
        if allocated is not None and allocated < 0:
            wh = row.wh.strip()
            return AvailabilityReport(
                sku=product.sku,
                product_name=product.name,
                status=StockStatus.UNDETERMINED,
                available_quantity=None,
                known_minimum=None,
                warehouses=[_warehouse(r) for r in rows],
                legacy_available_flag=product.legacy_available_flag,
                issues=[
                    _issue(
                        IssueCode.NEGATIVE_QUANTITY,
                        "allocated",
                        row.allocated,
                        f"Warehouse {wh} reports a negative allocated quantity ({allocated}) for {product.sku}.",
                    )
                ],
                explanation=(
                    f"Stock cannot be determined: warehouse {wh} reports a negative allocated quantity "
                    f"({allocated}) for {product.sku}."
                ),
            )
    return assess_availability(product, rows)
