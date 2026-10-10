#!/usr/bin/env bash
# Unified entry point for local APEX team workflows.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/team.sh <command> [arguments]

Commands:
  doctor                                      Validate .env and the DEV SQLcl identities (ORDS too, when configured)
  export <app_id>                             Export one numeric APEX app from DEV
  upgrade-apexlang <app_id> --env dev --mode builder|files
                                              Convert source explicitly to canonical APEX 26.2
  publish <app_id> [--env dev] [--force]      Drift-check and import to DEV
  publish <app_id> --file pages/<file>.apx [--file ...] [--no-team-notice]
                                              Verify selected existing DEV pages and synchronize source
  app-lock <app_id> [--env dev] [--comment <text>]
                                              Acquire a native DEV application lock
  app-unlock <app_id> [--env dev]             Release only your DEV application lock
  check-conflicts <folder> [...] (--env <env>|--local)
                                              Preflight selected migrations against local/live scope
  migrate <folder> [...] --env dev|staging|prod [--verbose] [--rehearse] [--report <file>]
                                              Apply, or rehearse DML in one transaction and roll it back
  revise <folder> [--reason TEXT]             Copy to the next migration revision
  revise --check <folder>                     Report whether local attempt/receipt evidence locks a folder
  verify <folder> [...] --env <env> [--phase pre|post|both] [--only-failed]
         [--format text|json] [--jobs N]     Evaluate migration checks read-only
  rollout <manifest.json> --env <env> [--from-step N] [--dry-run] [--report <file>]
                                              Run an ordered, hash-checked deployment manifest
  compare-schema [--from <env>] (--to <env>|--env <env>)
                (--object <name>|--pattern <glob>) [...] [--format text|json]
                                              Compare selected live schema objects read-only
  compare-env --from <env> --to <env> [--section <name>] [...] [--format text|json|markdown]
              [--emit-dba-script <file>]
                                              Compare complete environment catalogs by name, read-only
  baseline export-source --from <env> [--scratch <dir>]
                                              Export configured exact stored source and settings
  baseline export-grants --from <env> [--scratch <dir>]
                                              Export configured object and system grants
  baseline build --to <env> [--from <env>]    Build structure, grants, and exact-source migrations
  backup-db                                   Refresh the table and code mirrors (and ORDS, when configured)
  backup-ords                                 Export ORDS metadata read-only to database/<SCHEMA>/ords/schema.sql
  deploy <app_id> --env <staging|prod> [--manual]
                                              Confirm a promotion or print a DBA runbook
  upgrade-template [--source <url|path>] [--ref <ref>] [--apex-release 26.1|26.2] [--dry-run]
                                              Update template-owned files from the template
  verify-local [--format text|json] [--skills-root <path>] [...] [--live]
                                              Validate local environment, lock, apps and skills read-only
Options:
  --schema <NAME>                             Run one configured schema (any command except upgrade-template, verify-local)
  --help                                      Show this help
Environment:
  MIGRATION_PREFLIGHT_INVENTORY_RETRIES        Retry changing live catalogs (default 3)
  <ENV>_DBA_SQLCL_CONNECTION                   Read-only compare-env and baseline grant-export connection
USAGE
}

fail() { printf 'team error: %s\n' "$*" >&2; exit 2; }

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ -z "${1:-}" ]; then
  usage
  exit 0
fi

command_name="$1"
shift
# --schema NAME is the one selection channel: strip it and export PROJECT_SCHEMA
# so every child script's loader narrows to that schema.
if [ "$command_name" != upgrade-template ] && [ "$command_name" != verify-local ]; then
  schema_filtered=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --schema)
        # An empty name would select nothing, which means every schema.
        [ "$#" -ge 2 ] && [ -n "$2" ] || fail "--schema requires a schema name"
        export PROJECT_SCHEMA="$2"
        shift 2
        ;;
      --schema=*)
        [ -n "${1#--schema=}" ] || fail "--schema requires a schema name"
        export PROJECT_SCHEMA="${1#--schema=}"
        shift
        ;;
      *)
        schema_filtered+=("$1")
        shift
        ;;
    esac
  done
  set -- ${schema_filtered[@]+"${schema_filtered[@]}"}
  if [ -n "${PROJECT_SCHEMA:-}" ]; then
    schema_pattern='^[A-Z][A-Z0-9_$#]{0,127}$'
    # C locale: in en_US.UTF-8 [A-Z] also matches accented capitals.
    ( LC_ALL=C; [[ "$PROJECT_SCHEMA" =~ $schema_pattern ]] ) || fail "--schema must be an uppercase Oracle identifier"
  fi
fi
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Each helper takes over this process (exec), so a signal sent to team.sh alone
# (timeout, kill, a supervisor) reaches the helper instead of orphaning it.
case "$command_name" in
  doctor)
    [ "$#" -eq 0 ] || fail "doctor does not accept arguments"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
    # shellcheck source=load_env.sh
    source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
    # shellcheck source=sqlcl_safe.sh
    source "$REPO_ROOT/scripts/sqlcl_safe.sh"
    mkdir -p "$REPO_ROOT/scratch"

    # Ctrl-C or SIGTERM while SQLcl runs must not leave its working directory behind.
    doctor_workdir=""
    trap '[ -z "$doctor_workdir" ] || rm -rf -- "$doctor_workdir"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP

    doctor_one() {
      # Never let SQLcl start in the caller's directory: SQLcl executes a
      # login.sql found there before the doctor script. The optional fourth
      # argument names the script (doctor_ords.sql for the ORDS profile).
      local connection="$1" expected_user="$2" schema="$3" doctor_script="${4:-doctor.sql}" doctor_profile="${5:-identity}" workdir stdin output status=0 username_hex="-"
      workdir="$(mktemp -d "$REPO_ROOT/scratch/sqlcl-doctor.XXXXXX")"
      doctor_workdir="$workdir"
      if [ "$doctor_profile" = apex ]; then
        if [ -z "${APEX_WORKSPACE_USERNAME:-}" ]; then
          printf 'team error: set APEX_WORKSPACE_USERNAME to the existing Builder developer/admin login\n' >&2
          rm -rf -- "$workdir"; doctor_workdir=""; return 2
        fi
        if ! username_hex="$(python3 -c 'import sys; v=sys.argv[1]; sys.exit("APEX_WORKSPACE_USERNAME must be nonempty text without control characters (at most 255 UTF-8 bytes)") if not v.strip() or any(ord(c)<32 for c in v) or len(v.encode("utf-8"))>255 else print(v.encode("utf-8").hex().upper())' "$APEX_WORKSPACE_USERNAME")"; then
          rm -rf -- "$workdir"; doctor_workdir=""; return 2
        fi
      fi
      if [ "$doctor_profile" = apex ] && ! sqlcl_require_apex_version "$workdir"; then
        rm -rf -- "$workdir"
        doctor_workdir=""
        return 2
      fi
      stdin="$workdir/.sqlcl-stdin"
      : > "$stdin"
      output="$workdir/sqlcl-output.log"
      if ! invoke_sqlcl_safe "$workdir" \
        -S -noupdates -name "$connection" \
        "@$REPO_ROOT/scripts/$doctor_script" \
        "$schema" "$DB_ENVIRONMENT" "$expected_user" "$doctor_profile" "$username_hex" "$APEX_APP_ID" \
        < "$stdin" > "$output" 2>&1; then
        cat "$output" >&2
        printf 'team error: SQLcl doctor check failed for schema %s (connection %s)\n' "$schema" "$connection" >&2
        status=1
      else
        cat "$output"
        if ! grep -Fxq "APEX_DOCTOR_VERIFIED:$expected_user" "$output"; then
          printf 'team error: SQLcl did not verify the doctor script for schema %s; the result is unknown\n' "$schema" >&2
          status=1
        fi
        if [ "$doctor_profile" = apex ] && ! grep -Fxq 'APEX_RELEASE_VERIFIED:26.2' "$output"; then
          printf 'team error: APEX release was not verified; the result is unknown\n' >&2
          status=1
        fi
        if [ "$doctor_profile" = apex ] && ! grep -Fxq 'APEX_WORKSPACE_USERS_VERIFIED' "$output"; then
          printf 'team error: workspace developer validation was not verified; the result is unknown\n' >&2
          status=1
        fi
      fi
      rm -rf -- "$workdir"
      doctor_workdir=""
      return "$status"
    }

    doctor_seen="|"
    doctor_total=0
    doctor_failed=0
    # ORDS has its own floor; APEX checks its qualified floor separately.
    doctor_ords_sqlcl_ok=true
    if [ -n "$ORDS_SCHEMA" ]; then
      doctor_workdir="$(mktemp -d "$REPO_ROOT/scratch/sqlcl-doctor.XXXXXX")"
      sqlcl_require_ords_version "$doctor_workdir" || doctor_ords_sqlcl_ok=false
      rm -rf -- "$doctor_workdir"
      doctor_workdir=""
    fi
    for doctor_profile in apex tables code migration ords; do
      doctor_script=doctor.sql
      case "$doctor_profile" in
        apex)   doctor_schemas="$APEX_PARSING_SCHEMA"; doctor_connections="$APEX_SQLCL_CONNECTION"; doctor_users="$APEX_EXPECTED_USER" ;;
        tables) doctor_schemas="$TABLES_SCHEMA"; doctor_connections="$TABLES_SQLCL_CONNECTION"; doctor_users="$TABLES_EXPECTED_USER" ;;
        code)   doctor_schemas="$CODE_SCHEMA"; doctor_connections="$CODE_SQLCL_CONNECTION"; doctor_users="$CODE_EXPECTED_USER" ;;
        migration) doctor_schemas="${MIGRATION_SCHEMA:-}"; doctor_connections="${MIGRATION_SQLCL_CONNECTION:-}"; doctor_users="${MIGRATION_EXPECTED_USER:-}" ;;
        ords)   doctor_schemas="$ORDS_SCHEMA"; doctor_connections="$ORDS_SQLCL_CONNECTION"; doctor_users="$ORDS_EXPECTED_USER"; doctor_script=doctor_ords.sql ;;
      esac
      [ -n "$doctor_schemas" ] || continue
      IFS=',' read -r -a doctor_schema_list <<< "$doctor_schemas"
      IFS=',' read -r -a doctor_connection_list <<< "$doctor_connections"
      IFS=',' read -r -a doctor_user_list <<< "$doctor_users"
      for ((doctor_index = 0; doctor_index < ${#doctor_schema_list[@]}; doctor_index++)); do
        doctor_key="${doctor_connection_list[$doctor_index]}|${doctor_user_list[$doctor_index]}|${doctor_schema_list[$doctor_index]}"
        # The ORDS check is stricter, so another profile's check never stands in for it.
        [ "$doctor_profile" != ords ] || doctor_key="ords|$doctor_key"
        case "$doctor_seen" in *"|$doctor_key|"*) continue ;; esac
        doctor_seen="$doctor_seen$doctor_key|"
        doctor_total=$((doctor_total + 1))
        PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" read "$doctor_profile" \
          "${doctor_schema_list[$doctor_index]}"
        if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
          printf 'Doctor: schema %s via connection %s as %s\n' \
            "${doctor_schema_list[$doctor_index]}" "${doctor_connection_list[$doctor_index]}" "${doctor_user_list[$doctor_index]}"
        fi
        if [ "$doctor_profile" = ords ] && [ "$doctor_ords_sqlcl_ok" != true ]; then
          doctor_failed=$((doctor_failed + 1))
          continue
        fi
        doctor_one "${doctor_connection_list[$doctor_index]}" "${doctor_user_list[$doctor_index]}" \
          "${doctor_schema_list[$doctor_index]}" "$doctor_script" "$doctor_profile" || doctor_failed=$((doctor_failed + 1))
      done
    done
    [ "$doctor_total" -gt 0 ] || fail "no configured profile lists schema ${PROJECT_SCHEMA:-?}"
    [ "$doctor_failed" -eq 0 ] || fail "$doctor_failed of $doctor_total doctor check(s) failed"
    if [ "$doctor_total" -eq 1 ]; then
      printf 'Doctor checks passed for the configured DEV connection.\n'
    else
      printf 'Doctor checks passed for all %s configured DEV schema connections.\n' "$doctor_total"
    fi
    ;;
  export)
    [ "$#" -eq 1 ] || fail "usage: scripts/team.sh export <numeric_app_id>"
    [[ "$1" =~ ^[1-9][0-9]{0,17}$ ]] || fail "expected a positive numeric application id of at most 18 digits"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/export_apps.sh" "$1"
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
      exec "$REPO_ROOT/scripts/publish_app.sh" "$@"
    ;;
  app-lock|app-unlock)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh $command_name <numeric_app_id> [--env dev]"
    lock_operation=acquire
    [ "$command_name" != app-unlock ] || lock_operation=unlock
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/app_lock.sh" "$lock_operation" "$@"
    ;;
  upgrade-apexlang)
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/upgrade_apexlang.sh" "$@"
    ;;
  check-conflicts)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh check-conflicts <migration-folder> [...] (--env dev|staging|prod | --local)"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/check_conflicts.sh" "$@"
    ;;
  migrate)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh migrate <migration-folder> [...] --env dev|staging|prod [--rehearse] [--report <file>]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/migrate.sh" "$@"
    ;;
  revise)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh revise <folder> [--reason TEXT] | revise --check <folder>"
    cd "$REPO_ROOT"
    exec python3 -m scripts.migration_revision "$@"
    ;;
  verify)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh verify <migration-folder> [...] --env <env> [--phase pre|post|both] [--only-failed] [--format text|json] [--jobs N]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/verify_checks.sh" "$@"
    ;;
  rollout)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh rollout <manifest.json> --env <env> [--from-step N] [--dry-run] [--report <file>]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/rollout.sh" "$@"
    ;;
  compare-schema)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh compare-schema [--from <env>] (--to <env>|--env <env>) (--object <name>|--pattern <glob>) [...]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/compare_schema.sh" "$@"
    ;;
  compare-env)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh compare-env --from <env> --to <env> [--section <name>] [...] [--format text|json|markdown] [--emit-dba-script <file>]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/compare_schema.sh" compare-env "$@"
    ;;
  baseline)
    if [ "$#" -eq 0 ]; then
      fail "usage: scripts/team.sh baseline <export-source|export-grants|build> [options]"
    fi
    if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
      cd "$REPO_ROOT"
      exec python3 -m scripts.baseline "$@"
    fi
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
    # shellcheck source=load_env.sh
    source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
    cd "$REPO_ROOT"
    exec python3 -m scripts.baseline "$@"
    ;;
  backup-db)
    [ "$#" -eq 0 ] || fail "backup-db does not accept arguments"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/backup_db.sh"
    ;;
  backup-ords)
    [ "$#" -eq 0 ] || fail "backup-ords does not accept arguments"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/backup_db.sh" --ords-only
    ;;
  deploy)
    [ "$#" -ge 1 ] || fail "usage: scripts/team.sh deploy <numeric_app_id> --env <staging|prod> [--manual]"
    PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" \
      exec "$REPO_ROOT/scripts/deploy.sh" "$@"
    ;;
  verify-local)
    verify_script="$REPO_ROOT/scripts/verify_local.py"
    verify_root="$REPO_ROOT"
    case "$(uname -s 2>/dev/null)" in
      MINGW*|MSYS*|CYGWIN*)
        if command -v cygpath >/dev/null 2>&1; then
          verify_script="$(cygpath -m "$verify_script")"
          verify_root="$(cygpath -m "$REPO_ROOT")"
        fi
        ;;
    esac

    exec python3 "$verify_script" --repo-root "$verify_root" "$@"
    ;;



  upgrade-template)
    # A native Python cannot open /c/... paths. Git Bash rewrites them for it only when
    # they hold no glob characters such as [1], so convert them here.
    upgrade_script="$REPO_ROOT/scripts/upgrade_template.py"
    upgrade_root="$REPO_ROOT"
    case "$(uname -s 2>/dev/null)" in
      MINGW*|MSYS*|CYGWIN*)
        if command -v cygpath >/dev/null 2>&1; then
          upgrade_script="$(cygpath -m "$upgrade_script")"
          upgrade_root="$(cygpath -m "$REPO_ROOT")"
        fi
        ;;
    esac
    if python3 "$upgrade_script" --project-root "$upgrade_root" "$@"; then
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
