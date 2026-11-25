#!/bin/sh
# tools/owned_project.sh: succeed only if the default deployment's Docker resources belong to this checkout.
# Compose project names are not ownership: a second checkout has the same project and volume names. Every container of
# the project must have been started from this directory, and every named volume must carry the project's label and be
# used by no container outside the project. Read-only; run before a command that stops or removes anything.
set -eu
cd "$(dirname "$0")/.."
HERE="$(pwd -P)"
PROJECT=legacybridge-platform
refuse() { echo "refusing: $*; nothing was changed" >&2; exit 3; }
[ -z "${COMPOSE_PROJECT_NAME:-}${COMPOSE_FILE:-}" ] || refuse "COMPOSE_PROJECT_NAME or COMPOSE_FILE is set"
ids="$(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$PROJECT")" || refuse "Docker cannot be read"
for id in $ids; do
  dir="$(docker container inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id")" \
    || refuse "container $id cannot be read"
  [ "$dir" = "$HERE" ] || refuse "container $id was started from another checkout"
done
all="$(docker volume ls -q)" || refuse "Docker volumes cannot be read"
for v in lbf-pgdata lbf-skarvik-exchange lbf-skarvik-data lbf-wiretap-ledger lbf-pause; do
  echo "$all" | grep -Fxq -- "$v" || continue
  label="$(docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$v")" \
    || refuse "volume $v cannot be read"
  [ "$label" = "$PROJECT" ] || refuse "volume $v does not belong to the project"
  [ -n "$ids" ] || refuse "no container of this checkout proves that volume $v is its own"
  for user in $(docker ps -aq --no-trunc --filter "volume=$v"); do
    echo "$ids" | grep -Fxq -- "$user" || refuse "volume $v is used by container $user outside the project"
  done
done
