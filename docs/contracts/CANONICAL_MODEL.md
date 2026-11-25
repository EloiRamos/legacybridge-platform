# Canonical model — contract `lbf-canonical/1.0`

Source of truth: `backend/src/lbf/domain/`. All models are Pydantic v2, frozen, `extra="forbid"`.
Money is `Decimal` quantized to 0.01 and serialized as a string. Timestamps are timezone-aware UTC;
backend-local timestamps are converted at the adapter boundary and the raw value is retained.

| Concept | Module | Notes |
|---|---|---|
| Enumerations (states, outcomes, evidence classes, roles, permissions, actions) | `domain/enums.py` | closed sets |
| Canonical JSON + content hash | `domain/canonical.py` | see below |
| Customers (`BackendAccount`, `Party`, `CustomerSearchResult`) | `domain/models.py`, `domain/customers.py` | rules C-1, C-2 |
| Inventory (`CanonicalItem`, `ItemMapping`, `WarehouseQty`, `Availability`) | `domain/models.py`, `domain/inventory.py` | rules I-1…I-3 |
| Drafts (`DraftContent`, `DraftContentLine`, `DraftVersion`, `ValidationSnapshot`) | `domain/models.py` | hashed content vs validation data |
| Operation aggregate | `domain/models.py` | `correlation_value` is a correlation key, not an idempotency key |
| Submission (`SubmissionRequest`, `RenderedRequest`), observation, reconciliation | `domain/models.py` | rule R-1 enforced by validator |
| Outcomes/evidence | `domain/evidence.py` | closed mapping, validator on `RecordedOutcome` |
| Errors | `domain/errors.py` | taxonomy and HTTP mapping |
| Correlation values | `domain/correlation.py` | `"LB"` + 10 Crockford base32 chars (50 random bits) |

## Hashed content versus validation data

`DraftContent` is the business content bound by approvals: backend, native customer, lines
(`line_no`, `canonical_sku`, `native_item`, `quantity_base`, `quantity_native`, `native_uom`,
`unit_price_estimate`, `line_estimate`), and the SKARVIK-only `customer_po`, `requested_date`,
`delivery_note`. Availability status, blocking reasons and warnings are **validation data** stored in
`ValidationSnapshot` ("availability is validation data, not content"); they are not hashed.

## Canonical JSON

UTF-8; keys sorted; separators `(",", ":")`; `ensure_ascii=False`; floats rejected; dates and
timezone-aware datetimes ISO-8601; UUIDs as strings; enums by value. **Decimal normalization:**
finite values only, fixed-point notation (no exponent), trailing fractional zeros and a trailing
`.` removed, negative zero rendered `0` (e.g. `12.50 → "12.5"`, `1E+3 → "1000"`).

`content_hash = sha256(canonical_json({"schema":"lbf-canonical/1.0","operation_id":…,"version":…,"content":…}))`.
Ten golden vectors are committed in `backend/tests/unit/test_canonical.py`; the independent
re-implementation in `oracles/` is cross-checked against them.

## Customer rules

- **C-1** An ambiguity group is formed when two or more accounts **in the same backend** share a
  normalized name stem (case/accent folded, punctuation removed, trailing legal suffixes such as
  Ltd/AB/AS/Oy/GmbH removed). The UI requires an explicit account choice. Groups never span backends.
- **C-2** A party groups accounts only through the curated `party_accounts` crosswalk; name
  similarity never creates or extends a party. Linked accounts whose credit hold differs produce
  the issue `CROSS_BACKEND_HOLD_CONFLICT`.

## Inventory rules

- **I-1 (HALDEN)** v1 strict stock evidence unchanged (applied by the verbatim v1 normalizers).
- **I-2 (SKARVIK)** snapshot older than `inventory_max_age_seconds` ⇒ `STALE`; missing mapping or
  conversion factor ⇒ `UNDETERMINED`; `available_base = available_native × factor_num / factor_den`
  only when exactly divisible, else `UNDETERMINED` with `NON_INTEGRAL_CONVERSION`.
- **I-3** Each backend is listed separately; stock is never summed across backends.
