"""Reading SKARVIK snapshot extracts (newest complete file of each type) into canonical models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from lbf.adapters.skarvik.exchange import Share
from lbf.adapters.skarvik.formats import Extract, cut, parse_extract, parse_price
from lbf.domain.enums import AvailabilityStatus, CreditHold
from lbf.domain.errors import ErrorCode, IntegrationError
from lbf.domain.inventory import convert_to_base, is_stale, snapshot_age_seconds
from lbf.domain.models import Availability, BackendAccount, DataIssue, ItemMapping, NativeRef, WarehouseQty

STOCK_W = [16, 3, 9, 3]
ORDER_H_W = [1, 9, 15, 8, 20, 2, 8]
ORDER_L_W = [1, 9, 3, 16, 9, 3]
CUST_W = [8, 40, 1, 20, 2, 60]
ITEM_W = [16, 40, 3, 5, 10, 1]


def newest(share: Share, kind: str, *, required: bool = True) -> Extract | None:
    for path in sorted(share.extracts.glob(f"{kind}_*.dat"), reverse=True):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        parsed = parse_extract(kind, path.name, data)
        if parsed is not None:
            return parsed
    if required:
        raise IntegrationError(ErrorCode.BACKEND_UNAVAILABLE, f"no complete SKARVIK {kind} extract",
                               retryable_read=True)
    return None


@dataclass(frozen=True)
class ExtractOrderLine:
    lineno: int
    itemno: str
    qty: int
    uom: str


@dataclass(frozen=True)
class ExtractOrder:
    orderno: str
    msgid: str
    custno: str
    ordref: str
    status: str
    orddate: str
    lines: tuple[ExtractOrderLine, ...]


def orders(extract: Extract) -> list[ExtractOrder]:
    heads: dict[str, list[str]] = {}
    lines: dict[str, list[ExtractOrderLine]] = {}
    for rec in extract.records:
        if rec.startswith("H"):
            _, orderno, msgid, custno, ordref, status, orddate = (f.strip() for f in cut(rec, ORDER_H_W))
            heads[orderno] = [orderno, msgid, custno, ordref, status, orddate]
            lines.setdefault(orderno, [])
        elif rec.startswith("L"):
            _, orderno, lineno, itemno, qty, uom = (f.strip() for f in cut(rec, ORDER_L_W))
            lines.setdefault(orderno.strip(), []).append(ExtractOrderLine(int(lineno), itemno, int(qty), uom))
        else:
            raise IntegrationError(ErrorCode.BACKEND_MALFORMED, "unknown ORDERS record type")
    return [ExtractOrder(*heads[o], lines=tuple(lines.get(o, []))) for o in heads]  # type: ignore[misc]


def customers(extract: Extract) -> list[BackendAccount]:
    out = []
    for rec in extract.records:
        custno, name, sparr, city, ctry, note = (f.rstrip() for f in cut(rec, CUST_W))
        hold = {"J": CreditHold.HOLD, "N": CreditHold.CLEAR}.get(sparr, CreditHold.UNKNOWN)
        out.append(BackendAccount(
            ref=NativeRef(backend_id="skarvik", native_id=custno.strip()), name_raw=name, name_display=name.strip(),
            credit_hold=hold, city=city or None, country_code=ctry or None, notes=note or None,
            untrusted_text_fields=["notes"] if note else [],
        ))
    return out


@dataclass(frozen=True)
class ItemRow:
    itemno: str
    descr: str
    uom: str
    pack: int
    price: Decimal | None
    disc: bool


def items(extract: Extract) -> dict[str, ItemRow]:
    out = {}
    for rec in extract.records:
        itemno, descr, uom, pack, price, disc = cut(rec, ITEM_W)
        try:
            pack_n = int(pack.strip() or "0")
        except ValueError:
            pack_n = 0
        out[itemno.strip()] = ItemRow(itemno.strip(), descr.strip(), uom.strip(), pack_n, parse_price(price),
                                      disc.strip() == "J")
    return out


def availability(stock: Extract, item_rows: dict[str, ItemRow], mappings: dict[str, ItemMapping], *,
                 now: datetime, max_age_seconds: int) -> list[Availability]:
    """Rule I-2: stale snapshot ⇒ STALE; missing mapping or factor (incl. PACK = 0) ⇒ UNDETERMINED; conversion only
    when exactly divisible ⇒ else UNDETERMINED/NON_INTEGRAL_CONVERSION."""
    age = snapshot_age_seconds(stock.snapshot_at, now)
    stale = is_stale(stock.snapshot_at, now, max_age_seconds)
    result = []
    for rec in stock.records:
        itemno, wh, qty, uom = (f.strip() for f in cut(rec, STOCK_W))
        native = int(qty)
        mapping = mappings.get(itemno)
        item = item_rows.get(itemno)
        issues: list[DataIssue] = []
        base: int | None
        if stale:
            status, base = AvailabilityStatus.STALE, None
            explanation = (f"SKARVIK stock snapshot is {age} s old (limit {max_age_seconds} s); "
                           "quantities are not current.")
        elif mapping is None:
            status, base = AvailabilityStatus.UNDETERMINED, None
            issues.append(DataIssue(code="UNMAPPED_ITEM", field="native_item", raw_value=itemno,
                                    message=f"No crosswalk row maps SKARVIK item {itemno} to a group SKU."))
            explanation = f"Item {itemno} has no crosswalk mapping; availability in group units cannot be stated."
        elif item is None or item.pack <= 0:
            status, base = AvailabilityStatus.UNDETERMINED, None
            raw = None if item is None else str(item.pack)
            issues.append(DataIssue(code="MISSING_CONVERSION_FACTOR", field="pack", raw_value=raw,
                                    message=f"SKARVIK item {itemno} has no usable pack size (PACK={raw})."))
            explanation = f"Pack size of {itemno} is missing or zero, so {uom} cannot be converted."
        else:
            conv = convert_to_base(max(native, 0), mapping.factor_num, mapping.factor_den)
            if conv.issue is not None:
                status, base = AvailabilityStatus.UNDETERMINED, None
                issues.append(DataIssue(code=conv.issue, field="quantity", raw_value=str(native),
                                        message=f"{native} {uom} × {mapping.factor_num}/{mapping.factor_den} is not a "
                                                "whole number of base units."))
                explanation = f"{native} {uom} does not convert to a whole number of {mapping.canonical_sku} units."
            else:
                base = conv.available_base
                status = AvailabilityStatus.IN_STOCK if (base or 0) > 0 else AvailabilityStatus.OUT_OF_STOCK
                if native < 0:
                    issues.append(DataIssue(code="BACKORDERED", field="quantity", raw_value=str(native),
                                            message=f"SKARVIK shows {native} (backorders exceed stock)."))
                explanation = (f"{native} {uom} free in {wh} = {base} base units (snapshot {age} s old)."
                               if mapping.factor_num != mapping.factor_den else
                               f"{native} {uom} free in {wh} (snapshot {age} s old).")
        result.append(Availability(
            canonical_sku=mapping.canonical_sku if mapping else None, backend_id="skarvik", native_item=itemno,
            status=status, available_base=base, as_of=stock.snapshot_at, freshness="SNAPSHOT",
            snapshot_age_seconds=age,
            warehouses=[WarehouseQty(warehouse=wh, raw_on_hand=qty, on_hand=native, available_native=native,
                                     available_base=base)],
            issues=issues, explanation=explanation, legacy_available_flag=None,
        ))
    return result
