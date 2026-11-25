"""Additive revision: inference telemetry and budget ledger. Append-only.

Revision ID: 0004_inference
Revises: 0003_workflow_engine
"""

from __future__ import annotations

from alembic import op

revision = "0004_inference"
down_revision = "0003_workflow_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        SET search_path = lbf, public;
        CREATE TABLE inference_calls (
          id uuid PRIMARY KEY,
          assistant_turn_id uuid,
          correlation_id uuid NOT NULL,
          provider text NOT NULL,
          model_configured text,
          model_reported text,
          provider_request_id text,
          started_at timestamptz NOT NULL,
          latency_ms integer,
          status text NOT NULL CHECK (status IN ('COMPLETED','TOOL_CALLS','REFUSED','INCOMPLETE','FAILED','BUDGET_DENIED')),
          error_category text,
          retry_no integer NOT NULL DEFAULT 0,
          usage_input_tokens integer,
          usage_cached_input_tokens integer,
          usage_output_tokens integer,
          reserved_usd numeric(12,6),
          estimated_cost_usd numeric(12,6),
          cost_basis text CHECK (cost_basis IN ('REPORTED_USAGE','WORST_CASE_RESERVATION')),
          request_sha256 char(64),
          transport text NOT NULL DEFAULT 'live' CHECK (transport IN ('live','mock'))
        );
        CREATE INDEX inference_calls_turn_idx ON inference_calls(assistant_turn_id);
        CREATE TABLE inference_budget_ledger (
          id uuid PRIMARY KEY,
          scope text NOT NULL CHECK (scope IN ('TURN','DAILY','EVAL')),
          scope_key text NOT NULL,
          entry text NOT NULL CHECK (entry IN ('RESERVE','SETTLE')),
          inference_call_id uuid NOT NULL,
          amount_usd numeric(12,6) NOT NULL CHECK (amount_usd >= 0),
          cost_basis text,
          created_at timestamptz NOT NULL
        );
        CREATE INDEX inference_budget_scope_idx ON inference_budget_ledger(scope, scope_key);
        CREATE TRIGGER inference_calls_append_only BEFORE UPDATE OR DELETE ON inference_calls
          FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
        CREATE TRIGGER inference_budget_ledger_append_only BEFORE UPDATE OR DELETE ON inference_budget_ledger
          FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
        REVOKE ALL ON inference_calls, inference_budget_ledger FROM PUBLIC;
        GRANT SELECT, INSERT ON inference_calls, inference_budget_ledger TO lbf_app;
        """
    )


def downgrade() -> None:
    op.execute("SET search_path = lbf, public; DROP TABLE IF EXISTS inference_budget_ledger; "
               "DROP TABLE IF EXISTS inference_calls;")
