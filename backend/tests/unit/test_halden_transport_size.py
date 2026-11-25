"""``CapturingTransport`` bounds HALDEN response bodies.

Covered: below / exactly at / above the limit; a declared ``Content-Length`` above the limit refused before the body
is read; streamed bodies without a declared length bounded while reading; malformed, conflicting and lying
``Content-Length``; a compressed body that expands beyond the limit; and the classification contracts — an oversized
E8 response after the handoff is ``UNCERTAIN`` (never ``NOT_DELIVERED``) with exactly one POST and no resend, an
oversized read is ``BACKEND_MALFORMED`` without a retry, and reconciliation reports ``READ_FAILED`` (never absence).
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
from test_halden_adapter import CUSTOMER_417, CV, handoff_after_preflight, make, order_row, rq

from lbf.adapters.halden.transport import MAX_RESPONSE_BYTES, CapturingTransport, HaldenResponseTooLarge
from lbf.domain.enums import ReconciliationVerdict, TransportObservationKind
from lbf.domain.errors import ErrorCode, IntegrationError

K = TransportObservationKind
LIMIT = 64


class CountingStream(httpx.SyncByteStream):
    """A response body delivered in chunks; records how many chunks were pulled from the wire."""

    def __init__(self, total: int, chunk: int = 16) -> None:
        self.total, self.chunk, self.pulled = total, chunk, 0

    def __iter__(self):
        sent = 0
        while sent < self.total:
            n = min(self.chunk, self.total - sent)
            self.pulled += 1
            sent += n
            yield b"a" * n


def client_for(handler, limit: int = LIMIT) -> httpx.Client:
    return httpx.Client(transport=CapturingTransport(httpx.MockTransport(handler), max_response_bytes=limit))


def fixed(body: bytes, headers: dict[str, str] | None = None):
    return lambda request: httpx.Response(200, content=body, headers=headers)


# --------------------------------------------------------------------------------------------- transport level


def test_mi3_default_limit_is_explicit_and_used_by_the_adapter():
    assert MAX_RESPONSE_BYTES == 1_048_576
    adapter, _, _ = make({})
    assert adapter.transport.max_response_bytes == MAX_RESPONSE_BYTES


def test_mi3_body_below_the_limit_passes_unchanged():
    with client_for(fixed(b"x" * (LIMIT - 1))) as c:
        assert c.get("http://halden/halden/ping").content == b"x" * (LIMIT - 1)


def test_mi3_body_exactly_at_the_limit_passes():
    with client_for(fixed(b"x" * LIMIT)) as c:
        assert c.get("http://halden/halden/ping").content == b"x" * LIMIT


def test_mi3_body_one_byte_over_the_limit_is_refused_not_truncated():
    with client_for(fixed(b"x" * (LIMIT + 1))) as c, pytest.raises(HaldenResponseTooLarge):
        c.get("http://halden/halden/ping")


def test_mi3_declared_length_over_the_limit_is_refused_before_reading_the_body():
    stream = CountingStream(LIMIT + 100)

    def handler(request):
        return httpx.Response(200, headers={"Content-Length": str(LIMIT + 100)}, stream=stream)

    with client_for(handler) as c, pytest.raises(HaldenResponseTooLarge, match="declared"):
        c.get("http://halden/halden/stock")
    assert stream.pulled == 0


def test_mi3_streamed_body_without_declared_length_is_bounded_while_reading():
    stream = CountingStream(10 * LIMIT, chunk=16)
    with client_for(lambda request: httpx.Response(200, stream=stream)) as c, pytest.raises(HaldenResponseTooLarge):
        c.get("http://halden/halden/stock")
    assert stream.pulled == LIMIT // 16 + 1  # stopped at the first chunk past the limit; the rest never read


def test_mi3_streamed_body_without_declared_length_below_the_limit_passes():
    stream = CountingStream(LIMIT, chunk=16)
    with client_for(lambda request: httpx.Response(200, stream=stream)) as c:
        assert c.get("http://halden/halden/stock").content == b"a" * LIMIT


@pytest.mark.parametrize("value", ["abc", "-1", "", "10, 20", "1e3"])
def test_mi3_malformed_or_conflicting_content_length_is_refused(value):
    def handler(request):
        return httpx.Response(200, headers={"Content-Length": value}, stream=CountingStream(10))

    with client_for(handler) as c, pytest.raises(HaldenResponseTooLarge, match="Content-Length"):
        c.get("http://halden/halden/stock")


def test_mi3_repeated_identical_content_length_is_accepted():
    def handler(request):
        return httpx.Response(200, headers={"Content-Length": "10, 10"}, stream=CountingStream(10))

    with client_for(handler) as c:
        assert c.get("http://halden/halden/stock").content == b"a" * 10


def test_mi3_declared_length_below_the_limit_but_longer_body_is_still_refused():
    def handler(request):
        return httpx.Response(200, headers={"Content-Length": "8"}, stream=CountingStream(LIMIT * 4))

    with client_for(handler) as c, pytest.raises(HaldenResponseTooLarge):
        c.get("http://halden/halden/stock")


def test_mi3_compressed_body_expanding_beyond_the_limit_is_refused():
    packed = gzip.compress(b"a" * (LIMIT * 50))
    assert len(packed) < LIMIT
    with client_for(fixed(packed, {"Content-Encoding": "gzip"})) as c, pytest.raises(HaldenResponseTooLarge):
        c.get("http://halden/halden/stock")


def test_mi3_compressed_body_within_the_limit_is_decoded_once():
    packed = gzip.compress(b"hello halden")
    with client_for(fixed(packed, {"Content-Encoding": "gzip"})) as c:
        r = c.get("http://halden/halden/stock")
    assert r.content == b"hello halden" and "content-encoding" not in r.headers


# --------------------------------------------------------------------------------------------- classification


def oversized(obj: object):
    """A VALID JSON body that the bridge would accept, padded with trailing whitespace past the limit (so without the
    cap the pre-remediation code classifies it normally)."""
    body = json.dumps(obj).encode() + b" " * MAX_RESPONSE_BYTES
    return lambda request: httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


def test_mi3_oversized_e8_response_after_handoff_is_uncertain_with_one_post_and_no_resend():
    accepted = {"rc": 0, "order_no": "SO-240111", "total": "48.50"}  # would be ACCEPTED without the cap
    adapter, handler, sleeps = make({("POST", "/halden/orders"): [oversized(accepted)]})
    rendered, obs = handoff_after_preflight(adapter)
    assert obs.kind is K.UNCERTAIN  # the request may have reached HALDEN: backend outcome unknown (not ACCEPTED)
    assert obs.backend_order_ref is None
    assert obs.kind is not K.NOT_DELIVERED
    assert "HaldenResponseTooLarge" in (obs.detail or "")
    assert obs.payload_sha256_on_wire == rendered.payload_sha256  # the handoff attempt is recorded
    assert obs.response_sha256 is None  # no oversized body was captured
    assert handler.count("POST", "/halden/orders") == 1
    assert adapter.client.e8_request_count == 1
    assert sleeps == []


def test_mi3_oversized_read_fails_as_malformed_without_a_retry():
    adapter, handler, sleeps = make({("GET", "/halden/customers"): [oversized(CUSTOMER_417)]})
    with pytest.raises(IntegrationError) as exc:
        adapter.search_customers("Nordkap")
    assert exc.value.code is ErrorCode.BACKEND_MALFORMED
    assert handler.count("GET", "/halden/customers") == 1 and sleeps == []


def test_mi3_normal_read_still_works_through_the_bounded_transport():
    adapter, _, _ = make({("GET", "/halden/customers"): [httpx.Response(200, json=CUSTOMER_417)]})
    assert [a.ref.native_id for a in adapter.search_customers("Nordkap")] == ["00417"]


def test_mi3_oversized_reconciliation_read_is_read_failed_never_absence():
    adapter, _, _ = make({("GET", "/halden/orders"): [oversized({"data": {"orders": [order_row("SO-240111", CV)]},
                                                                    "continue": ""})],
                          ("GET", "/halden/orders/SO-240111"): [oversized({"rc": 404})]})
    result = adapter.reconcile(rq())
    assert result.verdict is ReconciliationVerdict.READ_FAILED
    assert result.absence_proven is False
