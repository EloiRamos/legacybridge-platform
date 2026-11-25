"""Workflow context: the database engine, the backend ports, master data, the clock and timing settings.

Backends are looked up by id in a mapping (data, not control flow); workflow code never branches on backend
identity — behavioural differences come only from :class:`BackendCapabilities`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, Engine, select

from lbf.domain.errors import DomainError, ErrorCode
from lbf.domain.models import ItemMapping
from lbf.persistence import tables as t
from lbf.ports.backend import LegacyBackendPort
from lbf.ports.capabilities import BackendCapabilities

RECHECK_OFFSETS_SECONDS = (5, 15, 60, 300, 900)
RECHECK_LIMIT = len(RECHECK_OFFSETS_SECONDS)
UNKNOWN_ESCALATION_SECONDS = 1800  # 30 min (T28)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WorkflowSettings:
    profile: str = "default"  # default | demo | test
    time_scale: float = 1.0  # LBF_TIME_SCALE, honoured only in the test profile
    authorization_ttl: timedelta = timedelta(minutes=15)
    claim_lease: timedelta = timedelta(seconds=30)
    transport_timeout_seconds: float = 5.0
    reconciliation_lease: timedelta = timedelta(seconds=60)
    validation_refresh_after: timedelta = timedelta(minutes=8)

    @classmethod
    def from_env(cls) -> WorkflowSettings:
        profile = os.environ.get("LBF_PROFILE", "default")
        return cls(
            profile=profile,
            time_scale=float(os.environ.get("LBF_TIME_SCALE", "1.0")),
            transport_timeout_seconds=float(os.environ.get("LBF_TRANSPORT_TIMEOUT_SEC", "5")),
        )

    @property
    def scale(self) -> float:
        """Only the re-check schedule and the 30-minute escalation timer are scaled, and only in ``test``."""
        return self.time_scale if self.profile == "test" else 1.0

    def recheck_due(self, unknown_since: datetime, auto_rechecks: int) -> datetime | None:
        if auto_rechecks >= RECHECK_LIMIT:
            return None
        return unknown_since + timedelta(seconds=RECHECK_OFFSETS_SECONDS[auto_rechecks] * self.scale)

    def unknown_escalation_at(self, unknown_since: datetime) -> datetime:
        return unknown_since + timedelta(seconds=UNKNOWN_ESCALATION_SECONDS * self.scale)

    def transmission_lease(self) -> timedelta:
        return timedelta(seconds=self.transport_timeout_seconds + 30)


@dataclass
class WorkflowContext:
    engine: Engine
    backends: Mapping[str, LegacyBackendPort]
    mappings: tuple[ItemMapping, ...] = ()
    clock: Callable[[], datetime] = utcnow
    settings: WorkflowSettings = field(default_factory=WorkflowSettings)

    def port(self, backend_id: str) -> LegacyBackendPort:
        try:
            return self.backends[backend_id]
        except KeyError as exc:
            raise DomainError(ErrorCode.NOT_FOUND, f"unknown backend {backend_id!r}") from exc

    def caps(self, backend_id: str) -> BackendCapabilities:
        return self.port(backend_id).capabilities()

    def mappings_for(self, backend_id: str) -> dict[str, ItemMapping]:
        return {m.native_item: m for m in self.mappings if m.backend_id == backend_id}


def load_mappings(conn: Connection) -> tuple[ItemMapping, ...]:
    rows = conn.execute(select(t.item_mappings)).all()
    return tuple(
        ItemMapping(backend_id=r.backend_id, native_item=r.native_item, canonical_sku=r.canonical_sku,
                    native_uom=r.native_uom, factor_num=r.factor_num, factor_den=r.factor_den)
        for r in rows
    )


def linked_accounts(conn: Connection, backend_id: str, native_customer_id: str) -> list[tuple[str, str]]:
    """Accounts linked to this one through the curated crosswalk (rule C-2), excluding itself."""
    pa = t.party_accounts
    party = conn.execute(
        select(pa.c.party_id).where(pa.c.backend_id == backend_id, pa.c.native_customer_id == native_customer_id)
    ).scalar()
    if party is None:
        return []
    rows = list(conn.execute(
        select(pa.c.backend_id, pa.c.native_customer_id).where(pa.c.party_id == party)
    ).all())
    return [(r.backend_id, r.native_customer_id) for r in rows
            if (r.backend_id, r.native_customer_id) != (backend_id, native_customer_id)]
