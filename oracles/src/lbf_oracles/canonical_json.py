"""Independent re-implementation of the canonical JSON rule.

UTF-8; keys sorted; separators ``(",", ":")``; non-ASCII kept; Decimals as normalized fixed-point strings
(no exponent, trailing fractional zeros removed, "-0" -> "0"); dates/datetimes ISO-8601 (datetimes must be aware);
UUIDs as strings; floats rejected. Written from the specification, not from ``lbf`` code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def _decimal(value: Decimal) -> str:
    if value.is_nan() or value.is_infinite():
        raise TypeError("non-finite decimal")
    sign, digits, exponent = value.as_tuple()
    text = "".join(map(str, digits)) or "0"
    exp = int(exponent)
    if exp >= 0:
        text = text + "0" * exp
    else:
        pad = -exp
        text = text.rjust(pad + 1, "0")
        text = text[:-pad] + "." + text[-pad:]
        text = text.rstrip("0").rstrip(".")
    text = text.lstrip("0") or "0"
    if text.startswith("."):
        text = "0" + text
    if sign and text != "0":
        text = "-" + text
    return text


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("float not permitted")
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetime")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("non-string key")
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    raise TypeError(f"unsupported {type(value).__name__}")


def dumps(value: Any) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(dumps(value)).hexdigest()


def content_hash(operation_id: str, version: int, content: Any) -> str:
    return sha256_hex({"schema": "lbf-canonical/1.0", "operation_id": str(operation_id), "version": version,
                       "content": content})
