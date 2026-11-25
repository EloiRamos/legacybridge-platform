# ADR-F12 — Compose profiles, three-command deployment, backup/restore with a mandatory reconciliation pass

- Status: Accepted
- Context: the fictional customer's IT must be able to install, operate and recover the system without the
  implementer; the simulators' fault controls must never be reachable in normal operation.
- Decision:
  - Docker Compose with profiles: `default` (customer shape: `db`, `migrate`, `halden`, `skarvik`, `app`; only
    `127.0.0.1:8080` published; simulation control off), `demo` (`LBF_PROFILE=demo`, `LBF_SIM_CONTROL=true`: demo
    router and fault presets for the sysadmin), `test` (adds the wire-tap, loopback test ports, pause points, scaled
    time; `deploy/compose.test.yaml`).
  - Secrets are files generated on the host (`make secrets`, git-ignored `deploy/secrets/`), mounted as Docker
    secrets; demo accounts are created by `make demo-users` with random passwords written to a 0600 file.
  - Deployment in three commands on a clean clone: `make secrets && make demo-users && make up` (OPS-01; `demo-users`
    starts the database and applies migrations itself).
  - `make backup` (`pg_dump --format=custom` + `.sha256`) and `make restore FILE=…` into a **fresh** volume
    (`deploy/restore.sh`), followed by the mandatory post-restore reconciliation pass
    (`deploy/post_restore_reconcile.py`): restoring the database does not restore HALDEN or SKARVIK state, so every
    `UNKNOWN` operation is re-checked read-only and the rest is listed (OPS-04).
- Consequences: compose pins global volume names (`lbf-pgdata`, …), so two checkouts cannot run side by side; restore
  is destructive for the local database volume by design (the dump is verified first); no TLS (loopback only); no
  external monitoring stack (`/healthz`, `/readyz`, `/metrics` only).
