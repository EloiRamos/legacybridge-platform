"""HALDEN adapter tests: v1 behaviour cases ported to the adapter (port list in docs/contracts/HALDEN_ADAPTER.md) and
the write-path design checks (one POST per handoff, no retry, capture of the bytes handed to the transport,
pre-handler refusal classification, UNCERTAIN on ambiguous outcomes, hook semantics).

The real v1 ``LegacyClient`` runs unchanged under ``HaldenAdapter``; only the innermost HTTP transport is an
``httpx.MockTransport`` (offline). Port IDs (P-xx) refer to the traceability table.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from lbf.adapters.halden import render as halden_render
from lbf.adapters.halden.adapter import HaldenAdapter
from lbf.adapters.halden.mapping import credit_hold
from lbf.domain.enums import AvailabilityStatus, CreditHold, ReconciliationVerdict, TransportObservationKind
from lbf.domain.errors import ErrorCode, IntegrationError
from lbf.domain.models import (
    ReconciliationQuery,
    RenderedRequest,
    SubmissionLine,
    SubmissionRequest,
)

GRANT = "STATUS=GRANTED\r\nTICKET=ABCDEF0123456789ABCDEF0123456789\r\nEXPIRES_IN=900\r\n"
K = TransportObservationKind
V = ReconciliationVerdict
CV = "LB7K2M9QXA4P"


class Scripted:
    """MockTransport handler: a session grant plus a per-path queue of responses/exceptions (v1 test pattern)."""

    def __init__(self, routes: dict[tuple[str, str], list]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key == ("POST", "/halden/session") and key not in self.routes:
            return httpx.Response(200, text=GRANT)
        queue = self.routes[key]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if (r.method, r.url.path) == (method, path))


CUSTOMER_417 = {"rows": [{"acct": "00417", "nm": "Nordkap Marine Services AS", "hold": "N"}], "next_offset": None}


def make(routes: dict, **kwargs) -> tuple[HaldenAdapter, Scripted, list[float]]:
    handler = Scripted({("GET", "/halden/customers"): [httpx.Response(200, json=CUSTOMER_417)], **routes})
    sleeps: list[float] = []
    adapter = HaldenAdapter("http://halden", "svc_bridge", "halden-demo-secret", mappings=(),
                            inner_transport=httpx.MockTransport(handler), sleep=sleeps.append, **kwargs)
    return adapter, handler, sleeps


def request(lines: list[tuple[str, int]] | None = None) -> SubmissionRequest:
    return SubmissionRequest(
        operation_id=uuid.uuid4(), attempt_no=1, transmission_no=1, authorization_id=uuid.uuid4(),
        backend_id="halden", native_customer_id="00417",
        lines=[SubmissionLine(native_item=i, quantity_native=q) for i, q in (lines or [("BW-1001", 10)])],
        correlation_value=CV, message_id=None)


def handoff_after_preflight(adapter: HaldenAdapter, req: SubmissionRequest | None = None):
    req = req or request()
    assert adapter.preflight(req).ok
    rendered = adapter.render(req)
    return rendered, adapter.handoff(rendered)


# --------------------------------------------------------------------------- P-01 E8 kinds, exactly one request


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (httpx.ConnectError("refused"), K.NOT_DELIVERED),
        (httpx.ConnectTimeout("no route"), K.NOT_DELIVERED),
        (httpx.Response(401, text="NO TICKET"), K.REFUSED_PRE_PROCESSING),
        (httpx.Response(403, text="<html><body><h2>403 Forbidden</h2></body></html>"), K.REFUSED_PRE_PROCESSING),
        (httpx.Response(440, json={"fault": "TICKET_EXPIRED"}), K.REFUSED_PRE_PROCESSING),
        (httpx.Response(429, text="THROTTLED", headers={"X-Halden-Wait": "3"}), K.REFUSED_PRE_PROCESSING),
        (httpx.Response(200, json={"rc": 12, "msg": "ACCOUNT_ON_HOLD"}), K.REJECTED),
        (httpx.Response(409, text="SHORT:BW-1010:2"), K.REJECTED),
        (httpx.Response(200, json={"rc": 0, "order_no": "SO-240111", "total": "48.50"}), K.ACCEPTED),
        (httpx.ReadTimeout("lost"), K.UNCERTAIN),
        (httpx.RemoteProtocolError("reset"), K.UNCERTAIN),
        (httpx.Response(500, text="<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>"), K.UNCERTAIN),
        (httpx.Response(200, text="garbage"), K.UNCERTAIN),
        (httpx.Response(200, json={"rc": 0}), K.UNCERTAIN),
        (httpx.Response(409, text="CONFLICT"), K.UNCERTAIN),
        (httpx.Response(302, headers={"Location": "/elsewhere"}), K.UNCERTAIN),
    ],
)
def test_p01_handoff_kinds_with_exactly_one_post_no_relogin_no_wait(reply, kind):
    adapter, handler, sleeps = make({("POST", "/halden/orders"): [reply]})
    rendered, obs = handoff_after_preflight(adapter)
    assert obs.kind is kind
    assert handler.count("POST", "/halden/orders") == 1
    assert handler.count("POST", "/halden/session") == 1  # the preflight login only; none around the E8
    assert sleeps == []  # no wait-and-resend
    # The capture records the bytes handed to the HTTP transport (an attempt), not proof of receipt.
    assert obs.payload_sha256_on_wire == rendered.payload_sha256
    if kind is K.ACCEPTED:
        assert obs.backend_order_ref == "SO-240111"
    if obs.http_status == 409 and kind is K.REJECTED:
        assert obs.backend_code == "SHORT:BW-1010:2"


# --------------------------------------------------------------------------- P-02 positional form = rendered bytes


def test_p02_rendered_bytes_equal_the_v1_positional_form_on_the_wire():
    ok = httpx.Response(200, json={"rc": 0, "order_no": "SO-240111", "total": "1.00"})
    adapter, handler, _ = make({("POST", "/halden/orders"): [ok]})
    rendered, obs = handoff_after_preflight(adapter, request([("BW-1001", 10), ("BW-1004", 1)]))
    sent = handler.requests[-1]
    assert sent.content == rendered.payload
    assert sent.content.decode() == f"acct=00417&ref={CV}&item=BW-1001&item=BW-1004&qty=10&qty=1"
    assert sent.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert obs.kind is K.ACCEPTED
    assert halden_render.render(request([("BW-1001", 10), ("BW-1004", 1)]), "http://halden").payload == sent.content


# --------------------------------------------------------------------------- P-03 ticket discarded on refusal


@pytest.mark.parametrize("status", [401, 403, 440])
def test_p03_pre_handler_refusal_discards_ticket_and_next_preflight_logs_in_again(status):
    adapter, handler, _ = make({("POST", "/halden/orders"): [httpx.Response(status, text="refused")]})
    _, obs = handoff_after_preflight(adapter)
    assert obs.kind is K.REFUSED_PRE_PROCESSING
    assert adapter.preflight(request()).ok
    assert handler.count("POST", "/halden/session") == 2
    assert handler.count("POST", "/halden/orders") == 1  # refusal is never followed by a resend


# --------------------------------------------------------------------------- design: hook, integrity, no retry


def test_hook_ordinary_failure_sends_nothing_and_is_not_delivered():
    adapter, handler, _ = make({("POST", "/halden/orders"): [httpx.Response(200, json={"rc": 0})]})
    assert adapter.preflight(request()).ok
    adapter.before_publish = lambda r: (_ for _ in ()).throw(RuntimeError("pause point broken"))
    obs = adapter.handoff(adapter.render(request()))
    assert obs.kind is K.NOT_DELIVERED and handler.count("POST", "/halden/orders") == 0


class Kill(BaseException):
    pass


def test_kill_at_before_handoff_sends_nothing():
    adapter, handler, _ = make({("POST", "/halden/orders"): [httpx.Response(200, json={"rc": 0})]})
    assert adapter.preflight(request()).ok
    adapter.before_publish = lambda r: (_ for _ in ()).throw(Kill())
    with pytest.raises(Kill):
        adapter.handoff(adapter.render(request()))
    assert handler.count("POST", "/halden/orders") == 0


def test_bytes_differing_from_the_rendered_payload_are_downgraded_to_uncertain_integrity_alert():
    ok = httpx.Response(200, json={"rc": 0, "order_no": "SO-240111", "total": "1.00"})
    adapter, _, _ = make({("POST", "/halden/orders"): [ok]})
    assert adapter.preflight(request()).ok
    genuine = adapter.render(request())
    forged = RenderedRequest(request=genuine.request, payload=b"acct=1", payload_sha256="0" * 64,
                             transport_descriptor=genuine.transport_descriptor)
    obs = adapter.handoff(forged)
    assert obs.kind is K.UNCERTAIN and "INTEGRITY_ALERT" in obs.detail
    assert obs.payload_sha256_on_wire == genuine.payload_sha256


def test_timeout_after_send_is_uncertain_and_never_retried():
    adapter, handler, sleeps = make({("POST", "/halden/orders"): [httpx.ReadTimeout("lost")] * 3})
    _, obs = handoff_after_preflight(adapter)
    assert obs.kind is K.UNCERTAIN and handler.count("POST", "/halden/orders") == 1 and sleeps == []


def test_capability_gated_methods_do_no_io():
    adapter, handler, _ = make({})
    rendered = adapter.render(request())
    for call in (lambda: adapter.poll_acknowledgements(), lambda: adapter.withdraw_unclaimed("X"),
                 lambda: adapter.redeliver(rendered)):
        with pytest.raises(IntegrationError) as exc:
            call()
        assert exc.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert handler.requests == []


# --------------------------------------------------------------------------- P-04 … P-10 reconciliation (v1 rows)


def order_row(no: str, ref: str | None, acct: str = "417") -> dict:
    return {"OrderNo": no, "Acct": acct, "Date": "21.11.2026", "Stat": "OP", "Total": "48.50", "Ref": ref}


def detail(no: str, ref: str | None, lines: list[tuple[str, str]], acct: str = "417") -> httpx.Response:
    return httpx.Response(200, json={"rc": 0, "order": {**order_row(no, ref, acct),
                                                          "lines": [{"ln": i, "item": it, "qty": q, "price": "4.85"}
                                                                    for i, (it, q) in enumerate(lines, 1)]}})


def listing(*rows: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": {"orders": list(rows)}, "continue": ""})


def rq(anchor: str | None = None) -> ReconciliationQuery:
    from datetime import UTC, datetime

    req = request()
    return ReconciliationQuery(operation_id=req.operation_id, backend_id="halden", native_customer_id="00417",
                               correlation_value=CV, message_id=None, expected_lines=req.lines,
                               anchor_order_ref=anchor, first_handoff_at=datetime(2026, 11, 21, tzinfo=UTC),
                               attempt_no=1)


def test_p04_row1_anchored_read_back_matches():
    adapter, handler, _ = make({("GET", "/halden/orders/SO-240111"): [detail("SO-240111", CV, [("BW-1001", "10")])]})
    result = adapter.reconcile(rq(anchor="SO-240111"))
    assert (result.verdict, result.matched_order_ref) == (V.MATCHED, "SO-240111")
    assert handler.count("GET", "/halden/orders") == 0  # anchored: only the anchor is read (R-3)
    assert result.absence_proven is False and [s.source for s in result.source_reads] == ["halden:E7"]


def test_p05_row3_anchored_404_is_mismatch_never_a_different_order():
    adapter, _, _ = make({
        ("GET", "/halden/orders/SO-240111"): [httpx.Response(200, json={"rc": 404, "msg": "ORDER NOT ON FILE"})],
        ("GET", "/halden/orders"): [listing(order_row("SO-240112", CV))],
    })
    result = adapter.reconcile(rq(anchor="SO-240111"))
    assert result.verdict is V.MISMATCH and result.matched_order_ref is None


def test_p06_row2_read_back_transport_failure_is_read_failed():
    adapter, _, _ = make({("GET", "/halden/orders/SO-240111"): [httpx.ReadTimeout("x")] * 3})
    result = adapter.reconcile(rq(anchor="SO-240111"))
    assert result.verdict is V.READ_FAILED and result.absence_proven is False


def test_p07_row9_no_visible_order_is_no_match_and_never_absence():
    adapter, _, _ = make({("GET", "/halden/orders"): [listing(order_row("SO-240100", "OTHER"))]})
    result = adapter.reconcile(rq())
    assert result.verdict is V.NO_MATCH and result.absence_proven is False and result.absence_evidence is None


def test_p08_row10_two_orders_with_the_reference_are_ambiguous():
    adapter, _, _ = make({("GET", "/halden/orders"): [listing(order_row("SO-240111", CV), order_row("SO-240112", CV))]})
    result = adapter.reconcile(rq())
    assert result.verdict is V.AMBIGUOUS and result.candidates == ["SO-240111", "SO-240112"]
    assert result.matched_order_ref is None


def test_p09_row10_sole_candidate_with_different_lines_is_mismatch():
    adapter, _, _ = make({
        ("GET", "/halden/orders"): [listing(order_row("SO-240111", CV))],
        ("GET", "/halden/orders/SO-240111"): [detail("SO-240111", CV, [("BW-1004", "1")])],
    })
    result = adapter.reconcile(rq())
    assert result.verdict is V.MISMATCH and result.matched_order_ref is None


def test_p10_row11_listing_failures_are_read_failed():
    adapter, _, _ = make({("GET", "/halden/orders"): [httpx.Response(500, text="x")] * 3})
    assert adapter.reconcile(rq()).verdict is V.READ_FAILED


def test_p11_null_reference_is_a_non_match_and_padded_reference_matches_after_trim():
    adapter, _, _ = make({
        ("GET", "/halden/orders"): [listing(order_row("SO-240100", None), order_row("SO-240111", f" {CV} "))],
        ("GET", "/halden/orders/SO-240111"): [detail("SO-240111", f" {CV} ", [("bw-1001 ", "10")])],
    })
    result = adapter.reconcile(rq())
    assert (result.verdict, result.matched_order_ref) == (V.MATCHED, "SO-240111")


def test_p12_price_total_and_status_are_not_compared():
    row = detail("SO-240111", CV, [("BW-1001", "10")])
    body = row.json()
    body["order"].update({"Total": "999.99", "Stat": "SH"})
    adapter, _, _ = make({("GET", "/halden/orders/SO-240111"): [httpx.Response(200, json=body)]})
    assert adapter.reconcile(rq(anchor="SO-240111")).verdict is V.MATCHED


# --------------------------------------------------------------------------- P-13 … P-15 strict stock evidence


ITEMS = ('<?xml version="1.0" encoding="UTF-8"?><ITEMS page="1" pages="1">'
         '<ITEM id="1"><ITMNO>BW-1001</ITMNO><DESCR>Deep Groove Ball Bearing</DESCR><CATG>Bearings</CATG>'
         "<UPRICE>4.85</UPRICE><UOM>EA</UOM><AVAIL_FLG>Y</AVAIL_FLG><DISC_FLG>N</DISC_FLG><UPDTS>2025-07-14</UPDTS>"
         "</ITEM></ITEMS>")
HEADER = "ITEM;WH;ON_HAND;ALLOCATED\r\n"


def stock_adapter(body: str) -> HaldenAdapter:
    adapter, _, _ = make({("GET", "/halden/items"): [httpx.Response(200, content=ITEMS.encode())],
                          ("GET", "/halden/stock"): [httpx.Response(200, content=body.encode())]})
    return adapter


@pytest.mark.parametrize("body", [
    HEADER + "BW-1001;DUS;240;0\r\nBW-1001;MAN;60\r\n",  # three-field row (v1 F03-3)
    HEADER + " ;MAN;?;0\r\n",  # missing SKU (v1 F03-5)
    HEADER + "BW-1001;DUS;240;0\r\nBW-1001;;60;0\r\n",  # missing warehouse (v1 F03-6)
])
def test_p13_structural_extract_defect_yields_no_availability_verdict(body):
    with pytest.raises(IntegrationError) as exc:
        stock_adapter(body).stock()
    assert exc.value.code is ErrorCode.BACKEND_MALFORMED


def test_p14_negative_allocation_is_undetermined():
    [a] = stock_adapter(HEADER + "BW-1001;DUS;0;-2\r\n").stock().availability
    assert a.status is AvailabilityStatus.UNDETERMINED and a.available_base is None
    assert [(i.code, i.field) for i in a.issues] == [("NEGATIVE_QUANTITY", "allocated")]


def test_p15_complete_extract_sums_warehouses_live():
    [a] = stock_adapter(HEADER + "BW-1001;DUS;240;20\r\nBW-1001;MAN;60;0\r\n").stock().availability
    assert (a.status, a.available_base, a.freshness) == (AvailabilityStatus.IN_STOCK, 280, "LIVE")


@pytest.mark.parametrize(("raw", "hold"), [
    ("J", CreditHold.HOLD), ("Y", CreditHold.HOLD), ("1", CreditHold.HOLD), ("true", CreditHold.HOLD),
    ("N", CreditHold.CLEAR), ("nein", CreditHold.CLEAR), ("0", CreditHold.CLEAR),
    ("", CreditHold.UNKNOWN), (None, CreditHold.UNKNOWN), ("vielleicht", CreditHold.UNKNOWN),
])
def test_p16_credit_hold_spellings(raw, hold):
    assert credit_hold(raw) is hold
