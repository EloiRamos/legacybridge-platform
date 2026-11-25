# Third-party notices

LegacyBridge is distributed as **source code** under the [Apache License, Version 2.0](LICENSE)
(SPDX: `Apache-2.0`), Copyright 2026 Elói Ramos. Neither this project nor its predecessor has a `NOTICE` file. No third-party source or
binary is included in this repository, with one exception: the HALDEN simulator and the HALDEN client bridge are reused
unchanged from the author's earlier project (same author, same license; hashes in `legacy-systems/HALDEN_IMPORT.sha256`
and `backend/src/lbf/adapters/halden/V1BRIDGE_IMPORT.sha256`).

The packages below are downloaded when you build. Their licenses apply to what you build; **container images that you build
and redistribute contain them, and the base images' own components, and need a notice review of their own.** That review
has not been done, because no image is published.

## Runtime packages

| Package | Version | License (package metadata) | Where |
|---|---|---|---|
| @tanstack/query-core | 5.108.2 | MIT | console bundle |
| @tanstack/react-query | 5.108.2 | MIT | console bundle |
| @tanstack/react-store | 0.11.2 | MIT | console bundle |
| @tanstack/react-table | 9.3.1 | MIT | console bundle |
| @tanstack/store | 0.11.2 | MIT | console bundle |
| @tanstack/table-core | 9.3.1 | MIT | console bundle |
| @types/react | 19.3.0 | MIT | console bundle |
| cookie | 1.1.1 | MIT | console bundle |
| csstype | 3.2.4 | MIT | console bundle |
| openapi-fetch | 0.17.0 | MIT | console bundle |
| openapi-typescript-helpers | 0.1.0 | MIT | console bundle |
| react | 19.3.1 | MIT | console bundle |
| react-dom | 19.3.1 | MIT | console bundle |
| react-router | 7.18.6 | MIT | console bundle |
| react-router-dom | 7.18.6 | MIT | console bundle |
| scheduler | 0.28.0 | MIT | console bundle |
| set-cookie-parser | 2.7.3 | MIT | console bundle |
| use-sync-external-store | 1.7.0 | MIT | console bundle |
| alembic | 1.20.1 | MIT | application |
| annotated-doc | 0.0.7 | MIT | application |
| annotated-types | 0.8.0 | MIT | application |
| anyio | 4.16.0 | MIT | application |
| argon2-cffi | 25.1.0 | MIT | application |
| argon2-cffi-bindings | 26.2.0 | MIT | application |
| certifi | 2026.11.20 | Mozilla Public License 2.0 (MPL 2.0) | application |
| cffi | 2.1.1 | MIT-0 | application |
| click | 8.5.1 | BSD-3-Clause | application |
| defusedxml | 0.7.1 | Python Software Foundation License | application |
| fastapi | 0.165.0 | MIT | application |
| h11 | 0.16.0 | MIT License | application |
| httpcore | 1.0.9 | BSD-3-Clause | application |
| httpcore2 | 2.16.2 | BSD-3-Clause | application |
| httpx | 0.28.1 | BSD License | application |
| httpx2 | 2.16.2 | BSD-3-Clause | application |
| idna | 3.20 | BSD-3-Clause | application |
| jiter | 0.19.0 | MIT | application |
| Mako | 1.4.3 | MIT | application |
| MarkupSafe | 3.0.4 | BSD-3-Clause | application |
| openai | 3.57.0 | Apache-2.0 | application |
| opentelemetry-api | 1.45.2 | Apache-2.0 | application |
| prometheus_client | 0.26.0 | Apache-2.0 AND BSD-2-Clause | application |
| psycopg | 3.3.9 | LGPL-3.0-only | application |
| psycopg-binary | 3.3.9 | LGPL-3.0-only | application |
| pycparser | 3.12 | BSD-3-Clause | application |
| pydantic | 2.14.2 | MIT | application |
| pydantic_core | 2.50.2 | MIT | application |
| sniffio | 1.3.1 | MIT License, Apache Software License | application |
| SQLAlchemy | 2.1.9 | MIT | application |
| starlette | 1.10.0 | BSD-3-Clause | application |
| truststore | 0.10.5 | MIT | application |
| typing-inspection | 0.4.5 | MIT | application |
| typing_extensions | 4.16.0 | PSF-2.0 | application |
| uvicorn | 0.54.0 | BSD-3-Clause | application |
| org.apache.tomcat.embed:tomcat-embed-core | 9.0.123 | Apache-2.0 | HALDEN simulator |
| org.xerial:sqlite-jdbc | 3.54.1.0 | Apache-2.0 | HALDEN simulator |

Base images: `postgres:17`, `python:3.12-slim-bookworm`, `node:24-bookworm-slim` (build), Maven and Eclipse Temurin 8 (HALDEN),
pinned by digest in the Dockerfiles and `compose.yaml`. Maven also resolves the transitive runtime dependencies of
Tomcat (`tomcat-annotations-api`, Apache-2.0) and `sqlite-jdbc` (`slf4j-api`, MIT); both end up in the HALDEN image. Development and test packages are listed in `backend/uv.lock`,
`frontend/package-lock.json` and `legacy-systems/*/`; the only copyleft license among the npm development packages is
MPL-2.0 (`@axe-core/playwright`, `axe-core`, `lightningcss`), used unmodified for tests and builds and not distributed.
`caniuse-lite` (development only) is data under CC-BY-4.0.

The PostgreSQL driver `psycopg` (LGPL-3.0-only) is used as an unmodified, replaceable Python package; `psycopg-binary` bundles
libpq, OpenSSL and other libraries under their own licenses. `certifi` (MPL-2.0) is used unmodified.

## Upstream notices

Packages keep their own license and notice files when installed. Built images must keep them, including the `NOTICE` files inside
the Apache Tomcat archives. Among the installed Python runtime packages, `prometheus_client` carries a `NOTICE`:

```text
Prometheus instrumentation library for Python applications
Copyright 2015 The Prometheus Authors

This product bundles decorator 4.0.10 which is available under a "2-clause BSD"
license. For details, see prometheus_client/decorator.py.
```
