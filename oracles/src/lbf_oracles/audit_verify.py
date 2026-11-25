"""Standalone audit-chain verifier. Standard library only.

Checks an export (JSON Lines, one ``lbf-audit/1.0`` event per line) against its manifest: file digest, count,
range, contiguity of ``seq``, ``prev_hash`` links and every ``event_hash``. Reports the exact first broken
sequence number. ``reconstruct`` rebuilds one operation's evidence chain and checks the ordering rules (a)–(d).

HASH-CHAIN INTEGRITY != EVENT COMPLETENESS != AUTHENTICITY.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from lbf_oracles.canonical_json import dumps

GENESIS = "0" * 64
NOT_APPLIED_EVIDENCE = {"NOT_SENT", "PRE_PROCESSING_REFUSAL", "NEVER_DELIVERED", "WITHDRAWN_BEFORE_PICKUP",
                        "APPROVAL_EXPIRED"}


@dataclass
class Verdict:
    valid: bool
    count: int = 0
    head_seq: int = 0
    head_hash: str = GENESIS
    first_broken_seq: int | None = None
    reason: str = ""
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": "VALID" if self.valid else "INVALID", "count": self.count, "head_seq": self.head_seq,
                "head_hash": self.head_hash, "first_broken_seq": self.first_broken_seq, "reason": self.reason,
                "problems": self.problems}


def event_hash(doc: dict[str, Any]) -> str:
    return hashlib.sha256(dumps({k: v for k, v in doc.items() if k != "event_hash"})).hexdigest()


def parse_jsonl(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8")
    if text and not text.endswith("\n"):
        raise ValueError("export does not end with a newline (truncated)")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def verify_events(events: list[dict[str, Any]], *, start_seq: int = 1, start_prev: str = GENESIS) -> Verdict:
    expected, prev = start_seq, start_prev
    for count, doc in enumerate(events):
        seq = doc.get("seq")
        if seq != expected:
            return Verdict(False, count, expected - 1, prev, expected, f"sequence gap: expected {expected}, got {seq}")
        if doc.get("prev_hash") != prev:
            return Verdict(False, count, seq - 1, prev, seq, "prev_hash does not link to the previous event")
        if event_hash(doc) != doc.get("event_hash"):
            return Verdict(False, count, seq - 1, prev, seq, "event_hash does not match the event content")
        prev, expected = doc["event_hash"], expected + 1
    return Verdict(True, len(events), expected - 1, prev)


def verify_export(jsonl: bytes, manifest: dict[str, Any]) -> Verdict:
    digest = hashlib.sha256(jsonl).hexdigest()
    if digest != manifest.get("jsonl_sha256"):
        return Verdict(False, reason="manifest jsonl_sha256 does not match the export file (truncated or altered)")
    try:
        events = parse_jsonl(jsonl)
    except (ValueError, json.JSONDecodeError) as exc:
        return Verdict(False, reason=f"unreadable export: {exc}")
    from_seq = int(manifest.get("from_seq", 1))
    verdict = verify_events(events, start_seq=from_seq, start_prev=manifest.get("first_prev_hash", GENESIS))
    if not verdict.valid:
        return verdict
    if verdict.count != manifest.get("count") or verdict.head_seq != manifest.get("to_seq"):
        return Verdict(False, verdict.count, verdict.head_seq, verdict.head_hash, None,
                       "manifest count/range does not match the export")
    if verdict.head_hash != manifest.get("head_hash"):
        return Verdict(False, verdict.count, verdict.head_seq, verdict.head_hash, None,
                       "manifest head_hash does not match the chain head")
    return verdict


def reconstruct(events: list[dict[str, Any]], operation_id: str) -> dict[str, Any]:
    """Rebuild one operation's evidence chain and check rules (a)–(d)."""
    ops = [e for e in events if e.get("operation_id") == operation_id]
    problems: list[str] = []
    approved_hashes: dict[str, str] = {}  # authorization_id -> content_hash carried by its grant
    grants: list[str] = []
    started_per_auth: dict[str, int] = {}
    chain: list[dict[str, Any]] = []
    for e in ops:
        et, corr, payload = e["event_type"], e.get("correlation") or {}, e.get("payload") or {}
        chain.append({"seq": e["seq"], "event_type": et, "state": e.get("state")})
        if et == "APPROVAL_GRANTED":
            grants.append(str(corr.get("content_hash")))
        if et == "AUTHORIZATION_CREATED":
            auth = str(corr.get("authorization_id"))
            approved_hashes[auth] = str(corr.get("content_hash") or (grants[-1] if grants else ""))
        if et == "TRANSMISSION_STARTED":
            auth = str(corr.get("authorization_id"))
            started_per_auth[auth] = started_per_auth.get(auth, 0) + 1
            if auth not in approved_hashes:
                problems.append(f"(a) seq {e['seq']}: TRANSMISSION_STARTED without a prior AUTHORIZATION_CREATED")
            elif payload.get("content_hash") and approved_hashes[auth] != payload.get("content_hash"):
                problems.append(f"(a) seq {e['seq']}: rendered content hash differs from the authorized one")
        if et == "OUTCOME_RECORDED":
            outcome, evidence = payload.get("outcome"), payload.get("evidence_class")
            if outcome == "CONFIRMED" and payload.get("verification_level") == "VERIFIED_BY_READ" \
                    and not payload.get("source_reads"):
                problems.append(f"(c) seq {e['seq']}: verified CONFIRMED without a source read")
            if outcome == "NOT_APPLIED" and evidence not in NOT_APPLIED_EVIDENCE:
                problems.append(f"(d) seq {e['seq']}: NOT_APPLIED with evidence {evidence!r}")
    problems += [f"(b) authorization {a}: {n} TRANSMISSION_STARTED" for a, n in started_per_auth.items() if n > 1]
    return {"operation_id": operation_id, "events": len(ops), "transmissions": sum(started_per_auth.values()),
            "chain": chain, "rules_pass": not problems, "problems": problems}
