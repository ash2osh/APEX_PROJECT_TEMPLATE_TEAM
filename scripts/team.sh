#!/usr/bin/env bash
# Unified entry point for local APEX team workflows.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/team.sh <command> [arguments]

Commands:
  doctor                                      Validate .env and the DEV SQLcl identity
  export <app_id>                             Export one numeric APEX app from DEV
  publish <app_id> [--env dev] [--force]      Drift-check and import to DEV
  check-conflicts                             Check migrations across developers
  migrate <migration.sql> [...]               Check conflicts, then apply migration(s)
  backup-db                                   Refresh the table and code mirrors
  deploy <app_id> --env <staging|prod> [--manual]
                                              Confirm a promotion or print a DBA runbook
  upgrade-template [--source <url|path>] [--ref <ref>] [--dry-run]
                                              Update template-owned files from the template
  --help                                      Show this help
USAGE
}

fail() { printf 'team error: %s\n' "$*" >&2; exit 2; }

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ -z "${1:-}" ]; then
  usage
  exit 0
fi

command_name="$1"
shift
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

case "$command_name" in
  doctor)
    [ "$#" -eq 0 ] || fail "doctor does not accept arguments"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
    # shellcheck source=load_env.sh
    source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
    PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" read apex
    mkdir -p "$REPO_ROOT/scratch"
    sqlcl_stdin="$(mktemp "$REPO_ROOT/scratch/.doctor-stdin.XXXXXX")"
    cleanup() { rm -f -- "$sqlcl_stdin"; }
    trap cleanup EXIT
    sql -S -noupdates -name "$APEX_SQLCL_CONNECTION" \
      "@$REPO_ROOT/scripts/doctor.sql" \
      "$APEX_PARSING_SCHEMA" "$DB_ENVIRONMENT" "$APEX_EXPECTED_USER" \
      < "$sqlcl_stdin"
    printf 'Doctor checks passed for the configured DEV connection.\n'
    ;;
  export)
    [ "$#" -eq 1 ] || fail "usage: scripts/team.sh export <numeric_app_id>"
    [[ "$1" =~ ^[1-9][0-9]*$ ]] || fail "expected a positive numeric application id"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      "$REPO_ROOT/scripts/export_apps.sh" "$1"
    ;;
  publish)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh publish <numeric_app_id> [--env dev] [--force]"
    publish_arguments=("$@")
    for publish_index in "${!publish_arguments[@]}"; do
      if [ "${publish_arguments[$publish_index]}" = "--env" ]; then
        next_index=$((publish_index + 1))
        [ "$next_index" -lt "${#publish_arguments[@]}" ] || fail "--env requires dev; use deploy for staging or production"
        [ "${publish_arguments[$next_index]}" = dev ] || fail "publish targets DEV only; use deploy for staging or production"
      fi
    done
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      "$REPO_ROOT/scripts/publish_app.sh" "$@"
    ;;
  check-conflicts)
    [ "$#" -eq 0 ] || fail "check-conflicts does not accept arguments"
    python3 "$REPO_ROOT/scripts/check_conflicts.py"
    ;;
  migrate)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh migrate <migrations/<developer>/<file>.sql> [...]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      "$REPO_ROOT/scripts/migrate.sh" "$@"
    ;;
  backup-db)
    [ "$#" -eq 0 ] || fail "backup-db does not accept arguments"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      "$REPO_ROOT/scripts/backup_db.sh"
    ;;
  deploy)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh deploy <numeric_app_id> --env <staging|prod> [--manual]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      "$REPO_ROOT/scripts/deploy.sh" "$@"
    ;;
  upgrade-template)
    if python3 "$REPO_ROOT/scripts/upgrade_template.py" --project-root "$REPO_ROOT" "$@"; then
      upgrade_status=0
    else
      upgrade_status=$?
    fi
    dry_run=false
    for upgrade_argument in "$@"; do [ "$upgrade_argument" != --dry-run ] || dry_run=true; done
    if [ "$upgrade_status" -ne 2 ] && [ "$dry_run" = false ] && [ -f "$REPO_ROOT/.env" ]; then
      if ! env_check="$(bash -c 'source "$1" "$2"' bash "$REPO_ROOT/scripts/load_env.sh" "$REPO_ROOT/.env" 2>&1)"; then
        printf 'warning: .env needs attention after the upgrade:\n%s\n' "$env_check" >&2
      fi
    fi
    exit "$upgrade_status"
    ;;
  *)
    fail "unknown command '$command_name'; use --help to list commands"
    ;;
esac
