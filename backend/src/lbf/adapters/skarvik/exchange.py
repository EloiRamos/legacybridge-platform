"""File operations on the exchange share: staged delivery, withdrawal, redelivery.

Delivery protocol: write ``inbox/.tmp-<MSGID>`` (``O_CREAT|O_EXCL``), fsync, invoke the ``before_publish`` hook,
then publish with an atomic ``rename(tmp, ORD_<MSGID>.csv)`` and fsync the directory. Only this process creates final
names (SKARVIK only removes them by claiming), so the existence check before the rename cannot race with another
creator. SKARVIK ignores dot files, so a staged temp file is not a handoff; the rename is. Because the rename is
atomic, a temp file that exists while the final name does not proves the message never reached SKARVIK
(``NEVER_DELIVERED``).
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from lbf.domain.enums import EvidenceClass, TransportObservationKind
from lbf.domain.models import RenderedRequest, TransportObservation
from lbf.ports.backend import WithdrawResult


class Share:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.inbox = root / "inbox"
        self.withdrawn = root / "withdrawn"
        self.acks = root / "outbox" / "acks"
        self.archive = root / "outbox" / "acks" / "archive"
        self.extracts = root / "outbox" / "extracts"

    def final(self, msgid: str) -> Path:
        return self.inbox / f"ORD_{msgid}.csv"

    def tmp(self, msgid: str) -> Path:
        """Staging name of the FIRST delivery; its presence without a final file proves non-delivery."""
        return self.inbox / f".tmp-{msgid}"

    def redelivery_tmp(self, msgid: str) -> Path:
        """Distinct staging name for a redelivery, so it can never be mistaken for first-delivery evidence."""
        return self.inbox / f".tmp-{msgid}-r"

    def writable(self) -> bool:
        return all(d.is_dir() and os.access(d, os.W_OK) for d in (self.inbox, self.withdrawn, self.archive))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def deliver(share: Share, rendered: RenderedRequest, *, clock: Callable[[], datetime],
            before_publish: Callable[[RenderedRequest], None], redelivery: bool = False) -> TransportObservation:
    msgid = rendered.request.message_id or ""
    final = share.final(msgid)
    if redelivery:
        # A redelivery only happens from UNKNOWN; a leftover first-delivery staging file is ours and is removed so that
        # it cannot later be read as never-delivered evidence while the redelivered message is being processed.
        share.tmp(msgid).unlink(missing_ok=True)
        tmp = share.redelivery_tmp(msgid)
    else:
        tmp = share.tmp(msgid)

    def obs(kind: TransportObservationKind, detail: str) -> TransportObservation:
        return TransportObservation(kind=kind, detail=detail, observed_at=clock(),
                                    payload_sha256_on_wire=rendered.payload_sha256)

    # --- stage (not visible to SKARVIK) -------------------------------------------------------
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, exc.errno)
        return obs(TransportObservationKind.NOT_DELIVERED, f"cannot create staging file ({code})")
    try:
        try:
            os.write(fd, rendered.payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return obs(TransportObservationKind.NOT_DELIVERED, f"staging write failed ({exc.__class__.__name__})")
    try:
        # Engine pause point ``before_handoff``: the message is durable but invisible to SKARVIK (dot file).
        # An ordinary exception here means nothing was published: remove the staging file -> NOT_DELIVERED.
        # A process kill (BaseException in tests) leaves the staging file as NEVER_DELIVERED evidence (R11).
        before_publish(rendered)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return obs(TransportObservationKind.NOT_DELIVERED, f"pre-publish hook failed ({exc.__class__.__name__})")
    # --- publish (the handoff) ------------------------------------------------------------------
    if final.exists():
        tmp.unlink(missing_ok=True)
        return obs(TransportObservationKind.NOT_DELIVERED,
                   "a message with this id is still in the inbox; nothing published")
    try:
        os.rename(tmp, final)
    except OSError as exc:
        if tmp.exists() and not final.exists():
            tmp.unlink(missing_ok=True)
            return obs(TransportObservationKind.NOT_DELIVERED, f"publish failed ({exc.__class__.__name__})")
        return obs(TransportObservationKind.UNCERTAIN, f"publish outcome unknown ({exc.__class__.__name__})")
    try:
        _fsync_dir(share.inbox)
    except OSError as exc:
        return obs(TransportObservationKind.UNCERTAIN, f"renamed but directory fsync failed ({exc.__class__.__name__})")
    return obs(TransportObservationKind.DELIVERED_PENDING_ACK, f"ORD_{msgid}.csv placed in inbox")


def withdraw(share: Share, msgid: str) -> WithdrawResult:
    """Rename ``inbox/ORD_x`` → ``withdrawn/ORD_x``. Success proves it will never be processed (S-I1)."""
    try:
        os.rename(share.final(msgid), share.withdrawn / f"ORD_{msgid}.csv")
    except FileNotFoundError:
        return WithdrawResult(withdrawn=False, absence_evidence=None,
                              detail="message not in inbox: already claimed by SKARVIK or never delivered")
    _fsync_dir(share.withdrawn)
    return WithdrawResult(withdrawn=True, absence_evidence=EvidenceClass.WITHDRAWN_BEFORE_PICKUP,
                          detail=f"ORD_{msgid}.csv moved to withdrawn/ before pickup")
