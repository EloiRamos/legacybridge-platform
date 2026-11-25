# HALDEN adapter — contract and v1 traceability

Fictional engagement; HALDEN ERP 3.1 is a simulator. Contract: `HALDEN_LEGACY_API.md` (imported unchanged from
v1 r2 `0a896d1`). Port: `ADAPTER_PORT.md`.

## 1. What the adapter reuses unchanged

`backend/src/lbf/adapters/halden/v1bridge/{errors,models,normalize,client}.py` are byte-identical copies of v1
`src/legacybridge/bridge/*` at r2 `0a896d1` (manifest `V1BRIDGE_IMPORT.sha256`, re-verified by
`make verify-imports`). `HaldenAdapter` wraps one `LegacyClient` (one HALDEN ticket, ADR-F04) and the pure v1
normalizers. No v1 file is edited.

## 2. Module map

| Module | Responsibility |
|---|---|
| `adapter.py` | `HaldenAdapter`: port methods; reads via v1 client + normalizers; `handoff` = one v1 `post_order_once` |
| `mapping.py` | Port ↔ v1 mapping: crosswalk rows, credit-hold spellings, `BridgeError` → `IntegrationError` |
| `render.py` | Pure E8 rendering, byte-identical to v1 `post_order_once` (`urlencode(form).encode("utf-8")`) |
| `transport.py` | `CapturingTransport` around `httpx.HTTPTransport(retries=0)`: records the SHA-256 of the E8 body handed to the transport and of the response body |
| `reconcile.py` | Rules R-2/R-3, refactored from v1 `service.py` (table §3) |
| `fake.py` | Offline stand-in for `LegacyClient` with HALDEN 3.1 semantics, seeded from the frozen `seed.sql` (tests only) |

## 3. v1 rule → location here (traceability)

v1 line numbers refer to `src/legacybridge/service.py` at r2 `0a896d1`.

| v1 rule (location) | Behaviour | Location here | Change |
|---|---|---|---|
| `_transmit` step 1 (l.433–448) | Pre-send preparation: `prepare_write` (ticket assurance + E3 account read); failure or missing account ⇒ no E8 | `HaldenAdapter.preflight`; workflow T10 (`NOT_APPLIED/NOT_SENT`) | Returns `PreflightResult`; the engine decides |
| `_transmit` step 2 (l.449–455) | Exactly one E8 via `post_order_once`; exceptions ⇒ `UNCERTAIN` | `HaldenAdapter.handoff` | Adds capture of the bytes handed to the transport; digest mismatch ⇒ `UNCERTAIN` + `INTEGRITY_ALERT` |
| `_classify` NOT_DELIVERED (l.473) | Connect failure ⇒ `NOT_APPLIED/NOT_SENT` (v1 ADR-4) | `E8_KIND_MAP` → `NOT_DELIVERED`; workflow | Same outcome |
| `_classify` PRE_HANDLER_REFUSAL (l.480) | 401/403/440/429 ⇒ `NOT_APPLIED` (I1) | → `REFUSED_PRE_PROCESSING` ⇒ `NOT_APPLIED/PRE_PROCESSING_REFUSAL` | Same outcome, canonical evidence name |
| `_classify` REJECTED / SHORT (l.487–501) | rc≠0 or 409 SHORT ⇒ `REJECTED` (I2) | → `REJECTED`, `backend_code` `rc=<n>` / `SHORT:<item>:<avail>` | Same outcome |
| `_classify` ACCEPTED (l.502) | ⇒ `_read_back` | → `ACCEPTED`; workflow phase-2 anchored `reconcile` | — |
| `_read_back` (l.534–576) | E7 of the acknowledged order: match ⇒ `READ_BACK`; transport failure ⇒ `ACKNOWLEDGED`; 404/mismatch ⇒ `UNKNOWN/READBACK_MISMATCH` | `HaldenReconciler.reconcile` with `anchor_order_ref` ⇒ `MATCHED` / `READ_FAILED` / `MISMATCH`; workflow maps | Same outcomes (`ACKNOWLEDGED` ⇒ `ACKNOWLEDGED_ONLY`) |
| `_reconcile` (l.577–640) | E6 listing; ≥ 2 refs ⇒ ambiguous; none ⇒ `NO_MATCH_AFTER_UNCERTAIN`; sole candidate ⇒ E7 + match | `HaldenReconciler.reconcile` (unanchored) | Sole non-matching/404 candidate is `MISMATCH` (v1: `AMBIGUOUS_MATCH`); both stay unresolved (`UNKNOWN`), never confirmed |
| `_matches_approved` (l.514–532) | Account, `Ref` (null non-match, tested before trimming), line multiset; price/total/date/status not compared | `reconcile.matches_approved` | Unchanged semantics |
| `_same_account` (l.508) | Account equality after v1 normalization | `reconcile.same_account` | Unchanged |
| `_order_rows` | E6 paging (cursor) | `HaldenReconciler.order_rows` (bounded at 50 pages, records each page as a `SourceRead`) | Adds source-read evidence |
| `_merge_lines` (l.253–274) | Duplicate SKUs merged; merged quantity ≤ 999 | Workflow draft validation | Moved to the canonical draft layer |
| strict stock evidence (v1 F03, `normalize.assess_stock`) | Structural extract defect ⇒ no verdict; anomalies ⇒ `UNDETERMINED` | `HaldenAdapter.stock` (v1 `assess_stock` verbatim) | Raises `BACKEND_MALFORMED` where v1 raised `LegacyMalformedResponse` |
| `DraftStore`, `BridgeService` orchestration | In-memory drafts and attempts | Durable workflow engine | Reimplemented |

## 4. Write-path guarantees and their limits

* **One POST per handoff call.** `handoff` calls `post_order_once` exactly once; the v1 client never retries,
  re-logs in or waits around an E8. The capture proves the bytes were *handed to the HTTP transport*; it is not
  proof that HALDEN received them (wire truth is the wire-tap ledger; business truth is HALDEN's own state read by
  the independent oracle client).
* **No automatic retry, no resend.** Nothing in the adapter loops over `handoff`.
* **Pre-handler refusals** (401/403/440/429) are `REFUSED_PRE_PROCESSING`: HALDEN invariant I1 guarantees no
  state change; the ticket is discarded so the next preflight logs in again.
* **Ambiguous outcomes** (timeout after connect, 5xx, malformed, unparseable) are `UNCERTAIN`; reconciliation can
  only ever return `MATCHED`, `NO_MATCH`, `AMBIGUOUS`, `MISMATCH` or `READ_FAILED` — `absence_proven` is always
  `False` because HALDEN cannot prove that an order was not booked.
* **Pause point `before_handoff`.** The injected `before_publish` hook runs before the request object exists. An
  ordinary exception from the hook ⇒ `NOT_DELIVERED` (nothing was sent). A process kill at the hook leaves the
  transmission `SENDING`; recovery marks it `INTERRUPTED` ⇒ `UNKNOWN` (the documented residual window).

## 5. Ported v1 tests (port list)

Ported to `backend/tests/unit/test_halden_adapter.py` (adapter level; the real v1 client under the adapter with an
`httpx.MockTransport`) and to the conformance suite.

| Port ID | v1 test | Test here |
|---|---|---|
| P-01 | `test_client_unit.py::test_post_order_once_kinds_with_exactly_one_request` (16 cases) | `test_p01_handoff_kinds_with_exactly_one_post_no_relogin_no_wait` |
| P-02 | `test_client_unit.py::test_post_order_once_form_is_positional` | `test_p02_rendered_bytes_equal_the_v1_positional_form_on_the_wire` |
| P-03 | `test_client_unit.py::test_post_order_once_discards_ticket_on_auth_refusal` | `test_p03_pre_handler_refusal_discards_ticket_and_next_preflight_logs_in_again` |
| P-04 | `test_service.py::test_lv_row1_read_back` | `test_p04_row1_anchored_read_back_matches` |
| P-05 | `test_service.py::test_lv_row3_read_back_404_via_override` | `test_p05_row3_anchored_404_is_mismatch_never_a_different_order` |
| P-06 | `test_service.py::test_lv_row2_read_back_transport_failure_via_override` | `test_p06_row2_read_back_transport_failure_is_read_failed` |
| P-07 | `test_service.py::test_lv_row8_and_row9_to_8` (row 9) | `test_p07_row9_no_visible_order_is_no_match_and_never_absence` |
| P-08 | `test_service.py::test_lv_row10_two_refs` | `test_p08_row10_two_orders_with_the_reference_are_ambiguous`; CF-09 |
| P-09 | `test_service.py::test_lv_row10_one_mismatching_ref` | `test_p09_row10_sole_candidate_with_different_lines_is_mismatch` |
| P-10 | `test_service.py::test_lv_row11_listing_failure_after_commit_then_500` | `test_p10_row11_listing_failures_are_read_failed` |
| P-11 | `_matches_approved` null/trim rule | `test_p11_null_reference_is_a_non_match_and_padded_reference_matches_after_trim` |
| P-12 | `_matches_approved` fields not compared | `test_p12_price_total_and_status_are_not_compared` |
| P-13 | `test_service.py::test_f03_3/5/6`; `test_client_unit.py::test_f03_4_stock_extract_strictness` | `test_p13_structural_extract_defect_yields_no_availability_verdict` |
| P-14 | `test_service.py::test_f03_7_negative_reserved_quantity` | `test_p14_negative_allocation_is_undetermined` |
| P-15 | `test_service.py::test_products_prices_categories_and_availability` (summing) | `test_p15_complete_extract_sums_warehouses_live` |
| P-16 | `test_normalize.py` flag spellings | `test_p16_credit_hold_spellings` |
| P-17 | `test_service.py::test_f01_t4_commit_then_500_is_reconciled` | CF-05 (`halden_fake`, `halden_live`) |
| P-18 | `test_service.py::test_reject_sends_nothing` / `test_lv_row4_legacy_rejection` | CF-03 |

Not ported (remain v1-only): Streamlit UI tests, LangChain assistant tests, `DraftStore` lifecycle tests (replaced by
the durable workflow), and live service tests whose behaviour is now covered by CF-01…CF-13 live.
