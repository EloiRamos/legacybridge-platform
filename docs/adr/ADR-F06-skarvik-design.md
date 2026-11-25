# ADR-F06 — SKARVIK OES 4 as an independent asynchronous file-exchange simulator

- Status: Accepted
- Context: this project must integrate two genuinely different legacy styles. HALDEN is synchronous HTTP with no
  idempotency. The fictional Norrvik system has no API: only a file share, batch pickup, acknowledgements and
  periodic extracts, with message-id deduplication for a bounded window.
- Decision: SKARVIK is an independent Python 3.12 stdlib + SQLite program (`legacy-systems/skarvik-oes`, package
  `skarvik`) with its own contract (`skarvik-exchange/1.0`), its own tests per invariant S-I1…S-I7 and per fault,
  a control plane present only when `SKARVIK_SIM_CONTROL=true`, and oracle endpoints that the application must never
  call. The contract is frozen (`SKARVIK_MANIFEST.sha256`) **before** the adapter is written.
- Consequences: the simulator is a fictional approximation built by the same implementer (RK-14); mitigated by
  freeze-before-adapter, invariant tests, separate oracles and independent review.
