# Outcomes and evidence classes (closed sets)

Source of truth: `backend/src/lbf/domain/evidence.py`. A Pydantic validator enforces the
mapping on every recorded outcome; each evidence class belongs to exactly one outcome.

| Outcome | Evidence classes | Verification level |
|---|---|---|
| `CONFIRMED` | `READ_BACK`, `RECONCILED`, `DEDUP_CONFIRMED`, `MANUAL_VERIFIED` | `VERIFIED_BY_READ` |
| `CONFIRMED` | `ACKNOWLEDGED_ONLY` ("acknowledged, not yet read back") | `ACKNOWLEDGED_ONLY` |
| `REJECTED` | `BACKEND_REJECTION` | n/a (backend no-effect guarantee) |
| `NOT_APPLIED` | `NOT_SENT`, `PRE_PROCESSING_REFUSAL`, `NEVER_DELIVERED`, `WITHDRAWN_BEFORE_PICKUP`, `APPROVAL_EXPIRED` | n/a (positive evidence of non-processing) |
| `UNKNOWN` | `NO_MATCH_AFTER_UNCERTAIN`, `READBACK_MISMATCH`, `RECONCILIATION_FAILED`, `ACK_OVERDUE`, `INTERRUPTED`, `SOURCE_STALE` | `UNRESOLVED` |
| `ESCALATED` (state) | `AMBIGUOUS_MATCH`, `CONTRADICTION`, `RECHECK_LIMIT`, `MANUAL` | `UNRESOLVED` |
| `CLOSED_ATTESTED` (state) | `ATTESTED_BOOKED`, `ATTESTED_NOT_BOOKED` | `ATTESTED` — **never "verified"** |

Rule **R-1**: `ReconciliationResult.absence_proven=True` only together with an evidence class of the
`NOT_APPLIED` row (validator). Rule **R-2**: a match requires account, correlation value (and message
id for SKARVIK) and the line multiset; price, total, date and status are not compared. Rule **R-3**:
an anchored reconcile reads only the anchor order and never confirms a different order.

Distinctions kept: REQUESTED ≠ AUTHORIZED ≠ ATTEMPTED ≠ OBSERVED EFFECT; OBSERVED RESPONSE ≠
VERIFIED BUSINESS OUTCOME. HASH-CHAIN INTEGRITY ≠ EVENT COMPLETENESS ≠ AUTHENTICITY.
