"""Additive revision: workflow engine bookkeeping.

* ``operations.pending_recheck_trigger`` — the trigger (AUTO/MANUAL/ACK/RECOVERY) of the next scheduled
  reconciliation run, so a restart resumes the schedule with the right provenance (R12).
* ``operations.phase2_pending`` — set by T-OBSERVE when phase-2 reads are still due for an ``ACCEPTED`` or
  ``UNCERTAIN`` observation; recovery re-runs phase 2 (read-only) if the process died in between.
* ``authorizations_claim_kind_idx`` — supports the executor's ``FOR UPDATE SKIP LOCKED`` claim query.

Revision ID: 0003_workflow_engine
Revises: 0002_seed_master_data
"""

from __future__ import annotations

from alembic import op

revision = "0003_workflow_engine"
down_revision = "0002_seed_master_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        SET search_path = lbf, public;
        ALTER TABLE operations ADD COLUMN pending_recheck_trigger text
          CHECK (pending_recheck_trigger IN ('AUTO', 'MANUAL', 'ACK', 'RECOVERY'));
        ALTER TABLE operations ADD COLUMN phase2_pending boolean NOT NULL DEFAULT false;
        GRANT UPDATE (pending_recheck_trigger, phase2_pending) ON operations TO lbf_app;
        CREATE INDEX authorizations_claim_kind_idx ON authorizations(state, kind, granted_at);
        CREATE INDEX operations_recheck_idx ON operations(state, next_recheck_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET search_path = lbf, public;
        DROP INDEX IF EXISTS operations_recheck_idx;
        DROP INDEX IF EXISTS authorizations_claim_kind_idx;
        ALTER TABLE operations DROP COLUMN IF EXISTS phase2_pending;
        ALTER TABLE operations DROP COLUMN IF EXISTS pending_recheck_trigger;
        """
    )
