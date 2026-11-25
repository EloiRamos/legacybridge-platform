"""Canonical JSON and content hashing.

``canonical_json``: UTF-8, keys sorted, separators ``(",", ":")``, ``ensure_ascii=False``,
Decimals as normalized plain strings, dates/datetimes ISO-8601, UUIDs as strings, enums by value,
**floats rejected**. The same function is re-implemented independently in ``oracles/`` and the two
are cross-checked by golden vectors.

Decimal normalization rule: finite only; rendered in fixed-point notation without exponent, with
trailing fractional zeros (and a trailing ".") removed; negative zero renders as "0".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from lbf.domain.enums import CANONICAL_SCHEMA


class CanonicalJsonError(TypeError):
    """Raised when a value cannot be represented canonically (e.g. a float)."""


def normalize_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise CanonicalJsonError(f"non-finite Decimal {value!r}")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


def _prepare(value: Any) -> Any:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalJsonError("floats are not permitted in canonical JSON")
    if isinstance(value, Decimal):
        return normalize_decimal(value)
    if isinstance(value, Enum):
        return _prepare(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalJsonError("naive datetimes are not permitted")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "model_dump"):
        return _prepare(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError(f"non-string key {key!r}")
            out[key] = _prepare(item)
        return out
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_prepare(item) for item in value]
    raise CanonicalJsonError(f"unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(_prepare(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(operation_id: UUID, version: int, content: Any) -> str:
    """``sha256(canonical_json({"schema", "operation_id", "version", "content"}))``."""
    return sha256_hex(
        canonical_json(
            {"schema": CANONICAL_SCHEMA, "operation_id": str(operation_id), "version": version, "content": content}
        )
    )
