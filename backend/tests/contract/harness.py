"""Backend harnesses for the conformance suite. Test infrastructure may know which backend it drives; the
conformance tests themselves use only the port, the capability predicates and these oracle functions."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lbf.adapters.halden.adapter import HaldenAdapter
from lbf.adapters.halden.fake import FakeHalden, FakeLegacyClient
from lbf.adapters.skarvik.adapter import SkarvikAdapter
from lbf.adapters.skarvik.fake import InProcessSkarvik, ManualClock
from lbf.domain.correlation import message_id, new_correlation_value
from lbf.domain.models import ItemMapping, ReconciliationQuery, SubmissionLine, SubmissionRequest
from lbf.ports.capabilities import skarvik_capabilities

REPO = Path(__file__).resolve().parents[3]
HALDEN_SEED = REPO / "legacy-systems/halden-erp/src/main/resources/db/seed.sql"
SKARVIK_SRC = REPO / "legacy-systems" / "skarvik-oes" / "src"
SKARVIK_SEED = REPO / "legacy-systems" / "skarvik-oes" / "seed" / "seed.json"


def load_mappings() -> tuple[ItemMapping, ...]:
    sys.path.insert(0, str(REPO / "backend" / "migrations" / "versions"))
    try:
        seed = __import__("0002_seed_master_data")
    finally:
        sys.path.pop(0)
    return tuple(ItemMapping(backend_id=b, native_item=n, canonical_sku=c, native_uom=u, factor_num=fn, factor_den=fd)
                 for b, n, c, u, fn, fd in seed.ITEM_MAPPINGS)


Clock = ManualClock


class SimulatedCrash(BaseException):
    pass


class Harness:
    name: str
    port: Any
    good_customer: str
    held_customer: str
    item: str
    item2: str

    def caps(self):
        return self.port.capabilities()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def request(self, *, customer: str | None = None, lines: list[tuple[str, int]] | None = None,
                cv: str | None = None, attempt: int = 1) -> SubmissionRequest:
        cv = cv or new_correlation_value()
        lines = lines or [(self.item, 2)]
        return SubmissionRequest(
            operation_id=uuid.uuid4(), attempt_no=attempt, transmission_no=1, authorization_id=uuid.uuid4(),
            backend_id=self.caps().backend_id, native_customer_id=customer or self.good_customer,
            lines=[SubmissionLine(native_item=i, quantity_native=q) for i, q in lines], correlation_value=cv,
            message_id=message_id(cv, attempt) if self.caps().is_async else None)

    def query(self, req: SubmissionRequest, first_handoff_at: datetime,
              anchor: str | None = None) -> ReconciliationQuery:
        return ReconciliationQuery(operation_id=req.operation_id, backend_id=req.backend_id,
                                   native_customer_id=req.native_customer_id, correlation_value=req.correlation_value,
                                   message_id=req.message_id, expected_lines=req.lines, anchor_order_ref=anchor,
                                   first_handoff_at=first_handoff_at, attempt_no=req.attempt_no)

    # oracle hooks (implemented per harness)
    def handoffs(self, req: SubmissionRequest) -> int: ...
    def orders(self, req: SubmissionRequest) -> list[str]: ...
    def settle(self) -> None: ...
    def arm_uncertain_commit(self) -> None: ...
    def plant_duplicate(self, req: SubmissionRequest) -> None: ...
    def state_digest(self) -> str: ...
    def io_count(self) -> int: ...
    def make_snapshots_stale(self, seconds: float) -> None:
        raise NotImplementedError("only snapshot backends")
    def close(self) -> None:
        return None


class HaldenFakeHarness(Harness):
    name = "halden_fake"
    good_customer, held_customer, item, item2 = "00417", "10871", "BW-1001", "BW-1004"

    def __init__(self) -> None:
        self.backend = FakeHalden.from_seed(HALDEN_SEED)
        self.client = FakeLegacyClient(self.backend)
        self.port = HaldenAdapter("http://halden.invalid", "x", "y", mappings=load_mappings(), client=self.client)

    def handoffs(self, req):
        return sum(1 for r in self.backend.received_refs if r == req.correlation_value)

    def orders(self, req):
        return [o.order_no for o in self.backend.orders_with_ref(req.correlation_value)]

    def settle(self):
        return None

    def arm_uncertain_commit(self):
        self.backend.next_e8_modes.append("commit_then_uncertain")

    def plant_duplicate(self, req):
        lines = [(ln.native_item, ln.quantity_native) for ln in req.lines]
        self.backend.book(req.native_customer_id, req.correlation_value, lines)
        self.backend.book(req.native_customer_id, req.correlation_value, lines)

    def state_digest(self):
        b = self.backend
        state = {"orders": {k: vars(v) for k, v in sorted(b.orders.items())}, "e8": b.e8_received,
                 "stock": b.stock}
        return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()

    def io_count(self):
        return self.backend.read_count + self.backend.e8_received


class SkarvikFakeHarness(Harness):
    """SKARVIK adapter over a temporary exchange share processed by the frozen simulator in-process (no container)."""

    name = "skarvik_fake"
    good_customer, held_customer, item, item2 = "KND-0081", "KND-0190", "NVB-6205-2RS", "NVV-DN50-SS"

    def __init__(self, tmp: Path) -> None:
        self.backend = InProcessSkarvik(tmp, simulator_src=SKARVIK_SRC, seed_file=SKARVIK_SEED,
                                        dedup_window_seconds=120)
        self.sim = self.backend.sim
        self.clock = self.backend.clock
        self.port = SkarvikAdapter(tmp / "exchange", mappings=load_mappings(),
                                   capabilities=skarvik_capabilities(test_profile=True), clock=self.clock)

    def now(self):
        return self.clock()

    def handoffs(self, req):
        claimed = sum(1 for r in self.sim.oracle_received() if (r["msgid"] or "").startswith(req.correlation_value))
        waiting = len(list(self.sim.inbox.glob(f"ORD_{req.correlation_value}-*.csv")))
        return claimed + waiting

    def orders(self, req):
        return [o["orderno"] for o in self.sim.oracle_orders() if (o["msgid"] or "").startswith(req.correlation_value)]

    def settle(self):
        self.sim.pickup_once()
        self.sim.flush_due_acks()
        self.clock.advance((self.caps().snapshot_max_lag_seconds or 0) + 1)
        self.sim.generate_extracts(force=True)

    def arm_uncertain_commit(self):
        self.sim.faults.counted["drop_ack"] = 1  # committed; the acknowledgement never arrives

    def plant_duplicate(self, req):
        rendered = self.port.render(req)
        self.sim.faults.duplicate_window_seconds = 1
        for _ in range(2):
            (self.sim.inbox / f"ORD_{req.message_id}.csv").write_bytes(rendered.payload)
            self.sim.pickup_once()
            self.clock.advance(2)
        self.sim.faults.duplicate_window_seconds = None

    def state_digest(self):
        state = {"orders": self.sim.oracle_orders(), "received": self.sim.oracle_received(),
                 "inbox": sorted(p.name for p in self.sim.inbox.iterdir()),
                 "withdrawn": sorted(p.name for p in self.sim.withdrawn.iterdir())}
        return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()

    def io_count(self):
        ex = self.sim.config.exchange_dir
        return sum(1 for _ in ex.rglob("*"))

    def make_snapshots_stale(self, seconds):
        self.sim.faults.stale_extracts = True
        self.clock.advance(seconds)
