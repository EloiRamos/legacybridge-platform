#!/bin/sh
# make restore FILE=deploy/backups/<dump> — restore into a FRESH database volume.
#
# Restoring the database does NOT restore backend state: HALDEN and SKARVIK may have moved on. After the restore the
# runbook requires a reconciliation pass over every non-terminal operation (deploy/post_restore_reconcile.py).
# Destructive by design for the local volume lbf-pgdata, which must belong to this checkout; the dump is verified against its .sha256 first.
set -eu
cd "$(dirname "$0")/.."
FILE="${1:?usage: deploy/restore.sh deploy/backups/<file>.dump}"
[ -f "$FILE" ] || { echo "no such dump: $FILE" >&2; exit 2; }
[ -f "$FILE.sha256" ] && shasum -a 256 -c "$FILE.sha256"
# LBF_NS=<name> works on that isolated stack; ownership is proven either way before anything is stopped or removed.
if [ -n "${LBF_NS:-}" ]; then
  dc() { ./tools/isolated.sh compose "$@"; }
  ./tools/isolated.sh owned
  VOLUME="$LBF_NS-pgdata"
else
  dc() { docker compose "$@"; }
  ./tools/owned_project.sh
  VOLUME=lbf-pgdata
fi
dc stop app
dc rm -sf db
docker volume rm "$VOLUME"
dc up -d --wait db                     # init creates the roles and an empty lbf database
dc exec -T db pg_restore --username postgres --dbname lbf --exit-on-error < "$FILE"
dc up -d --wait                         # migrate (no-op at head) + app; engines start with the app
echo "restored $FILE into a fresh volume; now run the post-restore reconciliation pass"
