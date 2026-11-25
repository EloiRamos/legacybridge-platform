# lbf_oracles — independent oracles

Ground truth **outside the application** (TR-17). Standard library only; never imports `lbf`
(boundary test B-3). Each module answers one question from a source the application does not control:

| Module | Source | Question |
|---|---|---|
| `halden_client` | HALDEN ERP E2/E6/E7 via its own HTTP client (not the v1 bridge) | Which HALDEN orders carry a reference? |
| `skarvik_client` | SKARVIK control-plane oracle endpoints `/_sim/oracle/{orders,received}` | What did SKARVIK claim and commit? |
| `wiretap_ledger` | Wire-tap ledger (recorded before forwarding) | How many E8 requests per correlation value reached the wire? |
| `canonical_json` | Independent re-implementation | Does the application hash what it claims? |
| `audit_verify` | Exported audit JSONL + manifest | Is the chain contiguous, linked and unmodified? |

`audit_verify.py` at this directory's root is the standalone command-line entry point.
