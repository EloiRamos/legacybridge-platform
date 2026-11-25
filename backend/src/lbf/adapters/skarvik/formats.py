"""SKARVIK wire formats as seen by the consumer (contract ``skarvik-exchange/1.0``, written against the frozen
contract document; no code is shared with the simulator)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from lbf.domain.errors import DomainError, ErrorCode
from lbf.domain.models import RenderedRequest, SubmissionRequest

ENCODING = "iso-8859-1"
CRLF = "\r\n"
STOCKHOLM = ZoneInfo("Europe/Stockholm")
FORBIDDEN = re.compile(r"[;\r\n]")


def stockholm_to_utc(local_text: str, fmt: str) -> datetime:
    """Convert SKARVIK local time without offset to UTC (fold=0 for the repeated DST hour)."""
    naive = datetime.strptime(local_text, fmt)
    return naive.replace(tzinfo=STOCKHOLM).astimezone(UTC)


def _field(name: str, value: str, max_len: int) -> str:
    if FORBIDDEN.search(value):
        raise DomainError(ErrorCode.VALIDATION, f"{name} must not contain ';', CR or LF")
    if len(value) > max_len:
        raise DomainError(ErrorCode.VALIDATION, f"{name} longer than {max_len} characters")
    try:
        value.encode(ENCODING)
    except UnicodeEncodeError as exc:
        raise DomainError(ErrorCode.VALIDATION, f"{name} contains characters outside ISO-8859-1") from exc
    return value


def render_order(request: SubmissionRequest, uoms: dict[str, str]) -> bytes:
    if request.message_id is None:
        raise DomainError(ErrorCode.VALIDATION, "SKARVIK requires a message id")
    msgid = _field("MSGID", request.message_id, 15)
    rows = [
        ";".join([
            "H", msgid,
            _field("CUSTNO", request.native_customer_id, 8),
            _field("ORDREF", request.correlation_value, 20),
            _field("CUSTPO", request.customer_po or "", 20),
            request.requested_date.strftime("%Y%m%d") if request.requested_date else "",
            _field("NOTE", request.delivery_note or "", 60),
            str(len(request.lines)),
        ])
    ]
    for n, line in enumerate(request.lines, start=1):
        uom = uoms.get(line.native_item)
        if uom is None:
            raise DomainError(ErrorCode.VALIDATION, f"no unit of measure known for SKARVIK item {line.native_item}")
        rows.append(f"L;{n};{_field('ITEMNO', line.native_item, 16)};{line.quantity_native};{uom}")
    rows.append(f"T;{msgid};{len(request.lines)};{sum(line.quantity_native for line in request.lines)}")
    return (CRLF.join(rows) + CRLF).encode(ENCODING)


def rendered(request: SubmissionRequest, uoms: dict[str, str], exchange_dir: str) -> RenderedRequest:
    payload = render_order(request, uoms)
    return RenderedRequest(
        request=request,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        transport_descriptor=f"file drop {exchange_dir.rstrip('/')}/inbox/ORD_{request.message_id}.csv "
                             "(tmp + fsync + atomic rename)",
    )


def parse_price(text: str) -> Decimal | None:
    raw = text.strip().replace(" ", "")
    if not raw:
        return None
    try:
        value = Decimal(raw.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


@dataclass(frozen=True)
class Extract:
    kind: str
    file_name: str
    snapshot_at: datetime  # UTC
    records: list[str]
    sha256: str


def parse_extract(kind: str, name: str, data: bytes) -> Extract | None:
    """Return the parsed extract, or ``None`` when the file is incomplete (no valid ``#END`` line)."""
    text = data.decode(ENCODING)
    if not text.endswith(CRLF):
        return None
    lines = text[: -len(CRLF)].split(CRLF)
    if len(lines) < 2 or not lines[0].startswith("#SNAPSHOT=") or not lines[-1].startswith("#END "):
        return None
    try:
        count = int(lines[-1][5:])
        stamp = lines[0].split("=", 1)[1].split(" ", 1)[0]
        snapshot = stockholm_to_utc(stamp, "%Y%m%d%H%M%S")
    except (ValueError, IndexError):
        return None
    records = lines[1:-1]
    if count != len(records):
        return None
    return Extract(kind=kind, file_name=name, snapshot_at=snapshot, records=records,
                   sha256=hashlib.sha256(data).hexdigest())


def cut(record: str, widths: list[int]) -> list[str]:
    out, pos = [], 0
    for w in widths:
        out.append(record[pos:pos + w])
        pos += w
    return out
