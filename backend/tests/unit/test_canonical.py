"""canonical_json golden vectors (10) and content hashing."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from lbf.domain.canonical import CanonicalJsonError, canonical_json, content_hash, normalize_decimal, sha256_hex

OP = UUID("12345678-1234-5678-1234-567812345678")

GOLDEN = [
    ("empty-object", {}, b"{}"),
    ("sorted-keys", {"b": 1, "a": 2}, b'{"a":2,"b":1}'),
    ("decimal-trailing-zero", {"x": Decimal("12.50")}, b'{"x":"12.5"}'),
    ("decimal-negative-zero", {"x": Decimal("-0.00")}, b'{"x":"0"}'),
    ("decimal-exponent", {"x": Decimal("1E+3")}, b'{"x":"1000"}'),
    ("utf8-unescaped", {"name": "Ålesund Havservice AS"}, '{"name":"Ålesund Havservice AS"}'.encode()),
    ("date", {"d": date(2026, 11, 21)}, b'{"d":"2026-11-21"}'),
    ("uuid", {"id": OP}, b'{"id":"12345678-1234-5678-1234-567812345678"}'),
    ("list-scalars", [True, None, "x", 3], b'[true,null,"x",3]'),
    (
        "nested-content",
        {"lines": [{"q": 10, "sku": "BW-1001"}], "backend_id": "halden", "c": "00417"},
        b'{"backend_id":"halden","c":"00417","lines":[{"q":10,"sku":"BW-1001"}]}',
    ),
]


@pytest.mark.parametrize(("name", "value", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden_vectors(name, value, expected):
    assert canonical_json(value) == expected


def test_floats_rejected():
    with pytest.raises(CanonicalJsonError):
        canonical_json({"x": 1.5})


def test_naive_datetime_rejected_and_aware_accepted():
    with pytest.raises(CanonicalJsonError):
        canonical_json({"t": datetime(2026, 1, 1)})
    assert canonical_json({"t": datetime(2026, 1, 1, tzinfo=UTC)}) == b'{"t":"2026-01-01T00:00:00+00:00"}'


def test_non_finite_decimal_rejected():
    with pytest.raises(CanonicalJsonError):
        normalize_decimal(Decimal("NaN"))


def test_content_hash_is_schema_bound_and_version_sensitive():
    content = {"backend_id": "halden", "native_customer_id": "00417"}
    h1 = content_hash(OP, 1, content)
    assert h1 == sha256_hex(
        b'{"content":{"backend_id":"halden","native_customer_id":"00417"},'
        b'"operation_id":"12345678-1234-5678-1234-567812345678","schema":"lbf-canonical/1.0","version":1}'
    )
    assert content_hash(OP, 2, content) != h1
    assert content_hash(OP, 1, {**content, "native_customer_id": "00418"}) != h1
