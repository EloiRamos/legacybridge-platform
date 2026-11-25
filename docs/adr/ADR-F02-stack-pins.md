# ADR-F02 — Technology stack and exact pins

- Status: Accepted
- Context: the engagement fixes the stack and requires exact pins (no ranges); "current stable" components
  are resolved at implementation time.

## Decision

Exact versions are recorded below as they are
resolved. Lockfiles (`backend/uv.lock`, `frontend/package-lock.json`) are authoritative.

### Toolchain observed (reference machine, macOS arm64)

| Tool | Version |
|---|---|
| Docker Engine | 29.8.0 |
| uv | 0.12.1 |
| Node.js | v24.21.0 |
| npm | 11.19.0 |

### Base images (pinned by digest)

| Image | Digest |
|---|---|
| `postgres:17` (PostgreSQL 17.12) | `sha256:223efeb196329b55b3b9a13db144956411dbc18e727e59f681c3e0aede6475cf` |
| `python:3.12-slim-bookworm` (Python 3.12.16) | `sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df` |
| `node:24-bookworm-slim` (frontend build stage only) | `sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54` |
| `maven:3.9.16-eclipse-temurin-8-noble` (HALDEN build, from its frozen Dockerfile) | `sha256:0f402712b7a469189cfea5ec69cdcadcb32b22aaf1ea62777ab925c3d8470175` |
| `eclipse-temurin:8u504-b01-jre-noble` (HALDEN runtime, from its frozen Dockerfile) | `sha256:a2553a2c9b9d39bb91eace7516cf49093ea1f419481ad4446530c768164386aa` |

The HALDEN Dockerfile is frozen and references tags; the digests above record what those tags
resolved to when the project image was built.

### Backend Python packages (exact; `backend/uv.lock` authoritative)

| Package | Version | Note |
|---|---|---|
| fastapi | 0.165.0 | newest stable |
| uvicorn | 0.54.0 | plain (no `[standard]` extras) |
| pydantic | 2.14.2 | v1 pin kept (preferred value) |
| httpx | 0.28.1 | v1 pin kept; used by the verbatim v1 `client.py` |
| sqlalchemy | 2.1.9 | Core only |
| psycopg[binary] | 3.3.9 | |
| alembic | 1.20.1 | |
| argon2-cffi | 25.1.0 | |
| defusedxml | 0.7.1 | |
| prometheus-client | 0.26.0 | |
| openai | 3.57.0 | newest release on PyPI; see compatibility note |
| pytest / pytest-timeout / hypothesis | 9.2.0 / 2.4.0 / 6.170.17 | dev |
| ruff / mypy | 0.17.6 / 2.4.1 | dev |
| pyyaml | 6.0.4 | dev only; deviation, see note below |

**Dev-only dependency deviation `pyyaml==6.0.4`.** Added to `[dependency-groups].dev` in
`backend/pyproject.toml` (exact pin, hashes in `backend/uv.lock`, PyPI source); it is not a runtime dependency and is
not installed into the application image. Reason: the YAML artifacts read by tests and harnesses
(`acceptance/fixtures/inventory_expectations.yaml` for CF-11, `resilience/expected/R*.yaml`, `evals/tasks/*.yaml`)
need a YAML parser and the standard library has none. It adds no runtime dependency,
service, egress target or paid account.

**OpenAI SDK ↔ `httpx==0.28.1`: compatible.** `openai 3.57.0` declares its HTTP dependency as
the separate distribution `httpx2>=2.12.0,<3` (resolved: `httpx2 2.16.2`, import name `httpx2`), not `httpx`. The two
distributions install side by side; `uv lock` resolved all pins together and the frozen v1 `httpx==0.28.1` pin did
not move. Consequence: the SDK's injectable HTTP client is an `httpx2` client, so the offline mock
transport is built on `httpx2` (test-only). `httpx2` is a transitive dependency of the selected SDK, not an added
direct runtime dependency; it introduces no service or network egress target.

## Consequences

Upgrades require a new ADR revision and re-running the affected stage validations.

**Update 2026-11-25.** The pinned Python base is the current published one and carries `libzstd1` 1.5.7+dfsg-1 (CVE-2026-124851). The three
runtime images upgrade that one package from Debian's security archive and their build fails if the fixed version is not installed. The
step goes when a base that contains the fix is pinned.
