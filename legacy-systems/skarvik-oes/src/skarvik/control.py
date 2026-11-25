"""Simulation control plane (only when SKARVIK_SIM_CONTROL=true; demo/test profiles).

POST /_sim/reset · POST /_sim/faults (JSON) · GET/DELETE /_sim/faults · GET /_sim/status
Oracle endpoints (for independent oracles and the resilience runner only — never the application):
GET /_sim/oracle/orders · GET /_sim/oracle/received
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from skarvik.simulator import COUNTED_FAULTS, Faults, Simulator


def apply_fault(sim: Simulator, body: dict[str, Any]) -> dict[str, Any]:
    name = body.get("fault")
    value = body.get("value")
    count = int(body.get("count", 1))
    f = sim.faults
    if name == "ack_delay":
        f.ack_delay_seconds = float(value or 0)
        f.ack_delay_count = count
    elif name in COUNTED_FAULTS:
        f.counted[name] = count
    elif name in ("pause_pickup", "stale_extracts"):
        setattr(f, name, bool(value if value is not None else True))
    elif name == "duplicate_window_seconds":
        f.duplicate_window_seconds = int(value) if value is not None else None
    else:
        raise ValueError(f"unknown fault {name!r}")
    return f.as_dict()


def make_handler(sim: Simulator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SKARVIK-SIM/4"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            return

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object expected")
            return data

        def do_GET(self) -> None:  # noqa: N802
            with sim.lock:
                if self.path == "/_sim/faults":
                    self._send(200, sim.faults.as_dict())
                elif self.path == "/_sim/oracle/orders":
                    self._send(200, sim.oracle_orders())
                elif self.path == "/_sim/oracle/received":
                    self._send(200, sim.oracle_received())
                elif self.path == "/_sim/status":
                    self._send(200, {"ok": True, "pending_acks": len(sim.pending_acks)})
                else:
                    self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path == "/_sim/reset":
                    sim.reset()
                    self._send(200, {"reset": True})
                elif self.path == "/_sim/faults":
                    with sim.lock:
                        self._send(200, apply_fault(sim, self._body()))
                elif self.path == "/_sim/pickup":
                    self._send(200, {"outcomes": sim.pickup_once()})
                elif self.path == "/_sim/extracts":
                    self._send(200, {"generated": sim.generate_extracts(force=True)})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            if self.path == "/_sim/faults":
                with sim.lock:
                    sim.faults = Faults()
                self._send(200, sim.faults.as_dict())
            else:
                self._send(404, {"error": "not found"})

    return Handler


def serve(sim: Simulator, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(sim))  # noqa: S104 - container-internal
    return server
