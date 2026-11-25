"""Live-smoke durable authorization ledger: identity binding, atomic reservation, cross-process
concurrency (separate OS processes, not threads), crash recovery and fail-closed behaviour. No network, no database
server: the ledger is a local SQLite file under ``tmp_path``."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from lbf.assistant.smoke_ledger import Ledger, LedgerError, authorization_digest, init_ledger

RUN = "o1a-ledger-run"
CHILD_TIMEOUT_S = 60


def auth(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": RUN, "purpose": "smoke", "provider": "openai", "model": "gpt-6-luna",
                            "max_requests": 3, "expires_at": "2099-01-01T00:00:00Z", "max_estimated_usd": "0.50",
                            "approved_by": "owner (test)"}
    base.update(overrides)
    return base


@pytest.fixture
def path(tmp_path: Path) -> Path:
    p = tmp_path / "o1a-ledger" / "ledger.sqlite3"
    init_ledger(p)
    return p


@pytest.fixture
def ledger(path: Path):
    led = Ledger(path)
    yield led
    led.close()


def drain(led: Ledger, run_id: str = RUN) -> list[int | None]:
    return [led.reserve(run_id, "drain") for _ in range(5)]


# ------------------------------------------------------------------------------------------- initialisation
def test_init_creates_a_private_ledger_and_never_replaces_it(path: Path):
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(LedgerError, match="already exists"):
        init_ledger(path)


def test_the_ledger_is_never_created_implicitly(tmp_path: Path):
    missing = tmp_path / "nowhere" / "ledger.sqlite3"
    with pytest.raises(LedgerError, match="never created implicitly"):
        Ledger(missing)
    assert not missing.exists() and not missing.parent.exists()


# ------------------------------------------------------------------------------------------- R-1 / R-10 / R-11
def test_r1_r10_sequential_replay_never_exceeds_the_cap(path: Path, ledger: Ledger):
    assert ledger.bind(auth()) is None
    assert drain(ledger) == [1, 2, 3, None, None]
    replay = Ledger(path)  # a new "process": fresh object, same durable file
    assert replay.bind(auth()) is None and replay.remaining(RUN) == 0 and drain(replay) == [None] * 5
    assert replay.consumed(RUN) == 3
    denials = [e for e in replay.events(RUN) if e["kind"] == "CAP_DENIED"]
    assert len(denials) == 2 + 5 and json.loads(denials[0]["detail"])["reason"] == "exhausted"


def test_r11_a_distinct_run_id_has_independent_capacity(ledger: Ledger):
    assert ledger.bind(auth()) is None and drain(ledger)[:3] == [1, 2, 3]
    assert ledger.bind(auth(run_id="o1a-other")) is None
    assert drain(ledger, "o1a-other") == [1, 2, 3, None, None]


def test_a_lower_cap_is_honoured(ledger: Ledger):
    assert ledger.bind(auth(max_requests=1)) is None
    assert drain(ledger) == [1, None, None, None, None]


def test_an_expired_authorization_reserves_nothing(ledger: Ledger):
    assert ledger.bind(auth(expires_at="2020-01-01T00:00:00Z")) is None
    assert ledger.reserve(RUN, "x") is None and ledger.consumed(RUN) == 0
    assert json.loads(ledger.events(RUN)[-1]["detail"])["reason"] == "expired"


# ------------------------------------------------------------------------------------------- R-8 identity
@pytest.mark.parametrize("change", [{"max_requests": 2}, {"expires_at": "2098-01-01T00:00:00Z"},
                                    {"max_estimated_usd": "9"}, {"extra": True}])
def test_r8_changed_content_under_the_same_run_id_is_refused_and_replenishes_nothing(ledger: Ledger, change):
    assert ledger.bind(auth()) is None
    ledger.reserve(RUN, "a")
    reason = ledger.bind(auth(**change))
    assert reason is not None and "identity conflict" in reason
    assert ledger.consumed(RUN) == 1 and ledger.remaining(RUN) == 2
    assert ledger.events(RUN)[-1]["kind"] == "IDENTITY_CONFLICT"


def test_r8_another_model_is_refused_without_writing_anything(path: Path, ledger: Ledger):
    assert ledger.bind(auth()) is None
    before = path.read_bytes()
    for model in ("gpt-other", "gpt-6-luna-2026-11-01", None):
        assert "authorized model" in (ledger.bind(auth(model=model)) or "")
        assert "authorized model" in (ledger.bind(auth(model=model, run_id="other")) or "")
    assert path.read_bytes() == before


def test_r8_key_order_and_whitespace_do_not_change_the_identity(ledger: Ledger):
    original = auth()
    reordered = json.loads(json.dumps(dict(reversed(list(original.items()))), indent=4))
    assert authorization_digest(original) == authorization_digest(reordered)
    assert ledger.bind(original) is None and ledger.bind(reordered) is None


# ------------------------------------------------------------------------------------------- database invariants
def test_database_rejects_slots_beyond_the_cap_duplicates_and_any_rewrite(path: Path, ledger: Ledger):
    assert ledger.bind(auth()) is None and drain(ledger)[:3] == [1, 2, 3]
    raw = sqlite3.connect(path)
    insert = ("INSERT INTO events (run_id, invocation_id, kind, slot_no, pid, at) "
              "VALUES (?, 'raw', 'SLOT_RESERVED', ?, 0, 'now')")
    with pytest.raises(sqlite3.IntegrityError, match="outside the authorized cap"):
        raw.execute(insert, (RUN, 4))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(insert, (RUN, 2))
    for sql in ("DELETE FROM events", "UPDATE events SET kind = 'CAP_DENIED'", "DELETE FROM authorizations",
                "UPDATE authorizations SET max_requests = 3", "DELETE FROM ledger_meta"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(sql)
    raw.close()
    assert Ledger(path).consumed(RUN) == 3


# ------------------------------------------------------------------------------------------- R-9 fail closed
def test_r9_corrupt_or_foreign_files_are_rejected(tmp_path: Path, path: Path):
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"\x00junk" * 4096)
    foreign = tmp_path / "foreign.sqlite3"
    sqlite3.connect(foreign).execute("CREATE TABLE t (x)").connection.commit()
    truncated = tmp_path / "truncated.sqlite3"
    truncated.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
    for bad in (garbage, foreign, truncated):
        with pytest.raises(LedgerError):
            Ledger(bad).reserve(RUN, "x")


def test_r9_a_ledger_with_its_integrity_triggers_removed_is_rejected(path: Path):
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER events_no_delete")
    raw.commit()
    raw.close()
    with pytest.raises(LedgerError, match="integrity triggers"):
        Ledger(path)


def test_r9_a_locked_ledger_fails_closed(path: Path):
    opened = Ledger(path, busy_timeout_s=0.2)
    assert opened.bind(auth()) is None
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(LedgerError, match="locked"):
            opened.reserve(RUN, "x")  # lock held by another connection beyond the busy timeout
        with pytest.raises(LedgerError):
            Ledger(path, busy_timeout_s=0.2)  # cannot even be opened and verified
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert Ledger(path).consumed(RUN) == 0


def test_r9_a_read_only_ledger_fails_closed(path: Path):
    assert Ledger(path).bind(auth()) is None
    path.chmod(0o400)
    try:
        with pytest.raises(LedgerError):
            Ledger(path).reserve(RUN, "x")
    finally:
        path.chmod(0o600)
    assert Ledger(path).consumed(RUN) == 0


# ------------------------------------------------------------------------------------------- R-2 separate processes
RACER = """
import json, os, sys, time
from pathlib import Path
from lbf.assistant.smoke_ledger import Ledger, LedgerError
path, run_id, go, attempts = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
ledger = Ledger(path, busy_timeout_s=30)
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.0005)
results = []
for _ in range(attempts):
    try:
        results.append(ledger.reserve(run_id, f"pid-{os.getpid()}", "race"))
    except LedgerError as exc:
        results.append("ERROR " + str(exc))
print(json.dumps({"pid": os.getpid(), "results": results}))
"""


def race(path: Path, tmp_path: Path, processes: int, attempts: int) -> list[Any]:
    go = tmp_path / "go"
    procs = [subprocess.Popen([sys.executable, "-c", RACER, str(path), RUN, str(go), str(attempts)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(processes)]
    time.sleep(1.0)  # let every process import and open the ledger
    go.touch()
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=CHILD_TIMEOUT_S)
        assert p.returncode == 0, err
        outputs.append(json.loads(out))
    assert len({o["pid"] for o in outputs}) == processes  # genuinely separate OS processes
    return [r for o in outputs for r in o["results"]]


def test_r2_separate_processes_last_remaining_slot(tmp_path: Path):
    p = tmp_path / "l.sqlite3"
    init_ledger(p)
    led = Ledger(p)
    assert led.bind(auth()) is None
    assert led.reserve(RUN, "pre") == 1 and led.reserve(RUN, "pre") == 2
    results = race(p, tmp_path, processes=8, attempts=1)
    assert sorted(results, key=str) == [3, *([None] * 7)]  # exactly one winner, no errors
    assert Ledger(p).consumed(RUN) == 3


def test_r2_separate_processes_draining_a_fresh_authorization_never_over_reserve(tmp_path: Path):
    p = tmp_path / "l.sqlite3"
    init_ledger(p)
    assert Ledger(p).bind(auth()) is None
    results = race(p, tmp_path, processes=8, attempts=3)
    assert sorted(r for r in results if r is not None) == [1, 2, 3]
    assert results.count(None) == 21 and not any(isinstance(r, str) for r in results)
    assert Ledger(p).consumed(RUN) == 3


# ------------------------------------------------------------------------------------------- R-6 crash recovery
CRASHER = """
import os, sys, time
from pathlib import Path
from lbf.assistant.provider import ProviderTurn
from lbf.assistant.smoke import CappedProvider, s1_request
from lbf.assistant.smoke_ledger import Ledger
path, run_id, point, marker = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
ledger = Ledger(path)
invocation = ledger.start_invocation(run_id)
if point == "before_reservation":
    os._exit(70)
real_reserve, real_record, real_event = Ledger.reserve, Ledger.record, Ledger._event

def reserve(*a, **k):
    slot = real_reserve(*a, **k)
    if point == "after_reservation_commit":
        os._exit(70)
    return slot

def record(self, run_id, invocation_id, kind, **k):
    real_record(self, run_id, invocation_id, kind, **k)
    if point == "before_delegation" and kind == "PROVIDER_DELEGATION_STARTED":
        os._exit(70)

def event(db, run_id, invocation_id, kind, **k):
    real_event(db, run_id, invocation_id, kind, **k)
    if point == "inside_reservation_transaction" and kind == "SLOT_RESERVED":
        os._exit(70)  # row inserted, COMMIT never issued

Ledger.reserve, Ledger.record = reserve, record  # the cap opens its own connection per request
Ledger._event = staticmethod(event)

class Inner:
    name, model, config = "fake", "gpt-6-luna", None
    def run(self, request, max_attempts=None):
        if point == "during_delegation":
            os._exit(70)
        if point == "unresolved_request":
            marker.touch()
            time.sleep(600)  # killed with SIGKILL by the parent while "in flight"
        return ProviderTurn(kind="FINAL", final_json="{}", model_reported="gpt-6-luna")

capped = CappedProvider(Inner(), 3, ledger, run_id, invocation)
capped.scenario = "S1"
capped.run(s1_request(run_id))
if point == "after_response_before_evidence":
    os._exit(70)
os._exit(0)
"""

CRASH_POINTS = {  # point: (slots consumed, delegations started, responses observed)
    "before_reservation": (0, 0, 0),
    "inside_reservation_transaction": (0, 0, 0),
    "after_reservation_commit": (1, 0, 0),
    "before_delegation": (1, 1, 0),
    "during_delegation": (1, 1, 0),
    "unresolved_request": (1, 1, 0),
    "after_response_before_evidence": (1, 1, 1),
}


@pytest.mark.parametrize("point", list(CRASH_POINTS))
def test_r6_crash_recovery_never_restores_reserved_capacity(path: Path, tmp_path: Path, point: str):
    assert Ledger(path).bind(auth()) is None
    marker = tmp_path / "in-flight"
    proc = subprocess.Popen([sys.executable, "-c", CRASHER, str(path), RUN, point, str(marker)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if point == "unresolved_request":
        deadline = time.monotonic() + CHILD_TIMEOUT_S
        while not marker.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.01)
        assert marker.exists(), proc.stderr.read() if proc.stderr else ""
        os.kill(proc.pid, signal.SIGKILL)
    _, err = proc.communicate(timeout=CHILD_TIMEOUT_S)
    assert proc.returncode in (70, -signal.SIGKILL), err

    recovered = Ledger(path)  # restart: integrity check passes, a hot journal is rolled back
    consumed, delegated, observed = CRASH_POINTS[point]
    view = recovered.summary(RUN)
    assert (view["slots_reserved"], view["delegations_started"], view["responses_observed"]) == (
        consumed, delegated, observed)
    assert view["sent_or_unknown"] == delegated - observed  # delegated without an observed response
    assert view["invocations_unfinished"] == 1  # the crash is visible without any summary.json
    # the invocation has no resolved end (R-5): the slots are neither restored nor usable any more
    assert recovered.remaining(RUN) == 0 and drain(recovered) == [None] * 5
    with pytest.raises(LedgerError, match="no resolved end"):
        recovered.start_invocation(RUN)
    assert recovered.consumed(RUN) == consumed
