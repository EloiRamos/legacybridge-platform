"""``CapturingTransport``: records the SHA-256 of the exact E8 request body put on the wire and of the
response body, per calling thread, around a retry-free ``httpx.HTTPTransport``.

It also bounds every HALDEN response body ("size limit at transport"): a declared
``Content-Length`` above ``max_response_bytes`` is refused before any body byte is read, and the body is otherwise
read incrementally and refused as soon as it exceeds the limit, so no oversized body is ever materialised. The limit
applies to the decoded body (what the bridge parses). A response is never truncated and passed on: it fails with
``HaldenResponseTooLarge`` (a ``LegacyMalformedResponse``). Consequences under the existing contracts:

* reads (E1–E7) fail with ``BACKEND_MALFORMED`` — not a transport error, so the v1 read retry does not repeat them;
* an E8 handoff whose response is refused has already been handed to the transport, so ``HaldenAdapter.handoff``
  records ``UNCERTAIN`` (backend outcome unknown) with the request hash — never ``NOT_DELIVERED`` — and nothing is
  resent (``post_order_once`` has no retry).
"""

from __future__ import annotations

import hashlib
import threading

import httpx

from lbf.adapters.halden.v1bridge.errors import LegacyMalformedResponse

# 1 MiB. The largest legitimate HALDEN bodies (E5 full stock extract, E3/E6 pages) are a few kilobytes against the
# simulator's seed data; the limit leaves two to three orders of magnitude of headroom and is fixed in code.
MAX_RESPONSE_BYTES = 1_048_576


class HaldenResponseTooLarge(LegacyMalformedResponse):
    """A HALDEN response body exceeded (or declared that it would exceed) the transport's size limit, or declared an
    unusable ``Content-Length``. The body is discarded; it is never truncated and treated as valid."""


def _declared_length(response: httpx.Response) -> int | None:
    """The declared body length, ``None`` when absent (e.g. chunked). Malformed values raise."""
    values = response.headers.get_list("content-length")
    if not values:
        return None
    distinct = {v.strip() for value in values for v in value.split(",")}
    if len(distinct) != 1 or not next(iter(distinct)).isdigit():
        raise HaldenResponseTooLarge("HALDEN ERP sent an unusable Content-Length header; the response was discarded.")
    return int(next(iter(distinct)))


class CapturingTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport | None = None, *,
                 max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self._inner = inner if inner is not None else httpx.HTTPTransport(retries=0)
        self._local = threading.local()
        self.max_response_bytes = max_response_bytes

    def reset_capture(self) -> None:
        self._local.request_sha256 = None
        self._local.response_sha256 = None
        self._local.requests = 0

    @property
    def last_request_sha256(self) -> str | None:
        return getattr(self._local, "request_sha256", None)

    @property
    def last_response_sha256(self) -> str | None:
        return getattr(self._local, "response_sha256", None)

    @property
    def requests_in_capture(self) -> int:
        return getattr(self._local, "requests", 0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        is_e8 = request.method == "POST" and request.url.path == "/halden/orders"
        if is_e8:
            body = request.read()
            self._local.request_sha256 = hashlib.sha256(body).hexdigest()
            self._local.requests = getattr(self._local, "requests", 0) + 1
        response = self._bounded(self._inner.handle_request(request), request)
        if is_e8:
            self._local.response_sha256 = hashlib.sha256(response.content).hexdigest()
        return response

    def _bounded(self, response: httpx.Response, request: httpx.Request) -> httpx.Response:
        limit = self.max_response_bytes
        try:
            declared = _declared_length(response)
            if declared is not None and declared > limit:
                raise HaldenResponseTooLarge(
                    f"HALDEN ERP declared a {declared}-byte response; the limit is {limit} bytes. Discarded unread.")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > limit:
                    raise HaldenResponseTooLarge(
                        f"HALDEN ERP response exceeded the {limit}-byte limit; discarded (never truncated).")
        finally:
            response.close()
        # The decoded body is handed on as-is; encoding/length headers now describe it.
        headers = [(k, v) for k, v in response.headers.multi_items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(response.status_code, headers=headers, content=bytes(body), request=request,
                              extensions=response.extensions)

    def close(self) -> None:
        self._inner.close()
