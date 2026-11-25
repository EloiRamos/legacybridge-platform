"""Acknowledgement files: hardened XML parsing with defusedxml (SEC-11) and archiving."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from lbf.adapters.skarvik.exchange import Share
from lbf.adapters.skarvik.formats import stockholm_to_utc
from lbf.ports.backend import AckObservation

ACK_NAME = re.compile(r"^ACK_([A-Z0-9][A-Z0-9-]{0,14})_(\d+)\.xml$")
MAX_ACK_BYTES = 64 * 1024


def parse_ack(name: str, data: bytes) -> AckObservation:
    digest = hashlib.sha256(data).hexdigest()
    m = ACK_NAME.match(name)
    name_msgid, name_seq = (m.group(1), int(m.group(2))) if m else (None, None)

    def malformed(detail: str) -> AckObservation:
        return AckObservation(message_id=name_msgid, seq=name_seq, status="MALFORMED", order_ref=None, code=None,
                              ts_utc=None, file_sha256=digest, file_name=name, detail=detail)

    if len(data) > MAX_ACK_BYTES:
        return malformed("acknowledgement larger than 64 KiB")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, SafeET.ParseError, ValueError) as exc:
        return malformed(f"unparseable acknowledgement ({exc.__class__.__name__})")
    attrs = root.attrib
    status = attrs.get("status", "")
    msgid = attrs.get("msgid")
    if root.tag != "ACK" or status not in ("ACCEPTED", "REJECTED", "DUPLICATE") or not msgid:
        return malformed("acknowledgement without the required ACK attributes")
    if name_msgid is not None and msgid != name_msgid:
        return malformed("message id in the file name and in the document differ")
    try:
        seq = int(attrs.get("seq", ""))
        ts = stockholm_to_utc(attrs.get("ts", ""), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return malformed("invalid seq or ts")
    if attrs.get("tz") != "Europe/Stockholm":
        return malformed("unexpected time zone")
    return AckObservation(message_id=msgid, seq=seq, status=status, order_ref=attrs.get("orderno") or None,
                          code=attrs.get("code") or None, ts_utc=ts, file_sha256=digest, file_name=name)


def poll(share: Share) -> list[AckObservation]:
    out = []
    for path in sorted(share.acks.glob("ACK_*.xml")):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        out.append(parse_ack(path.name, data))
    return out


def archive(share: Share, ack: AckObservation) -> None:
    source = share.acks / ack.file_name
    target = share.archive / ack.file_name
    if target.exists():
        source.unlink(missing_ok=True)
        return
    try:
        os.rename(source, target)
    except FileNotFoundError:
        pass


def acks_for(share: Share, msgid: str) -> list[AckObservation]:
    """All acknowledgements for a message id, pending or archived (read-only)."""
    found: dict[str, AckObservation] = {}
    for directory in (share.archive, share.acks):
        for path in sorted(directory.glob(f"ACK_{msgid}_*.xml")):
            try:
                ack = parse_ack(path.name, Path(path).read_bytes())
            except FileNotFoundError:
                continue
            found.setdefault(ack.file_sha256, ack)
    return sorted(found.values(), key=lambda a: (a.seq or 0, a.file_name))
