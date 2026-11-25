#!/bin/sh
# make backup — pg_dump (custom format) of the application database into deploy/backups/ (git-ignored).
set -eu
cd "$(dirname "$0")/.."
# LBF_NS=<name> works on that isolated stack (tools/isolated.sh), otherwise on the default deployment.
dc() { if [ -n "${LBF_NS:-}" ]; then ./tools/isolated.sh compose "$@"; else docker compose "$@"; fi; }
mkdir -p deploy/backups
chmod 700 deploy/backups
file="deploy/backups/lbf-$(date -u +%Y%m%dT%H%M%SZ).dump"
( umask 077; dc exec -T db pg_dump --username postgres --format=custom --dbname lbf > "$file" )
shasum -a 256 "$file" > "$file.sha256"
echo "$file"
