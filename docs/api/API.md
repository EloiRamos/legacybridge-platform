# API

The contract is [openapi.json](openapi.json), generated from the application (`make openapi`) and checked for drift
by `make verify`. The console's TypeScript client is generated from it
(`make api-client`), so an API change that the console does not follow fails the frontend build. This page is an
orientation, not a second contract.

## Conventions

- Base: `http://127.0.0.1:8080`, JSON, version prefix `/api/v1`.
- Authentication: `POST /api/v1/auth/login` sets an HttpOnly, SameSite=Strict session cookie and returns a CSRF
  token. Login is the public entry: it needs credentials and an allowed `Origin` (or `Referer`), no CSRF token. Every
  other state-changing request needs the session cookie, `X-CSRF-Token` and an allowed `Origin` or `Referer`; otherwise
  `403 CSRF_FAILED` (SEC-01/02). `GET /api/v1/auth/session` returns the session and a **fresh** CSRF token; the previous
  one stops working. Without a session every other API operation answers `401 UNAUTHENTICATED`.
- The contract declares this per operation: `security`, `x-permissions-any-of` (one of them suffices), `x-step-up`, and
  every error status with its possible codes. Segregation of duties, ownership, version and state depend on the order
  and are therefore errors, not declarations.
- Step-up: approve, decline, resolve, SKARVIK withdrawal and redelivery, and user or role changes require an authentication younger than the step-up window; otherwise
  `401 REAUTH_REQUIRED` — call `POST /api/v1/auth/reauth`.
- Errors: `application/problem+json` for every status, 422 included, with a stable `code` (`FORBIDDEN`, `SOD_VIOLATION`, `CONFLICT_VERSION`,
  `CONFLICT_STATE`, `CONFLICT_OVERLAP`, `REAUTH_REQUIRED`, `NOT_FOUND`, `VALIDATION`, …) and a `request_id`.
- `503 DATABASE_UNAVAILABLE`: the application database cannot be reached or no connection is free. The connection may
  have been lost during COMMIT, so the request may or may not have been applied: check the current state before
  repeating it. No backend write is performed without a committed precondition.
- Permissions are checked on the server for every call; the console renders only the actions listed in an
  operation's `permitted_actions`.
- `POST /api/v1/operations` accepts an `Idempotency-Key` header so that a repeated draft request returns the same
  draft. **The key is not bound to the body:** a repeat with a different body also returns the first draft, its version
  and content hash, with 201; the new body is ignored (L-24). Use a new key for every new intent and compare the returned
  `content_hash`. There is no idempotency for backend writes — those are governed by approvals and authorizations.
- `GET /metrics` is Prometheus text; `GET /readyz` answers 503 with its report while not ready.
- A command's 200 or 202 is not the business outcome. Read the order's state and evidence.

## Endpoints

| Area | Endpoints | Permission (examples) |
|---|---|---|
| Health | `GET /healthz`, `GET /readyz`, `GET /metrics` | public |
| Auth | `POST /auth/login`, `POST /auth/logout`, `POST /auth/reauth`, `GET /auth/session` | — |
| Customers | `GET /customers?q=`, `GET /customers/{backend_id}/{native_id}` | `customer.read` |
| Inventory | `GET /inventory`, `POST /inventory/annotations` | `inventory.read`, `inventory.annotate` |
| Operations | `GET /operations`, `POST /operations` (draft), `GET /operations/{id}` | `operation.read`, `draft.create` |
| Commands | `POST /operations/{id}/revisions`, `/submit`, `/approve`, `/decline`, `/withdraw`, `/recheck`, `/escalate`, `/resolve`, `/withdraw-unclaimed`, `/redelivery` | per command (role matrix in [ADR-F07](../adr/ADR-F07-authentication-and-authorization.md)) |
| Queues | `GET /approvals`; `GET /reconciliation`, `GET /dashboard` | `approval.grant.standard` or `approval.grant.any`; `operation.read` |
| Audit | `GET /audit/events`, `GET /audit/verify`, `POST /audit/exports` | `audit.read`, `audit.verify`, `audit.export` |
| Assistant | `GET /assistant/status`, `POST /assistant/turns` (with `LBF-Assistant-Version: 2`, `receipts`: drafts the application stored, `proposal`: a draft to confirm; by default the same facts are sentences in `notice`) | `assistant.use` |
| Admin | `GET /admin/users`, `POST /admin/users`, `POST /admin/users/{user_id}/roles`, `DELETE /admin/users/{user_id}/roles/{role}`, `GET /admin/config`, `GET /admin/ack-queue`, `GET /admin/engine` | `admin.*`, `ackqueue.read`, `backend.health.read` |
| Demo (only `demo`/`test` profiles) | `POST /demo/reset`, `POST /demo/faults` | `demo.control` (sysadmin) |

All paths above except Health are under `/api/v1`. The demo router is not mounted in the `default` profile (SEC-15);
`openapi.json` is generated in the test profile and therefore lists it.

## Example: approve exactly what you reviewed

```http
POST /api/v1/operations/{id}/approve
X-CSRF-Token: …
Origin: http://127.0.0.1:8080
Content-Type: application/json

{"version": 1, "content_hash": "6b62e7ed6569…", "override_reason": null}
```

The server re-reads the operation under a row lock, checks state `READY`, the current version and hash, segregation
of duties, step-up, policy (value threshold, holds, stock, overlap guard I5, validation age) and creates exactly one
`SUBMIT` authorization. A replay of the same request returns `409` (R08).

## Audit export

`POST /api/v1/audit/exports` (compliance reviewer) returns `{"manifest": {...}, "jsonl": "..."}`: canonical JSON Lines
plus a manifest with `from_seq`, `to_seq`, `count`, `first_prev_hash`, `head_hash` and `jsonl_sha256`. Keep the
`head_hash` outside the system and verify with:

```bash
python3 oracles/audit_verify.py export.jsonl manifest.json [--reconstruct <operation-id> ...]
```

Exit 0 means VALID (and every reconstruction rule passes).
