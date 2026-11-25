"""Engine runner: holds the singleton lease and runs each engine loop in its own thread."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from lbf.engines.ack_poller import AckPoller
from lbf.engines.executor import Executor, new_engine_id
from lbf.engines.lease import EngineLease
from lbf.engines.pause import PausePoints
from lbf.engines.reconciler import Reconciler
from lbf.engines.refresher import ValidationRefresher
from lbf.engines.sweeper import Sweeper
from lbf.workflow.context import WorkflowContext

log = logging.getLogger("lbf.engines")


@dataclass(frozen=True)
class Intervals:
    executor: float = 0.2
    ack_poller: float = 1.0
    reconciler: float = 0.5
    sweeper: float = 5.0
    refresher: float = 30.0
    heartbeat: float = 5.0


class EngineSet:
    """The five engines of one process. ``step()`` runs each once (deterministic tests)."""

    def __init__(self, ctx: WorkflowContext, engine_id: str | None = None, pauses: PausePoints | None = None) -> None:
        self.engine_id = engine_id or new_engine_id()
        self.pauses = pauses or PausePoints()
        self.executor = Executor(ctx, self.engine_id, self.pauses)
        self.ack_poller = AckPoller(ctx, self.engine_id, self.pauses)
        self.reconciler = Reconciler(ctx, self.engine_id, self.pauses)
        self.sweeper = Sweeper(ctx, self.engine_id)
        self.refresher = ValidationRefresher(ctx, self.engine_id)

    def step(self) -> int:
        n = self.sweeper.run_once()
        while self.executor.run_once():
            n += 1
        n += self.ack_poller.run_once() + self.reconciler.run_once()
        return n


class EngineRunner:
    def __init__(self, ctx: WorkflowContext, *, engine_id: str | None = None, pauses: PausePoints | None = None,
                 intervals: Intervals | None = None) -> None:
        self.ctx = ctx
        self.engines = EngineSet(ctx, engine_id, pauses)
        self.lease = EngineLease(ctx.engine, self.engines.engine_id, ctx.clock)
        self.intervals = intervals or Intervals()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def leader(self) -> bool:
        return self.lease.held

    def start(self) -> bool:
        if not self.lease.acquire():
            log.warning("engine lease held elsewhere: this process serves no engines (degraded:not-engine-leader)")
            return False
        self.engines.sweeper.run_once()  # recovery at engine start
        e, i = self.engines, self.intervals
        loops: list[tuple[str, Callable[[], object], float]] = [
            ("executor", e.executor.run_once, i.executor), ("ack-poller", e.ack_poller.run_once, i.ack_poller),
            ("reconciler", e.reconciler.run_once, i.reconciler), ("sweeper", e.sweeper.run_once, i.sweeper),
            ("refresher", e.refresher.run_once, i.refresher), ("heartbeat", self.lease.heartbeat, i.heartbeat)]
        for name, fn, interval in loops:
            th = threading.Thread(target=self._loop, args=(name, fn, interval), name=f"lbf-{name}", daemon=True)
            th.start()
            self._threads.append(th)
        return True

    def _loop(self, name: str, fn: Callable[[], object], interval: float) -> None:
        while not self._stop.is_set():
            try:
                fn()
            except Exception:
                log.exception("engine %s iteration failed", name)
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=10)
        self.lease.release()
