# ruff: noqa: S608, E501  (static DDL/seed literals; no external input)
"""Initial schema: tables, least-privilege grants, append-only and state-machine triggers.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from alembic import op

from lbf.domain.enums import OperationState, Role
from lbf.domain.state_machine import allowed_state_changes

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

STATES = ", ".join(f"'{s.value}'" for s in OperationState)
ROLES = ", ".join(f"'{r.value}'" for r in Role)

APPEND_ONLY = (
    "draft_versions",
    "validation_snapshots",
    "approvals",
    "acknowledgements",
    "audit_events",
    "audit_exports",
    "assistant_turns",
    "annotations",
)

DDL = f"""
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA lbf;
SET search_path = lbf, public;

CREATE TABLE users (
  id uuid PRIMARY KEY,
  username citext NOT NULL UNIQUE,
  display_name text NOT NULL,
  password_hash text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  failed_logins integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  password_changed_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_roles (
  user_id uuid NOT NULL REFERENCES users(id),
  role_code text NOT NULL CHECK (role_code IN ({ROLES})),
  granted_by uuid REFERENCES users(id),
  granted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, role_code),
  CONSTRAINT user_roles_no_self_grant CHECK (granted_by IS NULL OR granted_by <> user_id)
);

CREATE TABLE sessions (
  token_sha256 char(64) PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  csrf_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  authenticated_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE INDEX sessions_user_idx ON sessions(user_id);

CREATE TABLE backends (
  id text PRIMARY KEY CHECK (id IN ('halden', 'skarvik')),
  capabilities jsonb NOT NULL,
  contract_version text NOT NULL
);

CREATE TABLE parties (
  id uuid PRIMARY KEY,
  display_name text NOT NULL
);

CREATE TABLE party_accounts (
  party_id uuid NOT NULL REFERENCES parties(id),
  backend_id text NOT NULL REFERENCES backends(id),
  native_customer_id text NOT NULL,
  UNIQUE (backend_id, native_customer_id)
);

CREATE TABLE item_mappings (
  backend_id text NOT NULL REFERENCES backends(id),
  native_item text NOT NULL,
  canonical_sku text NOT NULL,
  native_uom text NOT NULL,
  factor_num integer NOT NULL CHECK (factor_num > 0),
  factor_den integer NOT NULL CHECK (factor_den > 0),
  PRIMARY KEY (backend_id, native_item)
);

CREATE TABLE allowed_transitions (
  from_state text NOT NULL,
  to_state text NOT NULL,
  PRIMARY KEY (from_state, to_state)
);

CREATE TABLE operations (
  id uuid PRIMARY KEY,
  correlation_value char(12) NOT NULL UNIQUE,
  backend_id text NOT NULL REFERENCES backends(id),
  native_customer_id text NOT NULL,
  state text NOT NULL CHECK (state IN ({STATES})),
  reconciling_from text CHECK (reconciling_from IN ('UNKNOWN', 'ESCALATED')),
  current_version integer NOT NULL CHECK (current_version >= 1),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  created_by uuid NOT NULL REFERENCES users(id),
  origin text NOT NULL CHECK (origin IN ('UI', 'ASSISTANT')),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  row_version bigint NOT NULL DEFAULT 0,
  escalation_id uuid,
  last_outcome text,
  last_evidence text,
  anchor_order_ref text,
  unknown_since timestamptz,
  auto_rechecks integer NOT NULL DEFAULT 0,
  next_recheck_at timestamptz,
  last_manual_recheck_at timestamptz,
  awaiting_ack_since timestamptz
);
CREATE INDEX operations_state_idx ON operations(state);
CREATE INDEX operations_account_idx ON operations(backend_id, native_customer_id);

CREATE TABLE draft_versions (
  operation_id uuid NOT NULL REFERENCES operations(id),
  version integer NOT NULL CHECK (version >= 1),
  content jsonb NOT NULL,
  content_hash char(64) NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id),
  origin text NOT NULL CHECK (origin IN ('UI', 'ASSISTANT')),
  created_at timestamptz NOT NULL,
  PRIMARY KEY (operation_id, version)
);

CREATE TABLE validation_snapshots (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL,
  version integer NOT NULL,
  validated_at timestamptz NOT NULL,
  checks jsonb NOT NULL,
  availability jsonb NOT NULL,
  lines jsonb NOT NULL,
  estimated_total numeric(12, 2),
  blocking boolean NOT NULL,
  FOREIGN KEY (operation_id, version) REFERENCES draft_versions(operation_id, version)
);
CREATE INDEX validation_snapshots_op_idx ON validation_snapshots(operation_id, version, validated_at);

CREATE TABLE approvals (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL,
  version integer NOT NULL,
  content_hash char(64) NOT NULL,
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  decision text NOT NULL CHECK (decision IN ('APPROVED', 'DECLINED')),
  decided_by uuid NOT NULL REFERENCES users(id),
  decided_at timestamptz NOT NULL,
  policy_version text NOT NULL,
  checks jsonb NOT NULL,
  override_reason text,
  reason text,
  FOREIGN KEY (operation_id, version) REFERENCES draft_versions(operation_id, version)
);
-- One approval per attempt (C-02). Declines are recorded in the same append-only table and do not
-- consume the attempt, so the uniqueness is partial on APPROVED decisions.
CREATE UNIQUE INDEX approvals_one_per_attempt ON approvals(operation_id, attempt_no) WHERE decision = 'APPROVED';

CREATE TABLE authorizations (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL REFERENCES operations(id),
  kind text NOT NULL CHECK (kind IN ('SUBMIT', 'REDELIVER', 'WITHDRAW')),
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  approval_id uuid REFERENCES approvals(id),
  granted_by uuid NOT NULL REFERENCES users(id),
  granted_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  state text NOT NULL CHECK (state IN ('PENDING', 'CLAIMED', 'CONSUMED', 'EXPIRED')),
  claimed_by text,
  lease_until timestamptz,
  consumed_at timestamptz,
  CHECK (kind <> 'SUBMIT' OR approval_id IS NOT NULL)
);
CREATE UNIQUE INDEX authorizations_one_open_per_operation ON authorizations(operation_id)
  WHERE state IN ('PENDING', 'CLAIMED');
CREATE INDEX authorizations_claim_idx ON authorizations(state, granted_at);

CREATE TABLE transmissions (
  id uuid PRIMARY KEY,
  authorization_id uuid NOT NULL UNIQUE REFERENCES authorizations(id),
  operation_id uuid NOT NULL REFERENCES operations(id),
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  transmission_no integer NOT NULL CHECK (transmission_no >= 1),
  kind text NOT NULL CHECK (kind IN ('SUBMIT', 'REDELIVER')),
  message_id text,
  payload bytea NOT NULL CHECK (octet_length(payload) <= 65536),
  payload_sha256 char(64) NOT NULL,
  transport_descriptor text NOT NULL,
  state text NOT NULL CHECK (state IN ('SENDING', 'OBSERVED', 'INTERRUPTED')),
  engine_id text NOT NULL,
  started_at timestamptz NOT NULL,
  lease_until timestamptz NOT NULL,
  observed_at timestamptz,
  observation jsonb,
  response_sha256 char(64),
  response_excerpt text,
  UNIQUE (operation_id, attempt_no, transmission_no)
);
-- at most one redelivery per attempt, enforced by the database (defence in depth).
CREATE UNIQUE INDEX transmissions_one_redelivery_per_attempt ON transmissions(operation_id, attempt_no)
  WHERE kind = 'REDELIVER';
CREATE INDEX transmissions_state_idx ON transmissions(state, lease_until);

CREATE TABLE acknowledgements (
  id uuid PRIMARY KEY,
  message_id text,
  seq integer,
  status text NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED', 'DUPLICATE', 'MALFORMED')),
  order_ref text,
  code text,
  ts_utc timestamptz,
  file_sha256 char(64) NOT NULL UNIQUE,
  file_name text NOT NULL,
  transmission_id uuid REFERENCES transmissions(id),
  operation_id uuid REFERENCES operations(id),
  recorded_at timestamptz NOT NULL
);
CREATE INDEX acknowledgements_msg_idx ON acknowledgements(message_id);

CREATE TABLE reconciliation_runs (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL REFERENCES operations(id),
  trigger text NOT NULL CHECK (trigger IN ('AUTO', 'MANUAL', 'ACK', 'RECOVERY')),
  requested_by uuid REFERENCES users(id),
  engine_id text,
  started_at timestamptz NOT NULL,
  finished_at timestamptz,
  verdict text,
  result jsonb,
  source_reads jsonb,
  lease_until timestamptz NOT NULL
);
CREATE UNIQUE INDEX reconciliation_one_open_per_operation ON reconciliation_runs(operation_id)
  WHERE finished_at IS NULL;

CREATE TABLE escalations (
  id uuid PRIMARY KEY,
  operation_id uuid NOT NULL REFERENCES operations(id),
  reason text NOT NULL,
  attempt_no integer NOT NULL,
  opened_at timestamptz NOT NULL,
  opened_by uuid REFERENCES users(id),
  resolved_at timestamptz,
  resolved_by uuid REFERENCES users(id),
  resolution text CHECK (resolution IN ('VERIFIED', 'ATTESTED_BOOKED', 'ATTESTED_NOT_BOOKED')),
  resolution_evidence jsonb,
  external_reference text,
  note text
);

CREATE TABLE audit_events (
  seq bigint PRIMARY KEY,
  event_id uuid NOT NULL UNIQUE,
  schema_version text NOT NULL,
  occurred_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL,
  event_type text NOT NULL,
  actor_type text NOT NULL CHECK (actor_type IN ('USER', 'SYSTEM', 'ASSISTANT')),
  actor_id text,
  actor_username text,
  on_behalf_of text,
  operation_id uuid,
  correlation jsonb NOT NULL,
  state_change jsonb,
  payload jsonb NOT NULL,
  prev_hash char(64) NOT NULL,
  event_hash char(64) NOT NULL UNIQUE
);
CREATE INDEX audit_events_operation_idx ON audit_events(operation_id, seq);

CREATE TABLE audit_exports (
  id uuid PRIMARY KEY,
  requested_by uuid NOT NULL REFERENCES users(id),
  from_seq bigint NOT NULL,
  to_seq bigint NOT NULL,
  head_hash char(64) NOT NULL,
  file_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL
);

CREATE TABLE assistant_turns (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  provider text NOT NULL,
  model text,
  prompt_version text NOT NULL,
  started_at timestamptz NOT NULL,
  latency_ms integer,
  input_tokens integer,
  output_tokens integer,
  tool_calls jsonb NOT NULL,
  refused_calls jsonb NOT NULL,
  final_answer jsonb,
  fallback_used boolean NOT NULL,
  fallback_reason text
);

CREATE TABLE engine_heartbeats (
  engine_id text PRIMARY KEY,
  started_at timestamptz NOT NULL,
  last_beat timestamptz NOT NULL
);

CREATE TABLE annotations (
  id uuid PRIMARY KEY,
  canonical_sku text,
  backend_id text REFERENCES backends(id),
  native_item text,
  author uuid NOT NULL REFERENCES users(id),
  body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 500),
  created_at timestamptz NOT NULL
);

CREATE TABLE idempotency_keys (
  user_id uuid NOT NULL REFERENCES users(id),
  key text NOT NULL CHECK (char_length(key) BETWEEN 1 AND 100),
  operation_id uuid NOT NULL REFERENCES operations(id),
  created_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, key)
);

-- ---------------------------------------------------------------- triggers

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE FUNCTION operations_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'DRAFT' THEN
      RAISE EXCEPTION 'operations: a new operation must start in DRAFT (T01), got %', NEW.state
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.id <> OLD.id OR NEW.correlation_value <> OLD.correlation_value OR NEW.backend_id <> OLD.backend_id
     OR NEW.native_customer_id <> OLD.native_customer_id OR NEW.created_by <> OLD.created_by
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'operations: identity columns are immutable' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NOT EXISTS (SELECT 1 FROM lbf.allowed_transitions t WHERE t.from_state = OLD.state AND t.to_state = NEW.state) THEN
      RAISE EXCEPTION 'operations: transition % -> % is not permitted', OLD.state, NEW.state
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION authorizations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id <> OLD.id OR NEW.operation_id <> OLD.operation_id OR NEW.kind <> OLD.kind
     OR NEW.attempt_no <> OLD.attempt_no OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
     OR NEW.granted_by <> OLD.granted_by OR NEW.granted_at <> OLD.granted_at OR NEW.expires_at <> OLD.expires_at THEN
    RAISE EXCEPTION 'authorizations: immutable columns changed' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
       (OLD.state = 'PENDING' AND NEW.state IN ('CLAIMED', 'EXPIRED'))
    OR (OLD.state = 'CLAIMED' AND NEW.state IN ('PENDING', 'CONSUMED', 'EXPIRED'))) THEN
    RAISE EXCEPTION 'authorizations: state % -> % is not permitted', OLD.state, NEW.state
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.state = 'CLAIMED' AND NEW.state IN ('PENDING', 'EXPIRED')
     AND EXISTS (SELECT 1 FROM lbf.transmissions x WHERE x.authorization_id = OLD.id) THEN
    RAISE EXCEPTION 'authorizations: a claim with a transmission cannot be released' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION transmissions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.payload <> OLD.payload OR NEW.payload_sha256 <> OLD.payload_sha256
     OR NEW.authorization_id <> OLD.authorization_id OR NEW.operation_id <> OLD.operation_id
     OR NEW.attempt_no <> OLD.attempt_no OR NEW.transmission_no <> OLD.transmission_no OR NEW.kind <> OLD.kind
     OR NEW.message_id IS DISTINCT FROM OLD.message_id OR NEW.started_at <> OLD.started_at THEN
    RAISE EXCEPTION 'transmissions: immutable columns changed' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT (OLD.state = 'SENDING' AND NEW.state IN ('OBSERVED', 'INTERRUPTED')) THEN
    RAISE EXCEPTION 'transmissions: state % -> % is not permitted', OLD.state, NEW.state
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION finish_once_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'reconciliation_runs' THEN
    IF OLD.finished_at IS NOT NULL THEN
      RAISE EXCEPTION 'reconciliation_runs: run already finished' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.operation_id <> OLD.operation_id OR NEW.trigger <> OLD.trigger
       OR NEW.started_at <> OLD.started_at THEN
      RAISE EXCEPTION 'reconciliation_runs: immutable columns changed' USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF OLD.resolved_at IS NOT NULL THEN
      RAISE EXCEPTION 'escalations: already resolved' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.operation_id <> OLD.operation_id OR NEW.reason <> OLD.reason
       OR NEW.opened_at <> OLD.opened_at OR NEW.attempt_no <> OLD.attempt_no THEN
      RAISE EXCEPTION 'escalations: immutable columns changed' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION last_sysadmin_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.role_code = 'sysadmin' AND NOT EXISTS (
      SELECT 1 FROM lbf.user_roles r JOIN lbf.users u ON u.id = r.user_id
      WHERE r.role_code = 'sysadmin' AND r.user_id <> OLD.user_id AND u.is_active) THEN
    RAISE EXCEPTION 'user_roles: the last sysadmin cannot be removed (SoD-3)' USING ERRCODE = 'check_violation';
  END IF;
  RETURN OLD;
END $$;

CREATE TRIGGER operations_state_guard BEFORE INSERT OR UPDATE ON operations
  FOR EACH ROW EXECUTE FUNCTION operations_state_guard();
CREATE TRIGGER authorizations_guard BEFORE UPDATE ON authorizations
  FOR EACH ROW EXECUTE FUNCTION authorizations_guard();
CREATE TRIGGER authorizations_no_delete BEFORE DELETE ON authorizations
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER transmissions_guard BEFORE UPDATE ON transmissions
  FOR EACH ROW EXECUTE FUNCTION transmissions_guard();
CREATE TRIGGER transmissions_no_delete BEFORE DELETE ON transmissions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER reconciliation_runs_guard BEFORE UPDATE ON reconciliation_runs
  FOR EACH ROW EXECUTE FUNCTION finish_once_guard();
CREATE TRIGGER escalations_guard BEFORE UPDATE ON escalations
  FOR EACH ROW EXECUTE FUNCTION finish_once_guard();
CREATE TRIGGER user_roles_last_sysadmin BEFORE DELETE ON user_roles
  FOR EACH ROW EXECUTE FUNCTION last_sysadmin_guard();
"""

GRANTS = """
SET search_path = lbf, public;
REVOKE ALL ON ALL TABLES IN SCHEMA lbf FROM PUBLIC;
GRANT USAGE ON SCHEMA lbf TO lbf_app, lbf_audit_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA lbf TO lbf_app;
-- mutable tables (column-limited where required)
GRANT INSERT ON users, user_roles, sessions, operations, authorizations, transmissions,
  reconciliation_runs, escalations, engine_heartbeats, idempotency_keys TO lbf_app;
GRANT UPDATE (display_name, password_hash, is_active, failed_logins, locked_until, password_changed_at) ON users TO lbf_app;
GRANT UPDATE, DELETE ON sessions TO lbf_app;
GRANT DELETE ON user_roles TO lbf_app;
GRANT UPDATE (state, reconciling_from, current_version, attempt_count, updated_at, row_version, escalation_id,
  last_outcome, last_evidence, anchor_order_ref, unknown_since, auto_rechecks, next_recheck_at,
  last_manual_recheck_at, awaiting_ack_since) ON operations TO lbf_app;
GRANT UPDATE (state, claimed_by, lease_until, consumed_at) ON authorizations TO lbf_app;
GRANT UPDATE (state, observed_at, observation, response_sha256, response_excerpt) ON transmissions TO lbf_app;
GRANT UPDATE (finished_at, verdict, result, source_reads, lease_until, engine_id) ON reconciliation_runs TO lbf_app;
GRANT UPDATE (resolved_at, resolved_by, resolution, resolution_evidence, external_reference, note) ON escalations TO lbf_app;
GRANT UPDATE (last_beat, started_at) ON engine_heartbeats TO lbf_app;
-- append-only tables: SELECT, INSERT only
GRANT INSERT ON draft_versions, validation_snapshots, approvals, acknowledgements, audit_events, audit_exports,
  assistant_turns, annotations TO lbf_app;
GRANT SELECT ON audit_events, audit_exports TO lbf_audit_reader;
"""


def upgrade() -> None:
    op.execute(DDL)
    rows = ",\n".join(f"('{a.value}', '{b.value}')" for a, b in sorted(allowed_state_changes()))
    op.execute(f"INSERT INTO lbf.allowed_transitions (from_state, to_state) VALUES\n{rows};")
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON lbf.{table} "
            "FOR EACH ROW EXECUTE FUNCTION lbf.forbid_mutation();"
        )
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(
        """
        SET search_path = lbf, public;
        DROP TABLE IF EXISTS idempotency_keys, annotations, engine_heartbeats, assistant_turns, audit_exports,
          audit_events, escalations, reconciliation_runs, acknowledgements, transmissions, authorizations, approvals,
          validation_snapshots, draft_versions, operations, allowed_transitions, item_mappings, party_accounts,
          parties, backends, sessions, user_roles, users CASCADE;
        DROP FUNCTION IF EXISTS forbid_mutation(), operations_state_guard(), authorizations_guard(),
          transmissions_guard(), finish_once_guard(), last_sysadmin_guard();
        """
    )
