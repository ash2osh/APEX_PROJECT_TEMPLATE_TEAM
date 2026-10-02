#!/usr/bin/env bash
# Export the configured APEX applications as APEXlang mirrors.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=sqlcl_safe.sh
source "$REPO_ROOT/scripts/sqlcl_safe.sh"

split_csv() {
  # split_csv <array-name> <value>: an empty value is an empty array.
  local -n split_out="$1"
  split_out=()
  [ -z "$2" ] || IFS=',' read -r -a split_out <<< "$2"
  return 0
}
split_csv APEX_SCHEMAS "$APEX_PARSING_SCHEMA"
split_csv APEX_CONNECTIONS "$APEX_SQLCL_CONNECTION"
split_csv APEX_USERS "$APEX_EXPECTED_USER"
if [ "${#APEX_SCHEMAS[@]}" -eq 0 ]; then
  echo "export error: the APEX profile does not list schema ${PROJECT_SCHEMA:-?}" >&2
  exit 2
fi

if [ "$#" -gt 1 ]; then
  echo "usage: scripts/export_apps.sh [numeric_app_id]" >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  [[ "$1" =~ ^[1-9][0-9]{0,17}$ ]] || { echo "export error: expected a positive numeric application id of at most 18 digits" >&2; exit 2; }
  APP_IDS=("$1")
else
  IFS=',' read -r -a APP_IDS <<< "$APEX_APP_ID"
fi

mkdir -p "$REPO_ROOT/scratch"
STAGING_DIR="$(mktemp -d "$REPO_ROOT/scratch/apex-export.XXXXXX")"
cleanup() { rm -rf -- "$STAGING_DIR"; }
trap cleanup EXIT

# SQLcl builds a JLine console over its standard input at startup. Handed a
# descriptor it cannot probe -- a pipe, or the Windows NUL device that
# /dev/null becomes under Git Bash -- it aborts with
# "java.io.IOException: Incorrect function" before running the script, and
# still exits 0. An empty regular file is a standard input every platform can
# probe, and it also stops SQLcl from consuming the caller's own input.
SQLCL_STDIN="$STAGING_DIR/.sqlcl-stdin"
: > "$SQLCL_STDIN"

# Which schema parses each application. With one schema configured that is the
# schema itself; with several it is read from the live workspace, using the
# first connection of the profile (the selected schema's, under --schema).
if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
  # Classify the lookup connection before any SQLcl session opens: the lookups
  # below use the first profile entry (the selected schema's, under --schema).
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read apex "${APEX_SCHEMAS[0]}"
fi
declare -A APP_SCHEMA_OF
for app_id in "${APP_IDS[@]}"; do
  if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
    app_schema="$(sqlcl_app_parsing_schema "${APEX_CONNECTIONS[0]}" "${APEX_USERS[0]}" \
      "${APEX_SCHEMAS[0]}" "$app_id" "$STAGING_DIR/lookup/$app_id")" || {
      echo "export error: could not determine the parsing schema of application $app_id" >&2
      exit 1
    }
    if [ -n "${PROJECT_SCHEMA:-}" ] && [ "$app_schema" != "$PROJECT_SCHEMA" ]; then
      echo "export error: application $app_id is parsed by $app_schema, not $PROJECT_SCHEMA" >&2
      exit 2
    fi
  else
    app_schema="${APEX_SCHEMAS[0]}"
  fi
  found=false
  for ((index = 0; index < ${#APEX_SCHEMAS[@]}; index++)); do
    [ "${APEX_SCHEMAS[$index]}" = "$app_schema" ] && found=true
  done
  if [ "$found" != true ]; then
    echo "export error: application $app_id is parsed by $app_schema, which is not listed in APEX_PARSING_SCHEMA (${APEX_SCHEMAS[*]})" >&2
    exit 2
  fi
  APP_SCHEMA_OF["$app_id"]="$app_schema"
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read apex "$app_schema"
done

# Refuse any dirty destination before opening the first export session. Git
# warns when the schema parent does not exist on a first export; suppress that
# diagnostic while preserving the command's failure status.
for app_id in "${APP_IDS[@]}"; do
  destination="apps/${APP_SCHEMA_OF[$app_id]}/$app_id"
  dirty_status="$(cd "$REPO_ROOT" && git status --porcelain --untracked-files=all -- "$destination" 2>/dev/null)" || {
    echo "unable to inspect Git status for mirror: $destination" >&2
    exit 1
  }
  if [ -n "$dirty_status" ]; then
    echo "refusing to export over dirty mirror: $destination" >&2
    echo "commit, stash, or remove local changes first" >&2
    exit 1
  fi
done

for app_id in "${APP_IDS[@]}"; do
  app_schema="${APP_SCHEMA_OF[$app_id]}"
  for ((index = 0; index < ${#APEX_SCHEMAS[@]}; index++)); do
    if [ "${APEX_SCHEMAS[$index]}" = "$app_schema" ]; then
      app_connection="${APEX_CONNECTIONS[$index]}"
      app_user="${APEX_USERS[$index]}"
    fi
  done
  STAGE_PARENT="$STAGING_DIR/staged/apps/$app_schema"
  mkdir -p "$STAGE_PARENT"
  RUN_DIR="$STAGING_DIR/runs/$app_id"
  RUN_STAGE_PARENT="$RUN_DIR/apps/$app_schema"
  mkdir -p "$RUN_STAGE_PARENT"
  SQLCL_OUTPUT="$RUN_DIR/sqlcl-output.log"

  if ! (
    invoke_sqlcl_safe "$RUN_DIR" \
      -S -noupdates -name "$app_connection" \
      "@$REPO_ROOT/scripts/export_apps.sql" \
      "$app_schema" "$app_id" "$DB_ENVIRONMENT" \
      "$app_user" < "$SQLCL_STDIN"
  ) > "$SQLCL_OUTPUT" 2>&1; then
    cat "$SQLCL_OUTPUT" >&2
    echo "APEX export for application $app_id failed in SQLcl" >&2
    exit 1
  fi
  if grep -Eq '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:|SQLcl Error:' "$SQLCL_OUTPUT"; then
    cat "$SQLCL_OUTPUT" >&2
    echo "APEX export for application $app_id reported a client or database error" >&2
    exit 1
  fi
  cat "$SQLCL_OUTPUT"

  # SQLcl names each export directory after the application alias, which can
  # change independently of the immutable application id used by the mirror.
  EXPORTED_DIR=""
  EXPORTED_COUNT=0
  while IFS= read -r -d '' candidate; do
    EXPORTED_DIR="$candidate"
    EXPORTED_COUNT=$((EXPORTED_COUNT + 1))
  done < <(find "$RUN_STAGE_PARENT" -mindepth 1 -maxdepth 1 -type d -print0)

  if [ "$EXPORTED_COUNT" -ne 1 ]; then
    echo "expected exactly one exported directory for application $app_id, found $EXPORTED_COUNT" >&2
    exit 1
  fi
  test -f "$EXPORTED_DIR/application.apx" || {
    echo "APEX export for application $app_id did not create application.apx" >&2
    exit 1
  }
  test -f "$EXPORTED_DIR/.apex/apexlang.json" || {
    echo "APEX export for application $app_id did not create .apex/apexlang.json" >&2
    exit 1
  }

  APP_STAGE="$STAGE_PARENT/$app_id"
  mv -- "$EXPORTED_DIR" "$APP_STAGE"
  "$REPO_ROOT/scripts/normalize_apx.sh" "$APP_STAGE"
  python3 "$REPO_ROOT/scripts/record_export_state.py" "$app_id" \
    "$RUN_DIR/.apex-export-before.txt" "$RUN_DIR/.apex-export-after.txt" \
    "$APP_STAGE/apex-team-export.json"
  python3 "$REPO_ROOT/scripts/preserve_deployments.py" \
    "$REPO_ROOT/apps/$app_schema/$app_id" "$APP_STAGE"
done

# Install only after every requested application has exported and verified, and
# install them in one call so a failure on the last application does not leave
# the earlier ones replaced.
REPLACE_ARGS=()
for app_id in "${APP_IDS[@]}"; do
  app_schema="${APP_SCHEMA_OF[$app_id]}"
  REPLACE_ARGS+=("$STAGING_DIR/staged/apps/$app_schema/$app_id" "apps/$app_schema/$app_id")
done
"$REPO_ROOT/scripts/replace_mirror.sh" "${REPLACE_ARGS[@]}"
