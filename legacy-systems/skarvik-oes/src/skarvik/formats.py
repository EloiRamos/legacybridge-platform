"""Wire formats of contract ``skarvik-exchange/1.0`` (order messages, acknowledgements, extracts).

Order message ``ORD_<MSGID>.csv``: ISO-8859-1, ``;`` separator, CRLF, no quoting::

    H;<MSGID>;<CUSTNO>;<ORDREF>;<CUSTPO>;<REQDATE yyyymmdd>;<NOTE>;<LINECOUNT>
    L;<LINENO>;<ITEMNO>;<QTY>;<UOM>
    T;<MSGID>;<LINECOUNT>;<QTYSUM>

Extracts are fixed-width ISO-8859-1 with CRLF; first line ``#SNAPSHOT=<yyyymmddHHMMSS> TZ=Europe/Stockholm``,
last line ``#END <recordcount>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import quoteattr

ENCODING = "iso-8859-1"
CRLF = "\r\n"
TZ_NAME = "Europe/Stockholm"
MSGID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,14}$")
REJECT_CODES = ("E-STRUCT", "E-CUST", "E-BLOCK", "E-ITEM", "E-UOM", "E-QTY")


class StructureError(ValueError):
    def __init__(self, message: str, msgid: str | None = None) -> None:
        super().__init__(message)
        self.msgid = msgid


@dataclass
class OrderLine:
    lineno: int
    itemno: str
    qty: int
    uom: str


@dataclass
class OrderMessage:
    msgid: str
    custno: str
    ordref: str
    custpo: str
    reqdate: str
    note: str
    lines: list[OrderLine] = field(default_factory=list)


def _header_msgid(text: str) -> str | None:
    first = text.split("\r\n", 1)[0].split("\n", 1)[0]
    parts = first.split(";")
    if len(parts) >= 2 and parts[0] == "H" and MSGID_RE.match(parts[1]):
        return parts[1]
    return None


def parse_order(data: bytes) -> OrderMessage:
    """Parse and structurally validate an order message. Raises :class:`StructureError` (with the MSGID
    when it can be read from the header, so an ``E-STRUCT`` acknowledgement can be written)."""
    text = data.decode(ENCODING)
    msgid = _header_msgid(text)
    if not text.endswith(CRLF):
        raise StructureError("message does not end with CRLF (truncated?)", msgid)
    rows = text[: -len(CRLF)].split(CRLF)
    if any("\n" in r or "\r" in r for r in rows):
        raise StructureError("bare CR or LF inside a record", msgid)
    if len(rows) < 3:
        raise StructureError("message needs header, at least one line and trailer", msgid)
    head = rows[0].split(";")
    if len(head) != 8 or head[0] != "H" or msgid is None:
        raise StructureError("malformed header", msgid)
    _, _, custno, ordref, custpo, reqdate, note, linecount = head
    trail = rows[-1].split(";")
    if len(trail) != 4 or trail[0] != "T":
        raise StructureError("missing or malformed trailer", msgid)
    if trail[1] != msgid:
        raise StructureError("trailer MSGID differs from header", msgid)
    if len(ordref) > 20 or len(custpo) > 20 or len(note) > 60:
        raise StructureError("field too long", msgid)
    if reqdate and not re.fullmatch(r"\d{8}", reqdate):
        raise StructureError("REQDATE must be yyyymmdd", msgid)
    lines: list[OrderLine] = []
    for i, raw in enumerate(rows[1:-1], start=1):
        parts = raw.split(";")
        if len(parts) != 5 or parts[0] != "L":
            raise StructureError(f"malformed line record {i}", msgid)
        try:
            lineno, qty = int(parts[1]), int(parts[3])
        except ValueError as exc:
            raise StructureError(f"non-numeric value in line {i}", msgid) from exc
        if lineno != i:
            raise StructureError(f"line numbers must be 1..n (got {lineno} at {i})", msgid)
        lines.append(OrderLine(lineno=lineno, itemno=parts[2], qty=qty, uom=parts[4]))
    try:
        declared, trailer_count, qtysum = int(linecount), int(trail[2]), int(trail[3])
    except ValueError as exc:
        raise StructureError("non-numeric LINECOUNT/QTYSUM", msgid) from exc
    if declared != len(lines) or trailer_count != len(lines):
        raise StructureError("LINECOUNT does not match the number of line records", msgid)
    if qtysum != sum(line.qty for line in lines):
        raise StructureError("QTYSUM does not match the sum of quantities", msgid)
    return OrderMessage(msgid=msgid, custno=custno, ordref=ordref, custpo=custpo, reqdate=reqdate, note=note,
                        lines=lines)


def render_order(msg: OrderMessage) -> bytes:
    """Reference renderer (used by the simulator's own tests; the adapter has its own renderer)."""
    rows = [f"H;{msg.msgid};{msg.custno};{msg.ordref};{msg.custpo};{msg.reqdate};{msg.note};{len(msg.lines)}"]
    rows += [f"L;{ln.lineno};{ln.itemno};{ln.qty};{ln.uom}" for ln in msg.lines]
    rows.append(f"T;{msg.msgid};{len(msg.lines)};{sum(ln.qty for ln in msg.lines)}")
    return (CRLF.join(rows) + CRLF).encode(ENCODING)


def render_ack(*, msgid: str, seq: int, status: str, orderno: str, code: str, local_ts: datetime) -> bytes:
    ts = local_ts.strftime("%Y-%m-%dT%H:%M:%S")
    element = (
        f"<ACK msgid={quoteattr(msgid)} seq=\"{seq}\" status={quoteattr(status)} orderno={quoteattr(orderno)} "
        f"code={quoteattr(code)} ts=\"{ts}\" tz=\"{TZ_NAME}\"/>"
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\r\n' + element + "\r\n").encode("utf-8")


def _fit(value: str, width: int) -> str:
    return value[:width].ljust(width)


def _num(value: int, width: int) -> str:
    text = str(value)
    if len(text) > width:
        raise ValueError(f"{value} does not fit {width}")
    return text.rjust(width)


def render_extract(kind: str, snapshot_local: datetime, records: list[str]) -> bytes:
    head = f"#SNAPSHOT={snapshot_local.strftime('%Y%m%d%H%M%S')} TZ={TZ_NAME}"
    body = [head, *records, f"#END {len(records)}"]
    return (CRLF.join(body) + CRLF).encode(ENCODING)


def stock_record(itemno: str, wh: str, qty: int, uom: str) -> str:
    return _fit(itemno, 16) + _fit(wh, 3) + _num(qty, 9) + _fit(uom, 3)


def order_header_record(orderno: str, msgid: str, custno: str, ordref: str, status: str, orddate: str) -> str:
    return "H" + _fit(orderno, 9) + _fit(msgid, 15) + _fit(custno, 8) + _fit(ordref, 20) + _fit(status, 2) + _fit(orddate, 8)


def order_line_record(orderno: str, lineno: int, itemno: str, qty: int, uom: str) -> str:
    return "L" + _fit(orderno, 9) + _num(lineno, 3) + _fit(itemno, 16) + _num(qty, 9) + _fit(uom, 3)


def customer_record(custno: str, name: str, sparr: str, city: str, ctry: str, note: str) -> str:
    return _fit(custno, 8) + _fit(name, 40) + _fit(sparr, 1) + _fit(city, 20) + _fit(ctry, 2) + _fit(note, 60)


def item_record(itemno: str, descr: str, uom: str, pack: int, price_eur: str, disc: str) -> str:
    return _fit(itemno, 16) + _fit(descr, 40) + _fit(uom, 3) + _num(pack, 5) + price_eur.rjust(10)[:10] + _fit(disc, 1)
