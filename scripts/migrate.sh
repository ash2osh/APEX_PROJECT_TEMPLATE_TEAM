#!/usr/bin/env bash
# Explicit environment-targeted migration runner; Python owns validation and apply.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/migrate.sh <migration-folder> [...] --env <dev|staging|prod>

Select one or more migrations/<YYYY-MM-DD_name-rNNN>/ folders in execution
order. Files inside each folder execute in ascending NNN order. Staging and
production require a live preflight and an explicit terminal confirmation.
USAGE
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi
[ "$#" -gt 0 ] || { usage >&2; exit 2; }

environment_count=0
selected_environment=""
arguments=("$@")
for ((argument_index = 0; argument_index < ${#arguments[@]}; argument_index++)); do
  argument="${arguments[$argument_index]}"
  if [ "$argument" = --env ]; then
    environment_count=$((environment_count + 1))
    next_index=$((argument_index + 1))
    if [ "$next_index" -ge "${#arguments[@]}" ]; then
      printf 'migration error: --env requires dev, staging, or prod\n' >&2
      exit 2
    fi
    selected_environment="${arguments[$next_index]}"
  fi
done
if [ "$environment_count" -ne 1 ]; then
  printf 'migration error: specify exactly one --env dev|staging|prod\n' >&2
  exit 2
fi
case "$selected_environment" in
  dev|staging|prod) ;;
  *) printf 'migration error: --env must be dev, staging, or prod\n' >&2; exit 2 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

cd "$REPO_ROOT"
exec python3 -m scripts.migrate "$@"
