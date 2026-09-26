#!/usr/bin/env bash
# Import one numeric APEX application using its committed environment descriptor.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/publish_app.sh <app_id> [--env <dev|staging|prod>] [--force]

Imports one APEXlang application with deployments/<env>.json. Staging and
production imports require interactive confirmation. --force skips only the
Builder drift check; it does not skip target confirmation or identity checks.
USAGE
}

fail() { printf 'publish error: %s\n' "$*" >&2; exit 2; }

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

app_id="${1:-}"
[[ "$app_id" =~ ^[1-9][0-9]*$ ]] || fail "expected a positive numeric application id"
shift

app_environment=dev
force=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env)
      [ "$#" -ge 2 ] || fail "--env requires dev, staging, or prod"
      app_environment="$2"
      shift 2
      ;;
    --force)
      force=true
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
  dev|staging|prod) ;;
  *) fail "unsupported environment '$app_environment'; use dev, staging, or prod" ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT_ENV_FILE="${PROJECT_ENV_FILE:-$REPO_ROOT/.env}"
# shellcheck source=load_env.sh
source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"

preferred_app_dir="$REPO_ROOT/apps/$APEX_PARSING_SCHEMA/$app_id"
if [ -d "$preferred_app_dir" ]; then
  app_dir="$preferred_app_dir"
else
  candidates=()
  direct_app_dir="$REPO_ROOT/apps/$app_id"
  [ ! -d "$direct_app_dir" ] || candidates+=("$direct_app_dir")
  shopt -s nullglob
  nested_candidates=("$REPO_ROOT"/apps/*/"$app_id")
  shopt -u nullglob
  for candidate in "${nested_candidates[@]}"; do
    [ ! -d "$candidate" ] || candidates+=("$candidate")
  done
  case "${#candidates[@]}" in
    0) fail "no application source directory found for id $app_id under apps/<schema>/$app_id or apps/$app_id" ;;
    1) app_dir="${candidates[0]}" ;;
    *) fail "application id $app_id resolves to multiple source directories; keep it unique or configure APEX_PARSING_SCHEMA" ;;
  esac
fi

deployment_file="$app_dir/deployments/$app_environment.json"
[ -f "$deployment_file" ] || fail "deployment descriptor not found: ${deployment_file#"$REPO_ROOT/"}"

deployment_values="$(python3 - "$deployment_file" "$app_id" <<'PY'
import json
import re
import sys

path, expected_id = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as source:
        descriptor = json.load(source)
    workspace = descriptor["workspace"]["name"]
    app = descriptor["app"]
    app_id = app["id"]
    schema = app["databaseSession"]["parsingSchema"]
except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
    print(f"invalid deployment descriptor: {exc}", file=sys.stderr)
    raise SystemExit(1)

if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id != int(expected_id):
    print(f"deployment app.id must be numeric {expected_id}", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(workspace, str) or not workspace.strip() or any(c in workspace for c in "\t\r\n"):
    print("deployment workspace.name must be a non-empty single-line string", file=sys.stderr)
    raise SystemExit(1)
if not isinstance(schema, str) or not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", schema):
    print("deployment parsingSchema must be an uppercase Oracle identifier", file=sys.stderr)
    raise SystemExit(1)
print(f"{workspace}\t{schema}")
PY
)" || exit 2
IFS=$'\t' read -r workspace_name parsing_schema <<< "$deployment_values"

if [ ! -f "$app_dir/application.apx" ] && [ ! -f "$app_dir/.apex/apexlang.json" ] && \
   [ -z "$(find "$app_dir" -type f -name '*.apx' -print -quit)" ]; then
  fail "no APEXlang source found in $app_dir"
fi

case "$app_environment" in
  dev)
    sqlcl_connection="$APEX_SQLCL_CONNECTION"
    expected_user="$APEX_EXPECTED_USER"
    target_label=DEV
    target_environment="$DB_ENVIRONMENT"
    ;;
  staging)
    sqlcl_connection="${STAGING_SQLCL_CONNECTION:-}"
    expected_user="${STAGING_EXPECTED_USER:-}"
    target_label=STAGING
    target_environment=staging
    [ -n "$sqlcl_connection" ] && [ -n "$expected_user" ] || \
      fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
    ;;
  prod)
    sqlcl_connection="${PROD_SQLCL_CONNECTION:-}"
    expected_user="${PROD_EXPECTED_USER:-}"
    target_label=PROD
    target_environment=production
    [ -n "$sqlcl_connection" ] && [ -n "$expected_user" ] || \
      fail "set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
    ;;
esac

if [ "$app_environment" != dev ]; then
  printf 'Deploying to %s. Proceed? [y/N]: ' "$target_label"
  answer=""
  IFS= read -r answer || true
  case "${answer,,}" in
    y|yes) ;;
    *) printf 'Publish cancelled.\n' >&2; exit 1 ;;
  esac
fi

if [ "$force" != true ]; then
  drift_guard="$REPO_ROOT/scripts/check_builder_drift.py"
  [ -f "$drift_guard" ] || fail "Builder drift guard is missing; refusing import"
  python3 "$drift_guard" "$app_id" "$sqlcl_connection" "$app_dir" \
    --expected-user "$expected_user"
fi

if [ "$app_environment" = dev ]; then
  PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" write apex
fi

mkdir -p "$REPO_ROOT/scratch"
staging_dir="$(mktemp -d "$REPO_ROOT/scratch/apex-publish.XXXXXX")"
cleanup() { rm -rf -- "$staging_dir"; }
trap cleanup EXIT
sqlcl_stdin="$staging_dir/.sqlcl-stdin"
: > "$sqlcl_stdin"

relative_deployment="deployments/$app_environment.json"
(
  cd "$app_dir"
  sql -S -noupdates -name "$sqlcl_connection" \
    "@$REPO_ROOT/scripts/publish_app.sql" \
    "$parsing_schema" "$target_environment" "$expected_user" "$relative_deployment" \
    < "$sqlcl_stdin"
)
printf 'Published APEX App %s to %s (%s / %s).\n' \
  "$app_id" "$target_label" "$workspace_name" "$parsing_schema"
