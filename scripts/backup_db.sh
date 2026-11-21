#!/usr/bin/env bash
# Refresh table, code and (when configured) ORDS mirrors through independent
# read targets. `--ords-only` is backup-ords: only the ORDS export, installed as
# database/<SCHEMA>/ords so the table and code mirrors stay as they are.
set -euo pipefail

ORDS_ONLY=false
case "${1:-}" in
  --ords-only) ORDS_ONLY=true; shift ;;
esac
if [ "$#" -ne 0 ]; then
  echo "backup error: unexpected argument: $1" >&2
  exit 2
fi

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
# ORDS is optional. The loader leaves these empty when the profile is not
# configured, and, under --schema, when the profile does not list the schema.
split_csv ORDS_SCHEMAS "${ORDS_SCHEMA:-}"
split_csv ORDS_CONNECTIONS "${ORDS_SQLCL_CONNECTION:-}"
split_csv ORDS_USERS "${ORDS_EXPECTED_USER:-}"
if [ "$ORDS_ONLY" = true ]; then
  TABLES_SCHEMAS=(); TABLES_CONNECTIONS=(); TABLES_USERS=()
  CODE_SCHEMAS=(); CODE_CONNECTIONS=(); CODE_USERS=()
  if [ "${PROJECT_ORDS_CONFIGURED:-false}" != true ]; then
    echo "backup-ords error: ORDS is not configured; set ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER in .env" >&2
    exit 2
  fi
  if [ "${#ORDS_SCHEMAS[@]}" -eq 0 ]; then
    echo "backup-ords error: the ORDS profile does not list schema ${PROJECT_SCHEMA:-?}" >&2
    exit 2
  fi
fi
if [ "${#TABLES_SCHEMAS[@]}" -eq 0 ] && [ "${#CODE_SCHEMAS[@]}" -eq 0 ] && [ "${#ORDS_SCHEMAS[@]}" -eq 0 ]; then
  echo "backup error: no profile lists schema ${PROJECT_SCHEMA:-?}; nothing to back up" >&2
  exit 2
fi
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read tables "${TABLES_SCHEMAS[$index]}"
done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read code "${CODE_SCHEMAS[$index]}"
done
for ((index = 0; index < ${#ORDS_SCHEMAS[@]}; index++)); do
  PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}" "$REPO_ROOT/scripts/check_db_target.sh" read ords "${ORDS_SCHEMAS[$index]}"
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
for ((index = 0; index < ${#ORDS_SCHEMAS[@]}; index++)); do add_backup_schema "${ORDS_SCHEMAS[$index]}"; done

# A schema whose table or code scope runs is replaced as a whole mirror
# (database/<SCHEMA>). A scope this run did not export is carried over from the
# committed mirror first (carry_over_unrefreshed_scopes), so the replacement does
# not delete it. A schema that only has an ORDS scope in this run is not
# replaced: only database/<SCHEMA>/ords is.
schema_in() {
  # schema_in <schema> <array-name>
  local -n members="$2"
  local member
  for member in ${members[@]+"${members[@]}"}; do
    [ "$member" = "$1" ] && return 0
  done
  return 1
}
schema_has_database_scope() { schema_in "$1" TABLES_SCHEMAS || schema_in "$1" CODE_SCHEMAS; }

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
  if schema_has_database_scope "$schema"; then
    destination="database/$schema"
  else
    destination="database/$schema/ords"
  fi
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
# The EXIT trap keeps the run's own status. Under errexit a failed rm here used to
# replace a refusal's 2 with 1; a scratch directory that cannot be removed is
# reported, and it never changes the verdict.
cleanup() {
  local status=$?
  rm -rf -- "$STAGING_DIR" || echo "backup warning: could not remove scratch directory $STAGING_DIR" >&2
  exit "$status"
}
trap cleanup EXIT
# Ctrl-C or SIGTERM ends the run with the conventional status after the EXIT
# trap above has removed the staging directory, instead of dying by signal.
trap 'exit 130' INT
trap 'exit 143' TERM HUP
mkdir -p "$STAGING_DIR/scripts"

# SQLcl builds a JLine console over its standard input at startup. Handed a
# descriptor it cannot probe -- a pipe, or the Windows NUL device that
# /dev/null becomes under Git Bash -- it aborts with
# "java.io.IOException: Incorrect function" before running the script, and
# still exits 0. An empty regular file is a standard input every platform can
# probe, and it also stops SQLcl from consuming the caller's own input.
SQLCL_STDIN="$STAGING_DIR/.sqlcl-stdin"
: > "$SQLCL_STDIN"

# An unsupported SQLcl release fails here, before any export session opens.
if [ "${#ORDS_SCHEMAS[@]}" -gt 0 ]; then
  sqlcl_require_ords_version "$STAGING_DIR/sqlcl-version" || exit 2
fi

# SQLcl's SPOOL does not create missing directories, so each scope's own
# directories are created just before its run -- and only its own, so a
# split-schema project does not install directories nothing ever writes to.
scope_directories() {
  case "$1" in
    tables) printf '%s\n' tables triggers sequences ;;
    code)   printf '%s\n' views packages procedures functions triggers synonyms ;;
    *) echo "unsupported backup scope: $1" >&2; return 1 ;;
  esac
}

# scope_stage_directory <schema> <scope>: the staging directory of one scope's
# export. Each scope spools into its own directory, because both scopes can
# write triggers/ for the same schema; the directories are merged afterward.
scope_stage_directory() {
  printf '%s-%s\n' "$(schema_stage_directory "$1")" "$2"
}

# scope_ran <scope> <schema>: whether this run exports that scope for the schema.
scope_ran() {
  local schema="$2"
  case "$1" in
    tables) schema_in "$schema" TABLES_SCHEMAS ;;
    code) schema_in "$schema" CODE_SCHEMAS ;;
    *) return 1 ;;
  esac
}

# running_scope_writes <schema> <entry>: whether a scope this run exports writes
# the folder <entry>. triggers/ is shared by both scopes, so a running scope's
# export is authoritative for it.
running_scope_writes() {
  local schema="$1"
  local entry="$2"
  local scope dir
  for scope in tables code; do
    scope_ran "$scope" "$schema" || continue
    while IFS= read -r dir; do
      if [ "$dir" = "$entry" ]; then return 0; fi
    done < <(scope_directories "$scope")
  done
  return 1
}

# carry_over_unrefreshed_scopes <schema> <staged-schema-directory>: the staged
# mirror replaces the whole committed one, so each scope this run did not export
# is copied from the committed mirror first, using the same outputs as
# scope_directories and manifest-<scope>.txt. Only what the committed mirror
# holds is copied. A scope that ran is never copied: it is replaced whole.
# A folder a running scope also writes (triggers/) is not copied, because that
# scope's export is the authoritative one for it.
carry_over_unrefreshed_scopes() {
  local schema="$1"
  local staged="$2"
  local scope entry
  local -a entries
  for scope in tables code; do
    if scope_ran "$scope" "$schema"; then continue; fi
    entries=()
    while IFS= read -r entry; do
      if running_scope_writes "$schema" "$entry"; then continue; fi
      entries+=("$entry")
    done < <(scope_directories "$scope")
    entries+=("manifest-$scope.txt")
    for entry in "${entries[@]}"; do
      if [ -e "$REPO_ROOT/database/$schema/$entry" ]; then
        cp -R -- "$REPO_ROOT/database/$schema/$entry" "$staged/$entry"
      fi
    done
  done
}

# A failed SPOOL inside the generated driver prints an SP2- message that does
# not stop SQLcl, so an object can go missing without any non-zero exit code.
# The manifest states how many objects each scope should have produced; refuse
# to install a mirror that does not have exactly that many files.
verify_scope_complete() {
  local scope="$1"
  local schema="$2"
  local mirror_stage="$STAGING_DIR/database/$(scope_stage_directory "$schema" "$scope")"
  local manifest="$mirror_stage/manifest-$scope.txt"

  python3 "$REPO_ROOT/scripts/backup_manifest.py" \
    --scope "$scope" \
    --manifest "$manifest" \
    --mirror "$mirror_stage" \
    --schema "$schema" || exit $?
}

# run_backup_scope <scope> <schema> <connection> <expected-user> <prefixes> <tables-prefixes>
# tables-prefixes is the seventh driver argument: TABLES_PREFIXES when this run
# also exports the tables scope for the schema, otherwise '-' (no tables scope).
run_backup_scope() {
  local scope="$1"
  local schema="$2"
  local connection="$3"
  local expected_user="$4"
  local prefixes="$5"
  local tables_prefixes="$6"
  # The SQLcl launcher on Windows expands an unquoted * against the working
  # directory, which shifts every later argument. backup_db.sql treats % as the
  # same "every object" value, and % cannot occur in an identifier prefix.
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      if [ "$prefixes" = "*" ]; then prefixes="%"; fi
      if [ "$tables_prefixes" = "*" ]; then tables_prefixes="%"; fi
      ;;
  esac
  local spool_schema
  spool_schema="$(scope_stage_directory "$schema" "$scope")"
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
      "$tables_prefixes" \
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

# One schema's ORDS export, judged by scripts/ords_export.py: SQLcl's exit
# status and a non-empty spool file prove nothing, so identity, completion, the
# dictionary inventory and consistency between two exports are all checked.
# Call it without `||`: a `||` caller turns errexit off for the whole body, so an
# unguarded failure in here would be ignored. Every refusal exits 2 directly.
run_ords_scope() {
  local schema="$1"
  local connection="$2"
  local expected_user="$3"
  local spool_schema
  spool_schema="$(schema_stage_directory "$schema")"
  mkdir -p "$STAGING_DIR/database/$spool_schema/ords" "$STAGING_DIR/verify/$spool_schema"
  local transcript="$STAGING_DIR/ords-transcript-$spool_schema.txt"
  (
    invoke_sqlcl_safe "$STAGING_DIR" \
      -S -noupdates -name "$connection" \
      "@$REPO_ROOT/scripts/ords_export.sql" \
      "$schema" "$DB_ENVIRONMENT" "$expected_user" "$spool_schema" \
      < "$SQLCL_STDIN" 2>&1 | awk '
        $0 == "Warning: This LONG setting may cause Java memory problems." { next }
        $0 == "It is recommended to reduce the setting and/or increase the memory available to Java." { next }
        { print }
      ' | tee "$transcript"
  ) || {
    local ords_status=$?
    [ "$ords_status" -gt 128 ] && exit "$ords_status"
    echo "ORDS export for $schema failed in SQLcl; the mirror was not replaced" >&2
    exit 2
  }
  python3 "$REPO_ROOT/scripts/ords_export.py" verify \
    --stage "$STAGING_DIR" --spool-schema "$spool_schema" \
    --schema "$schema" --expected-user "$expected_user" \
    --transcript "$transcript" || {
    echo "the mirror was not replaced" >&2
    exit 2
  }
}

# Every export and manifest must complete before any generated mirror changes.
for ((index = 0; index < ${#TABLES_SCHEMAS[@]}; index++)); do
  run_backup_scope tables "${TABLES_SCHEMAS[$index]}" "${TABLES_CONNECTIONS[$index]}" \
    "${TABLES_USERS[$index]}" "$TABLES_PREFIXES" "$TABLES_PREFIXES"
done
for ((index = 0; index < ${#CODE_SCHEMAS[@]}; index++)); do
  # The code scope excludes the table-owned triggers only when this run also
  # exports the tables scope for the schema; otherwise it keeps every trigger.
  code_tables_prefixes="-"
  if schema_in "${CODE_SCHEMAS[$index]}" TABLES_SCHEMAS; then code_tables_prefixes="$TABLES_PREFIXES"; fi
  run_backup_scope code "${CODE_SCHEMAS[$index]}" "${CODE_CONNECTIONS[$index]}" \
    "${CODE_USERS[$index]}" "$CODE_PREFIXES" "$code_tables_prefixes"
done
for ((index = 0; index < ${#ORDS_SCHEMAS[@]}; index++)); do
  run_ords_scope "${ORDS_SCHEMAS[$index]}" "${ORDS_CONNECTIONS[$index]}" "${ORDS_USERS[$index]}"
done

# SQLcl's SPOOL writes the platform line ending, so on Windows each staged mirror file arrives with CRLF. The committed
# mirror is LF (.gitattributes forces eol=lf): every .sql and .txt file under the directory has its CRLF pairs turned into
# LF. The bytes are rewritten directly, so the UTF-8 encoding, a byte-order mark and every other byte are kept.
# A symbolic link or a Windows junction is never followed: a link to a file outside the mirror would otherwise be rewritten
# through, and a linked directory is not entered. replace_mirror.sh refuses a stage that holds a link, so it is left as is.
normalize_mirror_lf() {
  python3 - "$1" <<'PY'
import os
import pathlib
import stat
import sys

# A junction is a reparse point that os.path.islink() does not report, so both are checked.
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link_status(status):
    if stat.S_ISLNK(status.st_mode):
        return True
    return bool(getattr(status, "st_file_attributes", 0) & REPARSE_POINT)


def is_link(path):
    return is_link_status(os.lstat(path))


root = sys.argv[1]
if not os.path.lexists(root):
    sys.exit(0)  # replace_mirror.sh reports a missing staging directory
if is_link(root):
    print(f"refusing to convert a mirror that is a symbolic link or junction: {root}", file=sys.stderr)
    sys.exit(2)
for directory, subdirectories, files in os.walk(root, followlinks=False):
    # os.walk would enter a junction, so linked directories are removed from the walk here.
    subdirectories[:] = [name for name in subdirectories if not is_link(os.path.join(directory, name))]
    for name in files:
        path = pathlib.Path(directory, name)
        if path.suffix.lower() not in (".sql", ".txt"):
            continue
        # One lstat decides both checks. A link or junction is skipped as before, and so is anything else that is not
        # a regular file: a FIFO named x.sql would block read_bytes forever.
        status = os.lstat(path)
        if is_link_status(status) or not stat.S_ISREG(status.st_mode):
            continue
        data = path.read_bytes()
        if b"\r\n" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n"))
PY
}

# merge_scope_stages <schema>: joins the scope staging directories of one schema
# into its whole-schema staging directory, which is then installed as the mirror.
# A relative path that both scopes wrote is refused before anything is replaced.
# Links are moved as links, so replace_mirror still sees and refuses them.
merge_scope_stages() {
  local schema="$1"
  local merged="$STAGING_DIR/database/$(schema_stage_directory "$schema")"
  local scope stage relative
  mkdir -p "$merged"
  for scope in tables code; do
    scope_ran "$scope" "$schema" || continue
    stage="$STAGING_DIR/database/$(scope_stage_directory "$schema" "$scope")"
    while IFS= read -r -d '' relative; do
      relative="${relative#./}"
      if [ -e "$merged/$relative" ] || [ -L "$merged/$relative" ]; then
        echo "backup error: both the tables and code exports wrote $relative for $schema; the mirror was not replaced" >&2
        exit 2
      fi
      mkdir -p -- "$(dirname -- "$merged/$relative")"
      mv -- "$stage/$relative" "$merged/$relative"
    done < <(cd "$stage" && find . ! -type d -print0)
  done
}

REPLACE_ARGS=()
for schema in "${BACKUP_SCHEMAS[@]}"; do
  spool_schema="$(schema_stage_directory "$schema")"
  if schema_has_database_scope "$schema"; then
    merge_scope_stages "$schema"
    mv -- "$STAGING_DIR/database/$spool_schema" "$STAGING_DIR/database/$schema"
    if ! schema_in "$schema" ORDS_SCHEMAS && [ -e "$REPO_ROOT/database/$schema/ords" ]; then
      # The whole schema mirror is replaced, and this run did not export ORDS
      # for the schema: carry its committed ORDS export over unchanged. Any edit
      # made since the dirty-mirror check is caught by the recheck in
      # replace_mirror, which refuses a dirty mirror.
      cp -R -- "$REPO_ROOT/database/$schema/ords" "$STAGING_DIR/database/$schema/ords"
    fi
    # The same replacement would delete a table or code scope this run did not
    # refresh, so carry those committed outputs over too.
    carry_over_unrefreshed_scopes "$schema" "$STAGING_DIR/database/$schema"
    # A scope that produced no objects of one type leaves an empty directory that
    # would otherwise be installed, implying "none exist" where the truth is
    # "none were looked for". Prune after verification, before replacement.
    find "$STAGING_DIR/database/$schema" -mindepth 1 -type d -empty -delete
    REPLACE_ARGS+=("$STAGING_DIR/database/$schema" "database/$schema")
  else
    mkdir -p "$STAGING_DIR/ords-install"
    mv -- "$STAGING_DIR/database/$spool_schema/ords" "$STAGING_DIR/ords-install/$schema"
    REPLACE_ARGS+=("$STAGING_DIR/ords-install/$schema" "database/$schema/ords")
  fi
done
for ((pair = 0; pair < ${#REPLACE_ARGS[@]}; pair += 2)); do
  normalize_mirror_lf "${REPLACE_ARGS[$pair]}"
done
"$REPO_ROOT/scripts/replace_mirror.sh" "${REPLACE_ARGS[@]}"
