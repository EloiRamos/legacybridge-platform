# Troubleshooting

Symptom → likely cause → action. The scenario column names the resilience scenario (`resilience/expected/R<nn>.yaml`)
that reproduces it; its recorded run is in `R<nn>.json` in the output directory of `make resilience`.

| Symptom | Likely cause | Action | Scenario |
|---|---|---|---|
| HALDEN order `NOT_APPLIED` with evidence `NOT_SENT` | HALDEN unreachable at preflight; nothing was sent | Restore HALDEN connectivity; approve again | R01 |
| SKARVIK order `UNKNOWN/ACK_OVERDUE` while SKARVIK is down | File delivered to the share, never picked up | Withdraw the unclaimed message (proves not applied) | R01 |
| HALDEN order `UNKNOWN`, later `ESCALATED/RECHECK_LIMIT` | Request lost before HALDEN; no order exists | Resolve by attestation ("not booked") after checking HALDEN | R02 |
| HALDEN order `UNKNOWN`, then `CONFIRMED/RECONCILED` | HALDEN committed after the client timeout | None; the re-check found the order | R03 |
| HALDEN 500 on the order request, order `CONFIRMED/RECONCILED` | HALDEN committed and then failed | None; reconciled immediately, nothing resent | R04 |
| Unreadable HALDEN response, order reconciled | Corrupted response | None; the observation is kept (control characters escaped) | R05 |
| `UNMATCHED_ACK` and `INTEGRITY_ALERT` events | Malformed SKARVIK acknowledgement | Inspect in the unmatched-ack queue; the extract read-back settles the order | R05 |
| HALDEN order `NOT_APPLIED/PRE_PROCESSING_REFUSAL` (440) | Expired or revoked HALDEN ticket | Check a second app instance is not running; approve again | R06 |
| Approve returns 401 | Session idle-expired or step-up older than 15 min | Sign in again / confirm identity (`/reauth`) | R06, SEC-04/06 |
| Approve returns 409 | Someone else approved first, or the draft changed | Reload; approve the current version | R07, R08 |
| Approve returns 409 `CONFLICT_OVERLAP` | An overlapping order is still in flight or unknown | Wait, or operations-manager override with a reason | R09 |
| `ESCALATED/AMBIGUOUS_MATCH` | More than one HALDEN order carries the same reference | Resolve by verified read of the right order number | R10 |
| Order `UNKNOWN/INTERRUPTED` after a restart | Process stopped during a handoff | Let re-checks run; never re-enter the order | R11, R12 |
| Repeated `READ_FAILED` reconciliation runs | HALDEN listing errors | Fix HALDEN; the operation stays `UNKNOWN` with its anchor | R13 |
| `CONFIRMED/ACKNOWLEDGED_ONLY`, later `READ_BACK` | Late SKARVIK acknowledgement; extract confirms later | None | R14 |
| `ESCALATED/CONTRADICTION` | SKARVIK acknowledged `REJECTED` but the extract lists the order | Resolve by verified read | R15 |
| 403 `FORBIDDEN` / `SOD_VIOLATION` | Role lacks the permission, or own draft | Ask a person with the right role | R16 |
| Assistant: "not available to your role" | The tool call runs with your permissions | Expected | R17, AI-SEC-03 |
| 500 on approve, nothing changed | The audit write failed; the whole transaction rolled back | Check database health and triggers | R18 |
| `/readyz` `degraded:not-engine-leader` | Another process holds the engine lease | Run exactly one app container | ADR-F04 |

## Assistant error categories (only with live inference enabled)

| Category | Meaning | Behaviour |
|---|---|---|
| `TIMEOUT` | No response within `LBF_OPENAI_TIMEOUT_SEC` | Never retried; fallback with notice |
| `RATE_LIMITED`, `SERVER_ERROR` | 429 / 5xx | Bounded retry (`LBF_OPENAI_MAX_RETRIES`, capped wait), then fallback |
| `CONNECTION` | Network failure | Retried only if nothing was sent |
| `AUTHENTICATION`, `PERMISSION`, `NOT_FOUND`, `BAD_REQUEST` | Key, model or request problem | Fallback; check key and `OPENAI_MODEL` |
| `CONFIG_REJECTED` | Incomplete configuration (key, model, prices) | No client is created; `/readyz` reports `assistant: misconfigured` |
| `MODEL_MISMATCH` | The provider reported another model than `gpt-6-luna` | The response is discarded and no further request is sent until the process is restarted (`/readyz`: `assistant: stopped:model-mismatch`, still ready). Report it; do not configure another model |
| `BUDGET_DENIED` | A ceiling would be exceeded | Request not sent; fallback; `ASSISTANT_BUDGET_EXHAUSTED` audited |
| `GROUNDING_FAILED` / refusal / incomplete | Answer cites unsupported facts or the model refused | One repair attempt, then fallback |
