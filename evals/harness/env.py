"""Deterministic evaluation environment: a fresh database ``lbf_eval`` on the test-profile PostgreSQL
(127.0.0.1:55432), the offline backends (HALDEN fake from the frozen seed, the frozen SKARVIK simulator in-process),
the seeded personas and three fixture operations (UNKNOWN, ESCALATED, AWAITING_ACK). No network beyond loopback."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

REPO = Path(__file__).resolve().parents[2]
SECRETS = Path(os.environ.get("LBF_TEST_SECRETS_DIR") or REPO / "deploy" / "secrets")  # synthetic values elsewhere
PASSWORD = "eval-persona-password-not-a-secret"  # noqa: S105 - throwaway eval DB personas


def _secret(name: str) -> str:
    return (SECRETS / f"{name}.txt").read_text(encoding="utf-8").strip()


@dataclass
class EvalEnv:
    db_name: str = "lbf_eval"
    port: int = int(os.environ.get("LBF_TEST_DB_PORT", "55432"))
    fixtures: dict[str, str] = field(default_factory=dict)

    def build(self) -> EvalEnv:
        from alembic import command
        from alembic.config import Config

        from lbf.adapters.halden.adapter import HaldenAdapter
        from lbf.adapters.halden.fake import FakeHalden, FakeLegacyClient
        from lbf.adapters.skarvik.adapter import SkarvikAdapter
        from lbf.adapters.skarvik.fake import InProcessSkarvik, ManualClock
        from lbf.api.services import AppConfig, AppServices
        from lbf.auth.service import AuthService
        from lbf.engines.runner import EngineSet
        from lbf.persistence.db import make_engine
        from lbf.persistence.settings import DbSettings
        from lbf.ports.capabilities import skarvik_capabilities
        from lbf.workflow.context import WorkflowContext, WorkflowSettings, load_mappings

        self.settings = DbSettings(host="127.0.0.1", port=self.port, name=self.db_name, passwords={
            "migrator": _secret("db_migrator"), "app": _secret("db_app"), "audit_reader": _secret("db_audit_reader"),
            "superuser": _secret("db_superuser")})
        admin = create_engine(self.settings.url("superuser", database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{self.db_name}" OWNER lbf_migrator'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{self.db_name}" TO lbf_app, lbf_audit_reader'))
        admin.dispose()
        cfg = Config(str(REPO / "backend" / "alembic.ini"))
        cfg.attributes["url"] = self.settings.url("migrator")
        command.upgrade(cfg, "head")
        self.engine = make_engine(self.settings, "app", pool_size=5)
        self.oracle_dsn = self.settings.url("superuser").replace("postgresql+psycopg://", "postgresql://")
        self.clock = ManualClock(datetime(2026, 11, 21, 9, 0, tzinfo=UTC))
        self.tmp = Path(tempfile.mkdtemp(prefix="lbf-eval-"))
        self.halden = FakeHalden.from_seed(REPO / "legacy-systems/halden-erp/src/main/resources/db/seed.sql")
        self.skarvik = InProcessSkarvik(self.tmp, simulator_src=REPO / "legacy-systems/skarvik-oes/src",
                                        seed_file=REPO / "legacy-systems/skarvik-oes/seed/seed.json",
                                        clock=self.clock)
        with self.engine.connect() as conn:
            mappings = load_mappings(conn)
        ports: dict[str, Any] = {
            "halden": HaldenAdapter("http://halden.invalid", "x", "y", mappings=mappings,
                                    client=FakeLegacyClient(self.halden), clock=self.clock),
            "skarvik": SkarvikAdapter(self.skarvik.exchange_dir, mappings=mappings,
                                      capabilities=skarvik_capabilities(test_profile=True), clock=self.clock),
        }
        self.workflow = WorkflowContext(engine=self.engine, backends=ports, mappings=mappings, clock=self.clock,
                                        settings=WorkflowSettings(profile="test"))
        self.svc = AppServices(config=AppConfig(profile="test", engines_enabled=False), engine=self.engine,
                               audit_engine=None, auth=AuthService(self.engine, clock=self.clock),
                               workflow=self.workflow)
        self.engines = EngineSet(self.workflow, "engine-eval")
        self._users()
        self._fixtures()
        return self

    def _users(self) -> None:
        from lbf.domain.enums import Role

        self.users: dict[str, Any] = {}
        roles = {"daniel": Role.SALES_OPS, "priya": Role.SALES_OPS, "marta": Role.OPS_MANAGER,
                 "sanna": Role.INVENTORY_COORDINATOR, "rafael": Role.INTEGRATION_ENGINEER}
        with self.engine.begin() as conn:
            for name, role in roles.items():
                self.users[name] = (self.svc.auth.create_user(conn, username=name, display_name=name,
                                                              password=PASSWORD, roles=[role], actor=None), role)

    def principal(self, name: str) -> Any:
        from lbf.auth.policy import Principal

        uid, role = self.users[name]
        return Principal(user_id=uid, username=name, roles=frozenset({role}), authenticated_at=self.clock())

    def _approved(self, backend: str, customer: str, sku: str, qty: int) -> Any:
        from lbf.workflow import commands
        from lbf.workflow.drafts import DraftRequest, LineRequest

        r = commands.create_draft(self.workflow, self.principal("daniel"), DraftRequest(
            backend_id=backend, native_customer_id=customer, lines=[LineRequest(canonical_sku=sku, quantity=qty)]))
        commands.submit_for_approval(self.workflow, self.principal("daniel"), r.operation_id, 1)
        commands.approve(self.workflow, self.principal("marta"), r.operation_id, version=1,
                         content_hash=r.content_hash)
        return r.operation_id

    def _fixtures(self) -> None:
        from lbf.persistence import tables as t
        from lbf.workflow import commands

        unknown = self._approved("halden", "52290", "BW-1004", 1)
        self.halden.next_e8_modes.append("lost_before_backend")
        self.engines.executor.run_once()
        escalated = self._approved("halden", "20514", "BW-1007", 2)
        self.halden.next_e8_modes.append("lost_before_backend")
        self.engines.executor.run_once()
        commands.escalate(self.workflow, self.principal("rafael"), escalated, "fixture: phoned HALDEN desk")
        self.skarvik.faults.pause_pickup = True
        awaiting = self._approved("skarvik", "KND-0203", "BW-1001", 4)
        self.engines.executor.run_once()
        with self.engine.connect() as conn:
            for name, op in (("UNKNOWN", unknown), ("ESCALATED", escalated), ("AWAITING_ACK", awaiting)):
                row = conn.execute(t.operations.select().where(t.operations.c.id == op)).one()
                assert row.state == name, (name, row.state)
                self.fixtures[name] = row.correlation_value
                self.fixtures[name + "_ID"] = str(op)

    def close(self) -> None:
        self.engine.dispose()
