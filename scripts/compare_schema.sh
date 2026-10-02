#!/usr/bin/env bash
# Read-only selected schema comparison.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

cd "$REPO_ROOT"
# A child with Ctrl-C caught, not exec: see check_conflicts.sh (Ctrl-C on Windows).
trap : INT
python3 -m scripts.compare_schema "$@"
