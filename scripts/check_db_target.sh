#!/usr/bin/env bash
# Pre-connect environment classification. Database identity is verified in SQL.
#
# Production safety in this template is an instruction to the client, not a
# privilege audit: read targets are allowed, write operation classes are
# refused, and the operator is told to run SELECT statements only.
set -euo pipefail

OPERATION="${1:?usage: check_db_target.sh <read|write> <tables|code|apex> [schema]}"
TARGET="${2:?usage: check_db_target.sh <read|write> <tables|code|apex> [schema]}"
SCHEMA_ARG="${3:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# A schema argument is the same selection as --schema; the loader narrows on it.
if [ -n "$SCHEMA_ARG" ]; then export PROJECT_SCHEMA="$SCHEMA_ARG"; fi
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"

case "$OPERATION" in
  read|write) ;;
  *) echo "unsupported database operation class: $OPERATION" >&2; exit 2 ;;
esac

case "$TARGET" in
  tables) TARGET_CONNECTION="$TABLES_SQLCL_CONNECTION" ;;
  code)   TARGET_CONNECTION="$CODE_SQLCL_CONNECTION" ;;
  apex)   TARGET_CONNECTION="$APEX_SQLCL_CONNECTION" ;;
  *) echo "unsupported database target: $TARGET" >&2; exit 2 ;;
esac

project_env_require_single "check_db_target ($TARGET)" || exit 2
if [ -z "$TARGET_CONNECTION" ]; then
  echo "the $TARGET profile does not list schema ${PROJECT_SCHEMA:-}" >&2
  exit 2
fi

shopt -s nocasematch
# Same production marker as scripts/db_targets.py.
production_marker='(^|[^[:alnum:]])(production|live)[0-9]*([^[:alnum:]]|$)|(prod|prd)[0-9]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[0-9])'
if [[ "$TARGET_CONNECTION" =~ $production_marker ]] && \
   [ "$DB_ENVIRONMENT" != production ]; then
  echo "$TARGET connection '$TARGET_CONNECTION' resembles production but DB_ENVIRONMENT=$DB_ENVIRONMENT" >&2
  echo "ask the user whether this is production before continuing" >&2
  exit 2
fi
shopt -u nocasematch

if [ "$DB_ENVIRONMENT" = production ]; then
  if [ "$OPERATION" != read ]; then
    echo "production database operations are always read-only; '$OPERATION' is blocked" >&2
    exit 2
  fi
  cat >&2 <<'NOTICE'
PRODUCTION SESSION - READ ONLY
  Run SELECT statements only.
  Do NOT run INSERT, UPDATE, DELETE, MERGE, or any other DML.
  Do NOT run CREATE, ALTER, DROP, TRUNCATE, or any other DDL.
  Do NOT COMMIT. Prepare changes for an approved deployment instead.
  This is not enforced by the database. It is your contract.
NOTICE
fi
