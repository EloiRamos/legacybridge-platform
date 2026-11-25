"""Independent HALDEN verification client: its own minimal E2/E6/E7/E8 client written from the frozen
API document ``docs/contracts/HALDEN_LEGACY_API.md`` — it shares no code with the v1 bridge or ``lbf``.

Used at quiescence to establish ground truth (which orders carry a reference) and by test harnesses to plant
orders outside the application (R10). It talks to HALDEN directly, never through the wire-tap, so its traffic is
never counted as application handoffs.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode

from lbf_oracles._http import request

FORM = "application/x-www-form-urlencoded; charset=UTF-8"
TICKET_HEADER = "X-Halden-Ticket"


class HaldenOracle:
    def __init__(self, base_url: str, login: str = "svc_bridge", secret: str = "halden-demo-secret") -> None:
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.secret = secret
        self._ticket: str | None = None

    # ------------------------------------------------------------------ session
    def _grant(self) -> str:
        body = urlencode([("login", self.login), ("secret", self.secret)]).encode()
        status, raw, _ = request("POST", f"{self.base_url}/halden/session", data=body, headers={"Content-Type": FORM})
        values = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)
        if status != 200 or values.get("STATUS") != "GRANTED":
            raise RuntimeError(f"HALDEN session refused (HTTP {status})")
        self._ticket = values["TICKET"].strip()
        return self._ticket

    def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        for _ in range(2):
            ticket = self._ticket or self._grant()
            url = f"{self.base_url}{path}" + (f"?{urlencode(params)}" if params else "")
            status, raw, _ = request("GET", url, headers={TICKET_HEADER: ticket})
            if status in (401, 403, 440):
                self._ticket = None
                continue
            if status != 200:
                raise RuntimeError(f"HALDEN GET {path} -> HTTP {status}")
            return json.loads(raw)
        raise RuntimeError("HALDEN refused the oracle ticket twice")

    # ------------------------------------------------------------------ reads
    def orders_for_account(self, acct: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(500):
            params = [("acct", str(int(acct)))] + ([("cursor", cursor)] if cursor else [])
            body = self._get("/halden/orders", params)
            rows.extend(body["data"]["orders"])
            cursor = body["continue"]
            if cursor == "":
                return rows
        raise RuntimeError("E6 paging did not terminate")

    def all_orders(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(5000):
            body = self._get("/halden/orders", [("cursor", cursor)] if cursor else None)
            rows.extend(body["data"]["orders"])
            cursor = body["continue"]
            if cursor == "":
                return rows
        raise RuntimeError("E6 paging did not terminate")

    def order(self, order_no: str) -> dict[str, Any] | None:
        body = self._get(f"/halden/orders/{quote(order_no, safe='')}")
        return body["order"] if body.get("rc") == 0 else None

    def orders_with_ref(self, ref: str, acct: str | None = None) -> list[str]:
        rows = self.orders_for_account(acct) if acct else self.all_orders()
        return sorted(str(r["OrderNo"]).strip() for r in rows if isinstance(r.get("Ref"), str)
                      and r["Ref"].strip() == ref)

    def stock_text(self) -> str:
        ticket = self._ticket or self._grant()
        status, raw, _ = request("GET", f"{self.base_url}/halden/stock", headers={TICKET_HEADER: ticket})
        if status != 200:
            raise RuntimeError(f"HALDEN stock -> HTTP {status}")
        return raw.decode("utf-8")

    # ------------------------------------------------------------------ harness actions (outside the application)
    def raw_e8(self, acct: str, ref: str, lines: list[tuple[str, int]]) -> tuple[int, bytes]:
        ticket = self._ticket or self._grant()
        form = [("acct", acct), ("ref", ref)] + [("item", i) for i, _ in lines] + [("qty", str(q)) for _, q in lines]
        status, raw, _ = request("POST", f"{self.base_url}/halden/orders", data=urlencode(form).encode(),
                                 headers={"Content-Type": FORM, TICKET_HEADER: ticket})
        return status, raw

    def sim_reset(self, **params: int) -> None:
        body = urlencode([(k, str(v)) for k, v in params.items()]).encode()
        status, raw, _ = request("POST", f"{self.base_url}/_sim/reset", data=body, headers={"Content-Type": FORM})
        if status != 200:
            raise RuntimeError(f"HALDEN reset -> HTTP {status}: {raw[:120]!r}")
        self._ticket = None

    def sim_fault(self, method: str, path_prefix: str, mode: str, count: int = 1, delay_ms: int = 0) -> None:
        form = [("method", method), ("path_prefix", path_prefix), ("mode", mode), ("count", str(count)),
                ("delay_ms", str(delay_ms))]
        status, raw, _ = request("POST", f"{self.base_url}/_sim/faults", data=urlencode(form).encode(),
                                 headers={"Content-Type": FORM})
        if status != 200:
            raise RuntimeError(f"HALDEN fault -> HTTP {status}: {raw[:120]!r}")

    def sim_clear_faults(self) -> None:
        request("DELETE", f"{self.base_url}/_sim/faults")
