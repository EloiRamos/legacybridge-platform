"""Test-profile control for the Playwright end-to-end suite.

The browser drives the console; this CLI does what a human tester would do *outside* the application: reset the
environment (initial conditions), arm simulator/wire-tap faults, restart or kill the application process at an
engine pause point, and read the independent oracles (wire-tap ledger, HALDEN via its own client, SKARVIK's own
ledgers). It reuses the resilience harness and prints one JSON object per call.

    cd backend && PYTHONPATH=.. uv run --frozen python ../acceptance/e2e_ctl.py <command> [args]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from resilience.harness.kill import kill_app  # noqa: E402
from resilience.harness.stack import Stack  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset")
    r.add_argument("--ticket-ttl", type=int)
    a = sub.add_parser("restart-app")
    a.add_argument("points", nargs="?", default="")
    sub.add_parser("kill")
    for name in ("reached", "release"):
        sub.add_parser(name).add_argument("point")
    hf = sub.add_parser("halden-fault")
    hf.add_argument("method")
    hf.add_argument("path")
    hf.add_argument("mode")
    hf.add_argument("--count", type=int, default=1)
    hf.add_argument("--delay-ms", type=int, default=0)
    sf = sub.add_parser("skarvik-fault")
    sf.add_argument("name")
    sf.add_argument("--value")
    sf.add_argument("--count", type=int, default=1)
    sub.add_parser("halden-clear-faults")
    wa = sub.add_parser("wiretap-arm")
    wa.add_argument("mode")
    wa.add_argument("--hold", type=float, default=20.0)
    sub.add_parser("counts").add_argument("correlation")
    args = ap.parse_args(argv)

    s = Stack()
    out: dict[str, object] = {"ok": True}
    if args.cmd == "reset":
        s.wait_ready()
        s.reset(ticket_ttl_sec=args.ticket_ttl)
    elif args.cmd == "restart-app":
        s.clear_pauses()
        s.restart_app(pause_points=args.points)
        s.wait_ready()
    elif args.cmd == "kill":
        out["killed_at"] = kill_app()
        s.clear_pauses()
    elif args.cmd == "reached":
        out["reached"] = s.reached(args.point)
    elif args.cmd == "release":
        s.release(args.point)
    elif args.cmd == "halden-fault":
        s.halden.sim_fault(args.method, args.path, args.mode, args.count, args.delay_ms)
    elif args.cmd == "skarvik-fault":
        value: object = args.value
        if args.value in ("true", "false"):
            value = args.value == "true"
        elif args.value is not None and args.value.replace(".", "", 1).isdigit():
            value = float(args.value)
        out["faults"] = s.skarvik.fault(args.name, value=value, count=args.count)
    elif args.cmd == "halden-clear-faults":
        s.halden.sim_clear_faults()
    elif args.cmd == "wiretap-arm":
        s.tap.arm(args.mode, hold_seconds=args.hold)
    elif args.cmd == "counts":
        cv = args.correlation
        out.update({"wire_e8": s.tap.e8_count(cv), "halden_orders": len(s.halden.orders_with_ref(cv)),
                    "halden_order_nos": s.halden.orders_with_ref(cv),
                    "skarvik_claims": len(s.skarvik.claims_for_message_prefix(cv)),
                    "skarvik_orders": len(s.skarvik.orders_for_message_prefix(cv))})
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
