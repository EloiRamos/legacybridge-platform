"""SKARVIK reconciliation from consumer-visible evidence only (never the oracle endpoints).

Evidence: acknowledgement files for the message id (pending or archived); the newest complete ORDERS extract,
used only if ``snapshot_at ≥ first_handoff_at + snapshot_max_lag`` (else ``SOURCE_STALE``, S-I7); the inbox
listing (final file present ⇒ not yet processed); a staged temp file present with no final file and no
acknowledgement ⇒ absence proven (``NEVER_DELIVERED``, rule R-1); an ack ``REJECTED`` contradicted by an extract
order for the message id ⇒ ``CONTRADICTION``.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from lbf.adapters.skarvik import acks as ack_files
from lbf.adapters.skarvik import extracts
from lbf.adapters.skarvik.exchange import Share
from lbf.domain.enums import EvidenceClass
from lbf.domain.enums import ReconciliationVerdict as V
from lbf.domain.errors import IntegrationError
from lbf.domain.models import ReconciliationQuery, ReconciliationResult, SourceRead
from lbf.ports.capabilities import BackendCapabilities


def _matches(order: extracts.ExtractOrder, query: ReconciliationQuery) -> bool:
    if order.custno != query.native_customer_id or order.ordref != query.correlation_value:
        return False
    if query.message_id is not None and order.msgid != query.message_id:
        return False
    booked = Counter((ln.itemno, ln.qty) for ln in order.lines)
    return booked == Counter((ln.native_item, ln.quantity_native) for ln in query.expected_lines)


def reconcile(share: Share, query: ReconciliationQuery, caps: BackendCapabilities,
              clock: Callable[[], datetime]) -> ReconciliationResult:
    msgid = query.message_id or ""
    reads: list[SourceRead] = []
    now = clock()
    try:
        tmp_present = share.tmp(msgid).exists()
        final_present = share.final(msgid).exists()
        listing = f"staged={tmp_present} published_in_inbox={final_present}"
        reads.append(SourceRead(source="skarvik:inbox-listing", read_at=now,
                                response_sha256=hashlib.sha256(listing.encode()).hexdigest(), summary=listing))
        acks = [a for a in ack_files.acks_for(share, msgid) if a.status != "MALFORMED"]
        for a in acks:
            reads.append(SourceRead(source="skarvik:ack", read_at=now, response_sha256=a.file_sha256,
                                    summary=f"{a.file_name}: {a.status} {a.order_ref or ''} {a.code or ''}".strip()))
        if tmp_present and not final_present and not acks:
            return ReconciliationResult(verdict=V.NO_MATCH, source_reads=reads, absence_proven=True,
                                        absence_evidence=EvidenceClass.NEVER_DELIVERED,
                                        detail=f"Message {msgid} is staged but was never published to the inbox.")
        rejected = [a for a in acks if a.status == "REJECTED"]
        extract = extracts.newest(share, "ORDERS", required=False)
        lag = timedelta(seconds=caps.snapshot_max_lag_seconds or 0)
        fresh = extract is not None and extract.snapshot_at >= query.first_handoff_at + lag
        if extract is not None:
            reads.append(SourceRead(source="skarvik:ORDERS-extract", read_at=now, snapshot_at=extract.snapshot_at,
                                    response_sha256=extract.sha256,
                                    summary=f"{extract.file_name} "
                                            f"({'fresh' if fresh else 'older than handoff + lag'})"))
        if final_present:
            return ReconciliationResult(verdict=V.NO_MATCH, source_reads=reads,
                                        detail=f"Message {msgid} is still in the inbox (not yet processed).")
        if extract is None or not fresh:
            return ReconciliationResult(verdict=V.SOURCE_STALE, source_reads=reads,
                                        detail="No ORDERS extract newer than the handoff plus the snapshot lag yet.")
        found = [o for o in extracts.orders(extract) if o.msgid == msgid]
        if query.anchor_order_ref:
            found = [o for o in extracts.orders(extract) if o.orderno == query.anchor_order_ref]
        if rejected and found:
            return ReconciliationResult(verdict=V.CONTRADICTION, candidates=[o.orderno for o in found],
                                        source_reads=reads,
                                        detail="Acknowledgement REJECTED but the ORDERS extract lists "
                                               f"{found[0].orderno} "
                                               f"for message {msgid}.")
        if len(found) >= 2:
            return ReconciliationResult(verdict=V.AMBIGUOUS, candidates=[o.orderno for o in found], source_reads=reads,
                                        detail=f"{len(found)} orders carry message id {msgid}.")
        if not found:
            return ReconciliationResult(verdict=V.NO_MATCH, source_reads=reads,
                                        detail=f"No order for message {msgid} in a snapshot taken after handoff + lag.")
        order = found[0]
        if _matches(order, query):
            return ReconciliationResult(verdict=V.MATCHED, matched_order_ref=order.orderno, candidates=[order.orderno],
                                        source_reads=reads,
                                        detail=f"Order {order.orderno} in snapshot {extract.file_name} matches "
                                               "account, reference, message id and lines.")
        return ReconciliationResult(verdict=V.MISMATCH, candidates=[order.orderno], source_reads=reads,
                                    detail=f"Order {order.orderno} differs from the approved content.")
    except (IntegrationError, OSError, ValueError) as exc:
        return ReconciliationResult(verdict=V.READ_FAILED, source_reads=reads,
                                    detail=f"SKARVIK exchange could not be read ({exc.__class__.__name__}).")
