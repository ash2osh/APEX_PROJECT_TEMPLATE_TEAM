#!/usr/bin/env bash
# Hash-checked execution of one ordered rollout manifest.
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  exec python3 "$(dirname "${BASH_SOURCE[0]}")/rollout.py" --help
fi

# Dry-run validates only local source and hashes; it needs no configured DB profile.
# A present .env is still loaded the way the real run loads it, so PROD_* and STAGING_*
# reach the frozen-target planning. Without a .env the dry run runs as before.
for rollout_argument in "$@"; do
  if [ "$rollout_argument" = "--dry-run" ]; then
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
    if [ -f "$PROJECT_ENV_FILE" ]; then
      # shellcheck source=load_env.sh
      source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
    fi
    cd "$REPO_ROOT"
    exec python3 "$REPO_ROOT/scripts/rollout.py" "$@"
  fi
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

cd "$REPO_ROOT"
exec python3 "$REPO_ROOT/scripts/rollout.py" "$@"
