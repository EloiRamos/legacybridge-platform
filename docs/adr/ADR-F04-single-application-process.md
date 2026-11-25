# ADR-F04 — One application process with a singleton engine lease

- Status: Accepted
- Context: HALDEN accepts one configured login; every new ticket grant revokes the previous one. Two processes with
  HALDEN clients revoke each other's tickets.
- Decision: one process (`uvicorn`, 1 worker) owns the single HALDEN client; background engines (executor, ack
  poller, reconciler, recovery sweeper, validation refresher) are threads. A PostgreSQL session advisory lock
  (`pg_try_advisory_lock(0x4C42460001)`) is the singleton engine lease; a non-leader serves no engines and reports
  `/readyz` = `degraded:not-engine-leader`.
- Correctness does not depend on the singleton: every write-safety invariant is enforced by constraints and row
  locks, and a concurrency test runs two engine instances against one database (C-05).
- Consequences: no horizontal scaling against HALDEN (stated limitation); a crash stops API and engines together,
  covered by restart-recovery tests (R11/R12).
