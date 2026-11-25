"""Expected facts computed directly from the frozen seed files (HALDEN ``seed.sql``, SKARVIK ``seed.json``)."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HALDEN_SEED = REPO / "legacy-systems/halden-erp/src/main/resources/db/seed.sql"
SKARVIK_SEED = REPO / "legacy-systems/skarvik-oes/seed/seed.json"
INSERT = re.compile(r"^INSERT INTO (\w+) VALUES \((.*)\);$")
TOKEN = re.compile(r"'((?:[^']|'')*)'|(NULL)|(-?\d+)")


@dataclass(frozen=True)
class Account:
    backend: str
    native_id: str
    name: str
    hold: str  # HOLD | CLEAR | UNKNOWN


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


@cache
def halden_tables() -> dict[str, list[list[object]]]:
    tables: dict[str, list[list[object]]] = {}
    for line in HALDEN_SEED.read_text(encoding="utf-8").splitlines():
        m = INSERT.match(line.strip())
        if m:
            vals = [None if n else (int(d) if d else s.replace("''", "'")) for s, n, d in TOKEN.findall(m.group(2))]
            tables.setdefault(m.group(1), []).append(vals)
    return tables


def _hold(flag: object) -> str:
    f = str(flag or "").strip().casefold()
    if f in ("j", "y", "1", "true", "ja", "yes"):
        return "HOLD"
    if f in ("n", "0", "false", "nein", "no"):
        return "CLEAR"
    return "UNKNOWN"


@cache
def accounts() -> tuple[Account, ...]:
    out = [Account("halden", str(c[0]), str(c[1] or ""), _hold(c[5])) for c in halden_tables()["CUSTMAST"]]
    seed = json.loads(SKARVIK_SEED.read_text(encoding="utf-8"))
    out += [Account("skarvik", c["custno"], c["name"], "HOLD" if c["sparr"] == "J" else "CLEAR")
            for c in seed["customers"]]
    return tuple(out)


def accounts_matching(query: str) -> list[Account]:
    q = fold(query)
    return [a for a in accounts() if q in fold(a.name) or q == fold(a.native_id)]


def halden_stock(sku: str) -> int | None:
    """Free HALDEN stock (on hand − allocated) over all warehouses, or None when any row is unreadable."""
    total = 0
    rows = [r for r in halden_tables()["STOCKBAL"] if str(r[0]).strip().upper() == sku.upper()]
    if not rows:
        return None
    for r in rows:
        try:
            on_hand, alloc = int(str(r[2]).strip()), int(str(r[3]).strip())
        except ValueError:
            return None
        if on_hand < 0 or alloc < 0:
            return None
        total += on_hand - alloc
    return total


def skarvik_stock(itemno: str) -> int | None:
    seed = json.loads(SKARVIK_SEED.read_text(encoding="utf-8"))
    rows = [s for s in seed["stock"] if s["itemno"] == itemno]
    return sum(int(r["qty"]) for r in rows) if rows else None


@cache
def inventory_expectations() -> dict[str, list[dict[str, object]]]:
    """BR-02 hand-derived expectations."""
    import yaml

    data = yaml.safe_load((REPO / "acceptance/fixtures/inventory_expectations.yaml").read_text(encoding="utf-8"))
    return {"halden": data["halden"], "skarvik": data["skarvik"]}


def expected_availability(backend: str, canonical_sku: str) -> list[dict[str, object]]:
    return [r for r in inventory_expectations()[backend] if r.get("canonical_sku") == canonical_sku]
