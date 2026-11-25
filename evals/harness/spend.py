"""One durable expenditure ceiling for every paid run: smoke tests, campaigns, failed attempts, reruns.

``SpendBudget`` is the production ``BudgetService`` with one more precondition: before a request, its worst case must
also fit under what the ledger has left (settled + outstanding + next <= ceiling). The file is append-only, is shared by
all campaigns and is never reset; a reservation without a settlement counts in full. It is a local control computed from
configured prices and the usage the provider reports, not a billing guarantee: the invoice is authoritative."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from lbf.assistant.budget import BudgetService, Denial, Reservation

MICRO = Decimal(1_000_000)
SCHEMA = """
CREATE TABLE meta (ceiling_micro_usd INTEGER NOT NULL CHECK (ceiling_micro_usd > 0), created_at TEXT NOT NULL);
CREATE TABLE entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    entry TEXT NOT NULL CHECK (entry IN ('RESERVE', 'SETTLE')),
    amount_micro_usd INTEGER NOT NULL CHECK (amount_micro_usd >= 0),
    basis TEXT,
    campaign TEXT,
    at TEXT NOT NULL,
    UNIQUE (call_id, entry)
);
CREATE TRIGGER settle_needs_reserve BEFORE INSERT ON entries WHEN NEW.entry = 'SETTLE' BEGIN
    SELECT RAISE(ABORT, 'not reserved') WHERE NOT EXISTS
        (SELECT 1 FROM entries WHERE call_id = NEW.call_id AND entry = 'RESERVE');
END;
CREATE TRIGGER meta_fixed BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER meta_kept BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER entries_fixed BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER entries_kept BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""
USED = """SELECT COALESCE(SUM(CASE WHEN entry = 'SETTLE' THEN amount_micro_usd END), 0),
 COALESCE(SUM(CASE WHEN entry = 'RESERVE' AND call_id NOT IN (SELECT call_id FROM entries WHERE entry = 'SETTLE')
              THEN amount_micro_usd END), 0) FROM entries"""


def _micro(amount: Decimal) -> int:
    return int((amount * MICRO).to_integral_value(rounding=ROUND_UP))


def init_spend_ledger(path: Path, ceiling_usd: Decimal) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError(f"an expenditure ledger already exists at {path}; it is never replaced")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO meta VALUES (?, ?)", (_micro(ceiling_usd), datetime.now(UTC).isoformat()))
        db.commit()
    return path


class SpendLedger:
    """Every operation opens the file again, so one object serves all worker threads."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise ValueError(f"no expenditure ledger at {path}; it is never created implicitly")
        self.path = path
        self.ceiling = self._ceiling()

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _ceiling(self) -> int:
        try:
            with closing(self._open()) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("expenditure ledger damaged")
                [(ceiling,)] = db.execute("SELECT ceiling_micro_usd FROM meta").fetchall()
                return int(ceiling)
        except sqlite3.Error as exc:
            raise ValueError(f"not an expenditure ledger: {exc}") from exc

    def _append(self, call_id: uuid.UUID, entry: str, amount: Decimal, basis: str | None, campaign: str | None,
                limited: bool) -> bool:
        with closing(self._open()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if limited and sum(db.execute(USED).fetchone()) + _micro(amount) > self.ceiling:
                    db.execute("ROLLBACK")
                    return False
                db.execute("INSERT INTO entries (call_id, entry, amount_micro_usd, basis, campaign, at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (str(call_id), entry, _micro(amount), basis, campaign,
                                                         datetime.now(UTC).isoformat()))
                db.execute("COMMIT")
                return True
            except sqlite3.Error as exc:
                db.execute("ROLLBACK")
                raise ValueError(f"expenditure ledger refused the entry: {exc}") from exc

    def reserve(self, call_id: uuid.UUID, amount: Decimal, campaign: str) -> bool:
        return self._append(call_id, "RESERVE", amount, None, campaign, True)

    def settle(self, call_id: uuid.UUID, amount: Decimal, basis: str) -> None:
        self._append(call_id, "SETTLE", amount, basis, None, False)

    def summary(self) -> dict[str, Any]:
        with closing(self._open()) as db:
            settled, outstanding = db.execute(USED).fetchone()
            rows = db.execute("SELECT campaign, COUNT(*) FROM entries WHERE entry = 'RESERVE' GROUP BY campaign "
                              "ORDER BY MIN(seq)").fetchall()
        return {"ceiling_usd": Decimal(self.ceiling) / MICRO, "settled_usd": Decimal(settled) / MICRO,
                "outstanding_usd": Decimal(outstanding) / MICRO,
                "remaining_usd": Decimal(self.ceiling - settled - outstanding) / MICRO,
                "requests": sum(n for _, n in rows), "requests_by_campaign": dict(rows)}


class SpendBudget(BudgetService):
    def __init__(self, *a: Any, spend: SpendLedger, campaign: str, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.spend, self.campaign = spend, campaign

    def reserve(self, input_bytes: int, *, turn_key: str | None, eval_key: str | None) -> Reservation | Denial:
        r = super().reserve(input_bytes, turn_key=turn_key, eval_key=eval_key)
        if isinstance(r, Reservation) and not self.spend.reserve(r.call_id, r.amount, self.campaign):
            super().settle(r, Decimal(0), "REPORTED_USAGE")  # nothing is sent
            return Denial("EXPENDITURE_CEILING")
        return r

    def settle(self, reservation: Reservation, amount: Decimal, basis: str) -> None:
        self.spend.settle(reservation.call_id, amount, basis)
        super().settle(reservation, amount, basis)
