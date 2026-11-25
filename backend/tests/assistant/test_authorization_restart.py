"""A paid authorization whose previous invocation has no resolved end admits nothing further (model rule,
R-5 and R-6): the process is lost before the abort is committed, the abort cannot be written, a delegation
has no recorded outcome. "Restart" is a fresh ledger object, a fresh process guard and a fresh mocked transport; every
test asserts the transport call count and what the durable file holds. Offline, synthetic key, per-test ledgers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from assistant_helpers import FINAL, MockOpenAI
from test_live_campaign import run as campaign_run
from test_live_campaign import scenario
from test_luna_contract import ANSWER, reporting
from test_smoke_executor import authorization, ledger_file, run, three_requests

from lbf.assistant import model_guard, smoke
from lbf.assistant.provider import ProviderTurn
from lbf.assistant.providers import openai_provider
from lbf.assistant.smoke_ledger import Ledger, LedgerError, init_ledger

LUNA, DATED, OTHER = "gpt-6-luna", "gpt-6-luna-2026-11-01", "gpt-6.1-terra"
RUN = "sm-restart"


@pytest.fixture(scope="module")
def eval_env():
    from evals.harness.env import EvalEnv

    env = EvalEnv().build()
    yield env
    env.close()


def new_process() -> None:
    openai_provider.PROCESS_GUARD = model_guard.ModelGuard()


def kinds(tmp_path: Path, run_id: str = RUN) -> list[str]:
    return [e["kind"] for e in Ledger(ledger_file(tmp_path)).events(run_id)]


def assert_terminal(eval_env, tmp_path: Path, run_id: str = RUN, **auth: Any) -> None:
    """Two restarts: zero requests, zero new slots, zero new delegations."""
    ledger = Ledger(ledger_file(tmp_path))
    slots = ledger.consumed(run_id)
    assert slots < 3  # capacity is left: only the terminal state refuses
    for n in range(2):
        new_process()
        again = three_requests()
        code, refused = run(eval_env, tmp_path, again, run_id=run_id, out_root=tmp_path / f"again-{n}", **auth)
        assert (code, again.requests) == (2, []) and refused["status"] == "REFUSED", refused
    assert Ledger(ledger_file(tmp_path)).consumed(run_id) == slots
    assert Ledger(ledger_file(tmp_path)).remaining(run_id) == 0
    assert kinds(tmp_path, run_id).count("INVOCATION_STARTED") == 1


# ---------------------------------------------------------------------------------- the process is lost
class Lost(BaseException):
    """The process disappears: nothing the executor does afterwards reaches the disk."""


def lose_the_process(monkeypatch, when) -> None:
    real_record, real_write = Ledger.record, Ledger._write
    gone = threading.Event()

    def record(self, run_id, invocation_id, kind, **kw):
        if gone.is_set() or when(kind, kw):
            gone.set()
            raise Lost
        return real_record(self, run_id, invocation_id, kind, **kw)

    def write(self):
        if gone.is_set():
            raise Lost
        return real_write(self)

    monkeypatch.setattr(Ledger, "record", record)
    monkeypatch.setattr(Ledger, "_write", write)


@pytest.mark.db
@pytest.mark.parametrize("queue", [[reporting(FINAL(ANSWER), OTHER)],
                                   [FINAL(ANSWER), reporting(FINAL(ANSWER), DATED)]], ids=["mismatch", "version"])
def test_a_process_lost_before_the_abort_commit_ends_the_authorization(eval_env, tmp_path, queue):
    with pytest.MonkeyPatch.context() as m:
        lose_the_process(m, lambda kind, kw: kind == "CAMPAIGN_ABORTED")
        mock = MockOpenAI()
        mock.queue = list(queue)
        with pytest.raises(Lost):
            run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
        assert len(mock.requests) == len(queue)
    assert "CAMPAIGN_ABORTED" not in kinds(tmp_path) and "INVOCATION_FINISHED" not in kinds(tmp_path)
    assert_terminal(eval_env, tmp_path)


@pytest.mark.db
def test_a_process_lost_while_a_request_is_in_flight_ends_the_authorization(eval_env, tmp_path):
    def interrupted(_request):
        raise KeyboardInterrupt

    mock = MockOpenAI()
    mock.__class__ = type("Interrupting", (MockOpenAI,), {"__call__": lambda self, r: (self.requests.append(r),
                                                                                       interrupted(r))})
    with pytest.raises(KeyboardInterrupt):  # the executor's own cleanup still runs, and must not call this finished
        run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
    assert len(mock.requests) == 1
    assert kinds(tmp_path).count("PROVIDER_DELEGATION_STARTED") == 1 and "RESPONSE_OBSERVED" not in kinds(tmp_path)
    assert_terminal(eval_env, tmp_path)


# ---------------------------------------------------------------------------------- the abort cannot be written
def failing(monkeypatch, *failing_kinds: str) -> None:
    real = Ledger._event

    def event(db, run_id, invocation_id, kind, **kw):
        if kind in failing_kinds:
            raise LedgerError("disk full (simulated)")
        return real(db, run_id, invocation_id, kind, **kw)

    monkeypatch.setattr(Ledger, "_event", staticmethod(event))


@pytest.mark.db
@pytest.mark.parametrize("queue", [[reporting(FINAL(ANSWER), OTHER)],
                                   [FINAL(ANSWER), reporting(FINAL(ANSWER), DATED)]], ids=["mismatch", "version"])
@pytest.mark.parametrize("also", [(), ("RESPONSE_OBSERVED",), ("RESPONSE_OBSERVED", "INVOCATION_FINISHED")])
def test_an_abort_that_cannot_be_written_still_ends_the_authorization(eval_env, tmp_path, queue, also):
    with pytest.MonkeyPatch.context() as m:
        failing(m, "CAMPAIGN_ABORTED", *also)
        mock = MockOpenAI()
        mock.queue = list(queue)
        code, s = run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
        assert code not in (0, 2) and len(mock.requests) == (1 if also else len(queue))
        assert s["authorization_ledger"]["ledger_failure"]
    assert "CAMPAIGN_ABORTED" not in kinds(tmp_path) and "INVOCATION_FINISHED" not in kinds(tmp_path)
    assert_terminal(eval_env, tmp_path)


@pytest.mark.db
def test_an_abort_refused_once_is_written_when_the_invocation_ends(eval_env, tmp_path, monkeypatch):
    real, failed = Ledger._event, []

    def event(db, run_id, invocation_id, kind, **kw):
        if kind == "CAMPAIGN_ABORTED" and not failed:
            failed.append(kind)
            raise LedgerError("busy (simulated)")
        return real(db, run_id, invocation_id, kind, **kw)

    monkeypatch.setattr(Ledger, "_event", staticmethod(event))
    mock = MockOpenAI()
    mock.queue = [reporting(FINAL(ANSWER), OTHER)]
    code, s = run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
    assert code == smoke.ABORT_EXIT and s["status"] == smoke.ABORTED and len(mock.requests) == 1
    assert kinds(tmp_path).count("CAMPAIGN_ABORTED") == 1
    assert_terminal(eval_env, tmp_path)


@pytest.mark.db
def test_an_accepted_response_whose_outcome_was_not_stored_ends_the_authorization(eval_env, tmp_path):
    """Its version would be unknown to a restart, so a later version change could not be seen (R-6)."""
    with pytest.MonkeyPatch.context() as m:
        failing(m, "RESPONSE_OBSERVED")
        mock = MockOpenAI()
        mock.queue = [FINAL(ANSWER)]
        code, _ = run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
        assert code == 1 and len(mock.requests) == 1
    assert_terminal(eval_env, tmp_path)


def test_a_recorded_mismatch_is_an_abort_even_without_the_abort_record(tmp_path):
    ledger = Ledger(ledger_file(tmp_path))
    assert ledger.bind(authorization(run_id=RUN)) is None
    invocation = ledger.start_invocation(RUN)
    assert ledger.reserve(RUN, invocation) == 1
    ledger.record(RUN, invocation, "PROVIDER_DELEGATION_STARTED", slot_no=1)
    ledger.record(RUN, invocation, "RESPONSE_OBSERVED", slot_no=1, detail={"error_category": "MODEL_MISMATCH"})
    assert ledger.reserve(RUN, invocation) is None and ledger.remaining(RUN) == 0
    assert "ABORTED_MODEL_MISMATCH" in (Ledger(ledger_file(tmp_path)).bind(authorization(run_id=RUN)) or "")


# ---------------------------------------------------------------------------------- real process death
KILLED = """
import os, sys, time
from pathlib import Path
from lbf.assistant.provider import ProviderTurn
from lbf.assistant.smoke import CappedProvider, s1_request
from lbf.assistant.smoke_ledger import Ledger
path, run_id, point, marker = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
ledger = Ledger(path)
real = Ledger.record
def record(self, run, inv, kind, **kw):
    if kind == "CAMPAIGN_ABORTED" and point == "before_abort_commit":
        marker.touch(); time.sleep(600)
    real(self, run, inv, kind, **kw)
Ledger.record = record
class Inner:
    name, model, config = "fake", "gpt-6-luna", None
    def run(self, request, max_attempts=None):
        if point == "in_flight":
            marker.touch(); time.sleep(600)
        return ProviderTurn(kind="FINAL", final_json="{}", model_reported="gpt-6.1-terra")
CappedProvider(Inner(), 3, ledger, run_id, ledger.start_invocation(run_id)).run(s1_request(run_id))
"""


@pytest.mark.parametrize("point", ["before_abort_commit", "in_flight"])
def test_a_killed_process_leaves_the_authorization_unusable(tmp_path, point):
    path, marker = ledger_file(tmp_path), tmp_path / "reached"
    assert Ledger(path).bind(authorization(run_id=RUN)) is None
    proc = subprocess.Popen([sys.executable, "-c", KILLED, str(path), RUN, point, str(marker)], stderr=subprocess.PIPE)
    deadline = time.monotonic() + 60
    while not marker.exists() and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.01)
    assert marker.exists(), proc.communicate()[1]
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(60)
    ledger = Ledger(path)
    assert ledger.consumed(RUN) == 1 and ledger.remaining(RUN) == 0
    with pytest.raises(LedgerError, match="no resolved end"):
        ledger.start_invocation(RUN)
    assert ledger.reserve(RUN, "another worker") is None and ledger.consumed(RUN) == 1


# ---------------------------------------------------------------------------------- concurrent admission
STARTER = """
import sys, time
from pathlib import Path
from lbf.assistant.smoke_ledger import Ledger, LedgerError
path, run_id, go, only = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4] == "only"
ledger = Ledger(path, busy_timeout_s=30)
while not go.exists():
    time.sleep(0.0005)
try:
    ledger.start_invocation(run_id, only=True) if only else ledger.start_invocation(run_id)
    print("ADMITTED")
except LedgerError:
    print("REFUSED")
"""


@pytest.mark.parametrize("only", ["only", "any"])
def test_of_concurrent_processes_exactly_one_is_admitted(tmp_path, only):
    path, go = tmp_path / "l.sqlite3", tmp_path / "go"
    init_ledger(path)
    assert Ledger(path).bind(authorization(run_id=RUN)) is None
    procs = [subprocess.Popen([sys.executable, "-c", STARTER, str(path), RUN, str(go), only], stdout=subprocess.PIPE,
                              text=True) for _ in range(6)]
    time.sleep(1.0)
    go.touch()
    assert sorted(p.communicate(timeout=60)[0].strip() for p in procs) == ["ADMITTED"] + ["REFUSED"] * 5
    assert [e["kind"] for e in Ledger(path).events(RUN)].count("INVOCATION_STARTED") == 1


@pytest.mark.db
def test_a_second_smoke_is_refused_while_the_first_runs_and_the_first_is_not_disturbed(eval_env, tmp_path):
    second: list[Any] = []

    class During(MockOpenAI):
        def __call__(self, request):
            if not second:
                other = three_requests()
                second.append((run(eval_env, tmp_path, other, run_id=RUN, out_root=tmp_path / "b"), other.requests))
            return super().__call__(request)

    mock = During()
    mock.queue = three_requests().queue
    code, s = run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
    ((other_code, refused), sent) = second[0]
    assert (other_code, sent, refused["error"]) == (2, [], "AUTHORIZATION_UNRESOLVED")
    assert code == 0 and s["status"] == "PASS" and len(mock.requests) == 3


def test_workers_of_one_invocation_do_not_block_each_other(tmp_path):
    ledger = Ledger(ledger_file(tmp_path))
    assert ledger.bind(authorization(run_id=RUN)) is None
    invocation = ledger.start_invocation(RUN)
    barrier = threading.Barrier(3, timeout=20)

    class Inner:
        name, model, config = "fake", LUNA, None

        def run(self, request, max_attempts=None):
            barrier.wait()  # all three delegations are open at the same time
            return ProviderTurn(kind="FINAL", final_json="{}", model_reported=LUNA, provider_request_id="r")

    capped = smoke.CappedProvider(Inner(), 3, ledger, RUN, invocation)
    turns: list[ProviderTurn] = []
    workers = [threading.Thread(target=lambda: turns.append(capped.run(smoke.s1_request(RUN)))) for _ in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(20)
    assert [t.kind for t in turns] == ["FINAL"] * 3 and ledger.consumed(RUN) == 3


# ---------------------------------------------------------------------------------- ordinary completion
@pytest.mark.db
def test_an_invocation_that_ended_normally_leaves_the_rest_of_the_authorization_usable(eval_env, tmp_path):
    limited = httpx2.Response(429, json={"error": {"message": "slow"}}, headers={"retry-after": "1"})
    mock = MockOpenAI()
    mock.queue = [httpx2.ReadTimeout("slow"), limited]  # no model report (R-7): resolved, not a mismatch
    code, _ = run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "a")
    assert code == 1 and len(mock.requests) == 2 and kinds(tmp_path)[-1] == "INVOCATION_FINISHED"
    new_process()
    later = three_requests()
    run(eval_env, tmp_path, later, run_id=RUN, out_root=tmp_path / "b")
    assert len(later.requests) == 1 and Ledger(ledger_file(tmp_path)).consumed(RUN) == 3


@pytest.mark.db
def test_a_failure_before_any_delegation_does_not_consume_the_authorization(eval_env, tmp_path):
    def broken(*_a):
        raise RuntimeError("environment")

    with pytest.raises(RuntimeError):
        run(eval_env, tmp_path, MockOpenAI(), run_id=RUN, out_root=tmp_path / "a", build=broken)
    mock = three_requests()
    assert run(eval_env, tmp_path, mock, run_id=RUN, out_root=tmp_path / "b")[0] == 0 and len(mock.requests) == 3


# ---------------------------------------------------------------------------------- the adjacent campaign runners
@pytest.mark.db
def test_a_campaign_cannot_start_again_with_another_ledger_file(eval_env, tmp_path):
    """The shared expenditure ledger knows the run, whichever campaign ledger is presented."""
    code, _, mock = campaign_run(eval_env, tmp_path, [reporting(FINAL(ANSWER), OTHER)], [scenario()])
    assert code == smoke.ABORT_EXIT and len(mock.requests) == 1
    for left in (tmp_path / "ledger.sqlite3", *tmp_path.rglob("*ledger*.sqlite3")):
        left.unlink(missing_ok=True)
    (tmp_path / "out").rename(tmp_path / "moved")
    new_process()
    code, refused, again = campaign_run(eval_env, tmp_path, [FINAL(ANSWER)], [scenario()])
    assert (code, again.requests) == (2, []) and "expenditure" in refused["reason"]


@pytest.mark.parametrize("module", ["evals/run.py", "evals/live_campaign.py", "evals/live_assistant.py"])
def test_every_campaign_runner_is_admitted_in_one_transaction(module):
    source = (Path(__file__).resolve().parents[3] / module).read_text(encoding="utf-8")
    assert "only=True" in source and "\"INVOCATION_STARTED\" for e in" not in source


def test_a_single_invocation_authorization_is_never_admitted_twice(tmp_path):
    ledger = Ledger(ledger_file(tmp_path))
    assert ledger.bind(authorization(run_id=RUN)) is None
    ledger.finish_invocation(RUN, ledger.start_invocation(RUN, only=True), {"status": "COMPLETE"})
    with pytest.raises(LedgerError, match="one invocation"):
        ledger.start_invocation(RUN, only=True)
    assert json.loads(Ledger(ledger_file(tmp_path)).events(RUN)[-1]["detail"])["reason"] == "consumed"


def test_money_stays_reserved_for_an_interrupted_delegation(tmp_path):
    """Refusing the restart never erases what the interrupted request may have cost."""
    from evals.harness.spend import SpendLedger, init_spend_ledger

    spend = SpendLedger(init_spend_ledger(tmp_path / "s.sqlite3", Decimal("0.50")))
    assert spend.reserve(uuid.uuid4(), Decimal("0.02"), RUN)
    assert SpendLedger(tmp_path / "s.sqlite3").summary()["outstanding_usd"] == Decimal("0.02")
