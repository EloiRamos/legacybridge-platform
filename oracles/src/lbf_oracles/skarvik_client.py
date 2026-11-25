"""SKARVIK oracle client: the simulator's own ledgers via its control plane. Only ``oracles/`` and
the resilience/test harnesses may call these endpoints; the application never does (boundary test B-5)."""

from __future__ import annotations

from typing import Any

from lbf_oracles._http import get_json, post_json, request


class SkarvikOracle:
    def __init__(self, control_url: str) -> None:
        self.base = control_url.rstrip("/")

    # ------------------------------------------------------------------ ground truth
    def orders(self) -> list[dict[str, Any]]:
        return list(get_json(f"{self.base}/_sim/oracle/orders"))

    def received(self) -> list[dict[str, Any]]:
        return list(get_json(f"{self.base}/_sim/oracle/received"))

    def orders_for_message_prefix(self, prefix: str) -> list[str]:
        return [o["orderno"] for o in self.orders() if (o.get("msgid") or "").startswith(prefix)]

    def claims_for_message_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return [r for r in self.received() if (r.get("msgid") or "").startswith(prefix)]

    # ------------------------------------------------------------------ harness control (test profile)
    def reset(self) -> None:
        post_json(f"{self.base}/_sim/reset", {})

    def fault(self, name: str, value: Any = None, count: int = 1) -> dict[str, Any]:
        return dict(post_json(f"{self.base}/_sim/faults", {"fault": name, "value": value, "count": count}))

    def clear_faults(self) -> None:
        request("DELETE", f"{self.base}/_sim/faults")

    def pickup(self) -> list[str]:
        return list(post_json(f"{self.base}/_sim/pickup", {})["outcomes"])

    def extracts(self) -> None:
        post_json(f"{self.base}/_sim/extracts", {})

    def status(self) -> dict[str, Any]:
        return dict(get_json(f"{self.base}/_sim/status"))
