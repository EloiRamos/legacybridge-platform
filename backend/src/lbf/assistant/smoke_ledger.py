"""Durable per-authorization request-slot ledger for the live smoke.

The cap ``max_requests`` applies to an authorization (its ``run_id``), not to one process. This ledger is a local
SQLite file outside the disposable ``lbf_eval`` database and outside the smoke evidence directories. Every provider
delegation needs a slot reserved and committed here first:

* ``reserve()`` runs in one ``BEGIN IMMEDIATE`` transaction (SQLite's write lock is an OS file lock, so it serialises
  separate processes): count the reserved slots, insert ``SLOT_RESERVED(slot_no = count + 1)`` only while
  ``count < max_requests`` and the authorization is unexpired, commit with ``synchronous=FULL``;
* inside the database a partial unique index and a trigger make ``slot_no`` unique and ``1..max_requests``, and
  triggers reject every ``UPDATE``/``DELETE``: slots are never refunded (a timeout, a crash or an unknown outcome
  keeps its slot; overcounting is accepted, undercounting is not);
* the identity of an authorization is its ``run_id`` bound to the SHA-256 of the canonical authorization JSON at first
  use; the same ``run_id`` with different content is an ``IDENTITY_CONFLICT``, never a fresh allowance;
* the ledger is never created implicitly. A missing, unreadable, invalid or locked ledger raises ``LedgerError`` and
  callers must fail closed (zero new delegations). ``init_ledger`` is a separate, explicit operator action;
* an authorization continues only from a resolved end (model rule, R-5 and R-6). Admission of an
  invocation is one transaction: it is refused while any earlier invocation is unfinished, or any slot has no recorded
  outcome, so a process lost between detecting an unexpected model and committing the abort leaves the authorization
  unusable. ``finish_invocation`` writes the end only when every slot is resolved and an abort, if there was one, is
  stored. Nothing clears this state: continuing needs a new owner-approved ``run_id``.

Recorded states: ``SLOT_RESERVED`` ≠ ``PROVIDER_DELEGATION_STARTED`` ≠ ``RESPONSE_OBSERVED`` / ``SENT_OR_UNKNOWN`` /
``NOT_SENT_LOCAL``. A reservation does not prove a request was sent; a delegation without a recorded outcome means
*sent or unknown*. The OpenAI dashboard and invoice remain authoritative for what was sent and billed. The ledger holds
no key, prompt or model output — only identifiers, counts, categories and timestamps.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lbf.assistant.config import AUTHORIZED_OPENAI_MODEL

SCHEMA_VERSION = "2"  # 2 adds CAMPAIGN_ABORTED; files of earlier runs are history and are not opened again
BUSY_TIMEOUT_S = 5.0
OUTCOMES = ("RESPONSE_OBSERVED", "SENT_OR_UNKNOWN", "NOT_SENT_LOCAL")
KINDS = ("INVOCATION_STARTED", "INVOCATION_FINISHED", "INVOCATION_REFUSED", "IDENTITY_CONFLICT", "CAP_DENIED",
         "SLOT_RESERVED", "PROVIDER_DELEGATION_STARTED", "CAMPAIGN_ABORTED", *OUTCOMES)
TRIGGERS = frozenset({"slot_within_cap", "meta_no_update", "meta_no_delete", "auth_no_update", "auth_no_delete",
                      "events_no_update", "events_no_delete"})

SCHEMA = """
CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE authorizations (
    run_id TEXT PRIMARY KEY,
    auth_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    max_requests INTEGER NOT NULL CHECK (max_requests BETWEEN 1 AND 3),
    expires_at TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES authorizations (run_id),
    invocation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('INVOCATION_STARTED', 'INVOCATION_FINISHED', 'INVOCATION_REFUSED',
        'IDENTITY_CONFLICT', 'CAP_DENIED', 'SLOT_RESERVED', 'PROVIDER_DELEGATION_STARTED', 'RESPONSE_OBSERVED',
        'SENT_OR_UNKNOWN', 'NOT_SENT_LOCAL', 'CAMPAIGN_ABORTED')),
    slot_no INTEGER,
    scenario TEXT,
    pid INTEGER NOT NULL,
    at TEXT NOT NULL,
    detail TEXT
);
CREATE UNIQUE INDEX one_reservation_per_slot ON events (run_id, slot_no) WHERE kind = 'SLOT_RESERVED';
CREATE TRIGGER slot_within_cap BEFORE INSERT ON events WHEN NEW.kind = 'SLOT_RESERVED' BEGIN
    SELECT RAISE(ABORT, 'slot outside the authorized cap') WHERE NEW.slot_no IS NULL OR NEW.slot_no < 1
        OR NEW.slot_no > (SELECT max_requests FROM authorizations WHERE run_id = NEW.run_id);
END;
CREATE TRIGGER meta_no_update BEFORE UPDATE ON ledger_meta BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON ledger_meta BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER auth_no_update BEFORE UPDATE ON authorizations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER auth_no_delete BEFORE DELETE ON authorizations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


class LedgerError(RuntimeError):
    """The ledger is missing, invalid, locked or failed to commit. Callers must fail closed."""


class NotAdmitted(LedgerError):
    """The ledger works and refuses a further invocation of this authorization."""


# a slot without a recorded outcome: the request was not sent, was lost, or is in flight — nothing here can tell
_OPEN_SLOTS = """SELECT 1 FROM events r WHERE r.run_id = :run AND r.kind = 'SLOT_RESERVED'
 AND (:invocation IS NULL OR r.invocation_id = :invocation) AND NOT EXISTS (SELECT 1 FROM events o
   WHERE o.run_id = r.run_id AND o.slot_no = r.slot_no
   AND o.kind IN ('RESPONSE_OBSERVED', 'SENT_OR_UNKNOWN', 'NOT_SENT_LOCAL')) LIMIT 1"""
_OTHERS = """SELECT invocation_id, EXISTS (SELECT 1 FROM events f WHERE f.run_id = s.run_id
   AND f.invocation_id = s.invocation_id AND f.kind = 'INVOCATION_FINISHED')
 FROM events s WHERE s.run_id = :run AND s.kind = 'INVOCATION_STARTED' AND s.invocation_id <> :own"""


def authorization_digest(auth: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the whole authorization snapshot (key order and whitespace do not matter)."""
    data = json.dumps(auth, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return parsed.astimezone(UTC)


def _connect(path: Path, busy_timeout_s: float) -> sqlite3.Connection:
    # mode=rw: never creates the file. isolation_level=None: transactions are explicit (BEGIN IMMEDIATE / COMMIT).
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None,
                         timeout=busy_timeout_s)
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_ledger(path: Path, *, max_cap: int = 3, purpose: str = "O1a durable authorization consumption (F-1)") -> str:
    """Create a new, empty ledger (explicit operator action). Refuses if anything already exists at ``path``.
    ``max_cap`` is the largest ``max_requests`` the file can ever hold: 3 for the smoke, 8 only for the
    two-turn live validation, 200 for an evaluation campaign."""
    if max_cap not in (3, 8, 200):
        raise LedgerError("unsupported request cap")
    if path.exists() or path.is_symlink():
        raise LedgerError(f"a ledger already exists at {path}; it is never replaced")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    ledger_id = uuid.uuid4().hex
    try:
        db = _connect(path, BUSY_TIMEOUT_S)
        try:
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise LedgerError("rollback-journal mode unavailable")
            db.executescript("BEGIN IMMEDIATE;" + SCHEMA.replace("BETWEEN 1 AND 3", f"BETWEEN 1 AND {max_cap}"))
            db.executemany("INSERT INTO ledger_meta (key, value) VALUES (?, ?)",
                           [("schema_version", SCHEMA_VERSION), ("ledger_id", ledger_id), ("created_at", _now()),
                            ("purpose", purpose)])
            db.execute("COMMIT")
        finally:
            db.close()
    except (sqlite3.Error, LedgerError) as exc:
        path.unlink(missing_ok=True)
        raise LedgerError(f"ledger initialization failed: {exc}") from exc
    return ledger_id


class Ledger:
    """An open, verified ledger. Every method raises ``LedgerError`` on any storage problem."""

    def __init__(self, path: Path, *, busy_timeout_s: float = BUSY_TIMEOUT_S) -> None:
        self.path = path
        if not path.is_file():
            raise LedgerError(f"no O1a authorization ledger at {path}; it is never created implicitly "
                              "(initialise it once with: python -m lbf.assistant.smoke init-ledger)")
        try:
            self._db = _connect(path, busy_timeout_s)
        except sqlite3.Error as exc:
            raise LedgerError(f"ledger unavailable: {exc}") from exc
        try:
            if self._db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise LedgerError("ledger integrity check failed")
            meta = dict(self._db.execute("SELECT key, value FROM ledger_meta").fetchall())
            triggers = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        except sqlite3.Error as exc:
            self._db.close()
            raise LedgerError(f"not a valid O1a ledger: {exc}") from exc
        except LedgerError:
            self._db.close()
            raise
        if meta.get("schema_version") != SCHEMA_VERSION or not meta.get("ledger_id") or triggers != TRIGGERS:
            self._db.close()
            raise LedgerError("not a valid O1a ledger (schema version, ledger_id or integrity triggers differ)")
        self.ledger_id: str = meta["ledger_id"]
        self.created_at: str = meta.get("created_at", "")

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------------------------------------- transactions
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            self._db.execute("BEGIN IMMEDIATE")  # cross-process write lock, held until COMMIT/ROLLBACK
        except sqlite3.Error as exc:
            raise LedgerError(f"ledger locked or unavailable: {exc}") from exc
        try:
            yield self._db
            self._db.execute("COMMIT")
        except BaseException as exc:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise LedgerError(f"ledger write failed: {exc}") from exc
            raise

    def _read(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise LedgerError(f"ledger read failed: {exc}") from exc

    @staticmethod
    def _event(db: sqlite3.Connection, run_id: str, invocation_id: str, kind: str, *, slot_no: int | None = None,
               scenario: str | None = None, detail: dict[str, Any] | None = None) -> None:
        db.execute("INSERT INTO events (run_id, invocation_id, kind, slot_no, scenario, pid, at, detail) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (run_id, invocation_id, kind, slot_no, scenario, os.getpid(), _now(),
                    None if detail is None else json.dumps(detail, sort_keys=True)))

    # ------------------------------------------------------------------------------------------- identity
    def bind(self, auth: dict[str, Any]) -> str | None:
        """Register the authorization at first use, or verify it is the same one. Returns a refusal reason."""
        run_id = str(auth["run_id"])
        if auth.get("model") != AUTHORIZED_OPENAI_MODEL:  # nothing is written
            return "authorization does not name the authorized model"
        digest = authorization_digest(auth)
        with self._write() as db:
            row = db.execute("SELECT auth_sha256 FROM authorizations WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO authorizations VALUES (?, ?, ?, ?, ?, ?)",
                           (run_id, digest, str(auth["model"]), int(auth["max_requests"]),
                            _utc(auth["expires_at"]).isoformat(), _now()))
                return None
            if row[0] != digest:
                self._event(db, run_id, "-", "IDENTITY_CONFLICT",
                            detail={"bound_sha256": row[0], "presented_sha256": digest})
                return (f"authorization identity conflict: run_id {run_id} is bound to a different authorization "
                        "(a changed authorization needs a new owner-approved run_id)")
            if self._aborted(db, run_id):
                self._event(db, run_id, "-", "INVOCATION_REFUSED", detail={"reason": "aborted"})
                return f"run {run_id} ended ABORTED_MODEL_MISMATCH; it never resumes"
        return None

    @staticmethod
    def _aborted(db: sqlite3.Connection, run_id: str) -> bool:
        # a recorded mismatch ends the authorization even if the abort record itself could not be written
        return db.execute("SELECT 1 FROM events WHERE run_id = ? AND (kind = 'CAMPAIGN_ABORTED' OR (kind = "
                          "'RESPONSE_OBSERVED' AND json_extract(detail, '$.error_category') = 'MODEL_MISMATCH')) "
                          "LIMIT 1", (run_id,)).fetchone() is not None

    @staticmethod
    def _unresolved(db: sqlite3.Connection, run_id: str, own: str = "") -> bool:
        """Whether another invocation of this authorization has no resolved end (running, lost, or not provable)."""
        return any(not finished or Ledger._open_slots(db, run_id, invocation) for invocation, finished in
                   db.execute(_OTHERS, {"run": run_id, "own": own}).fetchall())

    @staticmethod
    def _open_slots(db: sqlite3.Connection, run_id: str, invocation_id: str | None = None) -> bool:
        return db.execute(_OPEN_SLOTS, {"run": run_id, "invocation": invocation_id}).fetchone() is not None

    def observed_model(self, run_id: str) -> str | None:
        """The first model identifier accepted under this authorization, across invocations (R-6)."""
        for row in self._read("SELECT detail FROM events WHERE run_id = ? AND kind = 'RESPONSE_OBSERVED' "
                              "ORDER BY seq", (run_id,)):
            reported = json.loads(row[0] or "{}").get("model_reported")
            if reported:
                return str(reported)
        return None

    def bound_digest(self, run_id: str) -> str | None:
        rows = self._read("SELECT auth_sha256 FROM authorizations WHERE run_id = ?", (run_id,))
        return str(rows[0][0]) if rows else None

    # ------------------------------------------------------------------------------------------- slots
    def consumed(self, run_id: str) -> int:
        return int(self._read("SELECT COUNT(*) FROM events WHERE run_id = ? AND kind = 'SLOT_RESERVED'",
                              (run_id,))[0][0])

    def remaining(self, run_id: str) -> int:
        rows = self._read("SELECT max_requests FROM authorizations WHERE run_id = ?", (run_id,))
        if not rows:
            raise LedgerError(f"authorization {run_id} is not bound")
        try:
            if self._aborted(self._db, run_id) or self._unresolved(self._db, run_id):
                return 0
        except sqlite3.Error as exc:
            raise LedgerError(f"ledger read failed: {exc}") from exc
        return max(0, int(rows[0][0]) - self.consumed(run_id))

    def reserve(self, run_id: str, invocation_id: str, scenario: str | None = None) -> int | None:
        """Atomically reserve and commit the next slot. ``None``: aborted, another invocation unresolved, exhausted or
        expired (nothing reserved)."""
        with self._write() as db:
            row = db.execute("SELECT max_requests, expires_at FROM authorizations WHERE run_id = ?",
                             (run_id,)).fetchone()
            if row is None:
                raise LedgerError(f"authorization {run_id} is not bound")
            used = int(db.execute("SELECT COUNT(*) FROM events WHERE run_id = ? AND kind = 'SLOT_RESERVED'",
                                  (run_id,)).fetchone()[0])
            reason = ("aborted" if self._aborted(db, run_id) else
                      "unresolved" if self._unresolved(db, run_id, invocation_id) else
                      "exhausted" if used >= int(row[0]) else
                      "expired" if _utc(row[1]) <= datetime.now(UTC) else None)
            if reason is not None:
                self._event(db, run_id, invocation_id, "CAP_DENIED", scenario=scenario,
                            detail={"reason": reason, "consumed": used, "max_requests": int(row[0])})
                return None
            self._event(db, run_id, invocation_id, "SLOT_RESERVED", slot_no=used + 1, scenario=scenario)
            return used + 1

    def record(self, run_id: str, invocation_id: str, kind: str, *, slot_no: int | None = None,
               scenario: str | None = None, detail: dict[str, Any] | None = None) -> None:
        if kind not in KINDS or kind == "SLOT_RESERVED":
            raise ValueError(f"not a recordable ledger event: {kind}")
        with self._write() as db:
            self._event(db, run_id, invocation_id, kind, slot_no=slot_no, scenario=scenario, detail=detail)

    def start_invocation(self, run_id: str, *, only: bool = False) -> str:
        """Admit one invocation, atomically across processes. ``only``: the authorization has a single invocation."""
        invocation_id = uuid.uuid4().hex
        with self._write() as db:
            earlier = db.execute("SELECT 1 FROM events WHERE run_id = ? AND kind IN ('INVOCATION_STARTED', "
                                 "'SLOT_RESERVED') LIMIT 1", (run_id,)).fetchone() is not None
            reason = ("aborted" if self._aborted(db, run_id) else "consumed" if only and earlier else
                      "unresolved" if self._open_slots(db, run_id) or self._unresolved(db, run_id) else None)
            self._event(db, run_id, invocation_id if reason is None else "-",
                        "INVOCATION_STARTED" if reason is None else "INVOCATION_REFUSED",
                        detail=None if reason is None else {"reason": reason})
        if reason is not None:
            raise NotAdmitted({"aborted": f"run {run_id} ended ABORTED_MODEL_MISMATCH; it never resumes",
                               "consumed": f"run {run_id} has one invocation only, and it has started",
                               "unresolved": f"an earlier invocation of run {run_id} has no resolved end; a further "
                                             "run needs a new owner-approved run_id"}[reason])
        return invocation_id

    def finish_invocation(self, run_id: str, invocation_id: str, detail: dict[str, Any], *,
                          abort: dict[str, Any] | None = None, failed: bool = False) -> bool:
        """Record the end of an invocation if it is provable: every slot resolved, no ledger failure (``failed``), and
        the abort stored. Otherwise the invocation stays unfinished, which keeps the authorization unusable."""
        with self._write() as db:
            if abort is not None and db.execute("SELECT 1 FROM events WHERE run_id = ? AND kind = 'CAMPAIGN_ABORTED' "
                                                "LIMIT 1", (run_id,)).fetchone() is None:
                self._event(db, run_id, invocation_id, "CAMPAIGN_ABORTED", detail=abort)
            if failed or self._open_slots(db, run_id, invocation_id):
                return False
            self._event(db, run_id, invocation_id, "INVOCATION_FINISHED", detail=detail)
            return True

    # ------------------------------------------------------------------------------------------- evidence
    def events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT seq, run_id, invocation_id, kind, slot_no, scenario, pid, at, detail FROM events"
        rows = self._read(sql + (" WHERE run_id = ? ORDER BY seq" if run_id else " ORDER BY seq"),
                          (run_id,) if run_id else ())
        keys = ("seq", "run_id", "invocation_id", "kind", "slot_no", "scenario", "pid", "at", "detail")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def summary(self, run_id: str) -> dict[str, Any]:
        """Reviewer view of one authorization: slots, delegations and outcomes (unresolved ⇒ sent or unknown)."""
        events = self.events(run_id)
        reserved = {e["slot_no"] for e in events if e["kind"] == "SLOT_RESERVED"}
        delegated = {e["slot_no"] for e in events if e["kind"] == "PROVIDER_DELEGATION_STARTED"}
        resolved = {e["slot_no"]: e["kind"] for e in events if e["kind"] in OUTCOMES}
        started = {e["invocation_id"] for e in events if e["kind"] == "INVOCATION_STARTED"}
        finished = {e["invocation_id"] for e in events if e["kind"] == "INVOCATION_FINISHED"}
        return {
            "ledger_id": self.ledger_id, "auth_sha256": self.bound_digest(run_id),
            "slots_reserved": len(reserved), "delegations_started": len(delegated),
            "responses_observed": sum(1 for k in resolved.values() if k == "RESPONSE_OBSERVED"),
            "sent_or_unknown": sum(1 for s in delegated if resolved.get(s, "SENT_OR_UNKNOWN") == "SENT_OR_UNKNOWN"),
            "not_sent_local": sum(1 for k in resolved.values() if k == "NOT_SENT_LOCAL"),
            "reserved_never_delegated": len(reserved - delegated),
            "cap_denials": sum(1 for e in events if e["kind"] == "CAP_DENIED"),
            "invocations_started": len(started), "invocations_unfinished": len(started - finished),
        }
