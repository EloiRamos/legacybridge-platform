"""Pure, deterministic E8 rendering, byte-identical to v1 ``LegacyClient.post_order_once``."""

from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from lbf.domain.models import RenderedRequest, SubmissionRequest


def form_fields(request: SubmissionRequest) -> list[tuple[str, str]]:
    form: list[tuple[str, str]] = [("acct", str(request.native_customer_id)), ("ref", request.correlation_value)]
    form.extend(("item", line.native_item) for line in request.lines)
    form.extend(("qty", str(line.quantity_native)) for line in request.lines)
    return form


def render(request: SubmissionRequest, base_url: str) -> RenderedRequest:
    payload = urlencode(form_fields(request)).encode("utf-8")
    return RenderedRequest(
        request=request,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        transport_descriptor=f"HTTP POST {base_url.rstrip('/')}/halden/orders (E8, form-encoded)",
    )
