"""Independent oracles cross-checked against the application.

The oracles (``oracles/src/lbf_oracles``) never import ``lbf`` (B-3); this test is the only place both meet.
The wire-tap runs in-process against a loopback stub upstream (no container).
"""

from __future__ import annotations

import http.client
import importlib.util
import socket
import sys
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from lbf.domain.canonical import canonical_json, content_hash
from lbf.persistence.audit import GENESIS, document, hash_document

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "oracles" / "src"))

from lbf_oracles import audit_verify as oracle_audit  # noqa: E402
from lbf_oracles import canonical_json as oracle_cj  # noqa: E402
from lbf_oracles.wiretap_ledger import parse as parse_ledger  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_canonical import GOLDEN  # noqa: E402


def load_wiretap():
    spec = importlib.util.spec_from_file_location("wiretap", REPO / "tools" / "wiretap" / "wiretap.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- canonical JSON


@pytest.mark.parametrize(("name", "value", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_oracle_canonical_json_matches_golden_vectors_and_lbf(name, value, expected):
    assert oracle_cj.dumps(value) == expected == canonical_json(value)


@pytest.mark.parametrize("text", ["0", "0.001", "100.00", "48.50", "-0.00", "1E+2", "-12.340", "1234567.890"])
def test_oracle_decimal_normalization_agrees(text):
    assert oracle_cj.dumps({"x": Decimal(text)}) == canonical_json({"x": Decimal(text)})


def test_oracle_content_hash_agrees_with_application():
    op = uuid.uuid4()
    content = {"backend_id": "skarvik", "native_customer_id": "KND-0081", "delivery_note": "Kaj 7, Göteborg",
               "lines": [{"native_item": "NV-GL-100L", "quantity_base": 20, "unit_price": Decimal("12.50")}]}
    assert oracle_cj.content_hash(str(op), 3, content) == content_hash(op, 3, content)


def test_oracle_rejects_floats():
    with pytest.raises(TypeError):
        oracle_cj.dumps({"x": 1.5})


# --------------------------------------------------------------------------- audit verifier


def chain(n: int) -> list[dict]:
    prev, docs = GENESIS, []
    t = datetime(2026, 11, 21, 13, 0, tzinfo=UTC)
    for seq in range(1, n + 1):
        doc = document(seq=seq, event_id=uuid.uuid4(), event_type="APPROVAL_GRANTED", occurred_at=t, recorded_at=t,
                       actor_type="USER", actor_id="u1", actor_username="marta.ops", on_behalf_of=None,
                       operation_id=uuid.uuid4(), correlation={"attempt_no": 1, "content_hash": "ab" * 32},
                       state={"from": "READY", "to": "APPROVED"}, payload={"policy_version": "2026.11-1"},
                       prev_hash=prev)
        doc["event_hash"] = hash_document(doc)
        prev = doc["event_hash"]
        docs.append(doc)
    return docs


def export(docs: list[dict]) -> tuple[bytes, dict]:
    import hashlib
    import json

    data = b"".join(json.dumps(d, ensure_ascii=False, sort_keys=True).encode() + b"\n" for d in docs)
    manifest = {"schema": "lbf-audit/1.0", "from_seq": 1, "to_seq": docs[-1]["seq"], "count": len(docs),
                "first_prev_hash": GENESIS, "head_hash": docs[-1]["event_hash"],
                "jsonl_sha256": hashlib.sha256(data).hexdigest()}
    return data, manifest


def test_oracle_verifier_accepts_an_application_written_chain():
    data, manifest = export(chain(12))
    verdict = oracle_audit.verify_export(data, manifest)
    assert verdict.valid and verdict.count == 12 and verdict.head_hash == manifest["head_hash"]


def test_oracle_verifier_reports_the_exact_first_broken_seq_after_tampering():
    docs = chain(12)
    docs[6]["payload"] = {"policy_version": "tampered"}
    data, manifest = export(docs)
    verdict = oracle_audit.verify_export(data, manifest)
    assert not verdict.valid and verdict.first_broken_seq == 7


def test_oracle_verifier_rejects_a_truncated_export():
    data, manifest = export(chain(5))
    verdict = oracle_audit.verify_export(data[:-40], manifest)
    assert not verdict.valid and "manifest" in verdict.reason


# --------------------------------------------------------------------------- wire-tap


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: ClassVar[list[bytes]] = []

    def log_message(self, *a):
        return

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Upstream.hits.append(body)
        payload = b'{"rc":0,"order_no":"SO-240111","total":"1.00"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST


@pytest.fixture
def tap(tmp_path):
    wiretap = load_wiretap()
    Upstream.hits = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    ledger, modes = wiretap.Ledger(str(tmp_path / "ledger.jsonl")), wiretap.Modes()
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), wiretap.make_proxy_handler(
        f"http://127.0.0.1:{upstream.server_address[1]}", ledger, modes, 5.0))
    for server in (upstream, proxy):
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield proxy.server_address[1], ledger, modes
    proxy.shutdown()
    upstream.shutdown()


def post(port: int, ref: str, timeout: float = 5.0) -> tuple[int | None, bytes | Exception]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", "/halden/orders", body=f"acct=00417&ref={ref}&item=BW-1001&qty=1",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, http.client.HTTPException) as exc:
        return None, exc
    finally:
        conn.close()


def test_wiretap_relays_and_records_received_then_completed(tap):
    port, ledger, _ = tap
    status, body = post(port, "LBAAAAAAAAAA")
    assert status == 200 and b"SO-240111" in body
    [req] = parse_ledger(ledger.read())
    assert (req.form_ref, req.forwarded, req.status, req.client_outcome) == ("LBAAAAAAAAAA", True, 200, "RELAYED")
    assert len(Upstream.hits) == 1


def test_wiretap_blackhole_records_receipt_and_never_forwards(tap):
    port, ledger, modes = tap
    modes.arm("blackhole", hold_seconds=5)
    status, err = post(port, "LBBBBBBBBBBB", timeout=0.5)
    assert status is None and isinstance(err, socket.timeout | TimeoutError | OSError)
    deadline = datetime.now(UTC).timestamp() + 5
    while datetime.now(UTC).timestamp() < deadline:
        recs = parse_ledger(ledger.read())
        if recs and recs[0].forwarded is not None:
            break
    [req] = parse_ledger(ledger.read())
    assert req.form_ref == "LBBBBBBBBBBB" and req.forwarded is False and req.mode == "blackhole"
    assert Upstream.hits == []


def test_wiretap_drop_after_forward_resets_the_client_after_the_backend_got_it(tap):
    port, ledger, modes = tap
    modes.arm("drop_after_forward")
    status, err = post(port, "LBCCCCCCCCCC")
    assert status is None and isinstance(err, Exception)
    [req] = parse_ledger(ledger.read())
    assert req.forwarded is True and req.client_outcome == "RESET_AFTER_FORWARD" and len(Upstream.hits) == 1


def test_wiretap_corrupt_response_forwards_then_garbles(tap):
    port, ledger, modes = tap
    modes.arm("corrupt_response")
    status, body = post(port, "LBDDDDDDDDDD")
    assert status == 200 and b"SO-240111" not in body and b"WIRETAP-CORRUPTED" in body
    [req] = parse_ledger(ledger.read())
    assert req.forwarded is True and len(Upstream.hits) == 1


def test_wiretap_modes_are_consumed_and_counts_are_per_reference(tap):
    port, ledger, modes = tap
    modes.arm("corrupt_response", count=1)
    post(port, "LBEEEEEEEEEE")
    post(port, "LBEEEEEEEEEE")
    post(port, "LBFFFFFFFFFF")
    reqs = parse_ledger(ledger.read())
    assert [r.client_outcome for r in reqs] == ["CORRUPTED_RESPONSE", "RELAYED", "RELAYED"]
    assert sum(1 for r in reqs if r.form_ref == "LBEEEEEEEEEE") == 2
