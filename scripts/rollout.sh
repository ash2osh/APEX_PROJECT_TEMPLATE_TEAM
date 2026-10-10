#!/usr/bin/env bash
# Hash-checked execution of one ordered rollout manifest.
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  exec python3 "$(dirname "${BASH_SOURCE[0]}")/rollout.py" --help
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

cd "$REPO_ROOT"
exec python3 "$REPO_ROOT/scripts/rollout.py" "$@"
