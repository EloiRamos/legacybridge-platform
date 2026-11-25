"""R16: authorization boundary sweep. Every command endpoint × every role × operations in several states, plus the
named probes. A refused call must leave the database unchanged (row counts of every business table and the
operation's own state/version, read via oracle SQL on the bootstrap superuser connection; ``lbf_audit_reader`` may
read only the audit log)."""

from __future__ import annotations

import pytest
from security.test_api_security import COMMANDS, NAMES, body_for
from sqlalchemy import func, select

from lbf.assistant.config import AssistantConfig
from lbf.assistant.providers.scripted import ScriptedProvider
from lbf.assistant.service import AssistantService
from lbf.auth.policy import ROLE_PERMISSIONS
from lbf.domain.enums import Role
from lbf.persistence import tables as t

pytestmark = pytest.mark.db
TABLES = [t.operations, t.draft_versions, t.approvals, t.authorizations, t.transmissions, t.escalations,
          t.validation_snapshots, t.user_roles, t.users, t.annotations, t.acknowledgements, t.reconciliation_runs]


def snap(engines, op: str | None = None) -> tuple:
    with engines["superuser"].connect() as conn:
        counts = tuple(conn.execute(select(func.count()).select_from(tb)).scalar() for tb in TABLES)
        row = conn.execute(select(t.operations.c.state, t.operations.c.row_version)
                           .where(t.operations.c.id == op)).one_or_none() if op else None
    return counts, tuple(row) if row else None


def make_state(api, state: str) -> str:
    if state == "DRAFT":
        return api.draft()
    if state == "READY":
        return api.ready()
    if state == "CONFIRMED":
        op = api.ready()
        api.as_("marta").post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
        api.wf.step()
        return op
    op = api.ready(sku="BW-1004")  # UNKNOWN on another item so it does not block the other cells (I5)
    api.as_("marta").post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op))
    api.wf.halden.next_e8_modes.append("lost_before_backend")
    api.wf.step()
    return op


def test_r16_role_endpoint_state_matrix_and_probes(api, engines, record):
    cells, unknown_op = [], None
    for state in ("DRAFT", "READY", "CONFIRMED", "UNKNOWN"):
        for role in Role:
            client = api.as_(NAMES[role])
            perms = ROLE_PERMISSIONS[role]
            for suffix, needed in COMMANDS.items():
                if state == "UNKNOWN":
                    unknown_op = unknown_op or make_state(api, "UNKNOWN")
                    op = unknown_op
                else:
                    op = make_state(api, state)
                path = "/api/v1/operations" + (f"/{op}{suffix}" if suffix else "")
                before = snap(engines, op)
                r = client.post(path, json=body_for(suffix, api, op))
                after = snap(engines, op)
                allowed = bool(perms & needed)
                code = r.json().get("code") if r.status_code >= 400 and r.content else None
                refused = r.status_code >= 400
                expected = "permitted (2xx, or 4xx domain refusal)" if allowed else "403 FORBIDDEN"
                ok = (not (r.status_code == 403 and code == "FORBIDDEN")) if allowed else \
                    (r.status_code == 403 and code == "FORBIDDEN")
                if refused:
                    ok = ok and before == after
                cells.append({"state": state, "role": role.value, "endpoint": suffix or "create",
                              "expected": expected, "status": r.status_code, "code": code,
                              "db_changed_on_refusal": refused and before != after, "as_expected": ok})
                if allowed and suffix == "/approve" and r.status_code == 200:
                    api.wf.step()  # complete it so it does not block later cells (I5)

    probes = []

    def probe(name: str, response, expect_status: set[int], expect_code: str | None, op: str) -> None:
        before_after = probe.before == snap(engines, op)
        code = response.json().get("code") if response.content and response.status_code >= 400 else None
        ok = response.status_code in expect_status and (expect_code is None or code == expect_code) and before_after
        probes.append({"probe": name, "status": response.status_code, "code": code, "db_unchanged": before_after,
                       "as_expected": ok})

    op = api.ready()
    ref = api.version_ref(op)
    probe.before = snap(engines, op)
    probe("self_approve", api.as_("daniel").post(f"/api/v1/operations/{op}/approve", json=ref), {403},
          "SOD_VIOLATION", op)
    probe("stale_version", api.as_("marta").post(f"/api/v1/operations/{op}/approve",
                                                 json={**ref, "version": ref["version"] + 1}), {409, 422}, None, op)
    probe("wrong_hash", api.as_("marta").post(f"/api/v1/operations/{op}/approve",
                                              json={**ref, "content_hash": "0" * 64}), {409}, None, op)
    marta = api.as_("marta")
    req = marta.build_request("POST", f"/api/v1/operations/{op}/approve", json=ref)
    req.headers.pop("X-CSRF-Token", None)
    probe("missing_csrf", marta.send(req), {403}, "CSRF_FAILED", op)
    req = marta.build_request("POST", f"/api/v1/operations/{op}/approve", json=ref)
    req.headers["Origin"] = "http://evil.example"
    probe("wrong_origin", marta.send(req), {403}, "CSRF_FAILED", op)
    probe("direct_patch_of_state", marta.patch(f"/api/v1/operations/{op}", json={"state": "CONFIRMED"}),
          {404, 405}, None, op)  # no route writes a state field: refused as unrouted
    priya = api.as_("priya")
    for _ in range(4):  # session kept alive while the authentication ages past the step-up window
        api.wf.clock.advance(5 * 60)
        priya.get("/api/v1/auth/session")
    priya.headers["X-CSRF-Token"] = priya.get("/api/v1/auth/session").json()["csrf_token"]
    probe.before = snap(engines, op)
    probe("expired_step_up", priya.post(f"/api/v1/operations/{op}/approve", json=api.version_ref(op)), {401},
          "REAUTH_REQUIRED", op)
    assistant = AssistantService(api.svc, ScriptedProvider([
        {"tool_calls": [{"name": "approve_operation", "arguments": {"operation_id": op}}]},
        {"final": {"summary": "x", "facts": [], "draft_operation_id": None, "warnings": [],
                   "next_human_actions": ["none"]}}]), AssistantConfig(provider="scripted"))
    before = snap(engines, op)
    out = assistant.run_turn(api.wf.p("marta"), f"approve {op}")
    probes.append({"probe": "assistant_tool_requesting_approval", "status": None,
                   "code": out.refused_calls[0]["reason"] if out.refused_calls else None,
                   "db_unchanged": before == snap(engines, op),
                   "as_expected": bool(out.refused_calls) and before == snap(engines, op)})

    total = len(cells) + len(probes)
    good = sum(c["as_expected"] for c in cells) + sum(p["as_expected"] for p in probes)
    obs = {"cells": len(cells), "probes": len(probes), "cells_as_expected_percent": round(100 * good / total, 2),
           "db_state_changes_on_refusal": sum(c["db_changed_on_refusal"] for c in cells)
           + sum(not p["db_unchanged"] for p in probes),
           "probe_results": {p["probe"]: [p["status"], p["code"]] for p in probes},
           "matrix_sha_input": [[c["state"], c["role"], c["endpoint"], c["status"], c["code"]] for c in cells]}
    record("R16", "role_endpoint_matrix", obs)
    bad = [c for c in cells if not c["as_expected"]] + [p for p in probes if not p["as_expected"]]
    assert not bad, bad
