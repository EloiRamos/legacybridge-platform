"""SKARVIK's own SQLite database (simulator-internal; never read by the integration)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (custno TEXT PRIMARY KEY, name TEXT, sparr TEXT, city TEXT, ctry TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS items (itemno TEXT PRIMARY KEY, descr TEXT, uom TEXT, pack INTEGER, price_eur TEXT, disc TEXT);
CREATE TABLE IF NOT EXISTS stock (itemno TEXT, wh TEXT, qty INTEGER, PRIMARY KEY (itemno, wh));
CREATE TABLE IF NOT EXISTS orders (orderno TEXT PRIMARY KEY, msgid TEXT, custno TEXT, ordref TEXT, custpo TEXT,
  reqdate TEXT, note TEXT, status TEXT, orddate TEXT, created_utc TEXT);
CREATE TABLE IF NOT EXISTS order_lines (orderno TEXT, lineno INTEGER, itemno TEXT, qty INTEGER, uom TEXT,
  PRIMARY KEY (orderno, lineno));
CREATE TABLE IF NOT EXISTS messages (msgid TEXT PRIMARY KEY, first_seen_epoch REAL, orderno TEXT, ack_status TEXT,
  ack_code TEXT, ack_seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS received (id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT, msgid TEXT,
  sha256 TEXT, claimed_utc TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def is_seeded(self) -> bool:
        return self.one("SELECT value FROM counters WHERE name='seeded'") is not None

    def load_seed(self, seed_file: Path) -> None:
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        with self.lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            for table in ("customers", "items", "stock", "orders", "order_lines", "messages", "received", "counters"):
                c.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
            for r in data["customers"]:
                c.execute("INSERT INTO customers VALUES (?,?,?,?,?,?)",
                          (r["custno"], r["name"], r["sparr"], r["city"], r["ctry"], r["note"]))
            for r in data["items"]:
                c.execute("INSERT INTO items VALUES (?,?,?,?,?,?)",
                          (r["itemno"], r["descr"], r["uom"], r["pack"], r["price_eur"], r["disc"]))
            for r in data["stock"]:
                c.execute("INSERT INTO stock VALUES (?,?,?)", (r["itemno"], r["wh"], r["qty"]))
            for o in data["orders"]:
                c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (o["orderno"], o["msgid"], o["custno"], o["ordref"], "", "", "", o["status"], o["orddate"],
                           ""))
                for n, (item, qty, uom) in enumerate(o["lines"], start=1):
                    c.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)", (o["orderno"], n, item, qty, uom))
            last = max(int(o["orderno"].split("-")[1]) for o in data["orders"])
            c.execute("INSERT INTO counters VALUES ('next_order', ?)", (last + 1,))
            c.execute("INSERT INTO counters VALUES ('seeded', 1)")
            c.execute("COMMIT")

    def next_order_no(self) -> str:
        row = self.one("SELECT value FROM counters WHERE name='next_order'")
        assert row is not None
        return f"NV-{int(row['value']):06d}"
