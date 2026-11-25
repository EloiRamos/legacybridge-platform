#!/bin/sh
# Deploy-side adapter for the frozen HALDEN simulator. The simulator reads its single
# service credential only from the HALDEN_SECRET environment variable (HaldenConfig.java, frozen) and falls back to a
# built-in demo value when it is unset. This wrapper exports the credential from the mounted Docker secret and refuses
# to start when the secret is missing or empty, so the built-in fallback can never be used. The value is never printed.
set -eu
SECRET_FILE="${HALDEN_SECRET_FILE:-/run/secrets/halden_secret}"
if [ ! -r "$SECRET_FILE" ]; then
  echo "halden: secret file $SECRET_FILE is missing or unreadable; refusing to start" >&2
  exit 78
fi
HALDEN_SECRET="$(tr -d '\r\n' < "$SECRET_FILE")"
if [ -z "$HALDEN_SECRET" ]; then
  echo "halden: secret file $SECRET_FILE is empty; refusing to start" >&2
  exit 78
fi
export HALDEN_SECRET
exec "$@"
