#!/usr/bin/env bash
# Explicit DEV source conversion; Builder mode exports, files mode imports.
set -euo pipefail
fail() { printf 'source upgrade error: %s\n' "$*" >&2; exit 2; }
app_id="${1:-}"
[[ "$app_id" =~ ^[1-9][0-9]{0,17}$ ]] || fail 'expected a positive numeric application id'
shift
mode=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) [ "$#" -ge 2 ] && [ "$2" = dev ] || fail 'source conversion targets DEV only'; shift 2 ;;
    --mode) [ "$#" -ge 2 ] && [ -z "$mode" ] || fail '--mode requires builder or files once'; mode="$2"; shift 2 ;;
    *) fail "unknown option: $1" ;;
  esac
done
case "$mode" in builder) effect=read ;; files) effect=write ;; *) fail '--mode must be builder or files' ;; esac
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" "$effect" apex
description="$(PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/publish_app.sh" "$app_id" --env dev --describe)"
mapfile -t target <<< "$description"
[ "${#target[@]}" -eq 6 ] || fail 'could not resolve the selected DEV descriptor'
exec python3 "$REPO_ROOT/scripts/upgrade_apexlang.py" "$app_id" --mode "$mode" \
  --source-dir "${target[0]}" --repo-root "$REPO_ROOT" --workspace "${target[1]}" \
  --schema "${target[2]}" --connection "${target[3]}" --expected-user "${target[4]}" \
  --classification "${target[5]}" --developer "${APEX_WORKSPACE_USERNAME:-}"
