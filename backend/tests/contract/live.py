"""Live conformance harnesses: the adapters against the running test-profile simulators
(``docker compose --profile test up -d --build halden skarvik wiretap`` with ``LBF_SIM_CONTROL=true`` and the
test-profile SKARVIK timings). "Live" means the local simulator environment only — no external system.

Oracles are independent of the application: HALDEN ground truth through ``lbf_oracles.halden_client`` (direct, not
through the wire-tap), application handoffs through the wire-tap ledger, SKARVIK through its oracle endpoints.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from lbf.adapters.halden.adapter import HaldenAdapter
from lbf.adapters.skarvik.adapter import SkarvikAdapter
from lbf.ports.capabilities import skarvik_capabilities

from .harness import REPO, Harness, load_mappings

sys.path.insert(0, str(REPO / "oracles" / "src"))
from lbf_oracles.halden_client import HaldenOracle
from lbf_oracles.skarvik_client import SkarvikOracle
from lbf_oracles.wiretap_ledger import WiretapOracle

HALDEN_VIA_WIRETAP = os.environ.get("LBF_TEST_HALDEN_URL", "http://127.0.0.1:58091")
HALDEN_DIRECT = os.environ.get("LBF_TEST_HALDEN_ORACLE_URL", "http://127.0.0.1:58090")
WIRETAP_CONTROL = os.environ.get("LBF_TEST_WIRETAP_CONTROL_URL", "http://127.0.0.1:58099")
SKARVIK_CONTROL = os.environ.get("LBF_TEST_SKARVIK_CONTROL_URL", "http://127.0.0.1:58095")
EXCHANGE = Path(os.environ.get("LBF_TEST_EXCHANGE_DIR", str(REPO / "governance" / "work" / "skarvik-exchange")))
# The HALDEN credential the test-profile stack mounts as its Docker secret (deploy/compose.test.yaml).
HALDEN_SECRET_FILE = Path(os.environ.get("LBF_TEST_HALDEN_SECRET_FILE",
                                         str(REPO / "deploy" / "test-fixtures" / "halden_secret.txt")))


def _digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class HaldenLiveHarness(Harness):
    name = "halden_live"
    good_customer, held_customer, item, item2 = "00417", "10871", "BW-1001", "BW-1004"

    def __init__(self) -> None:
        secret = HALDEN_SECRET_FILE.read_text(encoding="utf-8").strip()
        self.oracle = HaldenOracle(HALDEN_DIRECT, secret=secret)
        self.tap = WiretapOracle(WIRETAP_CONTROL)
        self.oracle.sim_clear_faults()
        self.oracle.sim_reset(rate_limit=100000)
        self.tap.reset()
        self.port = HaldenAdapter(HALDEN_VIA_WIRETAP, "svc_bridge", secret, mappings=load_mappings(),
                                  timeout=5.0)

    def handoffs(self, req):
        return self.tap.e8_count(req.correlation_value)

    def orders(self, req):
        return self.oracle.orders_with_ref(req.correlation_value)

    def settle(self):
        return None

    def arm_uncertain_commit(self):
        self.oracle.sim_fault("POST", "/halden/orders", "commit_then_500", 1)

    def plant_duplicate(self, req):
        lines = [(ln.native_item, ln.quantity_native) for ln in req.lines]
        for _ in range(2):
            status, body = self.oracle.raw_e8(req.native_customer_id, req.correlation_value, lines)
            assert status == 200 and json.loads(body)["rc"] == 0, body

    def state_digest(self):
        return _digest({"orders": self.oracle.all_orders(), "stock": self.oracle.stock_text(),
                        "e8": sum(self.tap.e8_counts().values())})

    def io_count(self):
        return len(self.tap.requests())

    def close(self):
        self.port.close()
        self.oracle.sim_clear_faults()


class SkarvikLiveHarness(Harness):
    name = "skarvik_live"
    good_customer, held_customer, item, item2 = "KND-0081", "KND-0190", "NVB-6205-2RS", "NVV-DN50-SS"

    def __init__(self) -> None:
        self.oracle = SkarvikOracle(SKARVIK_CONTROL)
        self.oracle.clear_faults()
        self.oracle.reset()
        self.port = SkarvikAdapter(EXCHANGE, mappings=load_mappings(),
                                   capabilities=skarvik_capabilities(test_profile=True))

    def now(self):
        return datetime.now(UTC)

    def _await(self, predicate, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)

    def handoffs(self, req):
        claimed = len(self.oracle.claims_for_message_prefix(req.correlation_value))
        waiting = len(list((EXCHANGE / "inbox").glob(f"ORD_{req.correlation_value}-*.csv")))
        return claimed + waiting

    def orders(self, req):
        return self.oracle.orders_for_message_prefix(req.correlation_value)

    def settle(self):
        self.oracle.pickup()
        self._await(lambda: self.oracle.status()["pending_acks"] == 0)
        time.sleep((self.caps().snapshot_max_lag_seconds or 0) + 1.5)
        self.oracle.extracts()

    def arm_uncertain_commit(self):
        self.oracle.fault("drop_ack", count=1)

    def plant_duplicate(self, req):
        rendered = self.port.render(req)
        self.oracle.fault("duplicate_window_seconds", value=1)
        try:
            for _ in range(2):
                (EXCHANGE / "inbox" / f"ORD_{req.message_id}.csv").write_bytes(rendered.payload)
                self.oracle.pickup()
                time.sleep(2.2)
        finally:
            self.oracle.fault("duplicate_window_seconds", value=None)

    def state_digest(self):
        return _digest({"orders": self.oracle.orders(), "received": self.oracle.received(),
                        "inbox": sorted(p.name for p in (EXCHANGE / "inbox").iterdir()),
                        "withdrawn": sorted(p.name for p in (EXCHANGE / "withdrawn").iterdir())})

    def io_count(self):
        return sum(1 for d in ("inbox", "withdrawn", "outbox/acks/archive") for _ in (EXCHANGE / d).iterdir())

    def make_snapshots_stale(self, seconds):
        self.oracle.fault("stale_extracts", value=True)
        time.sleep(seconds)

    def close(self):
        self.oracle.clear_faults()


def make_live(name: str) -> Harness:
    return {"halden_live": HaldenLiveHarness, "skarvik_live": SkarvikLiveHarness}[name]()
