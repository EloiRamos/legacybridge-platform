#!/usr/bin/env python3
"""Post-restore reconciliation pass (runbook RECOVERY). Standard library only.

After a database restore, every non-terminal operation is re-examined against the backends:
* ``UNKNOWN``: a read-only re-check is requested (the reconciler reads the backend; nothing is ever resent);
* ``APPROVED``/``SUBMITTING``/``RECONCILING``: left to the engines, which recover them on start (sweeper);
* ``AWAITING_ACK``: left to the acknowledgement poller and its deadline;
* ``ESCALATED``: listed for a human decision.
Signs in as an integration engineer (``reconcile.recheck``); credentials come from the operator, never from code:
``LBF_RECONCILE_USER`` / ``LBF_RECONCILE_PASSWORD_FILE``. Prints a JSON report.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LBF_BASE_URL", "http://127.0.0.1:8080")
NON_TERMINAL = ("APPROVED", "SUBMITTING", "AWAITING_ACK", "UNKNOWN", "RECONCILING", "ESCALATED")


def main() -> int:
    user = os.environ["LBF_RECONCILE_USER"]
    with open(os.environ["LBF_RECONCILE_PASSWORD_FILE"], encoding="utf-8") as f:
        password = f.read().strip()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"Content-Type": "application/json", "Origin": BASE}

    def call(method: str, path: str, body: object | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with opener.open(req, timeout=30) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}

    status, login = call("POST", "/api/v1/auth/login", {"username": user, "password": password})
    if status != 200:
        print(json.dumps({"error": f"login failed ({status})"}))
        return 2
    headers["X-CSRF-Token"] = login["csrf_token"]
    report: dict[str, object] = {"rechecks_requested": [], "left_to_engines": [], "needs_decision": []}
    query = "&".join(f"state={s}" for s in NON_TERMINAL)
    _, listing = call("GET", f"/api/v1/operations?{query}")
    for op in listing.get("items", []):
        ref = {"operation_id": op["operation_id"], "correlation": op["correlation_value"], "state": op["state"]}
        if op["state"] == "UNKNOWN":
            code, _ = call("POST", f"/api/v1/operations/{op['operation_id']}/recheck")
            report["rechecks_requested"].append({**ref, "http": code})  # type: ignore[union-attr]
        elif op["state"] == "ESCALATED":
            report["needs_decision"].append(ref)  # type: ignore[union-attr]
        else:
            report["left_to_engines"].append(ref)  # type: ignore[union-attr]
    print(json.dumps(report, indent=1, sort_keys=True))
    failed = [r for r in report["rechecks_requested"] if r["http"] != 202]  # type: ignore[union-attr,index]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
