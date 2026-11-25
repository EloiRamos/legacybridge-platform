# Wire-tap (test profile only)

Recording and fault-injecting HTTP proxy placed between the application and HALDEN ERP 3.1 in the `test` compose
profile. It is an **independent observer**: standard library only, no import from `lbf`.

* Every request is written to the ledger (`/wiretap/ledger.jsonl`, fsync per record) **on receipt, before
  forwarding** (`phase: RECEIVED`), then completed (`phase: COMPLETED`: `forwarded`, `status`, `response_sha256`,
  `client_outcome`). For E8 (`POST /halden/orders`) the form `ref` is recorded as `form_ref`.
* What the ledger proves: a request with that body digest reached the wire-tap, and whether it was forwarded.
  It does **not** prove that HALDEN processed it (`RECEIVED ≠ FORWARDED ≠ BACKEND ACCEPTED`); HALDEN's own state is
  read by the independent oracle client (`oracles/`).
* Modes (`POST /_wiretap/modes {"mode": …, "count": n}`): `blackhole`, `corrupt_response`, `drop_after_forward`.
* `--passthrough` mode is a non-recording relay used only to give test oracles loopback access to the simulators.

Never part of the `default` or `demo` profiles.
