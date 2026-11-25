"""The OpenAPI document states what the server does (authentication, permissions, errors, media types)."""

from __future__ import annotations

import pytest
from api_support import Api
from workflow_support import WF

from lbf.api.openapi import document

pytestmark = pytest.mark.db
DOC = document()
OPS = [(m, p, op) for p, ops in DOC["paths"].items() for m, op in ops.items()]
PUBLIC = {"healthz", "readyz", "metrics", "login"}
UUID0 = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


def url(path: str) -> str:
    for name, value in (("operation_id", UUID0), ("user_id", UUID0), ("backend_id", "halden"), ("native_id", "00417"),
                        ("role", "SALES_OPS")):
        path = path.replace("{" + name + "}", value)
    return path


def test_the_operation_surface_is_unchanged():
    assert len(OPS) == 41 and len({op["operationId"] for *_, op in OPS}) == 41


@pytest.mark.parametrize(("method", "path", "op"), OPS, ids=[op["operationId"] for *_, op in OPS])
def test_declared_authentication_is_what_the_server_enforces(api, method, path, op):
    anonymous = api.anon().request(method.upper(), url(path), headers={"Origin": "http://testserver"})
    if op["operationId"] in PUBLIC:
        assert op["security"] == [] and anonymous.status_code in (200, 422, 503)
        return
    assert op["security"] == [{"session": [], "csrf": []} if method != "get" else {"session": []}]
    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "UNAUTHENTICATED")
    if method != "get":
        c = api.login("tomas")
        c.headers.pop("X-CSRF-Token")
        refused = c.request(method.upper(), url(path))
        assert (refused.status_code, refused.json()["code"]) == (403, "CSRF_FAILED")


ROLES = ("daniel", "marta", "rafael", "helena", "sanna", "tomas")


@pytest.mark.parametrize(("method", "path", "op"), [x for x in OPS if x[2].get("x-permissions-any-of")],
                         ids=[x[2]["operationId"] for x in OPS if x[2].get("x-permissions-any-of")])
def test_declared_permissions_are_what_the_server_enforces(api, method, path, op):
    """A role is refused with FORBIDDEN exactly when it holds none of the declared permissions."""
    if path.startswith("/api/v1/demo/"):
        pytest.skip("would reach the simulator control plane")
    for name in ROLES:
        c = api.login(name)
        held = set(c.get("/api/v1/auth/session").json()["permissions"])
        c = api.login(name)
        r = c.request(method.upper(), url(path), json={} if method != "get" else None)
        forbidden = r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
        if not held & set(op["x-permissions-any-of"]):
            assert r.status_code in (403, 422), (name, r.status_code)  # the body is validated before the permission
            assert forbidden or r.json()["code"] == "VALIDATION"
        else:
            assert not forbidden, name


def test_problem_responses_use_one_schema_and_media_type():
    for _, path, op in OPS:
        for status, r in op["responses"].items():
            if int(status) >= 400 and path != "/readyz":
                assert list(r["content"]) == ["application/problem+json"], (path, status)
    assert "HTTPValidationError" not in DOC["components"]["schemas"]


def test_validation_problem_and_metrics_media_types(api):
    c = api.as_("daniel")
    r = c.get("/api/v1/customers?q=x")
    assert r.status_code == 422 and r.headers["content-type"] == "application/problem+json" and r.json()["fields"]
    assert api.anon().get("/metrics").headers["content-type"].startswith("text/plain")


def test_a_repeated_idempotency_key_returns_the_first_draft_even_with_another_body(api):
    """L-24, documented behaviour kept as it is: the key identifies the request, not its body."""
    c = api.as_("daniel")
    body = {"backend_id": "halden", "native_customer_id": "00417",
            "lines": [{"canonical_sku": "BW-1001", "quantity": 1}]}
    first = c.post("/api/v1/operations", json=body, headers={"Idempotency-Key": "k-1"})
    body["lines"][0]["quantity"] = 5
    second = c.post("/api/v1/operations", json=body, headers={"Idempotency-Key": "k-1"})
    assert (first.status_code, second.status_code) == (201, 201) and first.json() == second.json()
    assert api.detail(first.json()["operation_id"])["versions"][0]["content"]["lines"][0]["quantity_base"] == 1
    assert "also when the body differs" in DOC["paths"]["/api/v1/operations"]["post"]["description"]
