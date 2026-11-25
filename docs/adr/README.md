# Architecture decision records

| ADR | Subject | Status |
|---|---|---|
| [ADR-F01](ADR-F01-repository-strategy.md) | Separate repository with byte-identical legacy imports | Accepted |
| [ADR-F02](ADR-F02-stack-pins.md) | Technology stack and exact pins | Accepted |
| [ADR-F03](ADR-F03-persistence-postgresql.md) | PostgreSQL 17 with SQLAlchemy Core and Alembic | Accepted |
| [ADR-F04](ADR-F04-single-application-process.md) | One application process with a singleton engine lease | Accepted |
| [ADR-F05](ADR-F05-correlation-keys.md) | Correlation values and message ids (not idempotency keys) | Accepted |
| [ADR-F06](ADR-F06-skarvik-design.md) | SKARVIK OES 4 as an independent asynchronous file-exchange simulator | Accepted |
| [ADR-F07](ADR-F07-authentication-and-authorization.md) | Local accounts, server sessions, CSRF, static RBAC and four-eyes approvals | Accepted |
| [ADR-F08](ADR-F08-ai-boundary.md) | Bounded assistant; OpenAI API as the exclusive live inference provider | Accepted |
| [ADR-F09](ADR-F09-halden-xml-trust.md) | HALDEN XML parsed by the frozen v1 bridge (trust assumption from v1 ADR-8) | Accepted |
| [ADR-F10](ADR-F10-openai-provider.md) | OpenAI provider (Responses API) and its SDK binding | Accepted |
| [ADR-F11](ADR-F11-evidence-architecture.md) | Evidence architecture: independent oracles, wire-tap, determinism gate | Accepted |
| [ADR-F12](ADR-F12-deployment-and-recovery.md) | Compose profiles, three-command deployment, backup/restore + reconciliation | Accepted |
