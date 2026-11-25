"""DB end-state oracle by plain SQL (psycopg), never through application code."""

from __future__ import annotations

from typing import Any

import psycopg

WRITE_TABLES = ("approvals", "authorizations", "transmissions", "escalations")


def counts(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        return {t: conn.execute(f"SELECT count(*) FROM lbf.{t}").fetchone()[0]  # noqa: S608 - fixed names
                for t in (*WRITE_TABLES, "operations", "draft_versions")}


def new_operations(dsn: str, since_count: int) -> list[dict[str, Any]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT o.id, o.backend_id, o.native_customer_id, o.state, o.origin, v.content "
            "FROM lbf.operations o JOIN lbf.draft_versions v "
            "ON v.operation_id = o.id AND v.version = o.current_version "
            "ORDER BY o.created_at").fetchall()
    return [{"id": str(r[0]), "backend_id": r[1], "customer": r[2], "state": r[3], "origin": r[4], "content": r[5]}
            for r in rows[since_count:]]


def state_of(dsn: str, correlation_value: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT state FROM lbf.operations WHERE correlation_value = %s",
                           (correlation_value,)).fetchone()
    return row[0] if row else None
