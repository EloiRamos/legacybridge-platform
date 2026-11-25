"""Workflow/engine test harness: real PostgreSQL (``lbf_test``), the offline HALDEN fake and the frozen SKARVIK
simulator in-process, one manual clock shared by all of them, and the engines stepped deterministically."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from support import make_user

from lbf.adapters.halden.adapter import HaldenAdapter
from lbf.adapters.halden.fake import FakeHalden, FakeLegacyClient
from lbf.adapters.skarvik.adapter import SkarvikAdapter
from lbf.adapters.skarvik.fake import InProcessSkarvik, ManualClock
from lbf.auth.policy import Principal
from lbf.domain.enums import OperationState, Role
from lbf.engines.pause import PausePoints
from lbf.engines.runner import EngineSet
from lbf.persistence import tables as t
from lbf.ports.capabilities import skarvik_capabilities
from lbf.workflow import commands
from lbf.workflow.context import WorkflowContext, WorkflowSettings, load_mappings
from lbf.workflow.drafts import DraftRequest, LineRequest

REPO = Path(__file__).resolve().parents[2]
HALDEN_SEED = REPO / "legacy-systems/halden-erp/src/main/resources/db/seed.sql"
SKARVIK_SRC = REPO / "legacy-systems/skarvik-oes/src"
SKARVIK_SEED = REPO / "legacy-systems/skarvik-oes/seed/seed.json"
S = OperationState

ACCOUNT = {"halden": "00417", "skarvik": "KND-0081"}
HELD = {"halden": "10871", "skarvik": "KND-0190"}
ITEM = "BW-1001"  # canonical; HALDEN BW-1001, SKARVIK NVB-6205-2RS


@dataclass
class WF:
    engine: Any
    tmp: Path
    clock: ManualClock = field(default_factory=ManualClock)
    profile: str = "test"
    time_scale: float = 1.0
    hook: Any = None

    def __post_init__(self) -> None:
        self.halden = FakeHalden.from_seed(HALDEN_SEED)
        self.halden_client = FakeLegacyClient(self.halden)
        self.skarvik = InProcessSkarvik(self.tmp, simulator_src=SKARVIK_SRC, seed_file=SKARVIK_SEED,
                                        dedup_window_seconds=120, clock=self.clock)
        with self.engine.connect() as conn:
            mappings = load_mappings(conn)
        self.ports = {
            "halden": HaldenAdapter("http://halden.invalid", "x", "y", mappings=mappings, client=self.halden_client,
                                    clock=self.clock),
            "skarvik": SkarvikAdapter(self.skarvik.exchange_dir, mappings=mappings,
                                      capabilities=skarvik_capabilities(test_profile=True), clock=self.clock),
        }
        self.settings = WorkflowSettings(profile=self.profile, time_scale=self.time_scale)
        self.ctx = WorkflowContext(engine=self.engine, backends=self.ports, mappings=mappings, clock=self.clock,
                                   settings=self.settings)
        self.pauses = PausePoints(profile=self.profile, hook=self._hook)
        self.engines = EngineSet(self.ctx, "engine-test-1", self.pauses)
        self.users = {
            "daniel": make_user(self.engine, "daniel.sales", Role.SALES_OPS),
            "priya": make_user(self.engine, "priya.sales", Role.SALES_OPS),
            "marta": make_user(self.engine, "marta.ops", Role.OPS_MANAGER),
            "rafael": make_user(self.engine, "rafael.integration", Role.INTEGRATION_ENGINEER),
            "helena": make_user(self.engine, "helena.compliance", Role.COMPLIANCE_REVIEWER),
            "sanna": make_user(self.engine, "sanna.inventory", Role.INVENTORY_COORDINATOR),
            "tomas": make_user(self.engine, "tomas.admin", Role.SYSADMIN),
        }
        self.roles = {"daniel": Role.SALES_OPS, "priya": Role.SALES_OPS, "marta": Role.OPS_MANAGER,
                      "rafael": Role.INTEGRATION_ENGINEER, "helena": Role.COMPLIANCE_REVIEWER,
                      "sanna": Role.INVENTORY_COORDINATOR, "tomas": Role.SYSADMIN}

    def _hook(self, name: str) -> None:
        if self.hook is not None:
            self.hook(name)

    # ------------------------------------------------------------------ principals and commands
    def p(self, name: str, *, fresh: bool = True) -> Principal:
        return Principal(user_id=self.users[name], username=name, roles=frozenset({self.roles[name]}),
                         authenticated_at=self.clock() if fresh else self.clock() - timedelta(hours=1))

    def request(self, backend: str, *, customer: str | None = None, qty: int = 10, sku: str = ITEM,
                **extra: Any) -> DraftRequest:
        return DraftRequest(backend_id=backend, native_customer_id=customer or ACCOUNT[backend],
                            lines=[LineRequest(canonical_sku=sku, quantity=qty)], **extra)

    def draft(self, backend: str, by: str = "daniel", **kw: Any) -> UUID:
        return commands.create_draft(self.ctx, self.p(by), self.request(backend, **kw)).operation_id

    def submit(self, op: UUID, by: str = "daniel") -> commands.SubmitResult:
        return commands.submit_for_approval(self.ctx, self.p(by), op, self.row(op).current_version)

    def approve(self, op: UUID, by: str = "priya", **kw: Any) -> commands.ApprovalResult:
        row = self.row(op)
        with self.engine.connect() as conn:
            h = conn.execute(select(t.draft_versions.c.content_hash).where(
                t.draft_versions.c.operation_id == op, t.draft_versions.c.version == row.current_version)).scalar()
        return commands.approve(self.ctx, self.p(by), op, version=row.current_version, content_hash=h, **kw)

    def approved(self, backend: str, **kw: Any) -> UUID:
        op = self.draft(backend, **kw)
        assert not self.submit(op).blocking
        self.approve(op)
        return op

    # ------------------------------------------------------------------ engines and backends
    def step(self, n: int = 1) -> None:
        for _ in range(n):
            self.engines.step()

    def settle_skarvik(self) -> None:
        self.skarvik.pickup()
        self.skarvik.flush_acks()
        self.clock.advance((self.ports["skarvik"].capabilities().snapshot_max_lag_seconds or 0) + 1)
        self.skarvik.extracts()

    # ------------------------------------------------------------------ observation
    def row(self, op: UUID) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(select(t.operations).where(t.operations.c.id == op)).one()

    def state(self, op: UUID) -> tuple[str, str | None]:
        r = self.row(op)
        return r.state, r.last_evidence

    def transmissions(self, op: UUID) -> list[Any]:
        with self.engine.connect() as conn:
            return list(conn.execute(select(t.transmissions).where(t.transmissions.c.operation_id == op)
                                     .order_by(t.transmissions.c.started_at)).all())

    def events(self, op: UUID) -> list[str]:
        with self.engine.connect() as conn:
            return list(conn.execute(select(t.audit_events.c.event_type).where(t.audit_events.c.operation_id == op)
                                     .order_by(t.audit_events.c.seq)).scalars())

    def handoffs(self, op: UUID) -> int:
        """Oracle count of handoffs that reached the backend for this operation's correlation value."""
        cv = self.row(op).correlation_value
        halden = sum(1 for r in self.halden.received_refs if r == cv)
        skarvik_claimed = sum(1 for r in self.skarvik.received() if (r["msgid"] or "").startswith(cv))
        skarvik_waiting = len(list(self.skarvik.inbox.glob(f"ORD_{cv}-*.csv")))
        return halden + skarvik_claimed + skarvik_waiting

    def backend_orders(self, op: UUID) -> list[str]:
        cv = self.row(op).correlation_value
        return ([o.order_no for o in self.halden.orders_with_ref(cv)]
                + [o["orderno"] for o in self.skarvik.orders() if (o["msgid"] or "").startswith(cv)])
