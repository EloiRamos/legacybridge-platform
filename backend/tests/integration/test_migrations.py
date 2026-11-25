"""Schema matches the Core metadata; the SQL transition table equals the Python table; seeds present."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from lbf.domain.state_machine import allowed_state_changes
from lbf.persistence import tables as t

pytestmark = pytest.mark.db


def test_metadata_matches_information_schema(engines):
    with engines["migrator"].connect() as conn:
        rows = conn.execute(
            text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'lbf'")
        ).all()
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)
    for table in t.metadata.sorted_tables:
        assert {c.name for c in table.columns} == actual.get(table.name), table.name


def test_sql_transition_table_equals_python_table(engines):
    with engines["app"].connect() as conn:
        rows = {(a, b) for a, b in conn.execute(text("SELECT from_state, to_state FROM allowed_transitions"))}
    assert rows == {(a.value, b.value) for a, b in allowed_state_changes()}


def test_seed_master_data(engines):
    with engines["app"].connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM backends")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM party_accounts")).scalar_one() == 4
        assert conn.execute(text("SELECT count(*) FROM item_mappings WHERE backend_id='skarvik'")).scalar_one() == 9
        caps = conn.execute(text("SELECT capabilities FROM backends WHERE id='skarvik'")).scalar_one()
        assert caps["dedup_window_seconds"] == 259200 and caps["write_mode"] == "ASYNC_FILE_DROP"
