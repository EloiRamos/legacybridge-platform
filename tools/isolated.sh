#!/bin/sh
# A disposable, isolated LegacyBridge stack for the demonstration and the public verification.
#   tools/isolated.sh up | check | down | db-up | fresh | url | credentials | logs | db | compose ...
# up      build and start (project $LBF_NS, default lbf-demo), create the seven fictional personas
# check   run the complete demonstration against it and assert every outcome with the simulators' own records
# down    remove exactly the containers, volumes, network and images this checkout created under $LBF_NS
# db-up   start only the database; fresh: fail if anything already exists under $LBF_NS
# compose any Compose command on that stack, after the same ownership check (campaign harness, backup and restore)
# Ownership: the first `up` records a random token (deploy/secrets/isolated-$LBF_NS.owner) and labels every resource
# with it. Before anything is changed or removed, every resource under the name must carry that label; one that does
# not (another checkout, another project, an older stack) stops the command. A name alone is never ownership.
# Never touches the default deployment, its volumes or the OpenAI key; live inference is off.
set -eu
cd "$(dirname "$0")/.."
export LBF_NS="${LBF_NS:-lbf-demo}" LBF_APP_PORT="${LBF_APP_PORT:-8088}" LBF_DB_PORT="${LBF_DB_PORT:-55440}" \
  LBF_HALDEN_PORT="${LBF_HALDEN_PORT:-58190}" LBF_SKARVIK_PORT="${LBF_SKARVIK_PORT:-58195}"
refuse() { echo "refusing: $*" >&2; exit 3; }
case "$LBF_NS" in
  lbf|lbf-internal|legacybridge|legacybridge-*|"") echo "refusing project name '$LBF_NS'" >&2; exit 2;;
  *[!a-z0-9-]*|-*|*-) echo "refusing project name '$LBF_NS' (lower case, digits and dashes)" >&2; exit 2;;
esac
unset OPENAI_API_KEY OPENAI_MODEL LBF_ASSISTANT_PROVIDER || true
if [ "${LBF_CAMPAIGN:-0}" = 1 ]; then
  for d in "${LBF_EXCHANGE_SOURCE:-}" "${LBF_PAUSE_SOURCE:-}"; do
    case "$d" in /*) ;; *) echo "refusing: LBF_CAMPAIGN=1 needs absolute LBF_EXCHANGE_SOURCE and LBF_PAUSE_SOURCE" >&2; exit 2;; esac
  done
  CAMPAIGN="-f deploy/compose.campaign.yaml --profile test"
else
  unset LBF_EXCHANGE_SOURCE LBF_PAUSE_SOURCE || true
  CAMPAIGN=""
fi
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES || true
CREDENTIALS="deploy/secrets/$LBF_NS-credentials.txt"
OWNER_FILE="deploy/secrets/isolated-$LBF_NS.owner"
LABEL="io.legacybridge.isolated.owner"
VOLUMES="pgdata skarvik-exchange skarvik-data wiretap-ledger pause"
IMAGES="app migrate halden skarvik wiretap"
dc() { docker compose -p "$LBF_NS" -f compose.yaml -f deploy/compose.isolated.yaml $CAMPAIGN "$@"; }

# Every resource under the name, one "kind id" per line: by Compose project label and by the fixed names of the overlay.
resources() {
  docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$LBF_NS" > "$TMP.c" || return 1
  docker volume ls -q > "$TMP.va" || return 1
  docker volume ls -q --filter "label=com.docker.compose.project=$LBF_NS" > "$TMP.v" || return 1
  for v in $VOLUMES; do grep -Fx -- "$LBF_NS-$v" "$TMP.va" >> "$TMP.v" || true; done
  docker network ls -q --no-trunc --filter "label=com.docker.compose.project=$LBF_NS" > "$TMP.n" || return 1
  docker network ls --no-trunc --format '{{.ID}} {{.Name}}' > "$TMP.na" || return 1
  awk -v n="$LBF_NS-internal" '$2 == n {print $1}' "$TMP.na" >> "$TMP.n"
  docker image ls --format '{{.Repository}}:{{.Tag}}' > "$TMP.ia" || return 1
  : > "$TMP.i"
  for i in $IMAGES; do grep -Fx -- "$LBF_NS/$i:local" "$TMP.ia" >> "$TMP.i" || true; done
  sort -u "$TMP.c" | sed 's/^/container /'; sort -u "$TMP.v" | sed 's/^/volume /'
  sort -u "$TMP.n" | sed 's/^/network /'; sort -u "$TMP.i" | sed 's/^/image /'
}
label_of() {
  case "$1" in
    container|image) docker "$1" inspect --format "{{index .Config.Labels \"$LABEL\"}}" "$2";;
    *) docker "$1" inspect --format "{{index .Labels \"$LABEL\"}}" "$2";;
  esac
}
# Fails closed: an unreadable inventory, an unreadable label or a foreign label all stop the command.
verify_owned() {
  resources > "$TMP" || refuse "the Docker inventory of '$LBF_NS' could not be read"
  token="$(cat "$OWNER_FILE" 2>/dev/null || true)"
  while read -r kind id; do
    [ -n "$token" ] || refuse "$kind $id exists under '$LBF_NS' but this checkout never created that stack"
    found="$(label_of "$kind" "$id")" || refuse "the owner of $kind $id could not be read"
    [ "$found" = "$token" ] || refuse "$kind $id under '$LBF_NS' belongs to something else; nothing was changed"
  done < "$TMP"
}
claim() {
  verify_owned
  mkdir -p deploy/secrets
  [ -s "$OWNER_FILE" ] || ( umask 077; od -An -N16 -tx1 /dev/urandom | tr -d ' \n' > "$OWNER_FILE" )
  LBF_OWNER="$(cat "$OWNER_FILE")"; export LBF_OWNER
}
TMP="$(mktemp -t lbf-isolated.XXXXXX)"
trap 'rm -f "$TMP" "$TMP".*' EXIT
case "${1:-}" in
  fresh)
    resources > "$TMP" || refuse "the Docker inventory of '$LBF_NS' could not be read"
    [ ! -s "$TMP" ] || refuse "resources already exist under '$LBF_NS'; choose another LBF_NS";;
  db-up)
    claim; ./deploy/make_secrets.sh >/dev/null; dc up -d --wait db;;
  up)
    claim
    ./deploy/make_secrets.sh
    dc up -d --build --wait db
    dc run --rm migrate
    if ! [ -s "$CREDENTIALS" ]; then
      ( umask 077; dc run --rm --no-deps -T app lbf-admin demo-users > "$CREDENTIALS" )
    fi
    dc up -d --build --wait app halden skarvik ${CAMPAIGN:+wiretap}
    echo "ready: http://127.0.0.1:$LBF_APP_PORT (personas: tools/isolated.sh credentials)";;
  check)
    cd backend && PYTHONPATH=../oracles/src uv run --frozen python ../tools/demo_check.py "../$CREDENTIALS";;
  down)
    verify_owned
    # exactly the verified identifiers, in dependency order; no orphan removal, no project-wide teardown
    for kind in container network volume image; do
      awk -v k="$kind" '$1 == k {print $2}' "$TMP" > "$TMP.k"
      while read -r id; do
        case "$kind" in
          container) docker rm -f -v "$id" >/dev/null < /dev/null;;
          image) docker image rm "$id" >/dev/null < /dev/null || true;;
          *) docker "$kind" rm "$id" >/dev/null < /dev/null;;
        esac
      done < "$TMP.k"
    done
    rm -f "$CREDENTIALS" "$OWNER_FILE";;
  url) echo "http://127.0.0.1:$LBF_APP_PORT";;
  credentials) cat "$CREDENTIALS";;
  logs) verify_owned; dc logs --tail=200 app;;
  db) echo "$LBF_DB_PORT";;
  owned) verify_owned; [ -s "$TMP" ] || refuse "nothing exists under '$LBF_NS'";;
  compose)
    shift
    claim
    dc "$@";;
  *) sed -n '2,10p' "$0" >&2; exit 2;;
esac
