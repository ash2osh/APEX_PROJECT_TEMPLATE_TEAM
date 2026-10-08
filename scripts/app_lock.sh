#!/usr/bin/env bash
# Explicit DEV application locks. No page locks or imports are performed.
set -euo pipefail
fail() { printf 'application lock error: %s\n' "$*" >&2; exit 2; }
operation="${1:-}"; app_id="${2:-}"
case "$operation" in acquire|unlock) ;; *) fail 'expected acquire or unlock' ;; esac
[[ "$app_id" =~ ^[1-9][0-9]{0,17}$ ]] || fail 'expected a positive numeric application id of at most 18 digits'
shift 2
comment=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) [ "$#" -ge 2 ] || fail '--env requires dev'; [ "$2" = dev ] || fail 'application locks target DEV only'; shift 2 ;;
    --comment) [ "$operation" = acquire ] || fail '--comment is supported only by app-lock'; [ "$#" -ge 2 ] || fail '--comment requires text'; comment="$2"; shift 2 ;;
    *) fail "unknown option: $1" ;;
  esac
done
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
[ -n "${APEX_WORKSPACE_USERNAME:-}" ] || fail 'set APEX_WORKSPACE_USERNAME to the existing Builder developer/admin login'
PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" write apex
description="$(PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/publish_app.sh" "$app_id" --env dev --describe)"
mapfile -t target <<< "$description"
[ "${#target[@]}" -eq 6 ] || fail 'could not resolve the application DEV descriptor'
mkdir -p "$REPO_ROOT/.sync-state/application-locks/$app_id"
recovery="$(mktemp -d "$REPO_ROOT/.sync-state/application-locks/$app_id/run.XXXXXX")"
exec python3 "$REPO_ROOT/scripts/application_lock.py" "$operation" --app-id "$app_id" \
  --workspace "${target[1]}" --schema "${target[2]}" --connection "${target[3]}" \
  --expected-user "${target[4]}" --classification "${target[5]}" \
  --developer "$APEX_WORKSPACE_USERNAME" --run-dir "$recovery" --comment "$comment"
