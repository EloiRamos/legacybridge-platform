"""Background engines: executor, ack poller, reconciler, recovery sweeper, validation refresher.

They run as threads in the single application process and only while it holds the singleton engine lease.
Correctness never depends on the singleton: every write-safety invariant is enforced by database constraints
and row locks (C-05).
"""
