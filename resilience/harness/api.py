"""HTTP client for the running console API as the demo personas (credentials from the git-ignored file)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
BASE = f"http://127.0.0.1:{os.environ.get('LBF_APP_PORT') or 8080}"
NS = os.environ.get("LBF_NS")
CREDENTIALS = REPO / "deploy/secrets" / (f"{NS}-credentials.txt" if NS else "demo-credentials.txt")
USERNAMES = {"daniel": "daniel.sales", "priya": "priya.sales", "marta": "marta.ops", "rafael": "rafael.integration",
             "helena": "helena.compliance", "sanna": "sanna.inventory", "tomas": "tomas.admin"}


def password(username: str) -> str:
    for line in CREDENTIALS.read_text().splitlines():
        if line.startswith(username + "\t"):
            return line.split("\t")[1]
    raise RuntimeError(f"no credentials for {username}; run make demo-users")


class Persona:
    """One signed-in persona. Logs in once per run (per-address login limit 10/min) and re-authenticates
    (step-up) when the last authentication is older than 10 minutes."""

    def __init__(self, name: str) -> None:
        self.username = USERNAMES[name]
        self.client = httpx.Client(base_url=BASE, timeout=30, headers={"Origin": BASE})
        self.authenticated = 0.0
        self._login()

    def _apply(self, r: httpx.Response) -> None:
        r.raise_for_status()
        self.client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        self.authenticated = time.monotonic()

    def _login(self) -> None:
        self._apply(self.client.post("/api/v1/auth/login", json={"username": self.username,
                                                                 "password": password(self.username)}))

    def fresh(self) -> None:
        if time.monotonic() - self.authenticated > 600:
            self._apply(self.client.post("/api/v1/auth/reauth", json={"password": password(self.username)}))

    def ensure_session(self) -> None:
        if self.client.get("/api/v1/auth/session").status_code == 401:
            self._login()
        else:
            self._apply(self.client.get("/api/v1/auth/session"))

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.get(path, **kw)

    def post(self, path: str, json: Any = None) -> httpx.Response:
        return self.client.post(path, json=json)


class Api:
    def __init__(self) -> None:
        self.p: dict[str, Persona] = {}

    def __call__(self, name: str) -> Persona:
        if name not in self.p:
            self.p[name] = Persona(name)
        return self.p[name]

    def draft(self, backend: str, customer: str, sku: str, qty: int, by: str = "daniel") -> str:
        r = self(by).post("/api/v1/operations", {"backend_id": backend, "native_customer_id": customer,
                                                 "lines": [{"canonical_sku": sku, "quantity": qty}]})
        r.raise_for_status()
        return str(r.json()["operation_id"])

    def detail(self, op: str, by: str = "marta") -> dict[str, Any]:
        r = self(by).get(f"/api/v1/operations/{op}")
        r.raise_for_status()
        return dict(r.json())

    def ready(self, backend: str, customer: str, sku: str, qty: int) -> str:
        op = self.draft(backend, customer, sku, qty)
        r = self("daniel").post(f"/api/v1/operations/{op}/submit", {"expected_version": 1})
        r.raise_for_status()
        assert r.json()["state"] == "READY", r.text
        return op

    def ref(self, op: str) -> dict[str, Any]:
        v = self.detail(op)["versions"][-1]
        return {"version": v["version"], "content_hash": v["content_hash"]}

    def approve(self, op: str, by: str = "marta", override_reason: str | None = None) -> httpx.Response:
        self(by).fresh()
        return self(by).post(f"/api/v1/operations/{op}/approve", {**self.ref(op), "override_reason": override_reason})

    def state(self, op: str) -> tuple[str, str | None]:
        o = self.detail(op)["operation"]
        return o["state"], o["last_evidence"]

    def wait_state(self, op: str, states: set[str], timeout: float) -> tuple[str, str | None]:
        deadline = time.monotonic() + timeout
        s = self.state(op)
        while s[0] not in states and time.monotonic() < deadline:
            time.sleep(0.3)
            s = self.state(op)
        return s
