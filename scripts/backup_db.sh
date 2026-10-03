#!/usr/bin/env bash
# Refresh table and code DBMS_METADATA mirrors through independent read targets.
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
split_csv TABLES_SCHEMAS "$TABLES_SCHEMA"
split_csv TABLES_CONNECTIONS "$TABLES_SQLCL_CONNECTION"
split_csv TABLES_USERS "$TABLES_EXPECTED_USER"
split_csv CODE_SCHEMAS "$CODE_SCHEMA"
split_csv CODE_CONNECTIONS "$CODE_SQLCL_CONNECTION"
split_csv CODE_USERS "$CODE_EXPECTED_USER"
if [ "${#TABLES_SCHEMAS[@]}" -eq 0 ] && [ "${#CODE_SCHEMAS[@]}" -eq 0 ]; then
  echo "backup error: no profile lists schema ${PROJECT_SCHEMA:-?}; nothing to back up" >&2
  exit 2
fi
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read tables "${TABLES_SCHEMAS[$index]}"
done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read code "${CODE_SCHEMAS[$index]}"
done

BACKUP_SCHEMAS=()
add_backup_schema() {
  local candidate="$1"
  local existing
  for existing in ${BACKUP_SCHEMAS[@]+"${BACKUP_SCHEMAS[@]}"}; do
    [ "$existing" = "$candidate" ] && return
  done
  BACKUP_SCHEMAS+=("$candidate")
}
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do add_backup_schema "${TABLES_SCHEMAS[$index]}"; done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do add_backup_schema "${CODE_SCHEMAS[$index]}"; done

# Use a reversible URL-safe Base64 name for SQLcl spool directories. SQLcl can
# misread '$' in SPOOL paths. The encoding is injective, contains no '$', and
# stays below the filesystem's component-length limit for Oracle's 128-byte
# identifier maximum. Mirrors are renamed to the configured schema afterward.
schema_stage_directory() {
  python3 - "$1" <<'PY'
import base64
import sys

encoded = base64.urlsafe_b64encode(sys.argv[1].encode("ascii")).decode("ascii").rstrip("=")
print(f".sqlcl-schema-{encoded}")
PY
}

# Refuse local mirror edits before making either database connection.
for schema in "${BACKUP_SCHEMAS[@]}"; do
  destination="database/$schema"
  dirty_status="$(cd "$REPO_ROOT" && git status --porcelain --untracked-files=all -- "$destination")" || {
    echo "unable to inspect Git status for mirror: $destination" >&2
    exit 2
  }
  if [ -n "$dirty_status" ]; then
    echo "refusing to back up over dirty mirror: $destination" >&2
    echo "commit, stash, or remove local changes first" >&2
    exit 2
  fi
done

mkdir -p "$REPO_ROOT/scratch"
STAGING_DIR="$(mktemp -d "$REPO_ROOT/scratch/db-backup.XXXXXX")"
cleanup() { rm -rf -- "$STAGING_DIR"; }
trap cleanup EXIT
mkdir -p "$STAGING_DIR/scripts"

# SQLcl builds a JLine console over its standard input at startup. Handed a
# descriptor it cannot probe -- a pipe, or the Windows NUL device that
# /dev/null becomes under Git Bash -- it aborts with
# "java.io.IOException: Incorrect function" before running the script, and
# still exits 0. An empty regular file is a standard input every platform can
# probe, and it also stops SQLcl from consuming the caller's own input.
SQLCL_STDIN="$STAGING_DIR/.sqlcl-stdin"
: > "$SQLCL_STDIN"

# SQLcl's SPOOL does not create missing directories, so each scope's own
# directories are created just before its run -- and only its own, so a
# split-schema project does not install directories nothing ever writes to.
scope_directories() {
  case "$1" in
    tables) printf '%s\n' tables ;;
    code)   printf '%s\n' views packages procedures functions triggers synonyms ;;
    *) echo "unsupported backup scope: $1" >&2; return 1 ;;
  esac
}

# A failed SPOOL inside the generated driver prints an SP2- message that does
# not stop SQLcl, so an object can go missing without any non-zero exit code.
# The manifest states how many objects each scope should have produced; refuse
# to install a mirror that does not have exactly that many files.
verify_scope_complete() {
  local scope="$1"
  local schema="$2"
  local spool_schema
  spool_schema="$(schema_stage_directory "$schema")"
  local mirror_stage="$STAGING_DIR/database/$spool_schema"
  local manifest="$mirror_stage/manifest-$scope.txt"
  local expected=0
  local counted=0
  local manifest_types='|'
  local line object_type count
  while IFS= read -r line || [ -n "$line" ]; do
    # SQLcl spools with the platform's line terminator, so on Windows every
    # manifest line arrives with a trailing CR. Left in place it defeats the
    # numeric test below, every count is skipped, and the guard silently
    # compares 0 against 0 -- passing an empty mirror straight through.
    line="${line%$'\r'}"
    [[ "$line" == *=* ]] || continue
    object_type="${line%=*}"
    manifest_types+="$object_type|"
    count="${line##*=}"
    [[ "$count" =~ ^[0-9]+$ ]] || continue
    expected=$((expected + count))
    counted=$((counted + 1))
  done < "$manifest"

  # No parsable counts at all means the manifest itself is unusable. Fail
  # closed rather than approving whatever happens to be staged.
  if [ "$counted" -eq 0 ]; then
    echo "database backup manifest for $schema ($scope) has no readable object" >&2
    echo "counts; the mirror was not replaced" >&2
    exit 2
  fi

  local -a required_types
  case "$scope" in
    tables) required_types=(TABLE) ;;
    code) required_types=(VIEW PACKAGE 'PACKAGE BODY' PROCEDURE FUNCTION SYNONYM TRIGGER) ;;
    *) echo "unsupported backup scope: $scope" >&2; exit 2 ;;
  esac
  local required_type
  for required_type in "${required_types[@]}"; do
    if [[ "$manifest_types" != *"|$required_type|"* ]]; then
      echo "database backup manifest for $schema ($scope) is missing the $required_type row; the mirror was not replaced" >&2
      exit 2
    fi
  done

  local actual=0
  local scope_dir found
  while IFS= read -r scope_dir; do
    found="$(find "$mirror_stage/$scope_dir" -maxdepth 1 -type f \
      -name '*.sql' 2>/dev/null | wc -l)"
    actual=$((actual + found))
  done < <(scope_directories "$scope")

  if [ "$expected" -ne "$actual" ]; then
    echo "database backup is incomplete for $schema ($scope): manifest expects" >&2
    echo "$expected object file(s) but $actual were written; the mirror was not replaced" >&2
    exit 2
  fi
}

run_backup_scope() {
  local scope="$1"
  local schema="$2"
  local connection="$3"
  local expected_user="$4"
  local prefixes="$5"
  # The SQLcl launcher on Windows expands an unquoted * against the working
  # directory, which shifts every later argument. backup_db.sql treats % as the
  # same "every object" value, and % cannot occur in an identifier prefix.
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      if [ "$prefixes" = "*" ]; then prefixes="%"; fi
      ;;
  esac
  local spool_schema
  spool_schema="$(schema_stage_directory "$schema")"
  local scope_dir
  while IFS= read -r scope_dir; do
    mkdir -p "$STAGING_DIR/database/$spool_schema/$scope_dir"
  done < <(scope_directories "$scope")
  # backup_db.sql sets LONG far above SQLcl's ~2 MB warning threshold so the
  # largest package body is never truncated; drop that advisory, keep the rest.
  (
    invoke_sqlcl_safe "$STAGING_DIR" \
      -S -noupdates -name "$connection" \
      "@$REPO_ROOT/scripts/backup_db.sql" \
      "$schema" "$scope" "$DB_ENVIRONMENT" "$expected_user" "$prefixes" "$spool_schema" \
      < "$SQLCL_STDIN" | awk '
        $0 == "Warning: This LONG setting may cause Java memory problems." { next }
        $0 == "It is recommended to reduce the setting and/or increase the memory available to Java." { next }
        { print }
      '
  ) || {
    # SQLcl failed (2, as every refusal of the wrappers), or a signal ended it (its status).
    local backup_status=$?
    [ "$backup_status" -gt 128 ] && exit "$backup_status"
    echo "database backup for $schema ($scope) failed in SQLcl; the mirror was not replaced" >&2
    exit 2
  }
  test -f "$STAGING_DIR/database/$spool_schema/manifest-$scope.txt" || {
    echo "database backup did not create manifest-$scope.txt for $schema" >&2
    exit 2
  }
  verify_scope_complete "$scope" "$schema"
}

# Both exports and manifests must complete before any generated mirror changes.
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do
  run_backup_scope tables "${TABLES_SCHEMAS[$index]}" "${TABLES_CONNECTIONS[$index]}" \
    "${TABLES_USERS[$index]}" "$TABLES_PREFIXES"
done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do
  run_backup_scope code "${CODE_SCHEMAS[$index]}" "${CODE_CONNECTIONS[$index]}" \
    "${CODE_USERS[$index]}" "$CODE_PREFIXES"
done

REPLACE_ARGS=()
for schema in "${BACKUP_SCHEMAS[@]}"; do
  spool_schema="$(schema_stage_directory "$schema")"
  mv -- "$STAGING_DIR/database/$spool_schema" "$STAGING_DIR/database/$schema"
  # A scope that produced no objects of one type leaves an empty directory that
  # would otherwise be installed, implying "none exist" where the truth is
  # "none were looked for". Prune after verification, before replacement.
  find "$STAGING_DIR/database/$schema" -mindepth 1 -type d -empty -delete
  REPLACE_ARGS+=("$STAGING_DIR/database/$schema" "database/$schema")
done
"$REPO_ROOT/scripts/replace_mirror.sh" "${REPLACE_ARGS[@]}"
