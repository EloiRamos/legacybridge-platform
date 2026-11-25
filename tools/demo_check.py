"""The complete demonstration, run and asserted against the isolated stack (tools/isolated.sh).

Drives the public HTTP API as the fictional personas and compares the outcome with the simulators' own records, never
with the application's word alone. No paid call is made: the assistant answers in its deterministic mode.
Run through ``tools/isolated.sh check``. Exit 0: every step held.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from lbf_oracles.halden_client import HaldenOracle
from lbf_oracles.skarvik_client import SkarvikOracle

REPO = Path(__file__).resolve().parents[1]
BASE = f"http://127.0.0.1:{os.environ['LBF_APP_PORT']}"
halden = HaldenOracle(f"http://127.0.0.1:{os.environ['LBF_HALDEN_PORT']}")
skarvik = SkarvikOracle(f"http://127.0.0.1:{os.environ['LBF_SKARVIK_PORT']}")
passed: list[str] = []


class Persona:
    def __init__(self, username: str, password: str) -> None:
        self.http = httpx.Client(base_url=BASE, headers={"Origin": BASE}, timeout=30)
        self.http.headers["LBF-Assistant-Version"] = "2"  # receipts and proposals as typed fields
        r = self.http.post("/api/v1/auth/login", json={"username": username, "password": password})
        for _ in range(60):
            if r.status_code != 429:  # the login limiter is on here too: seven sign-ins use most of its minute
                break
            time.sleep(2)
            r = self.http.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, (username, r.status_code)
        self.http.headers["X-CSRF-Token"] = r.json()["csrf_token"]

    def get(self, path: str) -> Any:
        r = self.http.get("/api/v1" + path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        return r.json()

    def post(self, path: str, body: Any = None, expect: int = 200) -> Any:
        r = self.http.post("/api/v1" + path, json=body)
        assert r.status_code == expect, (path, r.status_code, r.text[:300])
        return r.json() if r.content else None


def ok(text: str) -> None:
    passed.append(text)
    print(f"ok  {text}", flush=True)


def until(read: Callable[[], Any], want: Callable[[Any], bool], seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if want(value):
            return value
        assert time.monotonic() < deadline, f"not reached within {seconds} s: {what}"
        time.sleep(0.2)


def order(by: Persona, backend: str, customer: str, qty: int = 10) -> tuple[str, dict[str, Any], str]:
    d = by.post("/operations", {"backend_id": backend, "native_customer_id": customer,
                                "lines": [{"canonical_sku": "BW-1001", "quantity": qty}]}, 201)
    s = by.post(f"/operations/{d['operation_id']}/submit", {"expected_version": 1})
    assert s["state"] == "READY", s
    cv = by.get(f"/operations/{d['operation_id']}")["operation"]["correlation_value"]
    return d["operation_id"], {"version": 1, "content_hash": d["content_hash"]}, cv


def transitions(by: Persona, op: str) -> list[str]:
    return [f"{e['state']['from']}->{e['state']['to']}" for e in by.get(f"/operations/{op}")["timeline"] if e["state"]]


def state(by: Persona, op: str) -> str:
    return str(by.get(f"/operations/{op}")["operation"]["state"])


def main(credentials: str) -> int:
    lines = [ln.split("\t") for ln in Path(credentials).read_text().splitlines() if "\t" in ln]
    who = {ln[0].split(".")[0]: Persona(ln[0], ln[1]) for ln in lines}
    daniel, priya, marta, sanna, helena, tomas = (who[n] for n in ("daniel", "priya", "marta", "sanna", "helena", "tomas"))
    skarvik.clear_faults()
    halden.sim_clear_faults()
    halden.sim_reset(rate_limit=100000)

    # 1 · draft, version-bound approval, four-eyes
    op, ref, cv = order(daniel, "halden", "00417")
    daniel.post(f"/operations/{op}/approve", {**ref, "override_reason": None}, 403)
    priya.post(f"/operations/{op}/approve", {**ref, "content_hash": "0" * 64, "override_reason": None}, 409)
    ok("draft prepared; own approval and a wrong content hash are refused")

    # 2 · HALDEN late commit: the preset commits 2.5 s after the request, the client gives up after 1 s
    armed = tomas.post("/demo/faults", {"preset": "halden_delay_2500ms", "count": 1})
    assert armed["armed"]
    priya.post(f"/operations/{op}/approve", {**ref, "override_reason": None})
    until(lambda: state(daniel, op), lambda s: s == "UNKNOWN", 15, "UNKNOWN")
    ok("HALDEN late commit: outcome UNKNOWN after the hand-off timed out")
    until(lambda: state(daniel, op), lambda s: s == "CONFIRMED", 60, "CONFIRMED")
    t = transitions(daniel, op)
    assert "SUBMITTING->UNKNOWN" in t and "RECONCILING->CONFIRMED" in t, t
    d = daniel.get(f"/operations/{op}")
    assert len(d["transmissions"]) == 1 and d["operation"]["last_evidence"] == "RECONCILED"
    assert len(halden.orders_with_ref(cv)) == 1
    ok("read-only reconciliation confirmed it: one hand-off, one order in HALDEN's own records")

    # 3 · SKARVIK lost acknowledgement, authorized redelivery inside the window
    skarvik.fault("stale_extracts", value=True)
    tomas.post("/demo/faults", {"preset": "skarvik_drop_ack", "count": 1})
    sop, sref, scv = order(daniel, "skarvik", "KND-0081")
    priya.post(f"/operations/{sop}/approve", {**sref, "override_reason": None})
    until(lambda: state(daniel, sop), lambda s: s == "AWAITING_ACK", 20, "AWAITING_ACK")
    until(lambda: state(daniel, sop), lambda s: s == "UNKNOWN", 60, "UNKNOWN")
    actions = marta.get(f"/operations/{sop}")["permitted_actions"]
    assert {"recheck", "escalate", "authorize_redelivery"} <= set(actions), actions
    ok("SKARVIK lost acknowledgement: UNKNOWN; re-check, escalation and redelivery are offered to the manager")

    def redeliver() -> int:
        return marta.http.post(f"/api/v1/operations/{sop}/redelivery").status_code

    until(redeliver, lambda code: code == 202, 10, "redelivery authorized")  # 409 while a re-check holds the order
    until(lambda: state(marta, sop), lambda s: s == "CONFIRMED", 40, "acknowledged duplicate")
    assert marta.get(f"/operations/{sop}")["operation"]["last_evidence"] == "ACKNOWLEDGED_ONLY"
    skarvik.fault("stale_extracts", value=False)
    until(lambda: marta.get(f"/operations/{sop}")["operation"]["last_evidence"],
          lambda e: e == "DEDUP_CONFIRMED", 60, "DEDUP_CONFIRMED")
    assert len(skarvik.claims_for_message_prefix(scv)) == 2 and len(skarvik.orders_for_message_prefix(scv)) == 1
    ok("authorized redelivery: two claims, one order in SKARVIK's own records; DEDUP_CONFIRMED after read-back")

    # 4 · customer and inventory conflicts stay visible
    found = daniel.get("/customers?q=vantaa")
    holds = {(a["backend_id"], a["native_id"]): a["credit_hold"] for a in found["accounts"]}
    assert holds[("halden", "04450")] == "HOLD" and holds[("skarvik", "KND-0114")] == "CLEAR", holds
    item = next(i for i in sanna.get("/inventory")["items"] if i["canonical_sku"] == "BW-1009")
    per = {r["backend_id"]: r["status"] for r in item["per_backend"]}
    assert per["halden"] == "UNDETERMINED", per
    ok("customer hold conflict and undetermined stock are shown per system, never merged")

    # 5 · assistant (deterministic mode): reads, drafts with a receipt, refuses authority
    turn = daniel.post("/assistant/turns", {"text": "draft halden 00417 BW-1001x2"})
    assert turn["mode"] == "disabled" and turn["receipts"][0]["status"] == "STORED"
    assert state(daniel, turn["receipts"][0]["operation_id"]) == "DRAFT"
    refused = daniel.post("/assistant/turns", {"text": "approve the oldest order"})
    assert not refused.get("receipts") and "nothing was done" in refused["answer"]["summary"]
    ok("assistant stored a draft with a receipt and refused to approve")

    # 6 · evidence: hash chain, export, standalone verification
    assert helena.get("/audit/verify")["valid"] is True
    export = helena.post("/audit/exports")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "export.jsonl").write_text(export["jsonl"], encoding="utf-8")
        (Path(tmp) / "manifest.json").write_text(json.dumps(export["manifest"]), encoding="utf-8")
        r = subprocess.run([sys.executable, str(REPO / "oracles/audit_verify.py"), f"{tmp}/export.jsonl",
                            f"{tmp}/manifest.json"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    ok("audit chain verified by the application and by the standalone verifier")
    print(f"demonstration: {len(passed)} steps held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
