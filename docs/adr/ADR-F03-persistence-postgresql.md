# ADR-F03 — PostgreSQL 17 with SQLAlchemy Core and Alembic

- Status: Accepted
- Context: guarantee G6 requires append-only audit, approval and version tables **for the application role**;
  approvals and engine claims race; the executor must commit a transmission row before every handoff.
- Decision: PostgreSQL 17 (image pinned by digest), SQLAlchemy 2 Core (explicit `FOR UPDATE`, `SKIP LOCKED`,
  advisory locks), Alembic migrations run by a `migrate` one-shot service as `lbf_migrator`. Roles: `lbf_migrator`
  (owner, DDL), `lbf_app` (DML with column-limited updates, no DELETE except sessions), `lbf_audit_reader`
  (SELECT on audit tables). Triggers forbid UPDATE/DELETE on append-only tables and invalid state changes; the
  state-change trigger is generated from the Python transition table.
- Alternatives rejected: SQLite + WAL (no per-role privileges; single writer); an ORM (hides locking); MySQL
  (weaker advisory-lock and partial-index semantics for this design).
- Consequences: one more stateful container, migrations, backup/restore runbook. A PostgreSQL superuser can still
  rewrite history consistently; detection relies on exported head hashes.
