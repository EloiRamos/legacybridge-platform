# Install

Fictional engagement; the legacy systems are simulators bundled with the repository. Everything runs locally and
binds to `127.0.0.1`.

## Prerequisites

A Git checkout (two backend tests read Git metadata; a source archive runs the product but fails them), Docker with
Compose v2.24 or newer, GNU make, `uv` 0.13.4 or later (Python tooling for tests and helper scripts; earlier versions
bundle compromised CA material, GHSA-hq85-5cpj-mr62), Node.js 24 (console
build and end-to-end tests), Python 3.12 for the standalone verifier. Used so far: macOS on Apple silicon with Docker
Desktop only. First builds pull base images (PostgreSQL, Python, Node,
Maven/Temurin for HALDEN) and packages from PyPI, npm and Maven Central: the setup needs outbound network access, the
running stack does not.

## Three commands

```bash
make secrets
```

```bash
make demo-users
```

```bash
make up
```

- `make secrets` writes database passwords, the HALDEN secret file and an **empty** OpenAI key file into
  `deploy/secrets/` (git-ignored). Existing files are kept.
- `make demo-users` starts the database, applies migrations and creates the seven fictional personas with random
  passwords, appended to `deploy/secrets/demo-credentials.txt` (mode 0600). Run it once per database.
- `make up` builds and starts `db`, `migrate`, `halden`, `skarvik` and `app` in the `default` profile.

Check readiness:

```bash
curl -fsS http://127.0.0.1:8080/readyz
```

`status: ready` means the database is reachable and at the migration head, this process holds the engine lease,
HALDEN answers a ping and SKARVIK's newest snapshot is recent. With Docker image layers already
cached, an earlier version was ready in 16.6 s through these commands, and this version in 19 s through `make demo-up`. A first
build takes minutes. Keep at least 10 GiB of host storage free while Docker runs.

To try the demonstration or the checks without touching this deployment, use the isolated stack instead:
`make demo-up` ([walkthrough](../portfolio/DEMO_WALKTHROUGH.md)) or `make verify` ([what it covers](../evidence/VERIFICATION.md)).

## First login

Open <http://127.0.0.1:8080>, sign in as one of the personas from `deploy/secrets/demo-credentials.txt`
(`marta.ops`, `daniel.sales`, `priya.sales`, `sanna.inventory`, `rafael.integration`, `helena.compliance`,
`tomas.admin`). Passwords are random and never committed.

## Profiles

| Profile | Start | Adds |
|---|---|---|
| `default` | `make up` | the customer deployment shape; simulation controls off |
| `demo` | `LBF_PROFILE=demo LBF_SIM_CONTROL=true LBF_TRANSPORT_TIMEOUT_SEC=1 docker compose up -d --build --wait` | demo panel and fault presets for the sysadmin; the 1 s timeout is what makes the 2.5 s HALDEN delay preset end in an unknown outcome |
| isolated demonstration | `make demo-up` | a second, disposable stack with fixed timings |
| `test` | `make test-up` | wire-tap, loopback test ports, pause points, scaled time (automated acceptance only) |

## Stop and remove

```bash
make down
```

Data stays in the Docker volume `lbf-pgdata`; back it up first if you remove volumes ([RECOVERY.md](RECOVERY.md)).
