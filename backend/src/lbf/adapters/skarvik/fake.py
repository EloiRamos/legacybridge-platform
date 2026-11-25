"""Offline SKARVIK backend for tests: the FROZEN simulator (``legacy-systems/skarvik-oes``) run in-process against a
temporary exchange share, with a controllable clock.

For SKARVIK a hand-written fake would be a second,
unfrozen implementation of ``skarvik-exchange/1.0``; driving the frozen simulator in-process (no container, no
network) gives the same offline determinism with the frozen behaviour. The simulator source path is passed in
explicitly; production code never imports this module (boundary test).
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class ManualClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 21, 13, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class InProcessSkarvik:
    """Frozen simulator on ``<root>/exchange`` + ``<root>/data``; every step is explicit (no background threads)."""

    def __init__(self, root: Path, *, simulator_src: Path, seed_file: Path, dedup_window_seconds: int = 120,
                 clock: ManualClock | None = None) -> None:
        if str(simulator_src) not in sys.path:
            sys.path.insert(0, str(simulator_src))
        simulator = importlib.import_module("skarvik.simulator")
        self.clock = clock or ManualClock()
        self.exchange_dir = root / "exchange"
        cfg = simulator.Config(exchange_dir=self.exchange_dir, data_dir=root / "data", seed_file=seed_file,
                               dedup_window_seconds=dedup_window_seconds)

        def crash() -> None:
            raise simulator.SimulatedCrash("crash_after_commit")

        self.sim: Any = simulator.Simulator(cfg, clock=self.clock, crash=crash)
        self.sim.generate_extracts(force=True)

    def pickup(self) -> list[str]:
        return list(self.sim.pickup_once())

    def flush_acks(self) -> int:
        return int(self.sim.flush_due_acks())

    def extracts(self) -> None:
        self.sim.generate_extracts(force=True)

    def orders(self) -> list[dict[str, Any]]:
        return list(self.sim.oracle_orders())

    def received(self) -> list[dict[str, Any]]:
        return list(self.sim.oracle_received())

    @property
    def faults(self) -> Any:
        return self.sim.faults

    @property
    def inbox(self) -> Path:
        return Path(self.sim.inbox)

    @property
    def withdrawn(self) -> Path:
        return Path(self.sim.withdrawn)
