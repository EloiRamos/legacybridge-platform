"""Singleton engine lease: a PostgreSQL session-level advisory lock held on a dedicated connection (C-10).

A second process started by mistake does not obtain the lease and serves no engines (``/readyz`` reports
``degraded:not-engine-leader``). Correctness does not depend on the lease (C-05).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert

from lbf.persistence import tables as t
from lbf.persistence.uow import UnitOfWork
from lbf.workflow import metrics
from lbf.workflow.records import system_event

ENGINE_LEASE_KEY = 0x4C42460001


class EngineLease:
    def __init__(self, engine: Engine, engine_id: str, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.engine_id = engine_id
        self.clock = clock
        self._conn: Any = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    def acquire(self) -> bool:
        if self._conn is not None:
            return True
        conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ENGINE_LEASE_KEY}).scalar())
        if not got:
            conn.close()
            metrics.ENGINE_LEADER.set(0)
            return False
        self._conn = conn
        metrics.ENGINE_LEADER.set(1)
        with UnitOfWork(self.engine, clock=self.clock) as uow:
            now = uow.clock()
            uow.conn.execute(insert(t.engine_heartbeats).values(engine_id=self.engine_id, started_at=now,
                                                                last_beat=now)
                             .on_conflict_do_update(index_elements=["engine_id"],
                                                    set_={"started_at": now, "last_beat": now}))
            uow.append_event(system_event(self.engine_id, "ENGINE_LEADER_ACQUIRED",
                                          payload={"engine_id": self.engine_id}))
        return True

    def heartbeat(self) -> None:
        if self._conn is None:
            return
        now = self.clock()
        with self.engine.begin() as conn:
            conn.execute(insert(t.engine_heartbeats).values(engine_id=self.engine_id, started_at=now, last_beat=now)
                         .on_conflict_do_update(index_elements=["engine_id"], set_={"last_beat": now}))

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ENGINE_LEASE_KEY})
        finally:
            self._conn.close()
            self._conn = None
            metrics.ENGINE_LEADER.set(0)
