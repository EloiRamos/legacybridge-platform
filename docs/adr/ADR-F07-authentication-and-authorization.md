# ADR-F07 — Local accounts, server sessions, CSRF, static RBAC and four-eyes approvals

- Status: Accepted
- Decision: local users with Argon2id (argon2-cffi library defaults: `argon2.PasswordHasher()` — parameters of
  the pinned library), minimum length 12; opaque 32-byte session tokens stored as
  SHA-256, cookie `lbf_session` HttpOnly + SameSite=Strict (`Secure` when `LBF_COOKIE_SECURE=true`), idle 30 min,
  absolute 8 h, rotated at login and re-auth; synchronizer CSRF token + Origin check; lockout after 5 failures for
  15 min and 10 attempts/min per client address; step-up re-authentication (≤ 15 min) for approve, decline,
  redelivery/withdraw authorization, escalation resolution and role changes. Static versioned policy
  `POLICY_VERSION = "2026.11-1"` and SoD-1…SoD-5 enforced inside command transactions.
- Alternatives rejected: OIDC/Keycloak (operational weight without evidentiary gain for a loopback deployment);
  JWT (revocation/replay complexity).
- Consequences: the reference deployment is loopback HTTP; any non-loopback use requires TLS and `LBF_COOKIE_SECURE=true`.
