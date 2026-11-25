# Runbook

For the fictional customer's order desk and IT. Every procedure below is executed by the automated suite; the test
that exercises it is named in brackets.

## Daily checks

1. **Overview** (`/`): approvals waiting for you, *Outcome unknown*, *Needs a decision*, backend
   reachability and SKARVIK snapshot age.
2. **Reconciliation queue** (`/reconciliation`): every `UNKNOWN`, `RECONCILING`, `ESCALATED` and overdue
   `AWAITING_ACK` order. The oldest `UNKNOWN` age is also exported as `lbf_unknown_age_max_seconds` (`/metrics`).
3. **Unmatched acknowledgements** (`/admin`, integration engineer / operations manager): acknowledgement files that
   match no transmission or could not be parsed. They are never treated as acceptance or rejection.
4. **Evidence** (`/audit`, compliance): *Verify the chain* should report VALID; *Export audit* and verify the files periodically (§ Audit).

## Handling each state

| State shown | Meaning | What to do |
|---|---|---|
| **Outcome unknown** (`UNKNOWN`) | The backend may or may not have booked the order. | **Do not re-enter the order.** Wait for the automatic read-only re-checks (5 s, 15 s, 1 min, 5 min, 15 min after it became unknown) or press *Re-check (read-only)* (once per 10 s). After the fifth re-check, or 30 min, it is escalated. [R02, R03, R12, R13] |
| **Needs decision** (`ESCALATED`) | Automatic checks could not settle it (limit reached, ambiguous match, contradiction). | Operations manager resolves: **Resolve — verify order…** with the backend order number (the system reads it and confirms only if account, reference and lines match), or **Resolve — attest…** with an external reference (closed as *not verified*). The person who approved cannot resolve. [F2-09, signature 3] |
| **Delivered to SKARVIK — awaiting acknowledgement** | The file is in the inbox; SKARVIK has not acknowledged it yet. | Nothing until the deadline (300 s). Overdue ⇒ `UNKNOWN/ACK_OVERDUE`. [R01, R14] |
| SKARVIK overdue: file still unclaimed | SKARVIK has not picked the file up. | Operations manager: **Withdraw unclaimed message…** — moves it to `withdrawn/` and proves `NOT_APPLIED`. If SKARVIK already claimed it, nothing is proven and it stays unresolved. [R01, OPS-05] |
| SKARVIK overdue: file claimed, ack lost | SKARVIK processed it but the acknowledgement is missing. | Wait for the extract read-back, or operations manager: **Authorize redelivery…** — the identical file once more under the same message id, only while *Redelivery window closes* is in the future; SKARVIK deduplicates it. At most one redelivery per attempt. [signature 2/2b, CF-13] |
| **Not applied (proven)** | Positive evidence that the backend did not process it. | A new approval of the same draft is possible. |
| **Closed by attestation (not verified)** | A human closed it on an external reference. | No further action; it is labelled as not verified everywhere. |
| Approval expired | An authorization was not used within 15 min. | `NOT_APPLIED/APPROVAL_EXPIRED`; approve again if still wanted. [F2-07] |

### Withdraw or redeliver? (SKARVIK, operations manager)

| Observation | Decision |
|---|---|
| File still in `inbox/` (unclaimed) | Withdraw ⇒ proven not applied |
| File claimed, no ack, within the redelivery window | Redeliver once (dedup protects against a second order) |
| File claimed, window closed | Do **not** redeliver; wait for the extract, then resolve (verified or attested) |

### Overlap override policy

While an order for the same account and item is `APPROVED`, `SUBMITTING`, `AWAITING_ACK`, `UNKNOWN`, `RECONCILING`
or `ESCALATED`, a new overlapping order cannot be approved without an operations-manager override with a written
reason (≥ 20 characters). The override is audited (`OVERRIDE_RECORDED`). Use it only when the first order is known to
be lost. [R09, C-06]

## Assistant

Off by default (guided commands only). If live inference is ever enabled by the owner:

| Situation | Behaviour / action |
|---|---|
| Budget exhausted | The assistant answers with the deterministic router and a notice; the daily budget resets at 00:00 UTC; raise it only by changing `LBF_OPENAI_DAILY_BUDGET_USD` and restarting |
| OpenAI error (timeout, rate limit, server error, refusal, malformed output) | Fallback to the router with a named notice; check `/metrics` `lbf_inference_requests_total{status,error_category}` |
| Rotate or revoke the key | Replace or empty `deploy/secrets/openai_api_key.txt`, restart `app`, revoke at the provider |
| Disable immediately | Set `LBF_ASSISTANT_PROVIDER=disabled` (or keep `LBF_LIVE_INFERENCE=false`) and restart `app` |

## Audit export and verification (compliance)

`POST /api/v1/audit/exports` (or the API client) returns the JSON Lines export and its manifest. Store the manifest's
`head_hash` outside the system. Verify any export with:

```bash
python3 oracles/audit_verify.py export.jsonl manifest.json
```

A tampered row is reported with its sequence number; a truncated file fails the manifest check. [E-01…E-03]
