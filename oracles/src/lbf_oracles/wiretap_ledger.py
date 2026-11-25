"""Wire-tap ledger reader: what reached the wire, independently of the application (E-04)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from lbf_oracles._http import post_json, request


@dataclass(frozen=True)
class WireRequest:
    seq: int
    method: str
    path: str
    body_sha256: str
    form_ref: str | None
    mode: str | None
    forwarded: bool | None  # None: no COMPLETED record yet
    status: int | None
    client_outcome: str | None

    @property
    def is_e8(self) -> bool:
        return self.method == "POST" and self.path == "/halden/orders"


def parse(jsonl: bytes) -> list[WireRequest]:
    received: dict[int, dict[str, Any]] = {}
    completed: dict[int, dict[str, Any]] = {}
    for line in jsonl.decode("utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        (received if rec["phase"] == "RECEIVED" else completed)[int(rec["seq"])] = rec
    out = []
    for seq in sorted(received):
        r, c = received[seq], completed.get(seq, {})
        out.append(WireRequest(seq=seq, method=r["method"], path=r["path"], body_sha256=r["body_sha256"],
                               form_ref=r.get("form_ref"), mode=r.get("mode"), forwarded=c.get("forwarded"),
                               status=c.get("status"), client_outcome=c.get("client_outcome")))
    return out


class WiretapOracle:
    def __init__(self, control_url: str) -> None:
        self.base = control_url.rstrip("/")

    def raw(self) -> bytes:
        status, body, _ = request("GET", f"{self.base}/_wiretap/ledger")
        if status != 200:
            raise RuntimeError(f"wire-tap ledger -> HTTP {status}")
        return body

    def requests(self) -> list[WireRequest]:
        return parse(self.raw())

    def e8_count(self, ref: str) -> int:
        return sum(1 for r in self.requests() if r.is_e8 and r.form_ref == ref)

    def e8_counts(self) -> Counter[str]:
        return Counter(r.form_ref or "" for r in self.requests() if r.is_e8)

    def reset(self) -> None:
        post_json(f"{self.base}/_wiretap/reset", {})

    def arm(self, mode: str, count: int = 1, method: str = "POST", path: str = "/halden/orders",
            hold_seconds: float = 120.0) -> None:
        post_json(f"{self.base}/_wiretap/modes", {"mode": mode, "count": count, "method": method, "path": path,
                                                   "hold_seconds": hold_seconds})
