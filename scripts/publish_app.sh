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
# shellcheck source=sqlcl_safe.sh
source "$REPO_ROOT/scripts/sqlcl_safe.sh"

preferred_app_dir="$REPO_ROOT/apps/${PROJECT_SCHEMA:-$APEX_PARSING_SCHEMA}/$app_id"
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

# With several schemas the descriptor's parsing schema selects the connection.
# Folder, descriptor, --schema and (below) the live app must all agree.
if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
  app_folder_schema="$(basename "$(dirname "$app_dir")")"
  if [ "$app_folder_schema" != "$parsing_schema" ]; then
    fail "application $app_id is stored under apps/$app_folder_schema but its descriptor parses as $parsing_schema; move the folder or fix the descriptor"
  fi
  if [ -n "${PROJECT_SCHEMA:-}" ] && [ "$PROJECT_SCHEMA" != "$parsing_schema" ]; then
    fail "--schema $PROJECT_SCHEMA does not match the application's parsing schema $parsing_schema"
  fi
  export PROJECT_SCHEMA="$parsing_schema"
  # shellcheck source=load_env.sh
  source "$REPO_ROOT/scripts/load_env.sh" "$PROJECT_ENV_FILE"
fi

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

if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
  case "$app_environment" in
    staging)
      [ "${STAGING_SCHEMA:-}" = "$parsing_schema" ] || \
        fail "schema $parsing_schema is not listed in STAGING_SCHEMA, so it cannot be published to staging"
      ;;
    prod)
      [ "${PROD_SCHEMA:-}" = "$parsing_schema" ] || \
        fail "schema $parsing_schema is not listed in PROD_SCHEMA, so it cannot be published to production"
      ;;
  esac
fi

if [ "$describe" = true ]; then
  # Internal read-only interface for deploy.sh to show the selected descriptor
  # before it asks the operator to confirm or prints a DBA runbook.
  printf '%s\n' "$app_dir" "$workspace_name" "$parsing_schema" \
    "$sqlcl_connection" "$expected_user" "$target_environment"
  exit 0
fi

if [ -z "$sqlcl_connection" ] || [ -z "$expected_user" ]; then
  case "$app_environment" in
    dev)
      fail "schema $parsing_schema is not listed in APEX_PARSING_SCHEMA; add its connection and expected user to .env"
      ;;
    staging)
      if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
        fail "schema $parsing_schema is not listed in STAGING_SCHEMA, so it cannot be published to staging"
      fi
      fail "set STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER in .env to publish to staging"
      ;;
    prod)
      if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
        fail "schema $parsing_schema is not listed in PROD_SCHEMA, so it cannot be published to production"
      fi
      fail "set PROD_SQLCL_CONNECTION and PROD_EXPECTED_USER in .env to publish to production"
      ;;
  esac
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

# Classify the target before the live lookup opens its read-only session.
if [ "$app_environment" = dev ]; then
  PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" write apex
fi

# The live application must be parsed by the schema the descriptor names. An
# application that is not there yet (first import) is allowed.
if [ "$PROJECT_MULTI_SCHEMA" = true ]; then
  mkdir -p "$REPO_ROOT/scratch"
  lookup_dir="$(mktemp -d "$REPO_ROOT/scratch/apex-lookup.XXXXXX")"
  live_schema=""
  if live_schema="$(sqlcl_app_parsing_schema "$sqlcl_connection" "$expected_user" "$parsing_schema" "$app_id" "$lookup_dir" "$target_environment" 2>"$lookup_dir/error.txt")"; then
    if [ "$live_schema" != "$parsing_schema" ]; then
      rm -rf -- "$lookup_dir"
      fail "application $app_id is parsed by $live_schema, not the descriptor's $parsing_schema; refusing to import"
    fi
  elif ! grep -Fq "was not found in the workspace" "$lookup_dir/error.txt"; then
    cat "$lookup_dir/error.txt" >&2
    rm -rf -- "$lookup_dir"
    fail "could not verify the live parsing schema of application $app_id"
  fi
  rm -rf -- "$lookup_dir"
fi

mkdir -p "$REPO_ROOT/scratch"
staging_dir="$(mktemp -d "$REPO_ROOT/scratch/apex-publish.XXXXXX")"
restore_unstamped=false
cleanup() {
  if [ "$restore_unstamped" = true ]; then
    # Undo only our own stamp. An edit saved while the publish ran is kept.
    if cmp -s -- "$staging_dir/application.apx.stamped" "$app_dir/application.apx"; then
      cp -p -- "$staging_dir/application.apx.unstamped" "$app_dir/application.apx" || true
    else
      printf 'publish warning: %s changed while publishing; left as is (check its version line)\n' \
        "${app_dir#"$REPO_ROOT/"}/application.apx" >&2
    fi
  fi
  rm -rf -- "$staging_dir"
}
trap cleanup EXIT

# The import session re-checks the live state the drift guard approved; '-'
# skips that re-check for --force and for staging or production.
expected_live_state="-"
if [ "$app_environment" = dev ] && [ "$force" != true ]; then
  drift_guard="$REPO_ROOT/scripts/check_builder_drift.py"
  [ -f "$drift_guard" ] || fail "Builder drift guard is missing; refusing import"
  python3 "$drift_guard" "$app_id" "$sqlcl_connection" "$app_dir" \
    --expected-user "$expected_user" --state-out "$staging_dir/approved-live-state.txt"
  expected_live_state="$(head -n 1 "$staging_dir/approved-live-state.txt")"
  [[ "$expected_live_state" =~ ^(ABSENT|P\|([0-9T:-]+|NONE)\|[0-9A-F]*)$ ]] || \
    fail "Builder drift guard did not record the approved live state; refusing import"
fi
sqlcl_stdin="$staging_dir/.sqlcl-stdin"
: > "$sqlcl_stdin"
sqlcl_output="$staging_dir/sqlcl-output.log"

# An import leaves no Builder timestamp, so a DEV publish stamps its own tag
# into the application version before import. The drift guard compares that
# version to spot a teammate's import. Staging and production import the
# committed tag unchanged.
published_version=""
if [ "$app_environment" = dev ]; then
  [ -f "$app_dir/application.apx" ] || fail "DEV publish needs application.apx to stamp the publish tag"
  cp -p -- "$app_dir/application.apx" "$staging_dir/application.apx.unstamped"
  restore_unstamped=true
  published_version="$(python3 "$REPO_ROOT/scripts/stamp_publish_version.py" \
    "$app_dir/application.apx" "$DEVELOPER_NAME")" || exit 2
  cp -p -- "$app_dir/application.apx" "$staging_dir/application.apx.stamped"
  printf 'Stamped application version: %s\n' "$published_version"
fi

application_source="$app_dir"
deployment_file="$app_dir/deployments/$app_environment.json"
if ! (
  invoke_sqlcl_safe "$staging_dir" \
    -S -noupdates -name "$sqlcl_connection" \
    "@$REPO_ROOT/scripts/publish_app.sql" \
    "$parsing_schema" "$target_environment" "$expected_user" \
    "$application_source" "$deployment_file" "$app_id" "$expected_live_state" \
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
# SQLcl exits 0 without importing when, for example, the descriptor names an
# unknown workspace ("... is invalid"). Only its success line proves an import.
if ! grep -Eq '^[[:space:]]*Import successful\.[[:space:]]*$' "$sqlcl_output"; then
  fail "SQLcl did not report a successful APEX import; see the client output above"
fi
# The stamped source is live now; keep it for the developer to commit.
restore_unstamped=false

# Re-export from the selected target and compare exact APEXlang bytes before
# claiming success. In DEV this same stable observation advances the drift
# baseline; staging and production checks never modify the DEV marker.
verify_run_dir="$staging_dir/post-import/runs/$app_id"
verify_parent="$verify_run_dir/apps/$parsing_schema"
mkdir -p "$verify_parent"
verify_sqlcl_output="$staging_dir/post-import/sqlcl-output.log"
if ! (
  invoke_sqlcl_safe "$verify_run_dir" \
    -S -noupdates -name "$sqlcl_connection" \
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
if [ -n "$published_version" ]; then
  printf 'Commit the stamped version in %s: %s\n' \
    "${app_dir#"$REPO_ROOT/"}/application.apx" "$published_version"
fi
