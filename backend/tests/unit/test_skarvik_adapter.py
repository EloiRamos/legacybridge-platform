"""SKARVIK adapter write-path design verification.

Distinctions under test: TEMP_FILE_EXISTS != BACKEND_RECEIVED; the atomic rename is the handoff; the simulator's own
``received`` ledger (an oracle outside the adapter) is the ground truth of what SKARVIK claimed.
Runs the FROZEN simulator in-process against a temporary share (no container, no network).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from lbf.adapters.skarvik import exchange
from lbf.adapters.skarvik.adapter import SkarvikAdapter
from lbf.adapters.skarvik.fake import InProcessSkarvik
from lbf.adapters.skarvik.formats import render_order, stockholm_to_utc
from lbf.domain.correlation import message_id, new_correlation_value
from lbf.domain.enums import EvidenceClass, ReconciliationVerdict, TransportObservationKind
from lbf.domain.errors import DomainError
from lbf.domain.models import ItemMapping, ReconciliationQuery, SubmissionLine, SubmissionRequest
from lbf.ports.capabilities import skarvik_capabilities

REPO = Path(__file__).resolve().parents[3]
SIM_SRC = REPO / "legacy-systems" / "skarvik-oes" / "src"
SEED = REPO / "legacy-systems" / "skarvik-oes" / "seed" / "seed.json"
K = TransportObservationKind
V = ReconciliationVerdict


def _seed_mappings() -> tuple[ItemMapping, ...]:
    import importlib.util

    path = REPO / "backend" / "migrations" / "versions" / "0002_seed_master_data.py"
    spec = importlib.util.spec_from_file_location("seed_0002", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(ItemMapping(backend_id=b, native_item=n, canonical_sku=c, native_uom=u, factor_num=fn, factor_den=fd)
                 for b, n, c, u, fn, fd in module.ITEM_MAPPINGS)


MAPPINGS = _seed_mappings()


class Kill(BaseException):
    pass


@pytest.fixture
def env(tmp_path):
    backend = InProcessSkarvik(tmp_path, simulator_src=SIM_SRC, seed_file=SEED)
    adapter = SkarvikAdapter(backend.exchange_dir, mappings=MAPPINGS,
                             capabilities=skarvik_capabilities(test_profile=True), clock=backend.clock)
    return backend, adapter


def make_request(qty: int = 2) -> SubmissionRequest:
    cv = new_correlation_value()
    return SubmissionRequest(
        operation_id=uuid.uuid4(), attempt_no=1, transmission_no=1, authorization_id=uuid.uuid4(),
        backend_id="skarvik", native_customer_id="KND-0081",
        lines=[SubmissionLine(native_item="NVB-6205-2RS", quantity_native=qty)], correlation_value=cv,
        message_id=message_id(cv, 1))


def query(req: SubmissionRequest, started) -> ReconciliationQuery:
    return ReconciliationQuery(operation_id=req.operation_id, backend_id="skarvik",
                               native_customer_id=req.native_customer_id, correlation_value=req.correlation_value,
                               message_id=req.message_id, expected_lines=req.lines, anchor_order_ref=None,
                               first_handoff_at=started, attempt_no=1)


def settle(backend: InProcessSkarvik, adapter: SkarvikAdapter) -> None:
    backend.pickup()
    backend.flush_acks()
    backend.clock.advance((adapter.capabilities().snapshot_max_lag_seconds or 0) + 1)
    backend.extracts()


def claimed(backend: InProcessSkarvik, msgid: str) -> list[dict]:
    return [r for r in backend.received() if r["msgid"] == msgid]


def test_staged_temp_file_is_invisible_to_skarvik_and_publication_happens_only_after_the_hook(env):
    backend, adapter = env
    req = make_request()
    rendered = adapter.render(req)
    seen = {}

    def hook(r):
        staged = adapter.share.tmp(req.message_id)
        seen["staged_bytes"] = staged.read_bytes()
        seen["final_exists"] = adapter.share.final(req.message_id).exists()
        seen["pickup_during_hook"] = backend.pickup()  # SKARVIK runs while the message is staged
        seen["claimed_during_hook"] = claimed(backend, req.message_id)

    adapter.before_publish = hook
    obs = adapter.handoff(rendered)
    assert seen == {"staged_bytes": rendered.payload, "final_exists": False, "pickup_during_hook": [],
                    "claimed_during_hook": []}
    assert obs.kind is K.DELIVERED_PENDING_ACK
    assert not adapter.share.tmp(req.message_id).exists()  # the rename consumed the staging file
    assert adapter.share.final(req.message_id).read_bytes() == rendered.payload


def test_durability_order_is_file_fsync_then_hook_then_rename_then_directory_fsync(env, monkeypatch):
    _, adapter = env
    events: list[str] = []
    real_fsync, real_rename = exchange.os.fsync, exchange.os.rename
    monkeypatch.setattr(exchange.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(exchange.os, "rename", lambda a, b: (events.append("rename"), real_rename(a, b))[1])
    adapter.before_publish = lambda r: events.append("hook")
    assert adapter.handoff(adapter.render(make_request())).kind is K.DELIVERED_PENDING_ACK
    assert events == ["fsync", "hook", "rename", "fsync"]


def test_kill_before_publication_leaves_no_backend_visible_order_and_proves_never_delivered(env):
    backend, adapter = env
    req = make_request()
    started = backend.clock()

    def kill(_r):
        raise Kill

    adapter.before_publish = kill
    with pytest.raises(Kill):
        adapter.handoff(adapter.render(req))
    for _ in range(3):  # SKARVIK keeps running after the application died
        settle(backend, adapter)
    assert claimed(backend, req.message_id) == []
    assert [o for o in backend.orders() if o["msgid"] == req.message_id] == []
    assert adapter.share.tmp(req.message_id).exists() and not adapter.share.final(req.message_id).exists()
    result = adapter.reconcile(query(req, started))
    assert result.absence_proven is True and result.absence_evidence is EvidenceClass.NEVER_DELIVERED


def test_kill_after_publication_is_not_absence_and_reconciles_from_backend_evidence(env, monkeypatch):
    backend, adapter = env
    req = make_request()
    started = backend.clock()

    def kill_after_rename(_path):
        raise Kill

    monkeypatch.setattr(exchange, "_fsync_dir", kill_after_rename)
    with pytest.raises(Kill):
        adapter.handoff(adapter.render(req))
    monkeypatch.undo()
    assert adapter.share.final(req.message_id).exists() and not adapter.share.tmp(req.message_id).exists()
    before_pickup = adapter.reconcile(query(req, started))
    assert before_pickup.absence_proven is False and before_pickup.verdict is V.NO_MATCH  # still in inbox
    backend.faults.counted["drop_ack"] = 1  # and the acknowledgement is lost
    settle(backend, adapter)
    assert len(claimed(backend, req.message_id)) == 1
    after = adapter.reconcile(query(req, started))
    assert after.absence_proven is False and after.verdict is V.MATCHED


def test_application_restart_never_publishes_an_abandoned_staging_file(env):
    backend, adapter = env
    abandoned = make_request()

    def kill(_r):
        raise Kill

    adapter.before_publish = kill
    with pytest.raises(Kill):
        adapter.handoff(adapter.render(abandoned))
    restarted = SkarvikAdapter(backend.exchange_dir, mappings=MAPPINGS,
                               capabilities=skarvik_capabilities(test_profile=True), clock=backend.clock)
    restarted.health()
    restarted.poll_acknowledgements()
    restarted.stock()
    fresh = make_request(qty=3)
    assert restarted.handoff(restarted.render(fresh)).kind is K.DELIVERED_PENDING_ACK
    settle(backend, restarted)
    assert claimed(backend, abandoned.message_id) == []
    assert len(claimed(backend, fresh.message_id)) == 1
    assert restarted.share.tmp(abandoned.message_id).exists()
    assert not restarted.share.final(abandoned.message_id).exists()


def test_received_ledger_independently_reflects_the_published_bytes(env):
    backend, adapter = env
    req = make_request()
    rendered = adapter.render(req)
    adapter.handoff(rendered)
    settle(backend, adapter)
    [row] = claimed(backend, req.message_id)
    assert row["sha256"] == rendered.payload_sha256 and row["outcome"] == "ACCEPTED"


def test_r11_fault_points_are_distinguishable(env, monkeypatch, tmp_path):
    """before_handoff ⇒ absence proven (NEVER_DELIVERED); after the rename ⇒ never absence."""
    backend, adapter = env
    before, after = make_request(), make_request(qty=4)
    started = backend.clock()
    adapter.before_publish = lambda r: (_ for _ in ()).throw(Kill())
    with pytest.raises(Kill):
        adapter.handoff(adapter.render(before))
    adapter.before_publish = lambda r: None
    monkeypatch.setattr(exchange, "_fsync_dir", lambda p: (_ for _ in ()).throw(Kill()))
    with pytest.raises(Kill):
        adapter.handoff(adapter.render(after))
    monkeypatch.undo()
    settle(backend, adapter)
    r_before, r_after = adapter.reconcile(query(before, started)), adapter.reconcile(query(after, started))
    assert (r_before.absence_proven, r_before.absence_evidence) == (True, EvidenceClass.NEVER_DELIVERED)
    assert r_after.absence_proven is False


def test_hook_failure_with_an_ordinary_exception_publishes_nothing_and_cleans_up(env):
    backend, adapter = env
    req = make_request()
    adapter.before_publish = lambda r: (_ for _ in ()).throw(RuntimeError("pause point broken"))
    obs = adapter.handoff(adapter.render(req))
    assert obs.kind is K.NOT_DELIVERED
    assert not adapter.share.tmp(req.message_id).exists() and not adapter.share.final(req.message_id).exists()
    settle(backend, adapter)
    assert claimed(backend, req.message_id) == []


def test_staging_is_exclusive_and_never_clobbers_never_delivered_evidence(env):
    _, adapter = env
    req = make_request()
    adapter.share.tmp(req.message_id).write_bytes(b"evidence")
    obs = adapter.handoff(adapter.render(req))
    assert obs.kind is K.NOT_DELIVERED
    assert adapter.share.tmp(req.message_id).read_bytes() == b"evidence"
    assert not adapter.share.final(req.message_id).exists()


def test_directory_fsync_failure_after_rename_is_uncertain_not_absence(env, monkeypatch):
    _, adapter = env
    req = make_request()

    def boom(_p):
        raise OSError("EIO")

    monkeypatch.setattr(exchange, "_fsync_dir", boom)
    obs = adapter.handoff(adapter.render(req))
    assert obs.kind is K.UNCERTAIN
    assert adapter.share.final(req.message_id).exists()


def test_rename_failure_with_staging_file_intact_is_not_delivered(env, monkeypatch):
    _, adapter = env
    req = make_request()

    def fail(_a, _b):
        raise OSError("EXDEV")

    monkeypatch.setattr(exchange.os, "rename", fail)
    obs = adapter.handoff(adapter.render(req))
    assert obs.kind is K.NOT_DELIVERED
    assert not adapter.share.tmp(req.message_id).exists() and not adapter.share.final(req.message_id).exists()


def test_killed_redelivery_staging_file_is_never_first_delivery_evidence(env):
    backend, adapter = env
    req = make_request()
    started = backend.clock()
    rendered = adapter.render(req)
    backend.faults.counted["drop_ack"] = 1
    adapter.handoff(rendered)
    settle(backend, adapter)  # processed, acknowledgement lost
    adapter.before_publish = lambda r: (_ for _ in ()).throw(Kill())
    with pytest.raises(Kill):
        adapter.redeliver(rendered)
    adapter.before_publish = lambda r: None
    assert adapter.share.redelivery_tmp(req.message_id).exists()
    result = adapter.reconcile(query(req, started))
    assert result.absence_proven is False and result.verdict is V.MATCHED


def test_withdraw_before_pickup_proves_absence_and_after_pickup_proves_nothing(env):
    backend, adapter = env
    first, second = make_request(), make_request(qty=5)
    adapter.handoff(adapter.render(first))
    w = adapter.withdraw_unclaimed(first.message_id)
    assert (w.withdrawn, w.absence_evidence) == (True, EvidenceClass.WITHDRAWN_BEFORE_PICKUP)
    adapter.handoff(adapter.render(second))
    settle(backend, adapter)
    late = adapter.withdraw_unclaimed(second.message_id)
    assert (late.withdrawn, late.absence_evidence) == (False, None)
    settle(backend, adapter)
    assert claimed(backend, first.message_id) == [] and len(claimed(backend, second.message_id)) == 1


def test_latin1_and_forbidden_characters_are_refused_before_any_transmission():
    base = make_request()
    for note in ("Kaj 7 · Göteborg ✓", "a;b", "x\r\ny"):
        bad = base.model_copy(update={"delivery_note": note})
        with pytest.raises(DomainError):
            render_order(bad, {"NVB-6205-2RS": "EA"})
    ok = base.model_copy(update={"delivery_note": "Ålesund, Øresund, Göta"})
    assert "Ålesund, Øresund, Göta".encode("iso-8859-1") in render_order(ok, {"NVB-6205-2RS": "EA"})


@pytest.mark.parametrize(("local", "utc"), [
    ("2026-03-29T01:30:00", "2026-03-29T00:30:00+00:00"),  # before the spring-forward gap (CET)
    ("2026-03-29T03:30:00", "2026-03-29T01:30:00+00:00"),  # after it (CEST)
    ("2026-10-25T02:30:00", "2026-10-25T00:30:00+00:00"),  # repeated hour: fold=0 (first, CEST)
    ("2026-11-21T14:03:05", "2026-11-21T13:03:05+00:00"),
])
def test_stockholm_local_time_conversion_across_dst(local, utc):
    assert stockholm_to_utc(local, "%Y-%m-%dT%H:%M:%S").isoformat() == utc
