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
describe=false
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
    --describe)
      describe=true
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

python3 "$REPO_ROOT/scripts/validate_app_source.py" "$REPO_ROOT" "$app_dir" || exit 2

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
    ;;
  prod)
    sqlcl_connection="${PROD_SQLCL_CONNECTION:-}"
    expected_user="${PROD_EXPECTED_USER:-}"
    target_label=PROD
    target_environment=production
    ;;
esac

if [ "$describe" = true ]; then
  # Internal read-only interface for deploy.sh to show the selected descriptor
  # before it asks the operator to confirm or prints a DBA runbook.
  printf '%s\n' "$app_dir" "$workspace_name" "$parsing_schema" \
    "$sqlcl_connection" "$expected_user" "$target_environment"
  exit 0
fi

if [ "$app_environment" != dev ] && { [ -z "$sqlcl_connection" ] || [ -z "$expected_user" ]; }; then
  if [ "$app_environment" = staging ]; then
    fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
  fi
  fail "set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
fi

if [ "$app_environment" != dev ]; then
  printf 'Deploying to %s. Proceed? [y/N]: ' "$target_label"
  answer=""
  IFS= read -r answer || true
  case "${answer,,}" in
    y|yes) ;;
    *) printf 'Publish cancelled.\n' >&2; exit 1 ;;
  esac
fi

# Classify the target before the drift guard opens its read-only session.
if [ "$app_environment" = dev ]; then
  PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" write apex
fi

if [ "$app_environment" = dev ] && [ "$force" != true ]; then
  drift_guard="$REPO_ROOT/scripts/check_builder_drift.py"
  [ -f "$drift_guard" ] || fail "Builder drift guard is missing; refusing import"
  python3 "$drift_guard" "$app_id" "$sqlcl_connection" "$app_dir" \
    --expected-user "$expected_user"
fi

mkdir -p "$REPO_ROOT/scratch"
staging_dir="$(mktemp -d "$REPO_ROOT/scratch/apex-publish.XXXXXX")"
cleanup() { rm -rf -- "$staging_dir"; }
trap cleanup EXIT
sqlcl_stdin="$staging_dir/.sqlcl-stdin"
: > "$sqlcl_stdin"
sqlcl_output="$staging_dir/sqlcl-output.log"

relative_deployment="deployments/$app_environment.json"
if ! (
  cd "$app_dir"
  sql -S -noupdates -name "$sqlcl_connection" \
    "@$REPO_ROOT/scripts/publish_app.sql" \
    "$parsing_schema" "$target_environment" "$expected_user" "$relative_deployment" "$app_id" \
    < "$sqlcl_stdin"
) > "$sqlcl_output" 2>&1; then
  cat "$sqlcl_output" >&2
  fail "SQLcl application import failed; see the client output above"
fi
cat "$sqlcl_output"
if grep -Eq '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:' "$sqlcl_output"; then
  fail "SQLcl reported a client or database error during the application import"
fi
if ! grep -Eq "^[[:space:]]*APEX_IMPORT_VERIFIED:${app_id}[[:space:]]*$" "$sqlcl_output"; then
  fail "SQLcl did not verify the imported application; the import result is unknown"
fi

# Re-export from the selected target and compare exact APEXlang bytes before
# claiming success. In DEV this same stable observation advances the drift
# baseline; staging and production checks never modify the DEV marker.
verify_run_dir="$staging_dir/post-import/runs/$app_id"
verify_parent="$verify_run_dir/apps/$parsing_schema"
mkdir -p "$verify_parent"
verify_sqlcl_output="$staging_dir/post-import/sqlcl-output.log"
if ! (
  cd "$verify_run_dir"
  sql -S -noupdates -name "$sqlcl_connection" \
    "@$REPO_ROOT/scripts/export_apps.sql" \
    "$parsing_schema" "$app_id" "$target_environment" "$expected_user" \
    < "$sqlcl_stdin"
) > "$verify_sqlcl_output" 2>&1; then
  cat "$verify_sqlcl_output" >&2
  fail "post-import APEX export failed; the imported source was not verified"
fi
cat "$verify_sqlcl_output"
if grep -Eq '(SP2|TNS|ORA|PLS|SQL)-[0-9]{4,5}:' "$verify_sqlcl_output"; then
  fail "SQLcl reported an error while verifying the post-import APEX source"
fi

exported_dirs=()
while IFS= read -r -d '' exported_dir; do
  exported_dirs+=("$exported_dir")
done < <(find "$verify_parent" -mindepth 1 -maxdepth 1 -type d -print0)
if [ "${#exported_dirs[@]}" -ne 1 ]; then
  fail "expected exactly one post-import export for application $app_id, found ${#exported_dirs[@]}"
fi
exported_dir="${exported_dirs[0]}"
if [ ! -f "$exported_dir/application.apx" ] || [ ! -f "$exported_dir/.apex/apexlang.json" ]; then
  fail "post-import export for application $app_id is missing required APEXlang source files"
fi
"$REPO_ROOT/scripts/normalize_apx.sh" "$exported_dir"
verify_args=(
  "$app_id" "$app_dir" "$exported_dir"
  "$verify_run_dir/.apex-export-before.txt" "$verify_run_dir/.apex-export-after.txt"
  --repo-root "$REPO_ROOT"
)
if [ "$app_environment" = dev ]; then
  verify_args+=(--record-baseline)
fi
python3 "$REPO_ROOT/scripts/verify_publish_state.py" "${verify_args[@]}"
printf 'Published APEX App %s to %s (%s / %s).\n' \
  "$app_id" "$target_label" "$workspace_name" "$parsing_schema"
