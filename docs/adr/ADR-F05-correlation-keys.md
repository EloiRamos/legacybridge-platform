# ADR-F05 — Correlation values and message ids (not idempotency keys)

- Status: Accepted (carries v1 ADR-5)
- Decision: `correlation_value = "LB" + 10 Crockford base32 characters` from 50 random bits (12 characters, the
  exact width of HALDEN's free-text `ref`); unique in PostgreSQL, regenerated on collision before insert. SKARVIK
  message id = `<correlation>-<attempt:02d>` (15 characters); SKARVIK deduplicates on it within its window.
- It is a **correlation key, not an idempotency key**: HALDEN does not deduplicate, and the same text may appear on
  orders created outside the console (residual collision risk is documented; R10 covers a planted duplicate).
- Consequences: reconciliation matches account + correlation value (+ message id) + line multiset (rule R-2);
  two candidates ⇒ `AMBIGUOUS` ⇒ escalation, never a guess.
