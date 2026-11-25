"""Defect found by R05: a backend response containing a NUL byte could not be persisted (PostgreSQL text and
jsonb reject U+0000), so the executor lost the observation and the transmission ended INTERRUPTED via the sweeper.
Untrusted backend text entering the domain is made storable: control characters other than tab/newline/CR become a
visible ``\\xNN`` escape; ``response_sha256`` still binds the exact bytes."""

from __future__ import annotations

from datetime import UTC, datetime

from lbf.domain.enums import ReconciliationVerdict, TransportObservationKind
from lbf.domain.models import ReconciliationResult, SourceRead, TransportObservation, storable_text

NOW = datetime(2026, 11, 22, tzinfo=UTC)
CORRUPT = "\x00#WIRETAP-CORRUPTED#\x7f\x1b[0m\tok\nline\r"


def test_storable_text_escapes_controls_but_keeps_whitespace():
    assert storable_text(CORRUPT) == "\\x00#WIRETAP-CORRUPTED#\\x7f\\x1b[0m\tok\nline\r"
    assert storable_text("Ålesund — 10 × BW-1001") == "Ålesund — 10 × BW-1001"
    assert storable_text(None) is None


def test_transport_observation_fields_are_storable():
    obs = TransportObservation(kind=TransportObservationKind.UNCERTAIN, observed_at=NOW, response_excerpt=CORRUPT,
                               detail=CORRUPT, backend_code="\x00rc", backend_order_ref="SO\x00-1")
    for value in (obs.response_excerpt, obs.detail, obs.backend_code, obs.backend_order_ref):
        assert "\x00" not in value and "\\x00" in value
    assert len(TransportObservation(kind=TransportObservationKind.UNCERTAIN, observed_at=NOW,
                                    response_excerpt="\x00" * 600).response_excerpt) <= 512


def test_reconciliation_text_is_storable():
    read = SourceRead(source="halden E6", read_at=NOW, response_sha256="0" * 64, summary="\x00garbage")
    res = ReconciliationResult(verdict=ReconciliationVerdict.READ_FAILED, source_reads=[read], detail="\x00x")
    assert "\x00" not in res.detail and "\x00" not in res.source_reads[0].summary


def test_ack_observation_text_is_storable():
    from lbf.ports.backend import AckObservation

    ack = AckObservation(message_id="M\x00", seq=None, status="MALFORMED", order_ref=None, code="\x01",
                         ts_utc=None, file_sha256="0" * 64, file_name="ACK_x.csv", detail="\x00\x00")
    assert ack.message_id == "M\\x00" and ack.code == "\\x01" and ack.detail == "\\x00\\x00"
