"""API end-to-end flows over the real app: session/CSRF lifecycle, the full write path through the API, permitted
actions computed server-side, customer search (BR-01), inventory (BR-02), queues, audit, readiness, metrics."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from api_support import ORIGIN, Api
from workflow_support import WF

pytestmark = pytest.mark.db
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


def test_login_session_and_logout(api):
    c = api.login("daniel")
    s = c.get("/api/v1/auth/session").json()
    assert s["username"] == "daniel.sales" and "draft.create" in s["permissions"] and s["csrf_token"]
    assert "lbf_session" in c.cookies
    raw = api.anon().post("/api/v1/auth/login", json={"username": "daniel.sales",
                                                      "password": "correct-horse-battery-staple"},
                          headers={"Origin": ORIGIN}).headers["set-cookie"]
    assert "HttpOnly" in raw and "SameSite=strict" in raw and "Path=/" in raw
    c.headers["X-CSRF-Token"] = s["csrf_token"]  # the latest token issued by GET /session
    assert c.post("/api/v1/auth/logout").status_code == 204
    assert c.get("/api/v1/auth/session").status_code == 401


def test_bad_login_is_generic(api):
    r = api.anon().post("/api/v1/auth/login", json={"username": "daniel.sales", "password": "wrong-password!"},
                        headers={"Origin": ORIGIN})
    assert r.status_code == 401 and r.json()["detail"] == "Invalid username or password."
    r2 = api.anon().post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong-password!"},
                         headers={"Origin": ORIGIN})
    assert r2.json()["detail"] == r.json()["detail"]


def test_full_write_path_through_the_api_with_server_side_actions(api):
    op = api.ready()
    assert "approve" not in api.detail(op, by="daniel")["permitted_actions"]  # own draft (SoD-1)
    assert {"approve", "decline"} <= set(api.detail(op, by="priya")["permitted_actions"])
    queue = api.as_("priya").get("/api/v1/approvals").json()["items"]
    assert [i["operation_id"] for i in queue] == [op]
    assert api.as_("daniel").get("/api/v1/approvals").json()["items"] == []
    r = api.as_("priya").post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    assert r.status_code == 200 and r.json()["state"] == "APPROVED"
    api.wf.step()
    d = api.detail(op)
    assert d["operation"]["state"] == "CONFIRMED" and d["operation"]["last_evidence"] == "READ_BACK"
    assert d["operation"]["verification_level"] == "VERIFIED_BY_READ"
    assert d["transmissions"][0]["payload_sha256"] and "payload" not in d["transmissions"][0]
    types = [e["event_type"] for e in d["timeline"]]
    assert "TRANSMISSION_STARTED" in types and "OUTCOME_RECORDED" in types
    assert d["permitted_actions"] == []


def test_unknown_operation_shows_only_read_only_actions_and_is_queued(api):
    op = api.ready()
    api.as_("priya").post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    api.wf.halden.next_e8_modes.append("lost_before_backend")
    api.wf.step()
    assert api.detail(op)["operation"]["state"] == "UNKNOWN"
    assert set(api.detail(op, by="daniel")["permitted_actions"]) == {"escalate", "recheck"}
    queue = api.as_("rafael").get("/api/v1/reconciliation").json()["items"]
    assert [i["operation_id"] for i in queue] == [op]
    assert api.as_("rafael").post(f"/api/v1/operations/{op}/recheck").status_code == 202
    assert api.as_("rafael").post(f"/api/v1/operations/{op}/recheck").status_code == 429  # 1 per 10 s


def test_br01_customer_search_matches_fixture(api):
    fixture = yaml.safe_load((REPO / "acceptance/fixtures/customer_expectations.yaml").read_text("utf-8"))
    c = api.as_("sanna")
    for case in fixture["searches"]:
        body = c.get("/api/v1/customers", params={"q": case["query"]}).json()
        got = sorted([a["backend_id"], a["native_id"]] for a in body["accounts"])
        assert got == sorted(case["accounts"]), case["query"]
        if "ambiguity_groups" in case:
            want = [[f"{b}:{n}" for b, n in g] for g in case["ambiguity_groups"]]
            assert body["ambiguity_groups"] == want, case["query"]
        if case.get("parties") == []:
            assert body["parties"] == []
        for p in case.get("parties", []):
            [party] = [x for x in body["parties"] if x["display_name"] == p["display_name"]]
            if "accounts" in p:
                assert sorted([a["backend_id"], a["native_id"]] for a in party["accounts"]) == sorted(p["accounts"])
            for code in p.get("conflicts", []):
                assert code in {x["code"] for x in party["conflicts"]}
        for native, hold in case.get("holds", {}).items():
            assert {a["native_id"]: a["credit_hold"] for a in body["accounts"]}[native] == hold


def test_br02_inventory_lists_each_backend_separately(api):
    body = api.as_("sanna").get("/api/v1/inventory").json()
    bw1001 = next(i for i in body["items"] if i["canonical_sku"] == "BW-1001")
    assert sorted(a["backend_id"] for a in bw1001["per_backend"]) == ["halden", "skarvik"]
    assert all(a["available_base"] is None or a["available_base"] >= 0 for a in bw1001["per_backend"])
    assert body["backend_errors"] == {}


def test_untrusted_backend_text_is_marked(api):
    body = api.as_("sanna").get("/api/v1/customers", params={"q": "lesund"}).json()
    [acct] = [a for a in body["accounts"] if a["native_id"] == "KND-0177"]
    assert "SYSTEM:" in acct["untrusted_text"]["notes"]


def test_dashboard_audit_verify_readiness_metrics(api):
    op = api.ready()
    dash = api.as_("marta").get("/api/v1/dashboard").json()
    assert dash["pending_approvals"] == 1 and {b["backend_id"] for b in dash["backends"]} == {"halden", "skarvik"}
    events = api.as_("helena").get("/api/v1/audit/events", params={"operation_id": op}).json()["events"]
    assert events[0]["event_type"] == "OPERATION_CREATED"
    v = api.as_("helena").get("/api/v1/audit/verify").json()
    assert v["valid"] is True and v["count"] >= len(events)
    ready = api.anon().get("/readyz")
    assert ready.status_code == 200 and ready.json()["db"] == "ok"
    m = api.anon().get("/metrics").text
    assert "lbf_operations{state=\"READY\"} 1.0" in m and "lbf_backend_up" in m
    assert api.anon().get("/healthz").json() == {"status": "ok"}


def test_problem_details_shape(api):
    r = api.as_("daniel").get("/api/v1/operations/00000000-0000-0000-0000-000000000000")
    body = r.json()
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert set(body) >= {"type", "title", "status", "code", "detail", "request_id"}
    assert body["request_id"] == r.headers["x-request-id"]


def test_inventory_annotation_requires_permission_and_is_audited(api):
    body = {"canonical_sku": "BW-1009", "backend_id": "halden", "body": "duplicate item record bw-1009 confirmed"}
    assert api.as_("daniel").post("/api/v1/inventory/annotations", json=body).status_code == 403
    r = api.as_("sanna").post("/api/v1/inventory/annotations", json=body)
    assert r.status_code == 201 and r.json()["annotation_id"]
    events = api.as_("helena").get("/api/v1/audit/events").json()["events"]
    assert "INVENTORY_ANNOTATED" in [e["event_type"] for e in events]
