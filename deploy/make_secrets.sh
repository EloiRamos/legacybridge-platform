#!/bin/sh
# Generate local secrets (git-ignored). Existing files are kept, so re-running is safe.
# The OpenAI key file is created EMPTY; the owner places a key manually if live inference is ever authorized.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)/secrets"
mkdir -p "$DIR"
chmod 700 "$DIR"
for name in db_superuser db_migrator db_app db_audit_reader halden_secret; do
  f="$DIR/$name.txt"
  if [ ! -s "$f" ]; then
    # hex only: safe inside SQL literals and env files
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n' > "$f"
    chmod 600 "$f"
    echo "created $f"
  fi
done
if [ ! -e "$DIR/openai_api_key.txt" ]; then
  : > "$DIR/openai_api_key.txt"
  chmod 600 "$DIR/openai_api_key.txt"
  echo "created empty $DIR/openai_api_key.txt"
fi
