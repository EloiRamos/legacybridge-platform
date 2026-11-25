"""SQLAlchemy Core table metadata mirroring migration 0001 (schema ``lbf``).

A test compares these definitions with ``information_schema`` so the metadata cannot drift from the DDL.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

metadata = MetaData(schema="lbf")
TS = TIMESTAMP(timezone=True)
U = UUID(as_uuid=True)

users = Table(
    "users",
    metadata,
    Column("id", U, primary_key=True),
    Column("username", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("is_active", Boolean, nullable=False),
    Column("failed_logins", Integer, nullable=False),
    Column("locked_until", TS),
    Column("password_changed_at", TS, nullable=False),
    Column("created_at", TS, nullable=False),
)

user_roles = Table(
    "user_roles",
    metadata,
    Column("user_id", U, primary_key=True),
    Column("role_code", Text, primary_key=True),
    Column("granted_by", U),
    Column("granted_at", TS, nullable=False),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token_sha256", Text, primary_key=True),
    Column("user_id", U, nullable=False),
    Column("csrf_sha256", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("last_seen_at", TS, nullable=False),
    Column("authenticated_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS),
)

backends = Table(
    "backends",
    metadata,
    Column("id", Text, primary_key=True),
    Column("capabilities", JSONB, nullable=False),
    Column("contract_version", Text, nullable=False),
)

parties = Table("parties", metadata, Column("id", U, primary_key=True), Column("display_name", Text, nullable=False))

party_accounts = Table(
    "party_accounts",
    metadata,
    Column("party_id", U, nullable=False),
    Column("backend_id", Text, nullable=False),
    Column("native_customer_id", Text, nullable=False),
)

item_mappings = Table(
    "item_mappings",
    metadata,
    Column("backend_id", Text, primary_key=True),
    Column("native_item", Text, primary_key=True),
    Column("canonical_sku", Text, nullable=False),
    Column("native_uom", Text, nullable=False),
    Column("factor_num", Integer, nullable=False),
    Column("factor_den", Integer, nullable=False),
)

allowed_transitions = Table(
    "allowed_transitions",
    metadata,
    Column("from_state", Text, primary_key=True),
    Column("to_state", Text, primary_key=True),
)

operations = Table(
    "operations",
    metadata,
    Column("id", U, primary_key=True),
    Column("correlation_value", Text, nullable=False),
    Column("backend_id", Text, nullable=False),
    Column("native_customer_id", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("reconciling_from", Text),
    Column("current_version", Integer, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("created_by", U, nullable=False),
    Column("origin", Text, nullable=False),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
    Column("row_version", BigInteger, nullable=False),
    Column("escalation_id", U),
    Column("last_outcome", Text),
    Column("last_evidence", Text),
    Column("anchor_order_ref", Text),
    Column("unknown_since", TS),
    Column("auto_rechecks", Integer, nullable=False),
    Column("next_recheck_at", TS),
    Column("last_manual_recheck_at", TS),
    Column("awaiting_ack_since", TS),
    Column("pending_recheck_trigger", Text),  # 0003
    Column("phase2_pending", Boolean, nullable=False),  # 0003
)

draft_versions = Table(
    "draft_versions",
    metadata,
    Column("operation_id", U, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("content", JSONB, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("created_by", U, nullable=False),
    Column("origin", Text, nullable=False),
    Column("created_at", TS, nullable=False),
)

validation_snapshots = Table(
    "validation_snapshots",
    metadata,
    Column("id", U, primary_key=True),
    Column("operation_id", U, nullable=False),
    Column("version", Integer, nullable=False),
    Column("validated_at", TS, nullable=False),
    Column("checks", JSONB, nullable=False),
    Column("availability", JSONB, nullable=False),
    Column("lines", JSONB, nullable=False),
    Column("estimated_total", Numeric(12, 2)),
    Column("blocking", Boolean, nullable=False),
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", U, primary_key=True),
    Column("operation_id", U, nullable=False),
    Column("version", Integer, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("decision", Text, nullable=False),
    Column("decided_by", U, nullable=False),
    Column("decided_at", TS, nullable=False),
    Column("policy_version", Text, nullable=False),
    Column("checks", JSONB, nullable=False),
    Column("override_reason", Text),
    Column("reason", Text),
)

authorizations = Table(
    "authorizations",
    metadata,
    Column("id", U, primary_key=True),
    Column("operation_id", U, nullable=False),
    Column("kind", Text, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("approval_id", U),
    Column("granted_by", U, nullable=False),
    Column("granted_at", TS, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("state", Text, nullable=False),
    Column("claimed_by", Text),
    Column("lease_until", TS),
    Column("consumed_at", TS),
)

transmissions = Table(
    "transmissions",
    metadata,
    Column("id", U, primary_key=True),
    Column("authorization_id", U, nullable=False),
    Column("operation_id", U, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("transmission_no", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("message_id", Text),
    Column("payload", LargeBinary, nullable=False),
    Column("payload_sha256", Text, nullable=False),
    Column("transport_descriptor", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("engine_id", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("lease_until", TS, nullable=False),
    Column("observed_at", TS),
    Column("observation", JSONB),
    Column("response_sha256", Text),
    Column("response_excerpt", Text),
)

acknowledgements = Table(
    "acknowledgements",
    metadata,
    Column("id", U, primary_key=True),
    Column("message_id", Text),
    Column("seq", Integer),
    Column("status", Text, nullable=False),
    Column("order_ref", Text),
    Column("code", Text),
    Column("ts_utc", TS),
    Column("file_sha256", Text, nullable=False),
    Column("file_name", Text, nullable=False),
    Column("transmission_id", U),
    Column("operation_id", U),
    Column("recorded_at", TS, nullable=False),
)

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    Column("id", U, primary_key=True),
    Column("operation_id", U, nullable=False),
    Column("trigger", Text, nullable=False),
    Column("requested_by", U),
    Column("engine_id", Text),
    Column("started_at", TS, nullable=False),
    Column("finished_at", TS),
    Column("verdict", Text),
    Column("result", JSONB),
    Column("source_reads", JSONB),
    Column("lease_until", TS, nullable=False),
)

escalations = Table(
    "escalations",
    metadata,
    Column("id", U, primary_key=True),
    Column("operation_id", U, nullable=False),
    Column("reason", Text, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("opened_at", TS, nullable=False),
    Column("opened_by", U),
    Column("resolved_at", TS),
    Column("resolved_by", U),
    Column("resolution", Text),
    Column("resolution_evidence", JSONB),
    Column("external_reference", Text),
    Column("note", Text),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("seq", BigInteger, primary_key=True),
    Column("event_id", U, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("occurred_at", TS, nullable=False),
    Column("recorded_at", TS, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("actor_type", Text, nullable=False),
    Column("actor_id", Text),
    Column("actor_username", Text),
    Column("on_behalf_of", Text),
    Column("operation_id", U),
    Column("correlation", JSONB, nullable=False),
    Column("state_change", JSONB),
    Column("payload", JSONB, nullable=False),
    Column("prev_hash", Text, nullable=False),
    Column("event_hash", Text, nullable=False),
)

audit_exports = Table(
    "audit_exports",
    metadata,
    Column("id", U, primary_key=True),
    Column("requested_by", U, nullable=False),
    Column("from_seq", BigInteger, nullable=False),
    Column("to_seq", BigInteger, nullable=False),
    Column("head_hash", Text, nullable=False),
    Column("file_sha256", Text, nullable=False),
    Column("created_at", TS, nullable=False),
)

assistant_turns = Table(
    "assistant_turns",
    metadata,
    Column("id", U, primary_key=True),
    Column("user_id", U, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text),
    Column("prompt_version", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("latency_ms", Integer),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("tool_calls", JSONB, nullable=False),
    Column("refused_calls", JSONB, nullable=False),
    Column("final_answer", JSONB),
    Column("fallback_used", Boolean, nullable=False),
    Column("fallback_reason", Text),
)

engine_heartbeats = Table(
    "engine_heartbeats",
    metadata,
    Column("engine_id", Text, primary_key=True),
    Column("started_at", TS, nullable=False),
    Column("last_beat", TS, nullable=False),
)

annotations = Table(
    "annotations",
    metadata,
    Column("id", U, primary_key=True),
    Column("canonical_sku", Text),
    Column("backend_id", Text),
    Column("native_item", Text),
    Column("author", U, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", TS, nullable=False),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("user_id", U, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("operation_id", U, nullable=False),
    Column("created_at", TS, nullable=False),
)
