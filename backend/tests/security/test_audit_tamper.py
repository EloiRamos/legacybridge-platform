"""V7-4 / R18 b and c: the standalone verifier (``oracles``, stdlib only) detects a superuser's modification of one
audit row at that row's ``seq`` and rejects an export truncated mid-file; an untampered export is VALID."""

from __future__ import annotations

import json

import pytest
from api_support import Api
from resilience_support import record_observation, verifier_label, verify
from sqlalchemy import text
from workflow_support import WF

pytestmark = pytest.mark.db


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


def export(api: Api) -> tuple[bytes, dict]:
    r = api.as_("helena").post("/api/v1/audit/exports")
    assert r.status_code == 200, r.text
    body = r.json()
    return body["jsonl"].encode("utf-8"), body["manifest"]


def populate(api: Api) -> None:
    for _ in range(3):
        op = api.ready()
        api.as_("marta").post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
        api.wf.step()


def test_untampered_export_is_valid_and_logged(api):
    populate(api)
    jsonl, manifest = export(api)
    v = verify(jsonl, manifest)
    assert v["verdict"] == "VALID" and v["count"] == manifest["count"] >= 10
    with api.wf.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM lbf.audit_exports")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM lbf.audit_events WHERE event_type = 'AUDIT_EXPORTED' "
                                 "AND seq > :h"), {"h": manifest["to_seq"]}).scalar() == 1


def test_r18b_superuser_tamper_is_detected_at_the_modified_seq(api, engines):
    populate(api)
    with engines["superuser"].begin() as conn:
        target = conn.execute(text("SELECT seq FROM lbf.audit_events WHERE event_type = 'APPROVAL_GRANTED' "
                                   "ORDER BY seq LIMIT 1")).scalar()
        conn.execute(text("SET LOCAL session_replication_role = replica"))  # a superuser bypasses the triggers
        conn.execute(text("UPDATE lbf.audit_events SET payload = payload || '{\"tampered\": true}'::jsonb "
                          "WHERE seq = :s"), {"s": target})
    jsonl, manifest = export(api)
    v = verify(jsonl, manifest)
    obs = {"verifier": verifier_label(v), "first_broken_seq": "the modified row's seq"
           if v["first_broken_seq"] == target else v["first_broken_seq"], "modified_seq": target,
           "reason": v["reason"]}
    record_observation("R18", "superuser_tamper", obs)
    assert v["verdict"] == "INVALID" and v["first_broken_seq"] == target, v


def test_r18c_truncated_export_is_rejected(api):
    populate(api)
    jsonl, manifest = export(api)
    assert verify(jsonl, manifest)["verdict"] == "VALID"
    cut = jsonl[: len(jsonl) // 2]
    v = verify(cut, manifest)
    cut_at_line = jsonl[: jsonl.rindex(b"\n", 0, len(jsonl) // 2) + 1]  # truncated on a line boundary
    v2 = verify(cut_at_line, manifest)
    obs = {"verifier": verifier_label(v), "verifier_line_boundary": verifier_label(v2), "reason": v["reason"],
           "bytes_kept": len(cut), "bytes_total": len(jsonl)}
    record_observation("R18", "truncated_export", obs)
    assert v["verdict"] == "INVALID" and v2["verdict"] == "INVALID" and obs["verifier"] == "MANIFEST_MISMATCH"
    assert json.loads(json.dumps(manifest))["jsonl_sha256"] != __import__("hashlib").sha256(cut).hexdigest()
