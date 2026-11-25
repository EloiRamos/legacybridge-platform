"""SKARVIK OES 4 processing engine: pickup, dedup, validation, commit, acknowledgement, extracts, faults.

Invariants (contract §13.4.6):
  S-I1 a file still in inbox/ has not been processed; once moved by the consumer to withdrawn/ it never will be;
  S-I2 REJECTED ack ⇒ no order committed for that MSGID (except under the armed ``contradict_ack`` fault);
  S-I3 ACCEPTED ack is written only after the order commit;
  S-I4 same MSGID within the window ⇒ no second order; ack DUPLICATE referencing the first order;
  S-I5 same MSGID after the window ⇒ treated as new;
  S-I6 ORDREF is free text and never deduplicated;
  S-I7 extracts are snapshots.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from skarvik import formats
from skarvik.store import Store

STOCKHOLM = ZoneInfo(formats.TZ_NAME)
KEEP_EXTRACTS = 5


@dataclass
class Config:
    exchange_dir: Path
    data_dir: Path
    seed_file: Path
    pickup_interval: float = 10.0
    extract_interval: float = 30.0
    dedup_window_seconds: int = 259_200
    sim_control: bool = False
    control_port: int = 8095

    @classmethod
    def from_env(cls) -> Config:
        here = Path(__file__).resolve().parent
        default_seed = here.parent.parent / "seed" / "seed.json"
        return cls(
            exchange_dir=Path(os.environ.get("SKARVIK_EXCHANGE_DIR", "/exchange")),
            data_dir=Path(os.environ.get("SKARVIK_DATA_DIR", "/data")),
            seed_file=Path(os.environ.get("SKARVIK_SEED_FILE", str(default_seed))),
            pickup_interval=float(os.environ.get("SKARVIK_PICKUP_INTERVAL", "10")),
            extract_interval=float(os.environ.get("SKARVIK_EXTRACT_INTERVAL", "30")),
            dedup_window_seconds=int(os.environ.get("SKARVIK_DEDUP_WINDOW_SECONDS", "259200")),
            sim_control=os.environ.get("SKARVIK_SIM_CONTROL", "false").lower() == "true",
            control_port=int(os.environ.get("SKARVIK_CONTROL_PORT", "8095")),
        )


COUNTED_FAULTS = ("drop_ack", "truncate_on_pickup", "crash_after_commit", "contradict_ack", "malformed_ack")


@dataclass
class Faults:
    ack_delay_seconds: float = 0.0
    ack_delay_count: int = 0
    counted: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTED_FAULTS, 0))
    pause_pickup: bool = False
    stale_extracts: bool = False
    duplicate_window_seconds: int | None = None

    def take(self, name: str) -> bool:
        if self.counted.get(name, 0) > 0:
            self.counted[name] -= 1
            return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ack_delay": {"seconds": self.ack_delay_seconds, "remaining": self.ack_delay_count},
            **{k: {"remaining": v} for k, v in self.counted.items()},
            "pause_pickup": self.pause_pickup,
            "stale_extracts": self.stale_extracts,
            "duplicate_window_seconds": self.duplicate_window_seconds,
        }


class SimulatedCrash(RuntimeError):
    """Raised instead of exiting the process when the crash hook is replaced (tests)."""


def atomic_write(directory: Path, name: str, data: bytes) -> Path:
    tmp = directory / f".tmp-{name}"
    final = directory / name
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


class Simulator:
    def __init__(self, config: Config, *, clock: Callable[[], datetime] | None = None,
                 crash: Callable[[], None] | None = None) -> None:
        self.config = config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.crash = crash or (lambda: os._exit(3))
        self.faults = Faults()
        self.lock = threading.RLock()
        self.pending_acks: list[tuple[float, dict[str, Any]]] = []
        ex = config.exchange_dir
        self.inbox, self.work, self.withdrawn, self.error = ex / "inbox", ex / "work", ex / "withdrawn", ex / "error"
        self.acks, self.archive, self.extracts = ex / "outbox" / "acks", ex / "outbox" / "acks" / "archive", \
            ex / "outbox" / "extracts"
        self.ensure_dirs()
        self.store = Store(config.data_dir / "skarvik.db")
        if not self.store.is_seeded():
            self.store.load_seed(config.seed_file)
        self._last_extract = 0.0

    # ------------------------------------------------------------------ setup / reset

    def ensure_dirs(self) -> None:
        for d in (self.inbox, self.work, self.withdrawn, self.error, self.acks, self.archive, self.extracts):
            d.mkdir(parents=True, exist_ok=True)

    def window_seconds(self) -> int:
        return self.faults.duplicate_window_seconds or self.config.dedup_window_seconds

    def reset(self) -> None:
        with self.lock:
            for d in (self.inbox, self.work, self.withdrawn, self.error, self.archive, self.acks, self.extracts):
                for p in d.iterdir():
                    if p.is_file():
                        p.unlink()
            self.store.load_seed(self.config.seed_file)
            self.faults = Faults()
            self.pending_acks.clear()
            self.generate_extracts(force=True)

    def startup_recovery(self) -> list[str]:
        """Files left in work/ by a crash: if their MSGID was already committed, the acknowledgement was lost
        (``crash_after_commit``): the work file is removed without an ack. Otherwise they are reprocessed."""
        notes = []
        with self.lock:
            for path in sorted(self.work.glob("ORD_*.csv")):
                msgid = path.name[4:-4]
                row = self.store.one("SELECT ack_status FROM messages WHERE msgid=?", (msgid,))
                if row is not None and row["ack_status"] == "ACK_LOST":
                    path.unlink()
                    notes.append(f"ack lost for {msgid}")
                else:
                    data = path.read_bytes()
                    self._process(path, data, hashlib.sha256(data).hexdigest())
                    notes.append(f"reprocessed {msgid}")
        return notes

    # ------------------------------------------------------------------ pickup

    def pickup_once(self) -> list[str]:
        outcomes: list[str] = []
        with self.lock:
            if self.faults.pause_pickup:
                return outcomes
            for path in sorted(self.inbox.glob("ORD_*.csv")):
                target = self.work / path.name
                try:
                    os.rename(path, target)  # atomic claim: whoever renames first wins
                except FileNotFoundError:
                    continue  # withdrawn by the consumer or otherwise gone
                data = target.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                outcomes.append(self._process(target, data, digest))
        return outcomes

    def _record_received(self, name: str, msgid: str | None, digest: str, outcome: str) -> None:
        self.store.query(
            "INSERT INTO received (file_name, msgid, sha256, claimed_utc, outcome) VALUES (?,?,?,?,?)",
            (name, msgid, digest, self.clock().isoformat(), outcome),
        )

    def _process(self, path: Path, data: bytes, digest: str) -> str:
        now = self.clock()
        if self.faults.take("truncate_on_pickup"):
            data = data[: max(1, len(data) // 2)]
        try:
            msg = formats.parse_order(data)
        except (formats.StructureError, UnicodeDecodeError) as exc:
            msgid = getattr(exc, "msgid", None)
            if msgid is None:
                os.replace(path, self.error / path.name)
                self._record_received(path.name, None, digest, "ERROR_NO_MSGID")
                return "ERROR_NO_MSGID"
            self._record_received(path.name, msgid, digest, "REJECTED:E-STRUCT")
            self._remember(msgid, now, None, "REJECTED", "E-STRUCT")
            self._ack(msgid, "REJECTED", "", "E-STRUCT")
            path.unlink()
            return "REJECTED:E-STRUCT"

        seen = self.store.one("SELECT * FROM messages WHERE msgid=?", (msg.msgid,))
        if seen is not None and now.timestamp() - seen["first_seen_epoch"] < self.window_seconds():
            original = seen["orderno"] or ""
            code = seen["ack_code"] if seen["ack_status"] == "REJECTED" else "ACCEPTED"
            self._record_received(path.name, msg.msgid, digest, "DUPLICATE")
            self._ack(msg.msgid, "DUPLICATE", original, code or "")
            path.unlink()
            return "DUPLICATE"

        code = self._validate(msg)
        if code is not None:
            self._record_received(path.name, msg.msgid, digest, f"REJECTED:{code}")
            self._remember(msg.msgid, now, None, "REJECTED", code)
            self._ack(msg.msgid, "REJECTED", "", code)
            path.unlink()
            return f"REJECTED:{code}"

        orderno = self._commit(msg, now)
        self._record_received(path.name, msg.msgid, digest, "ACCEPTED")
        if self.faults.take("crash_after_commit"):
            self._remember(msg.msgid, now, orderno, "ACK_LOST", "")
            self.crash()
            raise SimulatedCrash(msg.msgid)
        if self.faults.take("drop_ack"):
            self._remember(msg.msgid, now, orderno, "ACK_LOST", "")
            path.unlink()
            return "ACCEPTED:ACK_DROPPED"
        if self.faults.take("contradict_ack"):
            self._remember(msg.msgid, now, orderno, "ACCEPTED", "")
            self._ack(msg.msgid, "REJECTED", "", "E-ITEM")
            path.unlink()
            return "ACCEPTED:ACK_CONTRADICTS"
        self._remember(msg.msgid, now, orderno, "ACCEPTED", "")
        self._ack(msg.msgid, "ACCEPTED", orderno, "")
        path.unlink()
        return "ACCEPTED"

    def _remember(self, msgid: str, now: datetime, orderno: str | None, status: str, code: str) -> None:
        self.store.query(
            "INSERT INTO messages (msgid, first_seen_epoch, orderno, ack_status, ack_code) VALUES (?,?,?,?,?) "
            "ON CONFLICT(msgid) DO UPDATE SET first_seen_epoch=excluded.first_seen_epoch, orderno=excluded.orderno, "
            "ack_status=excluded.ack_status, ack_code=excluded.ack_code",
            (msgid, now.timestamp(), orderno, status, code),
        )

    def _validate(self, msg: formats.OrderMessage) -> str | None:
        cust = self.store.one("SELECT * FROM customers WHERE custno=?", (msg.custno,))
        if cust is None:
            return "E-CUST"
        if cust["sparr"] == "J":
            return "E-BLOCK"
        for line in msg.lines:
            item = self.store.one("SELECT * FROM items WHERE itemno=?", (line.itemno,))
            if item is None or item["disc"] == "J":
                return "E-ITEM"
            if item["uom"] != line.uom:
                return "E-UOM"
            if not 1 <= line.qty <= 99_999:
                return "E-QTY"
        return None

    def _commit(self, msg: formats.OrderMessage, now: datetime) -> str:
        store = self.store
        with store.lock:
            c = store.conn
            c.execute("BEGIN IMMEDIATE")
            orderno = store.next_order_no()
            backorder = False
            for line in msg.lines:
                row = c.execute("SELECT qty FROM stock WHERE itemno=? AND wh='GBG'", (line.itemno,)).fetchone()
                available = row["qty"] if row else 0
                if line.qty > available:
                    backorder = True
                c.execute("INSERT INTO stock (itemno, wh, qty) VALUES (?, 'GBG', ?) "
                          "ON CONFLICT(itemno, wh) DO UPDATE SET qty = qty - ?", (line.itemno, -line.qty, line.qty))
            local = now.astimezone(STOCKHOLM)
            c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (orderno, msg.msgid, msg.custno, msg.ordref, msg.custpo, msg.reqdate, msg.note,
                       "BO" if backorder else "OP", local.strftime("%Y%m%d"), now.isoformat()))
            for line in msg.lines:
                c.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)",
                          (orderno, line.lineno, line.itemno, line.qty, line.uom))
            c.execute("UPDATE counters SET value = value + 1 WHERE name='next_order'")
            c.execute("COMMIT")
        return orderno

    # ------------------------------------------------------------------ acknowledgements

    def _ack(self, msgid: str, status: str, orderno: str, code: str) -> None:
        item = {"msgid": msgid, "status": status, "orderno": orderno, "code": code}
        if self.faults.ack_delay_count > 0:
            self.faults.ack_delay_count -= 1
            self.pending_acks.append((time.monotonic() + self.faults.ack_delay_seconds, item))
            return
        self._write_ack(item)

    def _write_ack(self, item: dict[str, Any]) -> None:
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO messages (msgid, first_seen_epoch, ack_seq) VALUES (?, ?, 0) ON CONFLICT(msgid) DO NOTHING",
                (item["msgid"], self.clock().timestamp()))
            self.store.conn.execute("UPDATE messages SET ack_seq = ack_seq + 1 WHERE msgid=?", (item["msgid"],))
            seq = self.store.conn.execute("SELECT ack_seq FROM messages WHERE msgid=?", (item["msgid"],)).fetchone()[0]
        name = f"ACK_{item['msgid']}_{seq}.xml"
        if self.faults.take("malformed_ack"):
            data = f'<ACK msgid="{item["msgid"]}" seq="{seq}" status='.encode()
        else:
            data = formats.render_ack(msgid=item["msgid"], seq=seq, status=item["status"], orderno=item["orderno"],
                                      code=item["code"], local_ts=self.clock().astimezone(STOCKHOLM))
        atomic_write(self.acks, name, data)

    def flush_due_acks(self) -> int:
        with self.lock:
            now = time.monotonic()
            due = [a for a in self.pending_acks if a[0] <= now]
            self.pending_acks = [a for a in self.pending_acks if a[0] > now]
            for _, item in due:
                self._write_ack(item)
            return len(due)

    # ------------------------------------------------------------------ extracts

    def generate_extracts(self, *, force: bool = False) -> bool:
        with self.lock:
            if self.faults.stale_extracts and not force:
                return False
            local = self.clock().astimezone(STOCKHOLM)
            stamp = local.strftime("%Y%m%d%H%M%S")
            s = self.store
            stock = [formats.stock_record(r["itemno"], r["wh"], r["qty"], self._uom(r["itemno"]))
                     for r in s.query("SELECT * FROM stock ORDER BY itemno, wh")]
            orders: list[str] = []
            for o in s.query("SELECT * FROM orders ORDER BY orderno"):
                orders.append(formats.order_header_record(o["orderno"], o["msgid"] or "", o["custno"], o["ordref"],
                                                          o["status"], o["orddate"]))
                for ln in s.query("SELECT * FROM order_lines WHERE orderno=? ORDER BY lineno", (o["orderno"],)):
                    orders.append(formats.order_line_record(o["orderno"], ln["lineno"], ln["itemno"], ln["qty"],
                                                            ln["uom"]))
            custs = [formats.customer_record(r["custno"], r["name"], r["sparr"], r["city"], r["ctry"], r["note"])
                     for r in s.query("SELECT * FROM customers ORDER BY custno")]
            items = [formats.item_record(r["itemno"], r["descr"], r["uom"], r["pack"], r["price_eur"], r["disc"])
                     for r in s.query("SELECT * FROM items ORDER BY itemno")]
            for kind, records in (("STOCK", stock), ("ORDERS", orders), ("CUST", custs), ("ITEMS", items)):
                atomic_write(self.extracts, f"{kind}_{stamp}.dat", formats.render_extract(kind, local, records))
                existing = sorted(self.extracts.glob(f"{kind}_*.dat"))
                for old in existing[:-KEEP_EXTRACTS]:
                    old.unlink()
            self._last_extract = time.monotonic()
            return True

    def _uom(self, itemno: str) -> str:
        row = self.store.one("SELECT uom FROM items WHERE itemno=?", (itemno,))
        return row["uom"] if row else ""

    # ------------------------------------------------------------------ oracle views (control plane only)

    def oracle_orders(self) -> list[dict[str, Any]]:
        out = []
        for o in self.store.query("SELECT * FROM orders ORDER BY orderno"):
            lines = [dict(r) for r in self.store.query(
                "SELECT lineno, itemno, qty, uom FROM order_lines WHERE orderno=? ORDER BY lineno", (o["orderno"],))]
            out.append({**dict(o), "lines": lines})
        return out

    def oracle_received(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.query("SELECT * FROM received ORDER BY id")]

    # ------------------------------------------------------------------ loop

    def tick(self) -> None:
        self.flush_due_acks()
        now = time.monotonic()
        if now - self._last_extract >= self.config.extract_interval:
            self.generate_extracts()
