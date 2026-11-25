"""The running test-profile stack (``make test-up``): compose control, process kill, pause points, reset."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "oracles" / "src"))

from lbf_oracles.halden_client import HaldenOracle  # noqa: E402
from lbf_oracles.skarvik_client import SkarvikOracle  # noqa: E402
from lbf_oracles.wiretap_ledger import WiretapOracle  # noqa: E402

# With LBF_NS set (and LBF_CAMPAIGN=1) everything runs on that isolated stack, through tools/isolated.sh; without it, on
# the test profile of the default project, and only after its ownership is proven.
NS = os.environ.get("LBF_NS")
PAUSE_DIR = Path(os.environ["LBF_PAUSE_SOURCE"]) if NS else REPO / "governance" / "work" / "lbf-pause"
EXCHANGE = Path(os.environ["LBF_EXCHANGE_SOURCE"]) if NS else REPO / "governance" / "work" / "skarvik-exchange"
TEST_ENV = {"LBF_PROFILE": "test", "LBF_SIM_CONTROL": "true",
            "SKARVIK_PICKUP_INTERVAL": "1",
            "SKARVIK_EXTRACT_INTERVAL": "2", "SKARVIK_DEDUP_WINDOW_SECONDS": "120",
            "LBF_EXCHANGE_SOURCE": "./governance/work/skarvik-exchange", "LBF_HALDEN_URL": "http://wiretap:8090",
            "LBF_TIME_SCALE": "0.02", "LBF_TRANSPORT_TIMEOUT_SEC": "1",
            "LBF_PAUSE_SOURCE": "./governance/work/lbf-pause"}
# Truncated between scenarios (an empty application database plus seeded users, crosswalk and master data).
VOLATILE = ("operations", "draft_versions", "validation_snapshots", "approvals", "authorizations", "transmissions",
            "acknowledgements", "reconciliation_runs", "escalations", "audit_events", "audit_exports",
            "assistant_turns", "idempotency_keys", "annotations", "inference_calls", "inference_budget_ledger",
            "engine_heartbeats")


def wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 0.25) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def prove_ownership() -> None:
    """Fails closed: the harness resets, stops and kills containers, so a project name alone is not enough."""
    cmd = ["./tools/isolated.sh", "owned"] if NS else ["./tools/owned_project.sh"]
    subprocess.run(cmd, cwd=REPO, check=True, capture_output=True)  # noqa: S603 (fixed argv)


def port(name: str, default: int) -> int:
    return int(os.environ[name]) if NS else default


class Stack:
    def __init__(self) -> None:
        prove_ownership()
        self.halden = HaldenOracle(f"http://127.0.0.1:{port('LBF_TAP_HALDEN_PORT', 58090)}")
        self.skarvik = SkarvikOracle(f"http://127.0.0.1:{port('LBF_TAP_SKARVIK_PORT', 58095)}")
        self.tap = WiretapOracle(f"http://127.0.0.1:{port('LBF_TAP_CONTROL_PORT', 58099)}")
        su = (REPO / "deploy/secrets/db_superuser.txt").read_text().strip()
        self.dsn = f"postgresql://postgres:{su}@127.0.0.1:{port('LBF_DB_PORT', 55432)}/lbf"
        PAUSE_DIR.mkdir(parents=True, exist_ok=True)
        self.on_reset: Callable[[], None] | None = None  # evidence collection before the state is discarded

    # ------------------------------------------------------------------ compose
    def compose(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("LBF_", "SKARVIK_", "HALDEN_"))}
        e.update(TEST_ENV)
        if NS:
            e.update({k: v for k, v in os.environ.items() if k.startswith("LBF_")})
        e.update(env or {})
        cmd = ["./tools/isolated.sh", "compose", *args] if NS else [
            "docker", "compose", "-f", "compose.yaml", "-f", "deploy/compose.test.yaml", "--profile", "test", *args]
        return subprocess.run(cmd, cwd=REPO, env=e, check=check, capture_output=True,  # noqa: S603 (fixed argv)
                              text=True).stdout

    def restart_app(self, pause_points: str = "") -> None:
        self.compose("up", "-d", "--wait", "app", env={"LBF_TEST_PAUSE_POINTS": pause_points})

    def stop(self, service: str) -> None:
        self.compose("stop", service)

    def start(self, service: str) -> None:
        self.compose("up", "-d", "--wait", service)

    # ------------------------------------------------------------------ pause points
    def reached(self, point: str, timeout: float = 30) -> bool:
        return bool(wait_for(lambda: (PAUSE_DIR / f"{point}.reached").exists(), timeout))

    def release(self, point: str) -> None:
        (PAUSE_DIR / f"{point}.release").write_text("go")

    def clear_pauses(self) -> None:
        for f in PAUSE_DIR.iterdir():
            f.unlink()

    # ------------------------------------------------------------------ reset (initial conditions)
    def reset(self, *, ticket_ttl_sec: int | None = None) -> None:
        if self.on_reset is not None:
            self.on_reset()
        self.halden.sim_clear_faults()
        params = {"rate_limit": 100000}
        if ticket_ttl_sec is not None:
            params["ticket_ttl_sec"] = ticket_ttl_sec
        self.halden.sim_reset(**params)
        self.skarvik.clear_faults()
        self.skarvik.reset()
        self.tap.reset()
        with psycopg.connect(self.dsn) as conn:
            conn.execute("TRUNCATE " + ", ".join(f"lbf.{t}" for t in VOLATILE) + " RESTART IDENTITY CASCADE")
        self.clear_pauses()

    def wait_ready(self, timeout: float = 120) -> None:
        """Block until every oracle endpoint answers (after ``up --build`` the simulators start asynchronously)."""
        def probe() -> bool:
            try:
                self.halden.sim_clear_faults()
                self.skarvik.status()
                self.tap.requests()
                return True
            except Exception:  # not ready yet: connection refused/reset while the services start
                return False

        if not wait_for(probe, timeout, interval=1.0):
            raise RuntimeError(f"oracle endpoints not ready after {timeout} s")

    def sql(self, query: str, *args: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.dsn) as conn:
            return list(conn.execute(query, args).fetchall())

    def count(self, table: str, where: str = "true", *args: Any) -> int:
        return int(self.sql(f"SELECT count(*) FROM lbf.{table} WHERE {where}", *args)[0][0])  # noqa: S608
