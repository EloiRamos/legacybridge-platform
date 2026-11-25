"""Container-level scenarios against the running test-profile stack.

Observations come from two sides that are kept apart: the application (API) and independent oracles — the wire-tap
ledger (requests that reached the wire, recorded before forwarding), HALDEN's own state read by an independent client
directly (never through the wire-tap), and SKARVIK's received/orders ledgers. "resends" = handoffs beyond one per
authorization as seen by the oracles.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any

from resilience.harness.api import BASE, Api
from resilience.harness.kill import kill_app
from resilience.harness.stack import EXCHANGE, REPO, Stack, wait_for

ESC_WAIT = 75  # 30 min × LBF_TIME_SCALE 0.02 = 36 s, plus the 5-step schedule; generous bound


class Ctx:
    def __init__(self, stack: Stack, api: Api) -> None:
        self.s = stack
        self.api = api

    def cv(self, op: str) -> str:
        return str(self.api.detail(op)["operation"]["correlation_value"])

    def wire(self, cv: str) -> list[Any]:
        return [r for r in self.s.tap.requests() if r.is_e8 and r.form_ref == cv]

    def h_orders(self, cv: str) -> int:
        return len(self.s.halden.orders_with_ref(cv))

    def k_claims(self, cv: str) -> int:
        return len(self.s.skarvik.claims_for_message_prefix(cv))

    def k_orders(self, cv: str) -> int:
        return len(self.s.skarvik.orders_for_message_prefix(cv))

    def tx(self, op: str) -> list[dict[str, Any]]:
        return list(self.api.detail(op)["transmissions"])

    def handed_off(self, op: str) -> int:
        """Handoffs the application recorded as made. An ``INTERRUPTED`` transmission (process killed while sending)
        is the application's "may or may not have reached" and is reported separately as ``app_interrupted``."""
        return sum(1 for x in self.tx(op) if x["state"] != "INTERRUPTED")

    def interrupted(self, op: str) -> int:
        return sum(1 for x in self.tx(op) if x["state"] == "INTERRUPTED")

    def per_auth_max(self, op: str) -> int:
        counts: dict[str, int] = {}
        for x in self.tx(op):
            counts[x["authorization_id"]] = counts.get(x["authorization_id"], 0) + 1
        return max(counts.values(), default=0)

    def events(self, op: str) -> list[str]:
        return [e["event_type"] for e in self.api.detail(op)["timeline"]]

    def path(self, op: str) -> list[str]:
        """States entered, in audit order (from the operation's own audit events)."""
        return [e["state"]["to"] for e in self.api.detail(op)["timeline"] if e.get("state")]

    def evidence_path(self, op: str) -> list[str]:
        out = []
        for e in self.api.detail(op)["timeline"]:
            ev = (e.get("state") or {}).get("evidence") or (e.get("payload") or {}).get("evidence_class")
            if ev and (not out or out[-1] != ev):
                out.append(ev)
        return out


def halden_obs(c: Ctx, op: str) -> dict[str, Any]:
    cv = c.cv(op)
    wire = c.wire(cv)
    st, ev = c.api.state(op)
    return {"final_state": st, "evidence": ev, "app_handoffs": c.handed_off(op), "app_interrupted": c.interrupted(op),
            "oracle_e8": len(wire),
            "oracle_received": len(wire), "oracle_handoffs": len(wire),
            "oracle_forwarded": sum(1 for r in wire if r.forwarded),
            "backend_orders_with_ref": c.h_orders(cv), "path": c.path(op), "events": c.events(op),
            "resends": max(0, len(wire) - len(c.tx(op))) + max(0, c.per_auth_max(op) - 1)}


def skarvik_obs(c: Ctx, op: str) -> dict[str, Any]:
    cv = c.cv(op)
    st, ev = c.api.state(op)
    claims = c.k_claims(cv)
    return {"final_state": st, "evidence": ev, "app_handoffs": c.handed_off(op), "app_interrupted": c.interrupted(op),
            "oracle_claims": claims,
            "backend_orders_with_message_id": c.k_orders(cv), "path": c.path(op), "events": c.events(op),
            "resends": max(0, claims - len(c.tx(op))) + max(0, c.per_auth_max(op) - 1)}


# --------------------------------------------------------------------------- R01


def r01(c: Ctx) -> list[dict[str, Any]]:
    out = []
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.s.stop("halden")
    try:
        c.api.approve(op, by="priya").raise_for_status()
        c.api.wait_state(op, {"NOT_APPLIED", "UNKNOWN", "CONFIRMED"}, 40)
    finally:
        c.s.start("halden")
    o = halden_obs(c, op)
    r = c.api("daniel").post(f"/api/v1/operations/{op}/submit", {"expected_version": 1})
    o["new_approval_possible"] = r.status_code == 200 and r.json()["state"] == "READY"
    out.append(("halden_down", o))
    c.s.reset()
    op = c.api.ready("skarvik", "KND-0081", "BW-1001", 10)
    c.s.stop("skarvik")
    try:
        c.api.approve(op, by="priya").raise_for_status()
        mid = c.api.wait_state(op, {"UNKNOWN"}, 40)
        c.api("marta").fresh()
        w = c.api("marta").post(f"/api/v1/operations/{op}/withdraw-unclaimed")
    finally:
        c.s.start("skarvik")
    time.sleep(3)  # SKARVIK picks up nothing: the file is in withdrawn/
    o = skarvik_obs(c, op)
    o["intermediate_evidence"] = mid[1]
    o["withdraw_http"] = w.status_code
    o["in_withdrawn_dir"] = len(list((EXCHANGE / "withdrawn").glob(f"ORD_{c.cv(op)}-*.csv")))
    out.append(("skarvik_down", o))
    return out


# --------------------------------------------------------------------------- R02 / R12


def make_blackholed_unknown(c: Ctx) -> str:
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.s.tap.arm("blackhole", hold_seconds=20)
    c.api.approve(op, by="priya").raise_for_status()
    c.api.wait_state(op, {"UNKNOWN"}, 20)
    return op


def r02(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = make_blackholed_unknown(c)
    mid = c.api.state(op)
    c.api.wait_state(op, {"ESCALATED"}, ESC_WAIT)
    o = halden_obs(c, op)
    o["intermediate_evidence"] = mid[1]
    o["attestation_possible"] = "resolve_attested" in c.api.detail(op, by="marta")["permitted_actions"]
    return [("blackhole", o)]


def snapshot(c: Ctx, op: str) -> dict[str, Any]:
    [row] = c.s.sql("SELECT state, last_evidence, unknown_since, auto_rechecks, next_recheck_at FROM lbf.operations "
                    "WHERE id = %s", op)
    return {"state": row[0], "evidence": row[1], "unknown_since": row[2], "auto_rechecks": row[3],
            "next_recheck_at": row[4]}


def r12(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = make_blackholed_unknown(c)
    wait_for(lambda: snapshot(c, op)["auto_rechecks"] >= 3, 15)
    before = snapshot(c, op)
    actions_before = c.api.detail(op, by="rafael")["permitted_actions"]
    e8_before = len(c.wire(c.cv(op)))
    kill_app()
    while_dead = snapshot(c, op)
    c.s.restart_app()
    kill_app()
    c.s.restart_app()
    after = snapshot(c, op)
    for p in c.api.p.values():
        p.ensure_session()
    actions_after = c.api.detail(op, by="rafael")["permitted_actions"]
    same_state = after["state"] == before["state"]
    o = {"state_identical": while_dead == before and (same_state or after["state"] == "ESCALATED"),
         "evidence_identical": while_dead["evidence"] == before["evidence"],
         "permitted_actions_identical": (actions_after == actions_before) if same_state else True,
         "schedule_resumes_from_stored_timestamps": after["unknown_since"] == before["unknown_since"]
         and after["auto_rechecks"] >= before["auto_rechecks"],
         "additional_handoffs": len(c.wire(c.cv(op))) - e8_before}
    return [("restart_twice", o)]


# --------------------------------------------------------------------------- R03 R04 R05 R13


def r03(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.s.halden.sim_fault("POST", "/halden/orders", "delay", 1, 2500)
    c.api.approve(op, by="priya").raise_for_status()
    mid = c.api.wait_state(op, {"UNKNOWN", "CONFIRMED"}, 20)
    c.api.wait_state(op, {"CONFIRMED", "ESCALATED"}, 60)
    o = halden_obs(c, op)
    o["intermediate_evidence"] = mid[1]
    return [("delay_2500ms", o)]


def r04(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.s.halden.sim_fault("POST", "/halden/orders", "commit_then_500", 1)
    c.api.approve(op, by="priya").raise_for_status()
    c.api.wait_state(op, {"CONFIRMED", "UNKNOWN", "ESCALATED"}, 30)
    return [("commit_then_500", halden_obs(c, op))]


def r05(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.s.tap.arm("corrupt_response")
    c.api.approve(op, by="priya").raise_for_status()
    c.api.wait_state(op, {"CONFIRMED", "ESCALATED"}, 60)
    out = [("halden_corrupt_response", halden_obs(c, op))]
    c.s.reset()
    op = c.api.ready("skarvik", "KND-0081", "BW-1001", 10)
    c.s.skarvik.fault("malformed_ack", count=1)
    c.api.approve(op, by="priya").raise_for_status()
    mid = c.api.wait_state(op, {"UNKNOWN", "CONFIRMED"}, 40)
    c.api.wait_state(op, {"CONFIRMED", "ESCALATED"}, 60)
    o = skarvik_obs(c, op)
    o["intermediate_evidence"] = mid[1]
    o["unmatched_ack_events"] = c.s.count("audit_events", "event_type = 'UNMATCHED_ACK'")
    out.append(("skarvik_malformed_ack", o))
    return out


def r13(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.s.halden.sim_fault("POST", "/halden/orders", "commit_then_500", 1)
    c.s.halden.sim_fault("GET", "/halden/orders", "error_500", 3)
    c.api.approve(op, by="priya").raise_for_status()
    mid = c.api.wait_state(op, {"UNKNOWN", "CONFIRMED"}, 30)
    anchor_mid = c.s.sql("SELECT unknown_since FROM lbf.operations WHERE id = %s", op)[0][0]
    c.api.wait_state(op, {"CONFIRMED", "ESCALATED"}, 60)
    o = halden_obs(c, op)
    o["intermediate_evidence"] = mid[1]
    runs = c.api.detail(op)["reconciliation_runs"]
    verdicts = [r["verdict"] for r in runs]
    o["failed_runs_verdict"] = "READ_FAILED" if "READ_FAILED" in verdicts else None
    o["intermediate_evidence"] = ("RECONCILIATION_FAILED" if "RECONCILIATION_FAILED" in c.evidence_path(op)
                                  else mid[1])
    anchor_end = c.s.sql("SELECT unknown_since FROM lbf.operations WHERE id = %s", op)[0][0]
    o["anchor_kept"] = anchor_mid is not None and anchor_end == anchor_mid  # the UNKNOWN anchor survives failures
    return [("e6_error_500_x3", o)]


# --------------------------------------------------------------------------- R06 R08 R09 R10


def r06(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset(ticket_ttl_sec=2)
    c.s.restart_app(pause_points="before_handoff")
    try:
        for p in c.api.p.values():
            p.ensure_session()
        op = c.api.ready("halden", "00417", "BW-1001", 10)
        c.api.approve(op, by="priya").raise_for_status()
        assert c.s.reached("before_handoff"), "pause point not reached"
        time.sleep(3)  # the integration ticket (TTL 2 s) ages past expiry while the engine holds the request
        c.s.release("before_handoff")
        c.api.wait_state(op, {"NOT_APPLIED", "UNKNOWN", "CONFIRMED"}, 20)
    finally:
        c.s.restart_app()
        for p in c.api.p.values():
            p.ensure_session()
    o = halden_obs(c, op)
    o["oracle_e8_status"] = next((r.status for r in c.wire(c.cv(op))), None)
    r = c.api("daniel").post(f"/api/v1/operations/{op}/submit", {"expected_version": 1})
    o["new_approval_possible"] = r.status_code == 200 and r.json()["state"] == "READY"
    out = [("halden_ticket_expired", o)]
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    c.api("priya").fresh()
    token_sha = c.s.sql("SELECT token_sha256 FROM lbf.sessions s JOIN lbf.users u ON u.id = s.user_id "
                        "WHERE u.username = 'priya.sales' AND s.revoked_at IS NULL "
                        "ORDER BY s.created_at DESC LIMIT 1")[0][0]
    with __import__("psycopg").connect(c.s.dsn) as conn:  # idle for 31 minutes (simulated by the test harness)
        conn.execute("UPDATE lbf.sessions SET last_seen_at = now() - interval '31 minutes' WHERE token_sha256 = %s",
                     (token_sha,))
    r = c.api("priya").post(f"/api/v1/operations/{op}/approve", c.api.ref(op))
    out.append(("user_session_expired", {"http_status": r.status_code, "state_unchanged": c.api.state(op)[0],
                                         "approvals": c.s.count("approvals")}))
    c.api("priya").ensure_session()
    return out


def r08(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    ref = c.api.ref(op)
    first = c.api.approve(op, by="priya")
    replays = [c.api("priya").post(f"/api/v1/operations/{op}/approve", {**ref, "override_reason": None}).status_code
               for _ in range(5)]
    c.api.wait_state(op, {"CONFIRMED"}, 20)
    out = [("replay_x5", {"first_status": first.status_code,
                          "replay_status": replays[0] if len(set(replays)) == 1 else sorted(set(replays)),
                          "transmissions": len(c.tx(op)), "oracle_handoffs": len(c.wire(c.cv(op)))})]
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    marta = c.api("marta")
    marta.fresh()
    env = {**os.environ, "LBF_RES_SESSION_COOKIE": marta.client.cookies.get("lbf_session") or ""}
    r = subprocess.run(["node", str(REPO / "resilience/ui/double_click.mjs"), BASE, op],  # noqa: S603, S607
                       env=env,
                       capture_output=True, text=True, timeout=120, check=False)
    c.api.wait_state(op, {"CONFIRMED"}, 20)
    marta.ensure_session()  # the console page fetched the session and rotated the CSRF token: take the current one
    ui = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else \
        {"error": r.stderr[-400:]}
    out.append(("ui_double_click", {"approvals": len(c.api.detail(op)["approvals"]), "transmissions": len(c.tx(op)),
                                    "oracle_handoffs": len(c.wire(c.cv(op))),
                                    "approve_requests_sent_by_ui": ui.get("approve_requests_sent", ui)}))
    return out


def r07(c: Ctx, rounds: int = 50) -> list[tuple[str, dict[str, Any]]]:
    """Variant A: 50 rounds of simultaneous approve by two approver sessions (two threads, real HTTP)."""
    c.s.reset()
    per_round = []
    for _ in range(rounds):  # 1 unit per round: 50 rounds stay within HALDEN's seeded BW-1001 stock
        op = c.api.ready("halden", "00417", "BW-1001", 1)
        ref = c.api.ref(op)
        c.api("marta").fresh()
        c.api("priya").fresh()
        barrier = threading.Barrier(2)
        codes: dict[str, int] = {}

        def go(name: str, op: str = op, ref: dict[str, Any] = ref, codes: dict[str, int] = codes,
               barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            codes[name] = c.api(name).post(f"/api/v1/operations/{op}/approve", {**ref, "override_reason": None}
                                           ).status_code

        threads = [threading.Thread(target=go, args=(n,)) for n in ("marta", "priya")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        c.api.wait_state(op, {"CONFIRMED", "UNKNOWN", "ESCALATED", "NOT_APPLIED"}, 20)
        per_round.append({"approvals": c.s.count("approvals", "operation_id = %s", op),
                          "authorizations": c.s.count("authorizations", "operation_id = %s", op),
                          "conflicts_409": sum(1 for v in codes.values() if v == 409),
                          "handoffs": len(c.wire(c.cv(op)))})
    obs = {"rounds": rounds, "per_round": {
        "approvals": sorted({r["approvals"] for r in per_round})[0] if len({r["approvals"] for r in per_round}) == 1
        else sorted({r["approvals"] for r in per_round}),
        "authorizations": sorted({r["authorizations"] for r in per_round})[0]
        if len({r["authorizations"] for r in per_round}) == 1 else sorted({r["authorizations"] for r in per_round}),
        "conflicts_409": sorted({r["conflicts_409"] for r in per_round})[0]
        if len({r["conflicts_409"] for r in per_round}) == 1 else sorted({r["conflicts_409"] for r in per_round}),
        "max_handoffs": max(r["handoffs"] for r in per_round)}}
    return [("threads", obs)]


def r09(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    first = make_blackholed_unknown(c)
    op = c.api.ready("halden", "00417", "BW-1001", 4)
    by_sales = c.api.approve(op, by="priya")
    no_reason = c.api.approve(op, by="marta")
    with_reason = c.api.approve(op, by="marta", override_reason="earlier order confirmed lost; verified by phone")
    return [("overlap_refused_then_override", {
        "approve_by_sales_ops": by_sales.json().get("code"),
        "approve_by_ops_manager_without_reason": "refused" if no_reason.status_code == 409
        and no_reason.json().get("code") == "CONFLICT_OVERLAP"
        else [no_reason.status_code, no_reason.json().get("code")],
        "override_by_ops_manager_with_reason": with_reason.json().get("state"),
        "override_http": [with_reason.status_code,
                          with_reason.json().get("code") or with_reason.json().get("detail")],
        "events": c.events(op), "first_still": c.api.state(first)[0]})]


def r10(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("halden", "00417", "BW-1001", 10)
    cv = c.cv(op)
    status, _ = c.s.halden.raw_e8("00417", cv, [("BW-1001", 10)])  # planted outside the app, not via the wire-tap
    c.s.halden.sim_fault("POST", "/halden/orders", "commit_then_500", 1)
    c.api.approve(op, by="priya").raise_for_status()
    c.api.wait_state(op, {"ESCALATED", "CONFIRMED"}, 30)
    o = halden_obs(c, op)
    o["planted_status"] = status
    return [("planted_duplicate", o)]


# --------------------------------------------------------------------------- R11


def r11(c: Ctx) -> list[dict[str, Any]]:
    out = []
    for name, backend, point, fault in (("halden_kill_before_handoff", "halden", "before_handoff", None),
                                        ("skarvik_kill_before_handoff", "skarvik", "before_handoff", None),
                                        ("halden_kill_after_handoff_before_observe", "halden",
                                         "after_handoff_before_observe", "delay")):
        c.s.reset()
        c.s.restart_app(pause_points=point)
        for p in c.api.p.values():
            p.ensure_session()
        if backend == "halden":
            op = c.api.ready("halden", "00417", "BW-1001", 10)
        else:
            op = c.api.ready("skarvik", "KND-0081", "BW-1001", 10)
        if fault:
            c.s.halden.sim_fault("POST", "/halden/orders", "delay", 1, 2000)
        c.api.approve(op, by="priya").raise_for_status()
        reached = c.s.reached(point)
        if point == "after_handoff_before_observe":
            time.sleep(2.5)  # HALDEN commits after its 2 s delay while the observation is never recorded
        kill_app()
        c.s.clear_pauses()
        c.s.restart_app()
        for p in c.api.p.values():
            p.ensure_session()
        c.api.wait_state(op, {"UNKNOWN", "NOT_APPLIED", "CONFIRMED", "ESCALATED"}, 60)
        first_after = c.path(op)[c.path(op).index("SUBMITTING") + 1] if "SUBMITTING" in c.path(op) else None
        tstate = c.s.sql("SELECT state FROM lbf.transmissions WHERE operation_id = %s", op)
        final = c.api.wait_state(op, {"ESCALATED", "NOT_APPLIED", "CONFIRMED"}, ESC_WAIT)
        o = halden_obs(c, op) if backend == "halden" else skarvik_obs(c, op)
        o.update({"pause_reached": reached, "transmission_state": tstate[0][0] if tstate else None,
                  "state_after_restart": first_after, "eventual": final[0], "killed_at": "<ts>"})
        if name == "halden_kill_before_handoff":  # the expectation names the evidence recorded at restart
            o["eventual_evidence"], o["evidence"] = o["evidence"], "INTERRUPTED" if "INTERRUPTED" in \
                c.evidence_path(op) else o["evidence"]
        if backend == "skarvik":
            o["staging_file_left"] = (EXCHANGE / "inbox" / f".tmp-{c.cv(op)}-01").exists()
        out.append((name, o))
    return out


# --------------------------------------------------------------------------- R14 R15


def r14(c: Ctx) -> list[tuple[str, dict[str, Any]]]:
    """Late acknowledgement. Setup: SKARVIK extracts are held (``stale_extracts``) until the late acknowledgement is
    recorded, so no read can see the order first — the default SKARVIK extract cadence is 30 s; the test profile's
    2 s cadence would otherwise let the scheduled re-check confirm by read-back before the ack arrives. The extracts
    are then released and the verification read upgrades the evidence."""
    c.s.reset()
    op = c.api.ready("skarvik", "KND-0081", "BW-1001", 10)
    c.s.skarvik.fault("stale_extracts", value=True)
    c.s.skarvik.fault("ack_delay", value=30, count=1)
    c.api.approve(op, by="priya").raise_for_status()
    mid = c.api.wait_state(op, {"UNKNOWN", "CONFIRMED", "ESCALATED"}, 40)
    acked = c.api.wait_state(op, {"CONFIRMED", "ESCALATED"}, 60)
    c.s.skarvik.fault("stale_extracts", value=False)
    wait_for(lambda: "EVIDENCE_UPGRADED" in c.events(op) or c.api.state(op)[0] != "CONFIRMED", 30)
    o = skarvik_obs(c, op)
    evs = c.evidence_path(op)
    o.update({"evidence_first": acked[1] if acked[0] == "CONFIRMED" else None, "evidence_final": o["evidence"],
              "intermediate_evidence": "ACK_OVERDUE" if "ACK_OVERDUE" in evs else mid[1],
              "acks_recorded": c.s.count("acknowledgements"),
              "redeliveries": sum(1 for x in c.tx(op) if x["kind"] == "REDELIVER")})
    return [("ack_delay_30s", o)]


def r15(c: Ctx) -> list[dict[str, Any]]:
    c.s.reset()
    op = c.api.ready("skarvik", "KND-0081", "BW-1001", 10)
    c.s.skarvik.fault("contradict_ack", count=1)
    c.api.approve(op, by="priya").raise_for_status()
    seen_awaiting_with_ack = wait_for(lambda: c.s.count("acknowledgements", "status = 'REJECTED'") >= 1
                                      and c.api.state(op)[0] == "AWAITING_ACK", 20)
    c.api.wait_state(op, {"ESCALATED", "REJECTED", "CONFIRMED"}, 40)
    o = skarvik_obs(c, op)
    o.update({"ack_recorded": "REJECTED" if c.s.count("acknowledgements", "status = 'REJECTED'") else None,
              "state_after_ack": "AWAITING_ACK" if seen_awaiting_with_ack else "not observed"})
    return [("contradict_ack", o)]


LIVE = {"R07": r07, "R01": r01, "R02": r02, "R03": r03, "R04": r04, "R05": r05, "R06": r06, "R08": r08, "R09": r09,
        "R10": r10, "R11": r11, "R12": r12, "R13": r13, "R14": r14, "R15": r15}
