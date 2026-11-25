"""SKARVIK OES 4 simulator tests: invariants S-I1…S-I7, every fault, formats and the control plane."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skarvik import formats
from skarvik.control import serve
from skarvik.simulator import Config, SimulatedCrash, Simulator

SEED = Path(__file__).resolve().parents[1] / "seed" / "seed.json"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 21, 13, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock()


def _crash():
    raise SimulatedCrash("crash")


@pytest.fixture
def sim(tmp_path, clock):
    cfg = Config(exchange_dir=tmp_path / "exchange", data_dir=tmp_path / "data", seed_file=SEED,
                 pickup_interval=1, extract_interval=2, dedup_window_seconds=120)
    return Simulator(cfg, clock=clock, crash=_crash)


def order(msgid="LB0000000001-01", custno="KND-0081", lines=(("NVB-6205-2RS", 10, "EA"),), ordref="LB0000000001"):
    msg = formats.OrderMessage(msgid=msgid, custno=custno, ordref=ordref, custpo="PO-1", reqdate="20261201",
                               note="Leverans vardagar", lines=[formats.OrderLine(i, it, q, u)
                                                                 for i, (it, q, u) in enumerate(lines, 1)])
    return formats.render_order(msg)


def drop(sim, data, msgid="LB0000000001-01"):
    (sim.inbox / f"ORD_{msgid}.csv").write_bytes(data)


def acks(sim):
    out = []
    for p in sorted(sim.acks.glob("ACK_*.xml")):
        out.append((p.name, p.read_text("utf-8")))
    return out


def orders_for(sim, msgid):
    return [o for o in sim.oracle_orders() if o["msgid"] == msgid]


# ------------------------------------------------------------------ invariants


def test_s_i1_inbox_file_unprocessed_and_withdrawn_never_processed(sim):
    drop(sim, order())
    assert orders_for(sim, "LB0000000001-01") == [] and acks(sim) == []  # still in inbox: not processed
    (sim.inbox / "ORD_LB0000000001-01.csv").rename(sim.withdrawn / "ORD_LB0000000001-01.csv")
    assert sim.pickup_once() == []
    assert orders_for(sim, "LB0000000001-01") == [] and sim.oracle_received() == []


def test_s_i2_rejected_means_no_order(sim):
    drop(sim, order(custno="KND-0190"))  # SPARR=J
    assert sim.pickup_once() == ["REJECTED:E-BLOCK"]
    assert orders_for(sim, "LB0000000001-01") == []
    assert 'status="REJECTED"' in acks(sim)[0][1] and 'code="E-BLOCK"' in acks(sim)[0][1]


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"custno": "KND-9999"}, "E-CUST"),
    ({"lines": (("NOPE", 1, "EA"),)}, "E-ITEM"),
    ({"lines": (("NVX-KEDJA-10", 1, "M"),)}, "E-ITEM"),  # discontinued
    ({"lines": (("NVB-6205-2RS", 1, "KRT"),)}, "E-UOM"),
    ({"lines": (("NVB-6205-2RS", 0, "EA"),)}, "E-QTY"),
])
def test_business_rejections(sim, kwargs, code):
    drop(sim, order(**kwargs))
    assert sim.pickup_once() == [f"REJECTED:{code}"]
    assert orders_for(sim, "LB0000000001-01") == []


def test_s_i3_accepted_ack_after_commit_with_backorder(sim):
    drop(sim, order(lines=(("NVH-HPU-22", 5, "EA"),)))  # only 2 in stock: accept with backorder
    assert sim.pickup_once() == ["ACCEPTED"]
    [o] = orders_for(sim, "LB0000000001-01")
    assert o["status"] == "BO"
    name, text = acks(sim)[0]
    assert name == "ACK_LB0000000001-01_1.xml" and f'orderno="{o["orderno"]}"' in text and 'status="ACCEPTED"' in text
    assert 'tz="Europe/Stockholm"' in text and 'ts="2026-11-21T14:00:00"' in text  # local time, no offset


def test_s_i4_duplicate_within_window(sim, clock):
    drop(sim, order())
    sim.pickup_once()
    clock.now += timedelta(seconds=60)
    drop(sim, order())
    assert sim.pickup_once() == ["DUPLICATE"]
    assert len(orders_for(sim, "LB0000000001-01")) == 1
    first = orders_for(sim, "LB0000000001-01")[0]["orderno"]
    name, text = acks(sim)[1]
    assert name == "ACK_LB0000000001-01_2.xml" and 'status="DUPLICATE"' in text and f'orderno="{first}"' in text


def test_s_i5_same_msgid_after_window_is_new(sim, clock):
    drop(sim, order())
    sim.pickup_once()
    clock.now += timedelta(seconds=121)
    drop(sim, order())
    assert sim.pickup_once() == ["ACCEPTED"]
    assert len(orders_for(sim, "LB0000000001-01")) == 2


def test_s_i6_ordref_never_deduplicated(sim):
    drop(sim, order(msgid="LB0000000001-01", ordref="SAME"), "LB0000000001-01")
    drop(sim, order(msgid="LB0000000002-01", ordref="SAME"), "LB0000000002-01")
    assert sim.pickup_once() == ["ACCEPTED", "ACCEPTED"]


def test_s_i7_extracts_are_snapshots(sim, clock):
    sim.generate_extracts(force=True)
    old = sorted(sim.extracts.glob("ORDERS_*.dat"))[-1]
    drop(sim, order())
    sim.pickup_once()
    assert "LB0000000001-01" not in old.read_bytes().decode("latin-1")  # older snapshot proves nothing
    clock.now += timedelta(seconds=2)
    sim.generate_extracts(force=True)
    new = sorted(sim.extracts.glob("ORDERS_*.dat"))[-1]
    text = new.read_bytes().decode("latin-1")
    assert new != old and "LB0000000001-01" in text
    assert text.startswith("#SNAPSHOT=20261121140002 TZ=Europe/Stockholm\r\n") and text.rstrip("\r\n").split("\r\n")[-1].startswith("#END ")


# ------------------------------------------------------------------ structure


def test_structure_errors_and_unreadable_files(sim):
    good = order()
    drop(sim, good[:-12], "LB0000000001-01")  # truncated trailer
    assert sim.pickup_once() == ["REJECTED:E-STRUCT"]
    (sim.inbox / "ORD_garbage.csv").write_bytes(b"\x00\x01not an order")
    assert sim.pickup_once() == ["ERROR_NO_MSGID"]
    assert (sim.error / "ORD_garbage.csv").exists()


def test_qtysum_and_linecount_checked():
    data = order().replace(b"T;LB0000000001-01;1;10", b"T;LB0000000001-01;1;11")
    with pytest.raises(formats.StructureError, match="QTYSUM"):
        formats.parse_order(data)


def test_latin1_round_trip():
    msg = formats.parse_order(order(ordref="Göta Ø Å"))
    assert msg.ordref == "Göta Ø Å"


def test_dot_files_ignored(sim):
    (sim.inbox / ".tmp-LB0000000001-01").write_bytes(order())
    assert sim.pickup_once() == []


# ------------------------------------------------------------------ faults


def test_fault_ack_delay(sim):
    sim.faults.ack_delay_seconds, sim.faults.ack_delay_count = 0.05, 1
    drop(sim, order())
    sim.pickup_once()
    assert acks(sim) == [] and len(orders_for(sim, "LB0000000001-01")) == 1
    time.sleep(0.06)
    assert sim.flush_due_acks() == 1 and len(acks(sim)) == 1


def test_fault_drop_ack(sim):
    sim.faults.counted["drop_ack"] = 1
    drop(sim, order())
    assert sim.pickup_once() == ["ACCEPTED:ACK_DROPPED"]
    assert acks(sim) == [] and len(orders_for(sim, "LB0000000001-01")) == 1


def test_fault_truncate_on_pickup(sim):
    sim.faults.counted["truncate_on_pickup"] = 1
    drop(sim, order())
    assert sim.pickup_once() == ["REJECTED:E-STRUCT"]
    assert orders_for(sim, "LB0000000001-01") == []


def test_fault_crash_after_commit_and_ack_lost_on_restart(sim, tmp_path, clock):
    sim.faults.counted["crash_after_commit"] = 1
    drop(sim, order())
    with pytest.raises(SimulatedCrash):
        sim.pickup_once()
    assert (sim.work / "ORD_LB0000000001-01.csv").exists()
    restarted = Simulator(sim.config, clock=clock, crash=_crash)
    assert restarted.startup_recovery() == ["ack lost for LB0000000001-01"]
    assert acks(restarted) == [] and len(orders_for(restarted, "LB0000000001-01")) == 1


def test_fault_contradict_ack(sim):
    sim.faults.counted["contradict_ack"] = 1
    drop(sim, order())
    assert sim.pickup_once() == ["ACCEPTED:ACK_CONTRADICTS"]
    assert 'status="REJECTED"' in acks(sim)[0][1] and len(orders_for(sim, "LB0000000001-01")) == 1


def test_fault_malformed_ack(sim):
    sim.faults.counted["malformed_ack"] = 1
    drop(sim, order())
    sim.pickup_once()
    name, text = acks(sim)[0]
    assert not text.rstrip().endswith("/>") and len(orders_for(sim, "LB0000000001-01")) == 1


def test_fault_pause_pickup(sim):
    sim.faults.pause_pickup = True
    drop(sim, order())
    assert sim.pickup_once() == [] and (sim.inbox / "ORD_LB0000000001-01.csv").exists()


def test_fault_stale_extracts(sim, clock):
    sim.generate_extracts(force=True)
    before = sorted(sim.extracts.glob("STOCK_*.dat"))
    sim.faults.stale_extracts = True
    clock.now += timedelta(seconds=10)
    assert sim.generate_extracts() is False
    assert sorted(sim.extracts.glob("STOCK_*.dat")) == before


def test_fault_duplicate_window_override(sim, clock):
    sim.faults.duplicate_window_seconds = 5
    drop(sim, order())
    sim.pickup_once()
    clock.now += timedelta(seconds=6)
    drop(sim, order())
    assert sim.pickup_once() == ["ACCEPTED"]


def test_reset_restores_seed(sim):
    drop(sim, order())
    sim.pickup_once()
    sim.faults.pause_pickup = True
    sim.reset()
    assert orders_for(sim, "LB0000000001-01") == [] and acks(sim) == [] and not sim.faults.pause_pickup
    assert len(sim.oracle_orders()) == 6 and sim.oracle_received() == []


# ------------------------------------------------------------------ extract formats


def test_extract_record_widths(sim):
    sim.generate_extracts(force=True)
    stock = sorted(sim.extracts.glob("STOCK_*.dat"))[-1].read_bytes().decode("latin-1").split("\r\n")
    assert all(len(r) == 31 for r in stock[1:-2])
    items = sorted(sim.extracts.glob("ITEMS_*.dat"))[-1].read_bytes().decode("latin-1").split("\r\n")
    assert all(len(r) == 75 for r in items[1:-2])
    custs = sorted(sim.extracts.glob("CUST_*.dat"))[-1].read_bytes().decode("latin-1").split("\r\n")
    assert all(len(r) == 131 for r in custs[1:-2]) and any("Ålesund" in r for r in custs)
    assert any("NV-GL-100L" in r and "   10" in r for r in items)


# ------------------------------------------------------------------ control plane


def _http(method, port, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback test server
        return json.loads(resp.read())


def test_control_plane_faults_oracles_and_reset(sim):
    server = serve(sim, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        armed = _http("POST", port, "/_sim/faults", {"fault": "drop_ack", "count": 2})
        assert armed["drop_ack"]["remaining"] == 2
        drop(sim, order())
        assert _http("POST", port, "/_sim/pickup", {})["outcomes"] == ["ACCEPTED:ACK_DROPPED"]
        received = _http("GET", port, "/_sim/oracle/received")
        assert received[0]["msgid"] == "LB0000000001-01" and len(received[0]["sha256"]) == 64
        assert any(o["msgid"] == "LB0000000001-01" for o in _http("GET", port, "/_sim/oracle/orders"))
        assert _http("DELETE", port, "/_sim/faults")["drop_ack"]["remaining"] == 0
        assert _http("POST", port, "/_sim/reset", {})["reset"] is True
        assert _http("GET", port, "/_sim/oracle/received") == []
    finally:
        server.shutdown()
