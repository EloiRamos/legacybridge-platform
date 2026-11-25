#!/bin/sh
# Public verification: what a reader can check from this repository alone, without private material or paid calls.
#
#   tools/verify.sh            everything below (about 25 minutes the first time)
#   tools/verify.sh static     documents, frozen imports, contract drift, lint, types, console unit tests
#   tools/verify.sh tests      backend and SKARVIK suites, scripted assistant evaluation (isolated database)
#   tools/verify.sh demo       the demonstration on the isolated stack, checked against the simulators' own records
#
# Needs a Git checkout (two backend tests read Git metadata; a source archive fails them), Docker with Compose v2.24+,
# uv 0.13.4 or later and Node 24, and network access for public images and packages. Uses its own Compose project
# ($LBF_NS, default a new lbf-verify-<random> per run), refuses to start if anything already exists under that name, and
# removes exactly what it created (KEEP=1 keeps it). It never touches the default deployment, never
# reads or needs an OpenAI key, and disables live inference. Keep 10 GiB of host storage free. The resilience campaign, backup and restore, and live
# model runs belong to the full acceptance campaign and are NOT part of this command (docs/evidence/VERIFICATION.md).
set -eu
cd "$(dirname "$0")/.."
export LBF_NS="${LBF_NS:-lbf-verify-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')}" LBF_APP_PORT="${LBF_APP_PORT:-8089}" LBF_DB_PORT="${LBF_DB_PORT:-55441}" \
  LBF_HALDEN_PORT="${LBF_HALDEN_PORT:-58290}" LBF_SKARVIK_PORT="${LBF_SKARVIK_PORT:-58295}"
export LBF_LIVE_INFERENCE=false LBF_TEST_DB_PORT="$LBF_DB_PORT"
unset OPENAI_API_KEY OPENAI_API_KEY_FILE OPENAI_MODEL LBF_ASSISTANT_PROVIDER || true
PART="${1:-all}"
FAILED=0
step() { # step <label> <command...>
  label="$1"; shift
  log="$(mktemp -t lbf-verify.XXXXXX)"
  if ( "$@" ) > "$log" 2>&1; then echo "PASS  $label"; else
    echo "FAIL  $label (log: $log)"; tail -15 "$log" | sed 's/^/      /'; FAILED=1; fi
}
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 2; }; }
need docker; need uv; need node; need npm
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "this command needs a Git checkout" >&2; exit 2; }
STARTED=0
start() { [ "$STARTED" = 1 ] || { ./tools/isolated.sh fresh || exit 3; STARTED=1; }; }
cleanup() {
  [ "$STARTED" = 1 ] || return 0
  if [ "${KEEP:-0}" = 1 ]; then echo "kept: LBF_NS=$LBF_NS (remove with LBF_NS=$LBF_NS tools/isolated.sh down)"; return 0; fi
  ./tools/isolated.sh down || echo "not removed: LBF_NS=$LBF_NS" >&2
}

static() {
  step "document links" python3 tools/check_links.py README.md docs
  step "claims and fictional labels" ./tools/claims_audit.sh --labels
  step "screenshot hashes" shasum -a 256 -c docs/portfolio/screenshots/SCREENSHOTS.sha256
  step "frozen legacy imports" python3 tools/verify_imports.py
  step "backend lint" sh -c 'cd backend && uv run --frozen ruff check src tests ../resilience ../evals'
  step "backend types" sh -c 'cd backend && uv run --frozen mypy'
  step "API contract equals the application" sh -c \
    'cd backend && uv run --frozen python -m lbf.api.openapi "${TMPDIR:-/tmp}/lbf-openapi.json" && cmp "${TMPDIR:-/tmp}/lbf-openapi.json" ../docs/api/openapi.json'
  step "console dependencies" sh -c 'cd frontend && npm ci --no-audit --no-fund --ignore-scripts'
  step "console types, lint, unit tests, build" sh -c \
    'cd frontend && npm run -s typecheck && npm run -s lint && npm run -s test && npm run -s build'
}
tests() {
  start
  step "isolated database" ./tools/isolated.sh db-up
  step "backend suite (without live simulators)" sh -c \
    "cd backend && uv run --frozen pytest -q -p no:cacheprovider -m 'not live' --ignore=tests/security/test_compose.py"
  step "SKARVIK simulator suite" sh -c 'cd legacy-systems/skarvik-oes && uv run --frozen pytest -q -p no:cacheprovider'
  step "assistant evaluation, scripted" sh -c \
    'cd backend && PYTHONPATH=.. uv run --frozen python -m evals.run --mode scripted --out "$(mktemp -d)"'
}
demo() {
  start
  step "isolated stack" ./tools/isolated.sh up
  step "demonstration, checked against the simulators" ./tools/isolated.sh check
}

trap cleanup EXIT
case "$PART" in
  static) static;;
  tests) tests;;
  demo) demo;;
  all) static; tests; demo;;
  *) sed -n '2,8p' "$0" >&2; exit 2;;
esac
[ "$FAILED" = 0 ] && echo "verification: all executed checks passed" || { echo "verification: FAILED"; exit 1; }
