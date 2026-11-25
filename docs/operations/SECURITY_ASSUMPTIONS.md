# Security assumptions

Fictional engagement, local deployment. This is the security model **as built and tested**; it is not a claim of
production readiness.

## Deployment assumptions

- Loopback only: the default profile publishes `127.0.0.1:8080` and nothing else (SEC-16). There is **no TLS**; put a
  TLS-terminating proxy in front and set `LBF_COOKIE_SECURE=true` before any network exposure.
- Local accounts only (Argon2id, minimum length 12, lockout after 5 failures for 15 min, 10 logins/min per address).
  No SSO or MFA.
- Secrets are files on the host (`deploy/secrets/`, git-ignored, mode 0600), mounted as Docker secrets. Anyone with
  shell access to the host can read them.
- The simulators run on an internal Docker network; their control planes are disabled in `default` (SEC-14).

## Controls per boundary (tested)

| Boundary | Controls | Tests |
|---|---|---|
| Browser → API | HttpOnly SameSite=Strict session cookie (idle 30 min, absolute 8 h), CSRF token + Origin check on every state change, step-up (15 min) for approve/decline/resolve, CSP `default-src 'self'`, no tokens in browser storage | SEC-01…08 |
| Authorization | Server-side role → permission matrix; segregation of duties SoD-1…5 (nobody approves a draft they created or edited; the invoking human authors assistant drafts; nobody changes their own roles; a redelivery authorizer did not author the draft; the approver cannot resolve the escalation) | SEC-07, SEC-09, R16 (264 role × command × state cells + 8 probes, 100 %) |
| Workflow | Version + hash bound approvals; one authorization per approval; one handoff per authorization (DB constraint); overlap guard with audited override | F2-01…F2-06, C-01…C-10, R07, R08 |
| Database | Least-privilege roles; `lbf_app` cannot update or delete history; append-only triggers; SQL transition trigger | SEC-12, SEC-13 |
| Audit | Hash chain committed with each transition; exports verifiable without the application | E-01…E-06, R18 |
| Assistant | 9 read/draft tools, human's permissions, strict schemas, untrusted text wrapped; no write beyond drafts | AI-SEC-01…07, R17 |
| Backend data | SKARVIK XML via defusedxml with size cap; control characters escaped before storage | SEC-11, D7-1 test |
| Secrets | Secret scan of repository and captured outputs (values from `deploy/secrets` + key/token patterns) | SEC-10 (0 findings) |

## Residual risks

- A privileged database administrator can rewrite the audit chain consistently; detection depends on head hashes
  retained outside the system.
- HALDEN XML is parsed by the frozen v1 bridge with the standard library; HALDEN response bodies are capped at 1 MiB
  at the transport (refused, never truncated), but entity expansion within a body under the cap is not bounded (ADR-F09).
- Dependency vulnerabilities were **not** audited (`pip-audit`/`npm audit` need vulnerability-database access that was
  not authorized); lockfiles pin exact versions.
- Prompt text is not a security boundary; the assistant's safety rests on the tool allowlist and the human's
  permissions. Live-model behaviour was observed in two bounded runs only: a failed smoke (2026-11-22) and one passed
  two-turn run (2026-11-23), both with read-only lookups; injection and refusals were not exercised live. The model provider
  receives the question, the session's recent questions and answers, and tool results (fictional data here); a real
  deployment would need a data-processing review. The conversation is kept in server memory only.
- No rate limiting beyond login, assistant turns and manual re-check; no WAF; single host.

More: [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md).
