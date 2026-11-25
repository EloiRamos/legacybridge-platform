"""Engine pause points: ``after_claim``, ``before_handoff``, ``after_handoff_before_observe``,
``before_ack_record``, ``mid_reconcile``.

Active **only** when ``LBF_PROFILE=test`` and the point is listed in ``LBF_TEST_PAUSE_POINTS`` (SEC-17): the engine
writes ``<dir>/<point>.reached`` and blocks until ``<point>.release`` exists. Tests running in-process may install
a callable hook instead (also test profile only), e.g. to model a process kill at that point.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

POINTS = ("after_claim", "before_handoff", "after_handoff_before_observe", "before_ack_record", "mid_reconcile")


class PausePoints:
    def __init__(self, profile: str = "default", points: frozenset[str] = frozenset(),
                 directory: Path | None = None, hook: Callable[[str], None] | None = None,
                 max_wait_seconds: float = 600.0) -> None:
        unknown = set(points) - set(POINTS)
        if unknown:
            raise ValueError(f"unknown pause points {sorted(unknown)}")
        self.active = profile == "test"
        self.points = points if self.active else frozenset()
        self.directory = directory
        self.hook = hook if self.active else None
        self.max_wait_seconds = max_wait_seconds

    @classmethod
    def from_env(cls) -> PausePoints:
        raw = os.environ.get("LBF_TEST_PAUSE_POINTS", "")
        return cls(profile=os.environ.get("LBF_PROFILE", "default"),
                   points=frozenset(p.strip() for p in raw.split(",") if p.strip()),
                   directory=Path(os.environ.get("LBF_PAUSE_DIR", "/run/lbf-pause")))

    def reach(self, name: str) -> None:
        if not self.active:
            return
        if self.hook is not None:
            self.hook(name)
        if name not in self.points or self.directory is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        reached, release = self.directory / f"{name}.reached", self.directory / f"{name}.release"
        reached.write_text(str(time.time()))
        deadline = time.monotonic() + self.max_wait_seconds
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        release.unlink(missing_ok=True)
        reached.unlink(missing_ok=True)
