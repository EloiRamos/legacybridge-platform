# Recovery

## Application crash or restart

The transmission row is committed before any handoff. After a restart the sweeper finds transmissions whose lease
expired without an observation and marks them `INTERRUPTED`; the operation becomes `UNKNOWN`. HALDEN: it stays
`UNKNOWN` until a read finds the order or a human resolves it (absence is never proof). SKARVIK: a leftover staging
file instead of a delivered file proves `NEVER_DELIVERED`. Nothing is resent. Re-check schedules continue from their
stored timestamps. [R11, R12; signature scenario 3]

## Database backup and restore

```bash
make backup
```

writes `deploy/backups/lbf-<UTC>.dump` (custom format) and a `.sha256` (git-ignored, mode 0700 directory).

```bash
make restore FILE=deploy/backups/lbf-<UTC>.dump
```

verifies the dump's hash, stops `app`, **recreates the database volume**, restores, and starts the stack again.

**Mandatory next step — post-restore reconciliation.** A restored database does not restore HALDEN or SKARVIK; orders
may have been booked or acknowledged since the backup. Run, as an integration engineer:

```bash
LBF_RECONCILE_USER=rafael.integration LBF_RECONCILE_PASSWORD_FILE=<file with the password> python3 deploy/post_restore_reconcile.py
```

It requests a read-only re-check of every `UNKNOWN` operation, lists `ESCALATED` ones for a decision and leaves
in-flight states to the engines. Tested in the recorded acceptance run: operations preserved, reconciliation run recorded
([verification](../evidence/VERIFICATION.md)). [OPS-04]

## SKARVIK share full or unwritable

Symptoms: a SKARVIK handoff whose file could not be published is recorded as `NOT_APPLIED/NEVER_DELIVERED` (the file
never reached `inbox/`); backend health may show the SKARVIK snapshot age growing. Free space on the volume
`lbf-skarvik-exchange` or fix permissions (the app runs as uid 10001), then re-approve affected drafts.

## HALDEN ticket thrash

HALDEN allows one service ticket; a second process with the same login revokes the first one's ticket, and requests
fail with 440 (`PRE_PROCESSING_REFUSAL`). Check that only one `app` container runs and that `/readyz` reports
`engine: leader`. Never scale `app` horizontally (ADR-F04).

## Audit verification failure

1. Do not modify the database. Record the verifier output (first broken sequence number, reason).
2. Compare the latest export's `head_hash` with the value retained outside the system.
3. Restore the last verified backup into a separate environment to compare rows; escalate to the compliance reviewer.
A superuser can rewrite the whole chain consistently; that is detectable only against the retained head hashes
(stated limitation).
