# ruff: noqa: S608, E501  (static DDL/seed literals; no external input)
"""Seed master data (fictional): backends + capabilities, curated customer crosswalk, item mappings.

Revision ID: 0002_seed_master_data
Revises: 0001_initial
"""

from __future__ import annotations

import json
import uuid

from alembic import op

from lbf.ports.capabilities import HALDEN_CAPABILITIES, SKARVIK_CAPABILITIES

revision = "0002_seed_master_data"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("6c62662d-6372-6f73-7377-616c6b000000")  # fixed namespace for deterministic party ids

PARTIES = {
    "Nordkap Marine Services AS": [("halden", "00417"), ("skarvik", "KND-0081")],
    "Vantaa Cold Storage Oy": [("halden", "04450"), ("skarvik", "KND-0114")],
}

# (backend, native_item, canonical_sku, native_uom, factor_num, factor_den)
# HALDEN item numbers are the group SKUs (identity mapping). SKARVIK items per the SKARVIK seed.
ITEM_MAPPINGS = [
    ("halden", "BW-1001", "BW-1001", "EA", 1, 1),
    ("halden", "BW-1002", "BW-1002", "EA", 1, 1),
    ("halden", "BW-1003", "BW-1003", "EA", 1, 1),
    ("halden", "BW-1004", "BW-1004", "EA", 1, 1),
    ("halden", "BW-1005", "BW-1005", "EA", 1, 1),
    ("halden", "BW-1006", "BW-1006", "BOX", 1, 1),
    ("halden", "BW-1007", "BW-1007", "EA", 1, 1),
    ("halden", "BW-1008", "BW-1008", "EA", 1, 1),
    ("halden", "BW-1009", "BW-1009", "EA", 1, 1),
    ("halden", "BW-1010", "BW-1010", "DR", 1, 1),
    ("halden", "BW-1011", "BW-1011", "EA", 1, 1),
    ("skarvik", "NVB-6205-2RS", "BW-1001", "EA", 1, 1),
    ("skarvik", "NVB-30206", "BW-1002", "EA", 1, 1),
    ("skarvik", "NVH-HPU-22", "BW-1003", "EA", 1, 1),
    ("skarvik", "NVV-DN50-SS", "BW-1004", "EA", 1, 1),
    ("skarvik", "NVP-HELM-W", "BW-1005", "KRT", 20, 1),  # carton of 20; SKARVIK item master says PACK 0
    ("skarvik", "NV-GL-100L", "BW-1006", "KRT", 10, 1),  # carton of 10 boxes
    ("skarvik", "NVH-SLANG-12", "BW-1009", "EA", 1, 1),
    ("skarvik", "NVL-VG220-20", "BW-1010", "DR", 1, 1),
    ("skarvik", "NVL-VG220-208", "BW-1010", "FAT", 52, 5),  # 208 L barrel = 10.4 x 20 L drums
]


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for caps in (HALDEN_CAPABILITIES, SKARVIK_CAPABILITIES):
        op.execute(
            f"INSERT INTO lbf.backends (id, capabilities, contract_version) VALUES "
            f"({_q(caps.backend_id)}, {_q(json.dumps(caps.model_dump(mode='json'), sort_keys=True))}::jsonb, "
            f"{_q(caps.contract_version)})"
        )
    for name, accounts in PARTIES.items():
        pid = uuid.uuid5(NAMESPACE, name)
        op.execute(f"INSERT INTO lbf.parties (id, display_name) VALUES ('{pid}', {_q(name)})")
        for backend, native in accounts:
            op.execute(
                "INSERT INTO lbf.party_accounts (party_id, backend_id, native_customer_id) "
                f"VALUES ('{pid}', {_q(backend)}, {_q(native)})"
            )
    for backend, native, sku, uom, num, den in ITEM_MAPPINGS:
        op.execute(
            "INSERT INTO lbf.item_mappings (backend_id, native_item, canonical_sku, native_uom, factor_num, factor_den) "
            f"VALUES ({_q(backend)}, {_q(native)}, {_q(sku)}, {_q(uom)}, {num}, {den})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM lbf.item_mappings; DELETE FROM lbf.party_accounts; DELETE FROM lbf.parties; DELETE FROM lbf.backends;"
    )
