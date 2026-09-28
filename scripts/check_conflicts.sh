#!/usr/bin/env bash
# Analyze explicitly selected migration folders, locally or against live state.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
has_environment=false
for argument in "$@"; do
  if [ "$argument" = --local ]; then
    cd "$REPO_ROOT"
    exec python3 -m scripts.migration_checks "$@"
  elif [ "$argument" = --env ]; then
    has_environment=true
  fi
done

if [ "$has_environment" != true ]; then
  cd "$REPO_ROOT"
  exec python3 -m scripts.migration_checks "$@"
fi

PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
cd "$REPO_ROOT"
exec python3 -m scripts.migration_checks "$@"
