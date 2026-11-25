# LEGACYBRIDGE Platform — developer and operator entry points.
SHELL := /bin/sh
COMPOSE := docker compose
COMPOSE_TEST := docker compose -f compose.yaml -f deploy/compose.test.yaml
# Test profile: simulation control on, SKARVIK fast timings, HALDEN via the wire-tap,
# exchange share bound to a git-ignored host directory so host-run oracles can read it.
TEST_ENV := LBF_PROFILE=test LBF_SIM_CONTROL=true SKARVIK_PICKUP_INTERVAL=1 SKARVIK_EXTRACT_INTERVAL=2 \
	SKARVIK_DEDUP_WINDOW_SECONDS=120 LBF_EXCHANGE_SOURCE=./governance/work/skarvik-exchange \
	LBF_HALDEN_URL=http://wiretap:8090 LBF_TIME_SCALE=0.02 LBF_TRANSPORT_TIMEOUT_SEC=1 \
	LBF_PAUSE_SOURCE=./governance/work/lbf-pause

.PHONY: secrets db-up migrate migrate-down test-db-up test-unit test-db verify-imports boundaries \
	up down demo-users openai-smoke eval-scripted eval-baseline openapi api-client test-up test-down test-api e2e security-compose logs \
	demo-up demo-check demo-down verify test-security resilience backup restore secret-scan

secrets:
	./deploy/make_secrets.sh

db-up:
	$(COMPOSE) up -d --wait db

migrate:
	$(COMPOSE) run --rm migrate

migrate-down:
	$(COMPOSE) run --rm migrate alembic downgrade base

test-db-up: secrets
	$(COMPOSE_TEST) up -d --wait db
	$(COMPOSE_TEST) run --rm migrate

test-unit:
	cd backend && uv run pytest tests/unit -q -p no:cacheprovider

test-db: test-db-up
	cd backend && uv run pytest tests/integration tests/security tests/concurrency -q -p no:cacheprovider

verify-imports:
	python3 tools/verify_imports.py

boundaries:
	cd backend && uv run pytest tests/unit/test_boundaries.py -q -p no:cacheprovider

# --- application -----------------------------------------------------------------------------------------------
up: secrets
	$(COMPOSE) up -d --build --wait

down:
	./tools/owned_project.sh
	$(COMPOSE) --profile test down

logs:
	$(COMPOSE) logs --tail=200 app

# Works on a clean clone before `make up` (OPS-01 order: secrets, demo-users, up): database and migrations first.
demo-users: secrets
	$(COMPOSE) up -d --wait db
	$(COMPOSE) run --rm migrate
	( umask 077; $(COMPOSE) run --rm --no-deps -T app lbf-admin demo-users >> deploy/secrets/demo-credentials.txt )

openapi:
	cd backend && uv run --frozen python -m lbf.api.openapi ../docs/api/openapi.json

api-client: openapi
	cd frontend && npm run api-client

test-up: secrets
	mkdir -p governance/work/skarvik-exchange governance/work/lbf-pause
	$(TEST_ENV) $(COMPOSE_TEST) --profile test up -d --build --wait

test-down:
	./tools/owned_project.sh
	$(COMPOSE_TEST) --profile test down

test-api: test-db-up
	cd backend && uv run --frozen pytest tests/api tests/security -q -p no:cacheprovider

e2e:
	cd frontend && npx playwright test

security-compose:
	cd backend && uv run --frozen pytest tests/security/test_compose.py -q -p no:cacheprovider

# V8-3: the security sweep (SEC-*, R16/R18 negative tests, boundaries) against the test database; the compose checks
# (SEC-14/15/16) need the default stack and run in `make security-compose`.
test-security: test-db-up
	cd backend && uv run --frozen pytest tests/security tests/resilience/test_r16_matrix.py \
		tests/resilience/test_r18_audit_failure.py tests/unit/test_boundaries.py -q -p no:cacheprovider \
		--deselect tests/security/test_compose.py::test_sec14_control_planes_unreachable_from_the_running_app \
		--deselect tests/security/test_compose.py::test_sec15_running_default_api_has_no_demo_endpoints

# --- isolated demonstration and public verification (own project, ports, volumes and images; no paid call) --------
demo-up:
	./tools/isolated.sh up

demo-check:
	./tools/isolated.sh check

demo-down:
	./tools/isolated.sh down

verify:
	./tools/verify.sh

# --- resilience (test profile running: make test-up) ----------------------------------------------
RUN ?= $(shell date -u +%Y%m%dT%H%M%SZ)
resilience:
	cd backend && PYTHONPATH=.. uv run --frozen python -m resilience.run --scenario all --seed 20261121 \
		--out ../acceptance/evidence/$(RUN)/resilience/run1

# --- operations ---------------------------------------------------------------------------------------------------
backup:
	./deploy/backup.sh

restore:
	./deploy/restore.sh $(FILE)

# V8-6: tracked and untracked (not ignored) files; secret values from deploy/secrets and key/token patterns
secret-scan:
	python3 acceptance/secret_scan.py --repo

# --- assistant (offline by default; no paid call) ----------------------------------------------------------------
EVAL_RUN ?= $(shell date -u +%Y%m%dT%H%M%SZ)
eval-scripted: test-db-up
	cd backend && env -u OPENAI_API_KEY LBF_LIVE_INFERENCE=false PYTHONPATH=.. uv run --frozen python -m evals.run \
		--mode scripted --out ../evals/reports/$(EVAL_RUN)/eval-scripted

eval-baseline: test-db-up
	cd backend && env -u OPENAI_API_KEY LBF_LIVE_INFERENCE=false PYTHONPATH=.. uv run --frozen python -m evals.run \
		--mode baseline --out ../evals/reports/$(EVAL_RUN)/eval-baseline

openai-smoke:
	cd backend && uv run --frozen python -m lbf.assistant.smoke
