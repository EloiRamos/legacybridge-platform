"""API security negative tests: SEC-01…SEC-09, SEC-15, SEC-18, SEC-19.

Endpoint lists are generated from the application's own OpenAPI document, so a new endpoint is covered
automatically. "DB unchanged" is checked by counting rows of every business table before and after.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import pytest
from api_support import ORIGIN, Api
from sqlalchemy import func, select
from support import PASSWORD
from workflow_support import WF

from lbf.auth.policy import ROLE_PERMISSIONS
from lbf.auth.service import AddressRateLimiter
from lbf.domain.enums import Permission as P
from lbf.domain.enums import Role
from lbf.persistence import tables as t

pytestmark = pytest.mark.db
PUBLIC = {("POST", "/api/v1/auth/login"), ("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/metrics"),
          ("GET", "/api/v1/openapi.json")}
BUSINESS = [t.operations, t.draft_versions, t.approvals, t.authorizations, t.transmissions, t.escalations,
            t.validation_snapshots, t.user_roles, t.users, t.annotations]


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


def operations(app) -> list[tuple[str, str]]:
    out = []
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            out.append((method.upper(), path))
    return sorted(out)


def concrete(path: str, op: str | None = None, user: str | None = None) -> str:
    path = path.replace("{operation_id}", op or str(uuid.uuid4())).replace("{user_id}", user or str(uuid.uuid4()))
    path = path.replace("{backend_id}", "halden").replace("{native_id}", "00417").replace("{role}", "sales_ops")
    return re.sub(r"\{[^}]+\}", "x", path)


def snapshot(api) -> dict[str, int]:
    with api.wf.engine.connect() as conn:
        return {tb.name: conn.execute(select(func.count()).select_from(tb)).scalar() for tb in BUSINESS}


def test_sec03_no_session_is_401_everywhere_non_public(api):
    anon = api.anon()
    checked = 0
    for method, path in operations(api.app):
        if (method, path) in PUBLIC:
            continue
        r = anon.request(method, concrete(path), json={}, headers={"Origin": ORIGIN})
        assert r.status_code == 401, (method, path, r.status_code)
        checked += 1
    assert checked >= 25


def test_sec01_missing_or_invalid_csrf_is_403_on_every_state_changing_endpoint(api):
    op = api.ready()
    marta = api.as_("marta")
    before = snapshot(api)
    unsafe = [(m, p) for m, p in operations(api.app)
              if m in ("POST", "PUT", "PATCH", "DELETE") and (m, p) not in PUBLIC]
    assert len(unsafe) >= 15
    for method, path in unsafe:
        for token in (None, "forged-token-value"):
            req = marta.build_request(method, concrete(path, op, str(api.wf.users["daniel"])), json={})
            req.headers.pop("X-CSRF-Token", None)  # never send the client's valid default token
            req.headers["Origin"] = ORIGIN
            if token is not None:
                req.headers["X-CSRF-Token"] = token
            r = marta.send(req)
            assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED", (method, path, token, r.text)
    assert snapshot(api) == before


def test_sec02_foreign_origin_is_403_even_with_a_valid_token(api):
    op = api.ready()
    marta = api.as_("marta")
    before = snapshot(api)
    for origin in ("http://evil.example", "null", None):
        req = marta.build_request("POST", f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
        req.headers.pop("Origin", None)
        if origin:
            req.headers["Origin"] = origin
        r = marta.send(req)
        assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
    login = api.anon().post("/api/v1/auth/login", json={"username": "marta.ops", "password": PASSWORD},
                            headers={"Origin": "http://evil.example"})
    assert login.status_code == 403
    assert snapshot(api) == before


def test_sec04_expired_idle_and_revoked_sessions_are_401(api):
    c = api.login("daniel")
    api.wf.clock.advance(31 * 60)
    assert c.get("/api/v1/auth/session").status_code == 401
    c2 = api.login("priya")
    s = c2.get("/api/v1/auth/session").json()
    c2.headers["X-CSRF-Token"] = s["csrf_token"]
    assert c2.post("/api/v1/auth/logout").status_code == 204
    assert c2.get("/api/v1/operations").status_code == 401
    c3 = api.login("marta")
    api.wf.clock.advance(8 * 3600 + 1)
    assert c3.get("/api/v1/operations").status_code == 401


def test_sec05_lockout_and_address_rate_limit(app_engine, tmp_path):
    api = Api(WF(app_engine, tmp_path))
    api.auth.limiter = AddressRateLimiter(limit=1000)
    anon = api.anon()
    for _ in range(5):
        assert anon.post("/api/v1/auth/login", json={"username": "daniel.sales", "password": "nope-nope-nope"},
                         headers={"Origin": ORIGIN}).status_code == 401
    locked = anon.post("/api/v1/auth/login", json={"username": "daniel.sales", "password": PASSWORD},
                       headers={"Origin": ORIGIN})
    assert locked.status_code == 401  # locked for 15 min even with the right password
    api.wf.clock.advance(16 * 60)
    assert anon.post("/api/v1/auth/login", json={"username": "daniel.sales", "password": PASSWORD},
                     headers={"Origin": ORIGIN}).status_code == 200
    api.auth.limiter = AddressRateLimiter(limit=10)
    codes = [anon.post("/api/v1/auth/login", json={"username": "x", "password": "y"},
                       headers={"Origin": ORIGIN}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_sec06_step_up_enforced_then_satisfied_by_reauth(api):
    op = api.ready()
    priya = api.as_("priya")
    for _ in range(4):  # keep the session alive (idle 30 min) while authentication ages past 15 min
        api.wf.clock.advance(5 * 60)
        priya.get("/api/v1/auth/session")
    s = priya.get("/api/v1/auth/session").json()
    priya.headers["X-CSRF-Token"] = s["csrf_token"]
    r = priya.post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    assert r.status_code == 401 and r.json()["code"] == "REAUTH_REQUIRED"
    re_auth = priya.post("/api/v1/auth/reauth", json={"password": PASSWORD})
    assert re_auth.status_code == 200
    priya.headers["X-CSRF-Token"] = re_auth.json()["csrf_token"]
    api.as_("daniel").post(f"/api/v1/operations/{op}/submit", json={"expected_version": 1})  # re-validate (> 10 min)
    ok = priya.post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    assert ok.status_code in (200, 409), ok.text  # step-up no longer the refusal reason
    assert ok.json().get("code") != "REAUTH_REQUIRED"


# --------------------------------------------------------------------------- SEC-07 role × endpoint matrix

COMMANDS = {  # endpoint suffix -> (permissions, any of)
    "": {P.DRAFT_CREATE},
    "/revisions": {P.DRAFT_EDIT}, "/submit": {P.DRAFT_SUBMIT},
    "/approve": {P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY},
    "/decline": {P.APPROVAL_GRANT_STANDARD, P.APPROVAL_GRANT_ANY},
    # the operation is created by another user, so own-draft withdrawal (draft.withdraw) does not apply
    "/withdraw": {P.APPROVAL_GRANT_ANY}, "/recheck": {P.RECONCILE_RECHECK},
    "/escalate": {P.ESCALATION_OPEN}, "/resolve": {P.ESCALATION_RESOLVE},
    "/withdraw-unclaimed": {P.SKARVIK_WITHDRAW_AUTHORIZE}, "/redelivery": {P.SKARVIK_REDELIVER_AUTHORIZE},
}
NAMES = {Role.OPS_MANAGER: "marta", Role.SALES_OPS: "priya", Role.INVENTORY_COORDINATOR: "sanna",
         Role.INTEGRATION_ENGINEER: "rafael", Role.COMPLIANCE_REVIEWER: "helena", Role.SYSADMIN: "tomas"}


def body_for(suffix: str, api: Api, op: str) -> dict:
    return {
        "": {"backend_id": "halden", "native_customer_id": "00417", "lines": [{"canonical_sku": "BW-1001",
                                                                                "quantity": 1}]},
        "/revisions": {"expected_version": 1, "backend_id": "halden", "native_customer_id": "00417",
                       "lines": [{"canonical_sku": "BW-1001", "quantity": 2}]},
        "/submit": {"expected_version": 1}, "/approve": api.version_ref(op),
        "/decline": {**api.version_ref(op), "reason": "matrix test decline"},
        "/escalate": {"note": "matrix"}, "/resolve": {"kind": "ATTESTED_BOOKED", "external_reference": "X-1",
                                                      "note": "matrix test attestation note"},
    }.get(suffix, {})


@pytest.mark.parametrize("role", list(Role))
def test_sec07_role_by_command_matrix(api, role):
    client = api.as_(NAMES[role])
    perms = ROLE_PERMISSIONS[role]
    rows = []
    for suffix, needed in COMMANDS.items():
        op = api.ready()
        path = "/api/v1/operations" + (f"/{op}{suffix}" if suffix else "")
        before = snapshot(api)
        r = client.post(path, json=body_for(suffix, api, op))
        allowed = bool(perms & needed)
        forbidden = r.status_code == 403 and r.json().get("code") == "FORBIDDEN"
        rows.append((suffix or "create", allowed, r.status_code))
        if allowed:
            assert not forbidden, (role, suffix, r.text)
        else:
            assert forbidden, (role, suffix, r.status_code, r.text)
            assert snapshot(api) == before, (role, suffix)
    reads = {"/api/v1/operations": P.OPERATION_READ, "/api/v1/customers?q=nordkap": P.CUSTOMER_READ,
             "/api/v1/inventory": P.INVENTORY_READ, "/api/v1/audit/events": P.AUDIT_READ,
             "/api/v1/audit/verify": P.AUDIT_VERIFY, "/api/v1/admin/users": P.ADMIN_USERS,
             "/api/v1/admin/ack-queue": P.ACKQUEUE_READ, "/api/v1/admin/engine": P.BACKEND_HEALTH_READ,
             "/api/v1/admin/config": P.ADMIN_CONFIG_READ}
    for path, needed in reads.items():
        r = client.get(path)
        assert (r.status_code != 403) == (needed in perms), (role, path, r.status_code)


def test_sec08_security_headers(api):
    for r in (api.anon().get("/healthz"), api.as_("daniel").get("/api/v1/operations")):
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert api.as_("daniel").get("/api/v1/operations").headers["cache-control"] == "no-store"


def test_sec09_sod_through_the_api(api):
    op = api.ready()
    r = api.as_("daniel").post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    assert r.status_code == 403 and r.json()["code"] == "SOD_VIOLATION"


def test_sec15_demo_router_absent_in_default_and_gated_in_test(app_engine, tmp_path):
    wf = WF(app_engine, tmp_path)
    default = Api(wf, profile="default")
    assert not [p for p in default.app.openapi()["paths"] if "/demo/" in p]
    assert default.as_("tomas").post("/api/v1/demo/reset").status_code == 404
    assert default.anon().post("/api/v1/demo/reset", headers={"Origin": ORIGIN}).status_code == 404
    s = default.as_("tomas").get("/api/v1/auth/session").json()
    assert "demo.control" not in s["permissions"]
    test = Api(wf, profile="test")
    assert [p for p in test.app.openapi()["paths"] if "/demo/" in p]
    assert test.as_("marta").post("/api/v1/demo/reset").status_code == 403
    assert "demo.control" in test.as_("tomas").get("/api/v1/auth/session").json()["permissions"]


def test_sec18_input_validation(api):
    c = api.as_("daniel")
    base = {"backend_id": "halden", "native_customer_id": "00417", "lines": [{"canonical_sku": "BW-1001",
                                                                               "quantity": 1}]}
    cases = [
        {**base, "role": "ops_manager"},  # extra field
        {**base, "lines": [{"canonical_sku": "BW-1001", "quantity": 0}]},
        {**base, "lines": [{"canonical_sku": "BW-1001", "quantity": 1000}]},
        {**base, "backend_id": "sap"},
        {**base, "delivery_note": "x" * 61},
        {**base, "lines": []},
    ]
    for body in cases:
        r = c.post("/api/v1/operations", json=body)
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION", body
    assert c.get("/api/v1/operations", params={"state": "HACKED"}).status_code == 422
    assert c.post(f"/api/v1/operations/{uuid.uuid4()}/approve",
                  json={"version": 1, "content_hash": "not-a-hash"}).status_code == 422


def test_sec19_admin_cannot_self_grant_or_approve(api):
    tomas = api.as_("tomas")
    me = str(api.wf.users["tomas"])
    r = tomas.post(f"/api/v1/admin/users/{me}/roles", json={"role": "ops_manager"})
    assert r.status_code == 403 and r.json()["code"] == "SOD_VIOLATION"
    op = api.ready()
    assert tomas.post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op)).status_code == 403
    other = str(api.wf.users["sanna"])
    assert tomas.post(f"/api/v1/admin/users/{other}/roles", json={"role": "sales_ops"}).status_code == 204


def test_admin_role_change_requires_step_up(api):
    tomas = api.as_("tomas")
    for _ in range(4):
        api.wf.clock.advance(timedelta(minutes=5).total_seconds())
        s = tomas.get("/api/v1/auth/session").json()
    tomas.headers["X-CSRF-Token"] = s["csrf_token"]
    r = tomas.post(f"/api/v1/admin/users/{api.wf.users['sanna']}/roles", json={"role": "sales_ops"})
    assert r.status_code == 401 and r.json()["code"] == "REAUTH_REQUIRED"
