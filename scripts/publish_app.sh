#!/usr/bin/env bash
# Import one numeric APEX application using its committed environment descriptor.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/publish_app.sh <app_id> [--env <dev|staging|prod>] [--force]
       scripts/publish_app.sh <app_id> --env dev --file <pages/file.apx> [--file ...] [--no-team-notice]

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
[[ "$app_id" =~ ^[1-9][0-9]{0,17}$ ]] || fail "expected a positive numeric application id of at most 18 digits"
shift

app_environment=dev
force=false
describe=false
selected_files=()
no_team_notice=false
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
    --file)
      [ "$#" -ge 2 ] && [ -n "$2" ] || fail '--file requires an app-relative page path'
      selected_files+=("$2"); shift 2
      ;;
    --no-team-notice)
      no_team_notice=true; shift
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
if [ "${#selected_files[@]}" -gt 0 ]; then
  [ "$app_environment" = dev ] || fail 'partial publishing targets DEV only'
  [ "$force" = false ] || fail 'partial publishing does not accept --force'
  [ "$describe" = false ] || fail '--describe cannot be combined with --file'
elif [ "$no_team_notice" = true ]; then
  fail '--no-team-notice requires --file'
fi

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

deployment_values="$(python3 "$REPO_ROOT/scripts/deployment_descriptor.py" "$deployment_file" "$app_id")" || exit 2
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
elif [ "$app_environment" = dev ]; then
  # One schema: the DEV descriptor must name it, and the folder is named after it.
  # Staging and production descriptors may name other schemas (apps/templates).
  app_folder_schema="$(basename "$(dirname "$app_dir")")"
  if [ "$app_folder_schema" != "$parsing_schema" ]; then
    fail "application $app_id is stored under apps/$app_folder_schema but its descriptor parses as $parsing_schema; move the folder or fix the descriptor"
  fi
  if [ "${APEX_PARSING_SCHEMA:-}" != "$parsing_schema" ]; then
    fail "schema $parsing_schema is not listed in APEX_PARSING_SCHEMA; add its connection and expected user to .env"
  fi
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

if [ "$app_environment" = dev ] && [ -z "${APEX_WORKSPACE_USERNAME:-}" ]; then
  fail "set APEX_WORKSPACE_USERNAME to the existing Builder developer/admin login"
fi
python3 "$REPO_ROOT/scripts/validate_app_source.py" "$REPO_ROOT" "$app_dir" --for-import || exit 2

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
  # An answer piped from Windows PowerShell arrives as "y" followed by CR LF; drop the CR.
  answer="${answer%$'\r'}"
  case "${answer,,}" in
    y|yes) ;;
    *) printf 'Publish cancelled.\n' >&2; exit 1 ;;
  esac
fi

# Classify the target before the live lookup opens its read-only session.
if [ "$app_environment" = dev ]; then
  PROJECT_ENV_FILE="$PROJECT_ENV_FILE" "$REPO_ROOT/scripts/check_db_target.sh" write apex
fi
if [ "${#selected_files[@]}" -gt 0 ]; then
  partial_args=()
  for selected_file in "${selected_files[@]}"; do partial_args+=(--file "$selected_file"); done
  [ "$no_team_notice" = false ] || partial_args+=(--no-team-notice)
  exec python3 "$REPO_ROOT/scripts/partial_publish.py" "$app_id" "${partial_args[@]}" \
    --source-dir "$app_dir" --repo-root "$REPO_ROOT" --workspace "$workspace_name" \
    --schema "$parsing_schema" --connection "$sqlcl_connection" --expected-user "$expected_user" \
    --classification "$target_environment" --developer "$APEX_WORKSPACE_USERNAME" --developer-name "$DEVELOPER_NAME"
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
# Set once the import changed the target and cleared after its verification,
# so a failure in between can say what to do next.
import_unverified=false
# Set while the import session runs and cleared once its output has been read: an
# interrupt in between leaves the import's result unknown (SQLcl may have
# finished it already).
import_running=false
lock_recovery=""
lock_held=false
import_attempted=false
# Replace <target> with <replacement> only while <target> still holds the bytes
# of <expected>. The target is renamed aside before the comparison and the
# replacement is installed without overwriting, so an editor save at any
# moment is kept rather than replaced. Returns 0 when swapped, 1 when the
# target changed or reappeared, 2 when the target could not be moved aside,
# and 3 when the replacement could not be moved into place.
swap_if_unchanged() {
  local target="$1" expected="$2" replacement="$3" aside="$4"
  mv -- "$target" "$aside" 2>/dev/null || return 2
  if ! cmp -s -- "$aside" "$expected"; then
    [ -e "$target" ] || mv -- "$aside" "$target"
    return 1
  fi
  if [ -e "$target" ]; then
    return 1
  fi
  mv -n -- "$replacement" "$target" 2>/dev/null || true
  # GNU and BSD mv -n may exit 0 without moving; the replacement's presence tells.
  [ ! -e "$replacement" ] && return 0
  [ -e "$target" ] && return 1
  return 3
}

cleanup() {
  local aside
  if [ -n "$lock_recovery" ]; then
    if [ "$lock_held" = true ] && [ "$import_attempted" = false ]; then
      if python3 "$REPO_ROOT/scripts/application_lock.py" release "${lock_args[@]}"; then
        lock_held=false
      fi
    fi
    if [ "$lock_held" = true ] || [ "$import_attempted" = true ] && [ -f "$lock_recovery/recovery.json" ]; then
      printf 'publish: preserve lock recovery evidence at %s; inspect the live application and coordinate recovery before app-unlock.\n' "$lock_recovery/recovery.json" >&2
    fi
  fi
  if [ "$import_running" = true ]; then
    if [ "$app_environment" = dev ]; then
      printf 'publish: interrupted while the import was running, so its result is unknown: DEV may or may not run your source. Commit your changes, run scripts/team.sh export %s, and read the live version; %s means the import completed.\n' \
        "$app_id" "${published_version:-the version you published}" >&2
    else
      printf 'publish: interrupted while the import into %s was running, so its result is unknown; inspect %s before importing again.\n' \
        "$target_label" "$target_label" >&2
    fi
  fi
  if [ "$import_unverified" = true ]; then
    if [ "$app_environment" = dev ]; then
      if [ -n "$published_version" ]; then
        printf 'publish: DEV now runs the imported source, but it was not verified. Commit the stamped %s first (it is what was imported), then run scripts/team.sh export %s to see what is live, reconcile, and commit.\n' \
          "${app_dir#"$REPO_ROOT/"}/application.apx" "$app_id" >&2
      else
        printf 'publish: DEV now runs the imported source, but it was not verified. Run scripts/team.sh export %s to see what is live, reconcile, and commit.\n' "$app_id" >&2
      fi
    else
      printf 'publish: %s now runs the imported source, but it was not verified; compare it with the committed source before importing again.\n' \
        "$target_label" >&2
    fi
  fi
  if [ "$restore_unstamped" = true ] && [ -e "$app_dir/application.apx" ]; then
    # Undo only our own stamp. An edit saved while the publish ran is kept.
    local restore_status=0
    swap_if_unchanged "$app_dir/application.apx" "$staging_dir/application.apx.stamped" \
      "$staging_dir/application.apx.restore" "$staging_dir/application.apx.displaced" || restore_status=$?
    if [ "$restore_status" -eq 1 ]; then
      printf 'publish warning: %s changed while publishing; left as is (check its version line)\n' \
        "${app_dir#"$REPO_ROOT/"}/application.apx" >&2
    elif [ "$restore_status" -ne 0 ]; then
      printf 'publish warning: could not remove the publish tag from %s; restore its version line by hand\n' \
        "${app_dir#"$REPO_ROOT/"}/application.apx" >&2
    fi
  fi
  # Interrupted or failed between moving application.apx aside and installing
  # its replacement: put the moved-aside file back before scratch is deleted.
  for aside in "$staging_dir/application.apx.before-stamp" "$staging_dir/application.apx.displaced"; do
    if [ ! -e "$app_dir/application.apx" ] && [ -e "$aside" ]; then
      mv -n -- "$aside" "$app_dir/application.apx" 2>/dev/null || true
      if [ -e "$aside" ] && [ ! -e "$app_dir/application.apx" ]; then
        printf 'publish error: could not put application.apx back; recover it from %s\n' "$aside" >&2
        return
      fi
    fi
  done
  if [ "$import_attempted" = true ]; then
    printf 'publish: unverified import diagnostics retained at %s\n' "$staging_dir" >&2
  elif ! rm -rf -- "$staging_dir" 2>/dev/null; then
    printf 'publish warning: could not remove the temporary directory %s; delete it after closing whatever holds a file in it\n' \
      "${staging_dir#"$REPO_ROOT/"}" >&2
  fi
}
printf '%s\n' '{"schemaVersion":1,"sourceVerified":false,"lifecycleStatus":"unavailable"}' > "$staging_dir/publish-verification.json"
trap cleanup EXIT

# The import session re-checks the live state the drift guard approved; '-'
# skips that re-check for --force and for staging or production.
expected_live_state="-"
sqlcl_require_apex_version "$staging_dir/version"
lock_assert_script="$REPO_ROOT/scripts/no_application_lock.sql"
if [ "$app_environment" = dev ]; then
  mkdir -p "$REPO_ROOT/.sync-state/application-locks/$app_id"
  lock_recovery="$(mktemp -d "$REPO_ROOT/.sync-state/application-locks/$app_id/publish.XXXXXX")"
  lock_args=(--connection "$sqlcl_connection" --expected-user "$expected_user" --schema "$parsing_schema"
    --workspace "$workspace_name" --developer "$APEX_WORKSPACE_USERNAME" --app-id "$app_id"
    --classification "$target_environment" --run-dir "$lock_recovery")
  python3 "$REPO_ROOT/scripts/application_lock.py" acquire "${lock_args[@]}"
  lock_held=true
  lock_assert_script="$lock_recovery/assert-lock.sql"
fi
lifecycle_args=(--connection "$sqlcl_connection" --expected-user "$expected_user" --schema "$parsing_schema"
  --workspace "$workspace_name" --app-id "$app_id" --environment "$app_environment")
python3 "$REPO_ROOT/scripts/application_lifecycle.py" capture "${lifecycle_args[@]}" --run-dir "$staging_dir/lifecycle-before"
if [ "$app_environment" = dev ] && [ "$force" != true ]; then
  drift_guard="$REPO_ROOT/scripts/check_builder_drift.py"
  [ -f "$drift_guard" ] || fail "Builder drift guard is missing; refusing import"
  python3 "$drift_guard" "$app_id" "$sqlcl_connection" "$app_dir" \
    --expected-user "$expected_user" --state-out "$staging_dir/approved-live-state.txt"
  expected_live_state="$(head -n 1 "$staging_dir/approved-live-state.txt")"
  [[ "$expected_live_state" =~ ^(ABSENT|P\.([0-9T:-]+|NONE)\.[0-9A-F]*)$ ]] || \
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
  # Stamp a private copy, then swap it in only if nobody saved the file
  # meanwhile; a failed stamp leaves the working file untouched.
  cp -p -- "$app_dir/application.apx" "$staging_dir/application.apx.unstamped"
  cp -p -- "$app_dir/application.apx" "$staging_dir/application.apx.stamping"
  published_version="$(python3 "$REPO_ROOT/scripts/stamp_publish_version.py" \
    "$staging_dir/application.apx.stamping" "$DEVELOPER_NAME")" || exit 2
  cp -p -- "$staging_dir/application.apx.stamping" "$staging_dir/application.apx.stamped"
  cp -p -- "$staging_dir/application.apx.unstamped" "$staging_dir/application.apx.restore"
  # scratch/ is inside the repository, so these renames stay on one filesystem.
  stamp_status=0
  swap_if_unchanged "$app_dir/application.apx" "$staging_dir/application.apx.unstamped" \
    "$staging_dir/application.apx.stamping" "$staging_dir/application.apx.before-stamp" || stamp_status=$?
  case "$stamp_status" in
    0) ;;
    2) fail "could not move application.apx to stamp the publish tag; close any program holding it open (or check the folder's permissions) and publish again" ;;
    3) fail "could not install the stamped application.apx; nothing was imported, publish again" ;;
    *) fail "application.apx changed while the publish tag was stamped; publish again" ;;
  esac
  restore_unstamped=true
  printf 'Stamped application version: %s\n' "$published_version"
fi

application_source="$app_dir"
deployment_file="$app_dir/deployments/$app_environment.json"
import_running=true
import_attempted=true
sqlcl_status=0
(
  invoke_sqlcl_safe "$staging_dir" \
    -S -noupdates -name "$sqlcl_connection" \
    "@$REPO_ROOT/scripts/publish_app.sql" \
    "$parsing_schema" "$target_environment" "$expected_user" \
    "$application_source" "$deployment_file" "$app_id" "$expected_live_state" "$lock_assert_script" \
    < "$sqlcl_stdin"
) > "$sqlcl_output" 2>&1 || sqlcl_status=$?
if [ "$sqlcl_status" -ne 0 ]; then
  # Above 128: a signal ended SQLcl or the subshell running it (Ctrl-C is 130; a
  # SIGTERM sent to that subshell is 143 and leaves SQLcl running). The import may
  # have completed, so leave import_running set: cleanup says the result is unknown.
  if [ "$sqlcl_status" -gt 128 ]; then
    exit "$sqlcl_status"
  fi
  import_running=false
  cat "$sqlcl_output" >&2
  fail "SQLcl application import failed; see the client output above"
fi
import_running=false
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
import_unverified=true

# Re-export from the selected target and compare exact APEXlang bytes before
# claiming success. In DEV this same stable observation advances the drift
# baseline; staging and production checks never modify the DEV marker.
verify_run_dir="$staging_dir/post-import/runs/$app_id"
verify_parent="$verify_run_dir/apps/$parsing_schema"
mkdir -p "$verify_parent"
verify_sqlcl_output="$staging_dir/post-import/sqlcl-output.log"
verify_status=0
(
  invoke_sqlcl_safe "$verify_run_dir" \
    -S -noupdates -name "$sqlcl_connection" \
    "@$REPO_ROOT/scripts/export_apps.sql" \
    "$parsing_schema" "$app_id" "$target_environment" "$expected_user" \
    < "$sqlcl_stdin"
) > "$verify_sqlcl_output" 2>&1 || verify_status=$?
if [ "$verify_status" -ne 0 ]; then
  # Ended by a signal, as above: cleanup says DEV runs an unverified import.
  if [ "$verify_status" -gt 128 ]; then
    exit "$verify_status"
  fi
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
  --deployment-file "$deployment_file" --deployment-state "$verify_run_dir/.apex-deployment-state.json"
)
python3 "$REPO_ROOT/scripts/verify_publish_state.py" "${verify_args[@]}"
printf '%s\n' '{"schemaVersion":1,"sourceVerified":true,"lifecycleStatus":"unavailable"}' > "$staging_dir/publish-verification.json"
python3 "$REPO_ROOT/scripts/application_lifecycle.py" verify "${lifecycle_args[@]}" \
  --before "$staging_dir/lifecycle-before/lifecycle-snapshot.json" --run-dir "$staging_dir/lifecycle-after" \
  --summary-path "$staging_dir/publish-verification.json"
if [ "$app_environment" = dev ]; then
  python3 "$REPO_ROOT/scripts/application_lock.py" check "${lock_args[@]}"
  python3 "$REPO_ROOT/scripts/application_lock.py" release "${lock_args[@]}"
  lock_held=false
  import_attempted=false
  python3 "$REPO_ROOT/scripts/verify_publish_state.py" "${verify_args[@]}" --record-baseline
fi
import_unverified=false
printf 'Published APEX App %s to %s (%s / %s).\n' \
  "$app_id" "$target_label" "$workspace_name" "$parsing_schema"
if [ -n "$published_version" ]; then
  printf 'Commit the stamped version in %s: %s\n' \
    "${app_dir#"$REPO_ROOT/"}/application.apx" "$published_version"
fi
