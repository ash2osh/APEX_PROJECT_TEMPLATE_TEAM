#!/usr/bin/env bash
# Explicit environment-targeted migration runner; Python owns validation and apply.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/migrate.sh <migration-folder> [...] --env <dev|staging|prod> [--verbose] [--rehearse] [--report <file>]

Select one or more migrations/<YYYY-MM-DD_name-rNNN>/ folders in execution
order. Files inside each folder execute in ascending NNN order. Staging and
production require a live preflight and an explicit terminal confirmation.
Use --verbose to print every failed postcondition when fresh verification fails.
Use --rehearse to run transaction-safe DML, check its postconditions, then roll it back.
DDL and any file the analyzer cannot prove transaction-safe are listed as not rehearsable.
--report writes the rehearsal result as JSON and is valid only with --rehearse.
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
# exec, except on Windows where Python is a child with Ctrl-C caught: see check_conflicts.sh.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    trap : INT
    # Git Bash reports 130 for a native child that ends within a moment of a Ctrl-C,
    # whatever status it chose, and an interrupted apply chooses 2 ("may be partially
    # applied") at once. Python also writes its status to this file.
    mkdir -p "$REPO_ROOT/scratch"
    status_file="$(mktemp "$REPO_ROOT/scratch/migrate-status.XXXXXX")"
    status=0
    MIGRATE_STATUS_FILE="$(cygpath -w "$status_file" 2>/dev/null || printf '%s' "$status_file")" \
      python3 -m scripts.migrate "$@" || status=$?
    if [ "$status" -gt 128 ] && [ -s "$status_file" ]; then
      recorded="$(tr -d '\r\n' < "$status_file")"
      case "$recorded" in
        ''|*[!0-9]*) ;;
        *) status="$recorded" ;;
      esac
    fi
    rm -f -- "$status_file"
    exit "$status"
    ;;
  *)
    exec python3 -m scripts.migrate "$@"
    ;;
esac
