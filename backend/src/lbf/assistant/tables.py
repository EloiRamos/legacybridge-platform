"""Core metadata for the inference tables (migration ``0004_inference``), on the shared ``lbf`` metadata so the
schema-equality test covers them."""

from __future__ import annotations

from sqlalchemy import Column, Integer, Numeric, Table, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from lbf.persistence.tables import metadata

TS = TIMESTAMP(timezone=True)
U = UUID(as_uuid=True)

inference_calls = Table(
    "inference_calls", metadata,
    Column("id", U, primary_key=True), Column("assistant_turn_id", U), Column("correlation_id", U, nullable=False),
    Column("provider", Text, nullable=False), Column("model_configured", Text), Column("model_reported", Text),
    Column("provider_request_id", Text), Column("started_at", TS, nullable=False), Column("latency_ms", Integer),
    Column("status", Text, nullable=False), Column("error_category", Text), Column("retry_no", Integer, nullable=False),
    Column("usage_input_tokens", Integer), Column("usage_cached_input_tokens", Integer),
    Column("usage_output_tokens", Integer), Column("reserved_usd", Numeric(12, 6)),
    Column("estimated_cost_usd", Numeric(12, 6)), Column("cost_basis", Text), Column("request_sha256", Text),
    Column("transport", Text, nullable=False),
)

inference_budget_ledger = Table(
    "inference_budget_ledger", metadata,
    Column("id", U, primary_key=True), Column("scope", Text, nullable=False), Column("scope_key", Text, nullable=False),
    Column("entry", Text, nullable=False), Column("inference_call_id", U, nullable=False),
    Column("amount_usd", Numeric(12, 6), nullable=False), Column("cost_basis", Text),
    Column("created_at", TS, nullable=False),
)
