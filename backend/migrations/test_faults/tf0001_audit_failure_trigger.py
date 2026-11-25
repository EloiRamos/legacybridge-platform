"""TEST-ONLY branch ``test_faults`` (R18a): a trigger that makes an audit insert fail for configured event
types. Never part of ``head`` of the main branch; applied to the test database only, by
``tests/resilience/test_r18_audit_failure.py``, with ``version_locations`` extended to this directory, and removed
again (``test_faults@base``) when the test ends.

Configuration: rows in ``lbf.test_fault_audit(event_type)``; an ``INSERT`` into ``lbf.audit_events`` whose
``event_type`` is listed raises ``R18A_INJECTED_AUDIT_FAILURE``. The function is ``SECURITY DEFINER`` (owner
``lbf_migrator``) so the application role needs no privilege on the configuration table.
"""

from __future__ import annotations

from alembic import op

revision = "tf0001_audit_failure_trigger"
down_revision = None
branch_labels = ("test_faults",)
depends_on = "0004_inference"


def upgrade() -> None:
    op.execute("CREATE TABLE lbf.test_fault_audit (event_type text PRIMARY KEY)")
    op.execute("""
        CREATE FUNCTION lbf.test_fault_audit_insert() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = lbf, pg_temp AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM lbf.test_fault_audit f WHERE f.event_type = NEW.event_type) THEN
                RAISE EXCEPTION 'R18A_INJECTED_AUDIT_FAILURE for %', NEW.event_type;
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER test_fault_audit_insert BEFORE INSERT ON lbf.audit_events "
               "FOR EACH ROW EXECUTE FUNCTION lbf.test_fault_audit_insert()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS test_fault_audit_insert ON lbf.audit_events")
    op.execute("DROP FUNCTION IF EXISTS lbf.test_fault_audit_insert()")
    op.execute("DROP TABLE IF EXISTS lbf.test_fault_audit")
