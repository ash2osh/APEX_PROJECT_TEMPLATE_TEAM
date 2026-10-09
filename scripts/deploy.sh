#!/usr/bin/env bash
# Confirmed staging/production promotion or a no-connection DBA runbook.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/deploy.sh <app_id> --env <staging|prod> [--manual]

Direct deployment shows the selected workspace and schema, then requires
interactive confirmation. --manual prints SQLcl instructions without connecting.
USAGE
}

fail() { printf 'deploy error: %s\n' "$*" >&2; exit 2; }

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

app_id="${1:-}"
[[ "$app_id" =~ ^[1-9][0-9]{0,17}$ ]] || fail "expected a positive numeric application id of at most 18 digits"
shift
app_environment=""
manual=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env)
      [ "$#" -ge 2 ] || fail "--env requires staging or prod"
      app_environment="$2"
      shift 2
      ;;
    --manual)
      manual=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *) fail "unknown option: $1" ;;
  esac
done
case "$app_environment" in
  staging|prod) ;;
  *) fail "select --env staging or --env prod" ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

description_file="$(mktemp "${TMPDIR:-/tmp}/apex-deploy-description.XXXXXX")"
cleanup() { rm -f -- "$description_file"; }
trap cleanup EXIT
PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/publish_app.sh" \
  "$app_id" --env "$app_environment" --describe > "$description_file"
mapfile -t description < "$description_file"
[ "${#description[@]}" -eq 6 ] || fail "could not read the application deployment descriptor"
app_dir="${description[0]}"
workspace_name="${description[1]}"
parsing_schema="${description[2]}"
sqlcl_connection="${description[3]}"
expected_user="${description[4]}"
target_environment="${description[5]}"

if [ "$manual" != true ] && { [ -z "$sqlcl_connection" ] || [ -z "$expected_user" ]; }; then
  if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
    fail "schema $parsing_schema is not listed in ${app_environment^^}_SCHEMA, so it cannot be deployed to $app_environment"
  fi
  if [ "$app_environment" = staging ]; then
    fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to deploy to staging"
  fi
  fail "set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to deploy to production"
fi

printf 'Application: %s\nWorkspace: %s\nTarget Schema: %s\nConnection: %s\n' \
  "$app_id" "$workspace_name" "$parsing_schema" "${sqlcl_connection:-<not configured>}"

if [ "$manual" = true ]; then
  case "$app_environment" in
    staging)
      sqlcl_connection="${sqlcl_connection:-YOUR_STAGING_SQLCL_CONNECTION}"
      expected_user="${expected_user:-YOUR_STAGING_EXPECTED_USER}"
      ;;
    prod)
      sqlcl_connection="${sqlcl_connection:-YOUR_PROD_SQLCL_CONNECTION}"
      expected_user="${expected_user:-YOUR_PROD_EXPECTED_USER}"
      ;;
  esac
  # On Windows the steps below are for Git Bash, and SQLcl is a native program:
  # print C:/... paths, which Git Bash and SQLcl both accept, not /c/... ones.
  runbook_root="$REPO_ROOT"
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      if command -v cygpath >/dev/null 2>&1; then
        runbook_root="$(cygpath -m "$REPO_ROOT")"
        app_dir="$(cygpath -m "$app_dir")"
      fi
      ;;
  esac
  printf '\nDBA runbook (this command did not connect to a database):\n'
  if [ "$runbook_root" != "$REPO_ROOT" ]; then
    printf '%s\n' 'These steps use Bash syntax: run them in Git Bash. Paths are written as C:/...'
  fi
  printf '1. Review the committed descriptor: %s\n' "$app_dir/deployments/$app_environment.json"
  printf '   python3 %q %q %q --for-import || exit 1\n' \
    "$runbook_root/scripts/validate_app_source.py" "$runbook_root" "$app_dir"
  printf '%s\n' \
    '2. From a shell with SQLcl and the approved connection configured, run:' \
    '   set -euo pipefail' \
    '   deployment_verified=false' \
    '   sqlcl_dir=$(mktemp -d "${TMPDIR:-/tmp}/apex-sqlcl-XXXXXX")' \
    '   trap '\''if [ "$deployment_verified" = true ]; then rm -rf -- "$sqlcl_dir"; else echo "Retained recovery: $sqlcl_dir" >&2; fi'\'' EXIT' \
    '   cd "$sqlcl_dir"' \
    '   export SQLPATH="$sqlcl_dir" ORACLE_PATH="$sqlcl_dir"'
  printf '   python3 %q capture --connection %q --expected-user %q --schema %q --workspace %q --app-id %q --environment %q --run-dir "$sqlcl_dir/lifecycle-before"\n' \
    "$runbook_root/scripts/application_lifecycle.py" "$sqlcl_connection" "$expected_user" "$parsing_schema" "$workspace_name" "$app_id" "$app_environment"
  printf '   sql -S -noupdates -name %q %q %q %q %q %q %q %q %q %q\n' \
    "$sqlcl_connection" "@$runbook_root/scripts/publish_app.sql" "$parsing_schema" \
    "$target_environment" "$expected_user" "$app_dir" \
    "$app_dir/deployments/$app_environment.json" "$app_id" - "$runbook_root/scripts/no_application_lock.sql"
  verify_parent="apps/$parsing_schema"
  printf '3. Check that import completed without SQLcl errors and printed "Import successful." and APEX_IMPORT_VERIFIED:%s.\n' "$app_id"
  printf '4. Re-export from the same target to a fresh temporary directory and verify exact APEXlang source bytes:\n'
  printf '   verify_dir=$(mktemp -d "${TMPDIR:-/tmp}/apex-manual-verify.XXXXXX")\n'
  printf '%s\n' '   trap '\''if [ "$deployment_verified" = true ]; then rm -rf -- "$sqlcl_dir" "$verify_dir"; else echo "Retained recovery: $sqlcl_dir $verify_dir" >&2; fi'\'' EXIT'
  printf '   cd "$verify_dir"\n'
  printf '   sql -S -noupdates -name %q %q %q %q %q %q\n' \
    "$sqlcl_connection" "@$runbook_root/scripts/export_apps.sql" \
    "$parsing_schema" "$app_id" "$target_environment" "$expected_user"
  printf '   shopt -s nullglob\n'
  printf '   export_dirs=(%q/*/)\n' "$verify_parent"
  printf '%s\n' '   test "${#export_dirs[@]}" -eq 1 || { echo "expected exactly one APEX export" >&2; exit 1; }'
  printf '%s\n' '   exported_dir="${export_dirs[0]%/}"'
  printf '   %q "$exported_dir"\n' "$runbook_root/scripts/normalize_apx.sh"
  printf '   python3 %q %q %q "$exported_dir" .apex-export-before.txt .apex-export-after.txt --repo-root %q --deployment-file %q --deployment-state .apex-deployment-state.json\n' \
    "$runbook_root/scripts/verify_publish_state.py" "$app_id" "$app_dir" "$runbook_root" "$app_dir/deployments/$app_environment.json"
  printf '   # Source check must print APEX_PUBLISH_SOURCE_VERIFIED:%s.\n' "$app_id"
  printf '5. Verify lifecycle independently; failures retain recovery diagnostics:\n'
  printf '   python3 %q verify --connection %q --expected-user %q --schema %q --workspace %q --app-id %q --environment %q --before "$sqlcl_dir/lifecycle-before/lifecycle-snapshot.json" --run-dir "$sqlcl_dir/lifecycle-after" --summary-path "$sqlcl_dir/publish-verification.json"\n' \
    "$runbook_root/scripts/application_lifecycle.py" "$sqlcl_connection" "$expected_user" "$parsing_schema" "$workspace_name" "$app_id" "$app_environment"
  printf '   deployment_verified=true\n'
  printf '   echo "Source and application lifecycle verified."\n'

  exit 0
fi

PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/publish_app.sh" \
  "$app_id" --env "$app_environment"
