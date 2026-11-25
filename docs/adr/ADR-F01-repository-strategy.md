# ADR-F01 — Separate repository with byte-identical legacy imports

- Status: Accepted
- Context: LEGACYBRIDGE v1 is published. This project must not alter v1, must reuse the
  HALDEN simulator and the v1 HTTP bridge, and must keep evidence reproducible.
- Decision: a separate repository. HALDEN (`legacy-erp/`, 35 files)
  and the v1 bridge (`errors.py`, `models.py`, `normalize.py`, `client.py`) are copied byte-identically from r2
  `0a896d1` with SHA-256 import manifests, re-verified by `make verify-imports` (`tools/verify_imports.py` also
  cross-checks each hash against the git blob at the source revision).
- Alternatives rejected: a directory inside v1 (mixed products, risk of editing frozen files); a `v2`
  branch (default-branch ambiguity, accidental merges).
- Consequences: duplicated HALDEN sources are controlled by the manifest; v1 repositories are read-only.
