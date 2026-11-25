"""HTTP adapter for HALDEN ERP 3.1.

This is the only module that imports ``httpx`` and the only place where legacy wire formats
(key=value text, JSON envelopes, XML pages, the semicolon stock extract) are parsed.

``LegacyClient`` reaches the product endpoints E1–E8 only. ``SimControlClient`` reaches the
simulation control plane C1–C4 only and is used for demo reset, scenario setup and tests.
"""

from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode

import httpx

from .errors import (
    BridgeError,
    LegacyAuthError,
    LegacyMalformedResponse,
    LegacyRateLimited,
    LegacyUnavailable,
)
from .models import ORDER_NO_RE, E8Observation, RawStockRow

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"
TICKET_HEADER = "X-Halden-Ticket"
STOCK_HEADER = "ITEM;WH;ON_HAND;ALLOCATED"
PRE_HANDLER_STATUSES = (401, 403, 440, 429)
AUTH_STATUSES = (401, 403, 440)
SHORT_RE = re.compile(r"^SHORT:([^:]+):(-?\d+)$")
WAREHOUSE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_ITEM_PAGES = 50


def make_http_client(timeout: float, transport: httpx.BaseTransport | None) -> httpx.Client:
    """No redirects and no transport-level retries; nothing may replay a request."""
    return httpx.Client(
        transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
    )


def _excerpt(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _decode_json(response: httpx.Response) -> Any:
    try:
        return json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LegacyMalformedResponse(f"HALDEN ERP returned data the bridge cannot interpret ({exc}).") from exc


def classify_e8_response(response: httpx.Response) -> E8Observation:
    """Classify one E8-shaped response (also used for the C4 simulation channel)."""
    status = response.status_code
    try:
        text = response.content.decode("utf-8")
    except UnicodeDecodeError:
        text = response.content.decode("utf-8", errors="replace")
    excerpt = _excerpt(text)
    if status in PRE_HANDLER_STATUSES:
        return E8Observation(
            kind="PRE_HANDLER_REFUSAL",
            http_status=status,
            detail=f"HTTP {status} from the gateway before any order logic ran.",
            body_excerpt=excerpt,
        )
    if status == 409:
        match = SHORT_RE.match(text.strip())
        if match:
            return E8Observation(
                kind="SHORT",
                http_status=status,
                short_item=match.group(1),
                short_available=int(match.group(2)),
                detail=f"HTTP 409 {text.strip()}",
                body_excerpt=excerpt,
            )
        return E8Observation(kind="UNCERTAIN", http_status=status, detail="HTTP 409 without the SHORT form.", body_excerpt=excerpt)
    if status == 200:
        try:
            body = json.loads(text)
        except ValueError:
            return E8Observation(kind="UNCERTAIN", http_status=status, detail="HTTP 200 with an unparseable body.", body_excerpt=excerpt)
        rc = body.get("rc") if isinstance(body, dict) else None
        if isinstance(rc, int) and not isinstance(rc, bool):
            if rc != 0:
                msg = body.get("msg")
                return E8Observation(
                    kind="REJECTED",
                    http_status=status,
                    rc=rc,
                    msg=msg if isinstance(msg, str) else None,
                    detail=f"HTTP 200 rc {rc} {msg}",
                    body_excerpt=excerpt,
                )
            order_no = body.get("order_no")
            total = body.get("total")
            if isinstance(order_no, str) and ORDER_NO_RE.match(order_no):
                return E8Observation(
                    kind="ACCEPTED",
                    http_status=status,
                    rc=0,
                    order_no=order_no,
                    total=total if isinstance(total, str) else None,
                    detail=f"HTTP 200 rc 0 order_no {order_no}",
                    body_excerpt=excerpt,
                )
            return E8Observation(kind="UNCERTAIN", http_status=status, rc=0, detail="HTTP 200 rc 0 without a valid order_no.", body_excerpt=excerpt)
        return E8Observation(kind="UNCERTAIN", http_status=status, detail="HTTP 200 without an integer rc.", body_excerpt=excerpt)
    return E8Observation(kind="UNCERTAIN", http_status=status, detail=f"HTTP {status}.", body_excerpt=excerpt)


def parse_stock_extract(text: str) -> list[RawStockRow]:
    """Strict E5 parser: any structural or identity defect makes the whole extract malformed."""
    body = text[:-2] if text.endswith("\r\n") else text
    lines = body.split("\r\n")
    if lines[0] != STOCK_HEADER:
        raise LegacyMalformedResponse("Stock extract unreadable: unexpected header line.")
    rows: list[RawStockRow] = []
    for number, line in enumerate(lines[1:], start=2):
        fields = line.split(";")
        if len(fields) != 4:
            raise LegacyMalformedResponse(
                f"Stock extract unreadable: line {number} has {len(fields)} fields instead of 4."
            )
        item, wh, on_hand, allocated = fields
        if not item.strip():
            raise LegacyMalformedResponse(f"Stock extract unreadable: line {number} has no item number.")
        if not wh.strip() or not WAREHOUSE_RE.match(wh.strip()):
            raise LegacyMalformedResponse(f"Stock extract unreadable: line {number} has no valid warehouse.")
        rows.append(RawStockRow(item=item, wh=wh, on_hand=on_hand, allocated=allocated))
    return rows


def parse_items_page(content: bytes) -> tuple[list[dict[str, Any]], int]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise LegacyMalformedResponse(f"HALDEN ERP returned an unreadable item page ({exc}).") from exc
    if root.tag != "ITEMS":
        raise LegacyMalformedResponse("HALDEN ERP returned an item page without an ITEMS root.")
    try:
        pages = int(root.attrib["pages"])
    except (KeyError, ValueError) as exc:
        raise LegacyMalformedResponse("HALDEN ERP returned an item page without an integer pages attribute.") from exc
    items: list[dict[str, Any]] = []
    for element in root.findall("ITEM"):
        try:
            record_id = int(element.attrib["id"])
        except (KeyError, ValueError) as exc:
            raise LegacyMalformedResponse("HALDEN ERP returned an item without an integer id.") from exc

        def field(tag: str) -> str | None:
            child = element.find(tag)
            return None if child is None else (child.text or "")

        items.append(
            {
                "id": record_id,
                "item_no": field("ITMNO"),
                "descr": field("DESCR"),
                "catg": field("CATG"),
                "price": field("UPRICE"),
                "uom": field("UOM"),
                "avail": field("AVAIL_FLG"),
                "disc": field("DISC_FLG"),
                "upd": field("UPDTS"),
            }
        )
    return items, pages


class LegacyClient:
    """Product-interface client for E1–E8."""

    def __init__(
        self,
        base_url: str,
        login: str,
        secret: str,
        *,
        timeout: float = 5.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._login_name = login
        self._secret = secret
        self.max_retries = max_retries
        self._sleep = sleep
        self._http = make_http_client(timeout, transport)
        self._ticket_lock = threading.Lock()
        self._ticket: str | None = None
        self._ticket_expires_at = 0.0
        self._counter_lock = threading.Lock()
        self.e8_request_count = 0

    # ------------------------------------------------------------------ tickets

    def _login(self) -> str:
        """E2. A read-class request: transport errors and 5xx are retried."""
        attempts = 1 + self.max_retries
        payload = urlencode([("login", self._login_name), ("secret", self._secret)])
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.post(
                    f"{self.base_url}/halden/session",
                    content=payload.encode("utf-8"),
                    headers={"Content-Type": FORM_CONTENT_TYPE},
                )
            except httpx.TransportError:
                if attempt < attempts:
                    self._sleep(0.25 * 2 ** (attempt - 1))
                    continue
                raise LegacyUnavailable(f"HALDEN ERP is not responding (timeout after {attempts} attempts).")
            if response.status_code >= 500:
                if attempt < attempts:
                    self._sleep(0.25 * 2 ** (attempt - 1))
                    continue
                raise LegacyUnavailable(f"HALDEN ERP is not responding (HTTP {response.status_code} after {attempts} attempts).")
            if response.status_code == 429:
                raise LegacyRateLimited()
            values: dict[str, str] = {}
            for line in response.content.decode("utf-8", errors="replace").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
            if response.status_code != 200 or values.get("STATUS") != "GRANTED":
                raise LegacyAuthError(
                    "HALDEN ERP rejected the integration credentials. Check LEGACY_LOGIN/LEGACY_SECRET."
                )
            ticket = values.get("TICKET")
            try:
                expires_in = int(values.get("EXPIRES_IN", ""))
            except ValueError:
                raise LegacyAuthError("HALDEN ERP granted a session without a valid EXPIRES_IN.")
            if not ticket:
                raise LegacyAuthError("HALDEN ERP granted a session without a ticket.")
            self._ticket = ticket
            self._ticket_expires_at = time.monotonic() + expires_in
            return ticket
        raise LegacyUnavailable()  # pragma: no cover - loop always returns or raises

    def _current_ticket(self, *, min_validity: float | None = None) -> str:
        """Lazy login. Reads use the cached ticket and rely on the gateway's verdict (one re-login);
        pre-send preparation additionally renews a ticket within ``min_validity`` seconds of expiry."""
        with self._ticket_lock:
            if self._ticket is None or (
                min_validity is not None and time.monotonic() + min_validity >= self._ticket_expires_at
            ):
                return self._login()
            return self._ticket

    def _discard_ticket(self, ticket: str | None) -> None:
        with self._ticket_lock:
            if ticket is None or self._ticket == ticket:
                self._ticket = None
                self._ticket_expires_at = 0.0

    # ------------------------------------------------------------------ reads

    def _get(self, path: str, params: Iterable[tuple[str, str]] | None = None, *, auth: bool = True) -> httpx.Response:
        attempts = 1 + self.max_retries
        url = f"{self.base_url}{path}"
        attempt = 0
        relogged = False
        last_status: int | None = None
        while True:
            attempt += 1
            headers = {}
            ticket = None
            if auth:
                ticket = self._current_ticket()
                headers[TICKET_HEADER] = ticket
            try:
                response = self._http.get(url, params=list(params or []), headers=headers)
            except httpx.TransportError as exc:
                if attempt < attempts:
                    self._sleep(0.25 * 2 ** (attempt - 1))
                    continue
                kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection failure"
                raise LegacyUnavailable(f"HALDEN ERP is not responding ({kind} after {attempts} attempts).") from exc
            status = response.status_code
            last_status = status
            if status >= 500:
                if attempt < attempts:
                    self._sleep(0.25 * 2 ** (attempt - 1))
                    continue
                raise LegacyUnavailable(f"HALDEN ERP is not responding (HTTP {status} after {attempts} attempts).")
            if auth and status in AUTH_STATUSES:
                self._discard_ticket(ticket)
                if not relogged:
                    relogged = True
                    attempt -= 1  # the single re-login and resend does not consume a read attempt
                    continue
                raise LegacyAuthError(
                    f"HALDEN ERP rejected the integration credentials (HTTP {status}). Check LEGACY_LOGIN/LEGACY_SECRET."
                )
            if status == 429:
                if attempt < attempts:
                    try:
                        wait = int(response.headers.get("X-Halden-Wait") or 1)
                    except ValueError:
                        wait = 1
                    self._sleep(min(max(wait, 1), 5))
                    continue
                raise LegacyRateLimited()
            if status != 200:
                raise LegacyMalformedResponse(f"HALDEN ERP answered HTTP {status} to {path}.")
            return response
        raise LegacyUnavailable(f"HTTP {last_status}")  # pragma: no cover

    def ping(self) -> str:
        return self._get("/halden/ping", auth=False).content.decode("utf-8").strip()

    def search_customers_raw(self, name: str | None = None, acct: int | str | None = None, offset: int = 0) -> dict:
        params: list[tuple[str, str]] = []
        if name is not None:
            params.append(("name", name))
        if acct is not None:
            params.append(("acct", str(acct)))
        params.append(("offset", str(offset)))
        body = _decode_json(self._get("/halden/customers", params))
        if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
            raise LegacyMalformedResponse("HALDEN ERP returned a customer page without rows.")
        next_offset = body.get("next_offset")
        if next_offset is not None and not isinstance(next_offset, int):
            raise LegacyMalformedResponse("HALDEN ERP returned a customer page with an invalid next_offset.")
        return body

    def list_items_raw(self, page: int = 1) -> tuple[list[dict[str, Any]], int]:
        return parse_items_page(self._get("/halden/items", [("page", str(page))]).content)

    def stock_raw(self, item: str | None = None) -> list[RawStockRow]:
        params = [("item", item)] if item is not None else []
        response = self._get("/halden/stock", params)
        try:
            text = response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LegacyMalformedResponse("Stock extract unreadable: not UTF-8.") from exc
        return parse_stock_extract(text)

    def list_orders_raw(self, acct: int | str | None = None, cursor: str | None = None) -> dict:
        params: list[tuple[str, str]] = []
        if acct is not None:
            params.append(("acct", str(acct)))
        if cursor:
            params.append(("cursor", cursor))
        body = _decode_json(self._get("/halden/orders", params))
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list) or not isinstance(body.get("continue"), str):
            raise LegacyMalformedResponse("HALDEN ERP returned an order listing in an unexpected shape.")
        return body

    def get_order_raw(self, order_no: str) -> dict:
        """E7. ``{"rc": 404, …}`` is returned as data; it never raises not-found."""
        body = _decode_json(self._get(f"/halden/orders/{quote(order_no.strip(), safe='')}"))
        rc = body.get("rc") if isinstance(body, dict) else None
        if not isinstance(rc, int) or isinstance(rc, bool):
            raise LegacyMalformedResponse("HALDEN ERP returned an order detail without an integer rc.")
        if rc == 0 and not isinstance(body.get("order"), dict):
            raise LegacyMalformedResponse("HALDEN ERP returned rc 0 without an order object.")
        return body

    # ------------------------------------------------------------------ write path

    def prepare_write(self, account_id: str) -> dict:
        """Pre-send preparation (reads only): ticket assurance plus one authenticated E3 ``acct`` read."""
        self._current_ticket(min_validity=30.0)
        return self.search_customers_raw(acct=int(account_id))

    def post_order_once(self, acct: str, ref: str, lines: Iterable[tuple[str, int]]) -> E8Observation:
        """Hand exactly one E8 request to the transport. No retry, no re-login, no 429 wait."""
        form: list[tuple[str, str]] = [("acct", str(acct)), ("ref", ref)]
        line_list = list(lines)
        form.extend(("item", sku) for sku, _ in line_list)
        form.extend(("qty", str(qty)) for _, qty in line_list)
        with self._ticket_lock:
            ticket = self._ticket
        headers = {"Content-Type": FORM_CONTENT_TYPE}
        if ticket:
            headers[TICKET_HEADER] = ticket
        with self._counter_lock:
            self.e8_request_count += 1
        try:
            response = self._http.post(
                f"{self.base_url}/halden/orders", content=urlencode(form).encode("utf-8"), headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return E8Observation(kind="NOT_DELIVERED", detail=f"No connection to HALDEN ERP ({type(exc).__name__}).")
        except httpx.HTTPError as exc:
            return E8Observation(kind="UNCERTAIN", detail=f"Transport error after connecting ({type(exc).__name__}).")
        observation = classify_e8_response(response)
        if response.status_code in AUTH_STATUSES:
            self._discard_ticket(ticket)
        return observation

    def close(self) -> None:
        self._http.close()


class SimControlClient:
    """Simulation control plane C1–C4 (demo reset, scenario setup and tests only)."""

    def __init__(self, base_url: str, *, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = make_http_client(timeout, transport)

    def _request(self, method: str, path: str, form: list[tuple[str, str]] | None = None) -> httpx.Response:
        try:
            response = self._http.request(
                method,
                f"{self.base_url}{path}",
                content=urlencode(form or []).encode("utf-8") if form is not None else None,
                headers={"Content-Type": FORM_CONTENT_TYPE} if form is not None else None,
            )
        except httpx.TransportError as exc:
            raise LegacyUnavailable(f"Simulation control is not responding ({type(exc).__name__}).") from exc
        if response.status_code == 404:
            raise BridgeError("SIMULATION_UNAVAILABLE", "Requires simulator control (HALDEN_SIM_CONTROL=true).")
        return response

    def _json(self, response: httpx.Response) -> dict:
        if response.status_code != 200:
            raise BridgeError("SIMULATION_REJECTED", f"Simulation control answered HTTP {response.status_code}: {_excerpt(response.text)}")
        body = _decode_json(response)
        if not isinstance(body, dict):
            raise LegacyMalformedResponse("Simulation control returned an unexpected body.")
        return body

    def reset(self, rate_limit: int | None = None, ticket_ttl_sec: int | None = None, rate_window_sec: int | None = None) -> bool:
        form = [
            (name, str(value))
            for name, value in (("rate_limit", rate_limit), ("ticket_ttl_sec", ticket_ttl_sec), ("rate_window_sec", rate_window_sec))
            if value is not None
        ]
        try:
            body = self._json(self._request("POST", "/_sim/reset", form))
        except BridgeError as exc:
            if exc.code == "SIMULATION_UNAVAILABLE":
                return False
            raise
        return body.get("reset") is True

    def arm_fault(self, method: str, path_prefix: str, mode: str, count: int = 1, delay_ms: int = 0) -> int:
        body = self._json(
            self._request(
                "POST",
                "/_sim/faults",
                [("method", method), ("path_prefix", path_prefix), ("mode", mode), ("count", str(count)), ("delay_ms", str(delay_ms))],
            )
        )
        return int(body.get("armed", 0))

    def list_faults(self) -> list[dict]:
        return list(self._json(self._request("GET", "/_sim/faults")).get("faults", []))

    def clear_faults(self) -> None:
        self._json(self._request("DELETE", "/_sim/faults"))

    def external_order(self, acct: str, item: str, qty: int) -> E8Observation:
        response = self._request("POST", "/_sim/external-order", [("acct", str(acct)), ("item", item), ("qty", str(qty))])
        return classify_e8_response(response)

    def close(self) -> None:
        self._http.close()
