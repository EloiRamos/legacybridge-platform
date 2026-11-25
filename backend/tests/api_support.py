"""API test harness: the real FastAPI app over the workflow harness (PostgreSQL + offline backends + manual clock),
driven in-process with Starlette's TestClient (no sockets)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from fastapi.testclient import TestClient
from support import PASSWORD
from workflow_support import WF

from lbf.api.app import create_app
from lbf.api.services import AppConfig, AppServices
from lbf.auth.service import AddressRateLimiter, AuthService

ORIGIN = "http://testserver"


@cache
def _contract() -> list[tuple[re.Pattern[str], dict[str, Any]]]:
    from lbf.api.openapi import document

    return [(re.compile("^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(p)) + "$"), ops)
            for p, ops in document()["paths"].items()]


def documented(response: Any) -> None:
    """Every response of the API suites must be one the committed contract documents: status, media type, code."""
    path, method = response.request.url.path, response.request.method.lower()
    found = [ops[method] for rx, ops in _contract() if rx.match(path) and method in ops]
    if not found or (path.startswith("/api/v1/demo/") and response.status_code == 404):
        return  # not an API operation (unknown path, SPA), or the demo router is not mounted in this profile
    spec = found[0]["responses"].get(str(response.status_code))
    assert spec is not None, f"undocumented: {method} {path} -> {response.status_code}"
    content = spec.get("content")
    if content is None:
        return
    media = response.headers.get("content-type", "").split(";")[0]
    assert media in content, f"undocumented media type: {method} {path} {response.status_code} {media}"
    if media == "application/problem+json" and response.status_code != 503:
        response.read()
        codes = content[media]["schema"]["allOf"][1]["properties"]["code"]["enum"]
        assert response.json()["code"] in codes, f"undocumented code: {method} {path} {response.json()['code']}"


@dataclass
class Api:
    wf: WF
    profile: str = "test"
    clients: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config = AppConfig(profile=self.profile, public_origin=ORIGIN, engines_enabled=False)
        self.auth = AuthService(self.wf.engine, clock=self.wf.clock, limiter=AddressRateLimiter(limit=10_000))
        self.svc = AppServices(config=self.config, engine=self.wf.engine, audit_engine=None, auth=self.auth,
                               workflow=self.wf.ctx)
        self.app = create_app(self.svc)

    def anon(self) -> TestClient:
        return self._client()

    def _client(self) -> TestClient:
        client = TestClient(self.app, base_url=ORIGIN)
        client.event_hooks["response"].append(documented)
        return client

    def login(self, name: str) -> TestClient:
        username = {"daniel": "daniel.sales", "priya": "priya.sales", "marta": "marta.ops",
                    "rafael": "rafael.integration", "helena": "helena.compliance", "sanna": "sanna.inventory",
                    "tomas": "tomas.admin"}[name]
        client = self._client()
        r = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD},
                        headers={"Origin": ORIGIN})
        assert r.status_code == 200, r.text
        client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": r.json()["csrf_token"]})
        self.clients[name] = client
        return client

    def as_(self, name: str) -> TestClient:
        return self.clients.get(name) or self.login(name)

    def draft(self, by: str = "daniel", backend: str = "halden", customer: str = "00417", sku: str = "BW-1001",
              qty: int = 10) -> str:
        r = self.as_(by).post("/api/v1/operations", json={
            "backend_id": backend, "native_customer_id": customer, "lines": [{"canonical_sku": sku, "quantity": qty}]})
        assert r.status_code == 201, r.text
        return str(r.json()["operation_id"])

    def detail(self, op: str, by: str = "daniel") -> dict[str, Any]:
        r = self.as_(by).get(f"/api/v1/operations/{op}")
        assert r.status_code == 200, r.text
        return dict(r.json())

    def ready(self, **kw: Any) -> str:
        op = self.draft(**kw)
        r = self.as_("daniel").post(f"/api/v1/operations/{op}/submit", json={"expected_version": 1})
        assert r.status_code == 200 and r.json()["state"] == "READY", r.text
        return op

    def version_ref(self, op: str) -> dict[str, Any]:
        d = self.detail(op)
        v = d["versions"][-1]
        return {"version": v["version"], "content_hash": v["content_hash"]}
