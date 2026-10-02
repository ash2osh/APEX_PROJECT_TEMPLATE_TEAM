#!/usr/bin/env bash
# Analyze explicitly selected migration folders, locally or against live state.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Python runs as a child, with Ctrl-C caught here, not through exec: on Windows Git Bash
# ends itself at once on Ctrl-C (status 512) while Python is still cleaning up, so the
# prompt would return before Python has finished; a Bash that catches it waits.
run_preflight() {
  cd "$REPO_ROOT"
  trap : INT
  python3 -m scripts.migration_checks "$@"
}

has_environment=false
for argument in "$@"; do
  if [ "$argument" = --local ]; then
    run_preflight "$@"
    exit $?
  elif [ "$argument" = --env ]; then
    has_environment=true
  fi
done

if [ "$has_environment" != true ]; then
  run_preflight "$@"
  exit $?
fi

PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
run_preflight "$@"
