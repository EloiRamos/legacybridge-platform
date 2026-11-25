#!/usr/bin/env python3
"""Wire-tap: recording / fault-injecting HTTP proxy between the application and HALDEN (test profile only).

Python standard library only; it imports nothing from ``lbf`` (it is an independent observer).

Ledger (``$WIRETAP_LEDGER``, JSON Lines, append-only, fsync per record):
  * ``{"phase": "RECEIVED", ...}`` is written and fsynced **on receipt, before anything is forwarded** — it is the
    independent record that a request reached the wire-tap (``RECEIVED != FORWARDED != BACKEND_ACCEPTED``);
  * ``{"phase": "COMPLETED", ...}`` follows with ``forwarded``, ``status`` and ``response_sha256``.
  For ``POST /halden/orders`` (E8) the form field ``ref`` is recorded as ``form_ref`` so oracles can count handoffs
  per correlation value without trusting the application.

Fault modes (armed for the next ``count`` matching requests; default match ``POST /halden/orders``):
  * ``blackhole``          never forward; hold the client connection until it disconnects (or ``hold_seconds``);
  * ``corrupt_response``   forward, then replace the response body with garbage (status kept);
  * ``drop_after_forward`` forward, then reset the client connection without any response.

Control plane (``$WIRETAP_CONTROL_PORT``): ``GET /_wiretap/health``, ``GET /_wiretap/ledger`` (JSON Lines),
``GET``/``POST``/``DELETE /_wiretap/modes``, ``POST /_wiretap/reset`` (clears modes and truncates the ledger).

``--passthrough`` runs a plain non-recording TCP-level HTTP relay (used to give test oracles loopback access to a
simulator without being counted as application traffic).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import select
import socket
import struct
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
MODES = ("blackhole", "corrupt_response", "drop_after_forward")
MAX_BODY = 1024 * 1024


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        self.seq = max(self.seq, int(json.loads(line).get("seq", 0)))

    def append(self, record: dict[str, Any], *, new_seq: bool) -> int:
        with self.lock:
            if new_seq:
                self.seq += 1
                record["seq"] = self.seq
            line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            return int(record["seq"])

    def read(self) -> bytes:
        with self.lock:
            try:
                with open(self.path, "rb") as fh:
                    return fh.read()
            except FileNotFoundError:
                return b""

    def truncate(self) -> None:
        with self.lock:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            self.seq = 0


class Modes:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.armed: list[dict[str, Any]] = []

    def arm(self, mode: str, method: str = "POST", path: str = "/halden/orders", count: int = 1,
            hold_seconds: float = 120.0) -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if count < 1 or count > 100:
            raise ValueError("count must be 1..100")
        entry = {"mode": mode, "method": method.upper(), "path": path, "remaining": count,
                 "hold_seconds": float(hold_seconds)}
        with self.lock:
            self.armed.append(entry)
        return dict(entry)

    def take(self, method: str, path: str) -> dict[str, Any] | None:
        with self.lock:
            for entry in self.armed:
                if entry["method"] == method and path == entry["path"] and entry["remaining"] > 0:
                    entry["remaining"] -= 1
                    taken = dict(entry)
                    self.armed = [e for e in self.armed if e["remaining"] > 0]
                    return taken
        return None

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(e) for e in self.armed]

    def clear(self) -> None:
        with self.lock:
            self.armed = []


def reset_connection(sock: socket.socket) -> None:
    """Close with RST (SO_LINGER 0): the client sees a connection reset, never a response."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    finally:
        sock.close()


def make_proxy_handler(upstream: str, ledger: Ledger, modes: Modes, timeout: float) -> type[BaseHTTPRequestHandler]:
    target = urlsplit(upstream)

    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LBF-WIRETAP/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            form_ref = None
            if self.command == "POST" and parts.path == "/halden/orders":
                values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).get("ref")
                form_ref = values[0] if values else None
            mode = modes.take(self.command, parts.path)
            seq = ledger.append({
                "phase": "RECEIVED", "ts": now_iso(), "method": self.command, "path": parts.path,
                "query": parts.query, "body_sha256": hashlib.sha256(body).hexdigest(), "form_ref": form_ref,
                "mode": mode["mode"] if mode else None,
            }, new_seq=True)
            done: dict[str, Any] = {"phase": "COMPLETED", "seq": seq, "ts": None, "forwarded": False, "status": None,
                                    "response_sha256": None, "client_outcome": None}
            if mode and mode["mode"] == "blackhole":
                deadline = time.monotonic() + mode["hold_seconds"]
                self.connection.setblocking(False)
                try:
                    while time.monotonic() < deadline:
                        readable, _, _ = select.select([self.connection], [], [], 0.2)
                        if readable:
                            try:
                                if self.connection.recv(1) == b"":
                                    break  # client disconnected (timed out)
                            except (BlockingIOError, InterruptedError):
                                continue
                            except OSError:
                                break
                finally:
                    done.update(ts=now_iso(), client_outcome="HELD_UNTIL_DISCONNECT")
                    ledger.append(done, new_seq=False)
                    self.close_connection = True
                    reset_connection(self.connection)
                return
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
            try:
                conn.request(self.command, self.path, body=body if (body or self.command == "POST") else None,
                             headers=headers)
                done["forwarded"] = True
                response = conn.getresponse()
                payload = response.read()
                status = response.status
                resp_headers = [(k, v) for k, v in response.getheaders() if k.lower() not in HOP_BY_HOP]
            except OSError as exc:
                done.update(ts=now_iso(), client_outcome=f"UPSTREAM_ERROR:{exc.__class__.__name__}")
                ledger.append(done, new_seq=False)
                self.send_error(502)
                return
            finally:
                conn.close()
            done.update(status=status, response_sha256=hashlib.sha256(payload).hexdigest())
            if mode and mode["mode"] == "drop_after_forward":
                done.update(ts=now_iso(), client_outcome="RESET_AFTER_FORWARD")
                ledger.append(done, new_seq=False)
                self.close_connection = True
                reset_connection(self.connection)
                return
            if mode and mode["mode"] == "corrupt_response":
                payload = b"\x00#WIRETAP-CORRUPTED#\xff" + payload[::-1][:64]
                resp_headers = [(k, v) for k, v in resp_headers if k.lower() != "content-type"]
                resp_headers.append(("Content-Type", "application/octet-stream"))
                done["client_outcome"] = "CORRUPTED_RESPONSE"
            else:
                done["client_outcome"] = "RELAYED"
            done["ts"] = now_iso()
            ledger.append(done, new_seq=False)
            self.send_response(status)
            for k, v in resp_headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = _handle  # noqa: N815

    return Proxy


def make_control_handler(ledger: Ledger, modes: Modes) -> type[BaseHTTPRequestHandler]:
    class Control(BaseHTTPRequestHandler):
        server_version = "LBF-WIRETAP-CONTROL/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, status: int, payload: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode())

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/_wiretap/health":
                self._json(200, {"ok": True})
            elif self.path == "/_wiretap/ledger":
                self._send(200, ledger.read(), "application/x-ndjson")
            elif self.path == "/_wiretap/modes":
                self._json(200, modes.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if self.path == "/_wiretap/modes":
                    self._json(200, modes.arm(**body))
                elif self.path == "/_wiretap/reset":
                    modes.clear()
                    ledger.truncate()
                    self._json(200, {"reset": True})
                else:
                    self._json(404, {"error": "not found"})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            if self.path == "/_wiretap/modes":
                modes.clear()
                self._json(200, [])
            else:
                self._json(404, {"error": "not found"})

    return Control


def relay(client: socket.socket, upstream: tuple[str, int]) -> None:
    try:
        server = socket.create_connection(upstream, timeout=10)
    except OSError:
        client.close()
        return
    sockets = [client, server]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 300)
            if not readable:
                return
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return
                (server if s is client else client).sendall(data)
    except OSError:
        return
    finally:
        client.close()
        server.close()


def serve_passthrough(listen_port: int, upstream: str) -> None:
    target = urlsplit(upstream)
    addr = (target.hostname or "localhost", target.port or 80)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", listen_port))  # noqa: S104 - container-internal; published to 127.0.0.1 only
    sock.listen(64)
    print(f"passthrough :{listen_port} -> {upstream}", flush=True)
    while True:
        client, _ = sock.accept()
        threading.Thread(target=relay, args=(client, addr), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", default=os.environ.get("WIRETAP_UPSTREAM", "http://halden:8090"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WIRETAP_PORT", "8090")))
    parser.add_argument("--control-port", type=int, default=int(os.environ.get("WIRETAP_CONTROL_PORT", "8099")))
    parser.add_argument("--ledger", default=os.environ.get("WIRETAP_LEDGER", "/wiretap/ledger.jsonl"))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("WIRETAP_UPSTREAM_TIMEOUT", "30")))
    parser.add_argument("--passthrough", action="store_true")
    parser.add_argument("--oracle-relays", default=os.environ.get("WIRETAP_ORACLE_RELAYS", ""),
                        help="comma-separated PORT=URL non-recording relays giving test oracles simulator access")
    args = parser.parse_args(argv)
    if args.passthrough:
        serve_passthrough(args.port, args.upstream)
        return 0
    for spec in filter(None, (s.strip() for s in args.oracle_relays.split(","))):
        port, url = spec.split("=", 1)
        threading.Thread(target=serve_passthrough, args=(int(port), url), daemon=True).start()
    ledger, modes = Ledger(args.ledger), Modes()
    proxy = ThreadingHTTPServer(("0.0.0.0", args.port),  # noqa: S104 - container-internal
                                make_proxy_handler(args.upstream, ledger, modes, args.timeout))
    control = ThreadingHTTPServer(("0.0.0.0", args.control_port),  # noqa: S104
                                  make_control_handler(ledger, modes))
    proxy.daemon_threads = control.daemon_threads = True
    threading.Thread(target=control.serve_forever, daemon=True).start()
    print(f"wiretap :{args.port} -> {args.upstream}; control :{args.control_port}; ledger {args.ledger}", flush=True)
    proxy.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
