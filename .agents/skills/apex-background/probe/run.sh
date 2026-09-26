#!/usr/bin/env bash
# Local APEX background probe. Database phases require the configured DEV
# SQLcl profile and an interactive, phase-specific confirmation.
set -euo pipefail

usage() {
  printf 'usage: run.sh <install|start|finish|report|uninstall> <sqlcl-connection> <workspace> <parsing-schema>\n' >&2
  exit 2
}

fail() {
  printf 'probe error: %s\n' "$1" >&2
  exit 1
}

[[ "$#" -eq 4 ]] || usage
phase="$1"
connection="$2"
workspace="$3"
schema="$4"
app_id=9901
case "$phase" in
  install|start|finish|report|uninstall) ;;
  *) usage ;;
esac

[[ "$connection" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || fail 'invalid SQLcl saved connection name'
[[ "$workspace" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$ ]] || fail 'workspace must be a simple APEX workspace name'
[[ "$schema" =~ ^[A-Za-z][A-Za-z0-9_\$#]{0,29}$ ]] || fail 'parsing schema must be an unquoted Oracle identifier'

probe_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(git -C "$probe_dir" rev-parse --show-toplevel)"
state_dir="$probe_dir/.run"

if [[ -L "$state_dir" || ( -e "$state_dir" && ! -d "$state_dir" ) ]]; then
  fail '.run must be a real directory, not a file or symlink'
fi

if [[ "$phase" == report ]]; then
  [[ -d "$state_dir" ]] || fail 'no local probe run exists; run the gated database phases first'
  for input in "$state_dir/log.csv" "$state_dir/faults.csv"; do
    [[ -f "$input" && ! -L "$input" ]] || fail "missing or unsafe report input: ${input##*/}"
  done
  : "${APEX_VERSION:?set APEX_VERSION}"
  : "${DATABASE_NAME:?set DATABASE_NAME}"
  python3 "$probe_dir/render_findings.py" "$state_dir/log.csv" "$state_dir/faults.csv" \
    --apex "$APEX_VERSION" --database "$DATABASE_NAME"
  exit 0
fi

[[ -f "$repo_root/.env" ]] || fail 'copy .env.example to .env and configure the DEV APEX profile first'
profile_value() {
  python3 - "$repo_root/.env" "$1" <<'PY'
from pathlib import Path
import sys

path, wanted = Path(sys.argv[1]), sys.argv[2]
matches = []
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    key, separator, value = line.partition("=")
    if separator and key.strip() == wanted:
        matches.append(value)
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
PY
}

db_environment="$(profile_value DB_ENVIRONMENT)" || fail 'DB_ENVIRONMENT must be set once in .env'
profile_connection="$(profile_value APEX_SQLCL_CONNECTION)" || fail 'APEX_SQLCL_CONNECTION must be set once in .env'
profile_schema="$(profile_value APEX_PARSING_SCHEMA)" || fail 'APEX_PARSING_SCHEMA must be set once in .env'
expected_user="$(profile_value APEX_EXPECTED_USER)" || fail 'APEX_EXPECTED_USER must be set once in .env'
[[ "$db_environment" == development ]] || fail 'probe phases are restricted to DB_ENVIRONMENT=development'
[[ "$connection" == "$profile_connection" ]] || fail 'connection does not match APEX_SQLCL_CONNECTION in .env'
[[ "${schema^^}" == "${profile_schema^^}" ]] || fail 'schema does not match APEX_PARSING_SCHEMA in .env'
[[ "${schema^^}" == "${expected_user^^}" ]] || fail 'schema does not match APEX_EXPECTED_USER in .env'
command -v sql >/dev/null 2>&1 || fail 'SQLcl command "sql" was not found on PATH'

if [[ ! -e "$state_dir" ]]; then
  mkdir -m 700 "$state_dir"
fi
[[ -O "$state_dir" ]] || fail '.run must be owned by the current user'
chmod 700 "$state_dir"
safe_output() {
  local path="$1"
  [[ ! -L "$path" ]] || fail "refusing symlink output: ${path##*/}"
  [[ ! -e "$path" || -f "$path" ]] || fail "output must be a regular file: ${path##*/}"
  if [[ -e "$path" ]]; then
    [[ "$(stat -c %h -- "$path")" == 1 ]] || fail "refusing hard-linked output: ${path##*/}"
  fi
}

[[ ! -L "$state_dir/.stdin" ]] || fail '.run/.stdin must not be a symlink'
[[ ! -e "$state_dir/.stdin" || -f "$state_dir/.stdin" ]] || fail '.run/.stdin must be a regular file'
if [[ -e "$state_dir/.stdin" ]]; then
  [[ "$(stat -c %h -- "$state_dir/.stdin")" == 1 ]] || fail 'refusing hard-linked .run/.stdin'
fi
: > "$state_dir/.stdin"
chmod 600 "$state_dir/.stdin"

sqlcl() {
  sql -S -noupdates -name "$connection" "$@" < "$state_dir/.stdin"
}

require_line() {
  grep -Eq "^[[:space:]]*$1[[:space:]]*$" "$2" || {
    cat "$2" >&2
    fail "SQLcl did not report $1"
  }
}

preflight_log="$state_dir/preflight.log"
safe_output "$preflight_log"
(cd "$probe_dir" && sqlcl @target-check.sql "$schema" "$workspace" "$phase") | tee "$preflight_log"
require_line APEX_BG_PROBE_PREFLIGHT_OK "$preflight_log"
target_line="$(grep -E '^[[:space:]]*APEX_BG_PROBE_TARGET:' "$preflight_log" | tail -n 1 | xargs)"
[[ -n "$target_line" ]] || fail 'SQLcl did not return the target database identity'
object_state="$(sed -n 's/^[[:space:]]*APEX_BG_PROBE_OBJECTS://p' "$preflight_log" | tail -n 1 | tr -d '[:space:]')"

case "$phase" in
  install) effect='create probe objects and import application 9901' ;;
  start) effect='start automations, create tasks/workflow, and enable the scheduled automation' ;;
  finish) effect='finish the probe run, refresh its package body, approve probe tasks, and disable the scheduled automation' ;;
  uninstall) effect='remove application 9901 and drop marked probe objects' ;;
esac
[[ -t 0 ]] || fail 'interactive phase confirmation is required'
confirmation="CONFIRM $phase $connection $workspace $schema $target_line"
printf '\nThis phase will %s.\nTarget: %s\nType exactly: %s\n' "$effect" "$target_line" "$confirmation" >&2
read -r answer
[[ "$answer" == "$confirmation" ]] || fail 'phase confirmation did not match'

case "$phase" in
  install)
    if [[ -L "$state_dir/app" || ( -e "$state_dir/app" && ! -d "$state_dir/app" ) ]]; then
      fail '.run/app must be a real directory, not a file or symlink'
    fi
    if [[ -d "$state_dir/app" ]]; then
      [[ -O "$state_dir/app" ]] || fail '.run/app must be owned by the current user'
      previous_app="$state_dir/app.previous-$(date -u +%Y%m%dT%H%M%S%N)"
      [[ ! -e "$previous_app" && ! -L "$previous_app" ]] || fail 'could not allocate a unique previous app path'
      mv -- "$state_dir/app" "$previous_app"
      printf 'Preserved the prior staged app at %s\n' "$previous_app"
    fi
    safe_output "$state_dir/import.sql"
    safe_output "$state_dir/install.log"
    safe_output "$state_dir/import.log"
    python3 - "$probe_dir/app" "$state_dir/app" "$workspace" "$schema" <<'PY'
from pathlib import Path
import json
import shutil
import sys

source, destination = Path(sys.argv[1]), Path(sys.argv[2])
workspace, schema = sys.argv[3:]
for path in source.rglob("*"):
    if path.is_symlink():
        raise SystemExit(f"refusing symlink in probe app source: {path}")
shutil.copytree(source, destination)
(destination / "deployments").mkdir(exist_ok=True)
descriptor = {
    "workspace": {"name": workspace},
    "app": {"id": 9901, "databaseSession": {"parsingSchema": schema}},
}
(destination / "deployments" / "probe.json").write_text(
    json.dumps(descriptor, indent=2) + "\n", encoding="utf-8"
)
PY
    printf 'apex import -input . -deployment deployments/probe.json\nexit\n' > "$state_dir/import.sql"
    case "$object_state" in
      FRESH)
        (cd "$probe_dir" && sqlcl @install.sql) | tee "$state_dir/install.log"
        require_line APEX_BG_PROBE_INSTALLED "$state_dir/install.log"
        require_line APEX_BG_PROBE_PACKAGE_BODY_READY "$state_dir/install.log"
        ;;
      OWNED)
        printf 'Resuming import; the marked probe objects are already installed.\n' | tee "$state_dir/install.log"
        ;;
      *) fail 'SQLcl did not report a recognized probe-object state' ;;
    esac
    (cd "$state_dir/app" && sqlcl "@$state_dir/import.sql") | tee "$state_dir/import.log"
    grep -Fq 'Import successful.' "$state_dir/import.log" || fail 'SQLcl did not report a successful APEXlang import'
    ;;
  start)
    safe_output "$state_dir/start.log"
    label="apex-bg-probe $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    (cd "$probe_dir" && sqlcl @contexts.sql "$app_id" "$label") | tee "$state_dir/start.log"
    require_line APEX_BG_PROBE_STARTED "$state_dir/start.log"
    printf 'Open application 9901 (APEX-BG-PROBE) in workspace %s and click "Run probe".\n' "$workspace"
    printf 'Local ORDS URL template: http://localhost:8181/ords/r/%s/apex-bg-probe/home\n' "${workspace,,}"
    ;;
  finish)
    safe_output "$state_dir/finish.log"
    safe_output "$state_dir/log.csv"
    safe_output "$state_dir/faults.csv"
    archive_stamp="$(date -u +%Y%m%dT%H%M%S%N)"
    for name in finish.log log.csv faults.csv; do
      prior="$state_dir/$name"
      if [[ -e "$prior" ]]; then
        archive="$state_dir/$name.previous-$archive_stamp"
        safe_output "$archive"
        mv -- "$prior" "$archive"
      fi
    done
    finish_status=0
    (cd "$state_dir" && sqlcl "@$probe_dir/finish.sql" "$app_id") | tee "$state_dir/finish.log" || finish_status=$?
    [[ -s "$state_dir/log.csv" ]] || fail 'finish did not export log.csv; prior evidence was retained in .run'
    [[ -s "$state_dir/faults.csv" ]] || fail 'finish did not export faults.csv; prior evidence was retained in .run'
    if (( finish_status != 0 )); then
      printf 'finish returned an error after exporting evidence; the active database run was retained. Run the report phase to inspect it.\n' >&2
      fail 'finish did not complete; report the retained evidence, then retry or uninstall'
    fi
    require_line APEX_BG_PROBE_PACKAGE_BODY_READY "$state_dir/finish.log"
    require_line APEX_BG_PROBE_FINISHED "$state_dir/finish.log"
    ;;
  uninstall)
    safe_output "$state_dir/remove.sql"
    safe_output "$state_dir/remove.log"
    safe_output "$state_dir/uninstall.log"
    cat > "$state_dir/remove.sql" <<SQL
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
BEGIN
  DECLARE
    l_count PLS_INTEGER;
    l_id_count PLS_INTEGER;
    l_workspace_id NUMBER;
    l_current_workspace_id NUMBER;
  BEGIN
    l_workspace_id := apex_util.find_security_group_id(p_workspace => '$workspace');
    IF l_workspace_id IS NULL OR l_workspace_id = 0 THEN
      raise_application_error(-20021, 'The requested APEX workspace is missing or ambiguous');
    END IF;
    apex_util.set_security_group_id(p_security_group_id => l_workspace_id);
    l_current_workspace_id := NVL(NV('FLOW_SECURITY_GROUP_ID'), 0);
    IF l_current_workspace_id <> l_workspace_id THEN
      raise_application_error(-20022, 'The current APEX security group does not match the requested workspace');
    END IF;
    SELECT COUNT(*) INTO l_count
      FROM apex_workspaces
     WHERE workspace_id = l_workspace_id
       AND UPPER(workspace) = UPPER('$workspace');
    IF l_count <> 1 THEN
      raise_application_error(-20022, 'The requested APEX workspace context is not visible');
    END IF;
    SELECT COUNT(*) INTO l_id_count
      FROM apex_applications WHERE application_id = $app_id;
    SELECT COUNT(*) INTO l_count
      FROM apex_applications
     WHERE application_id = $app_id
       AND UPPER(alias) = 'APEX-BG-PROBE'
       AND UPPER(workspace) = UPPER('$workspace')
       AND UPPER(owner) = UPPER('$schema');
    IF l_count = 1 THEN
      apex_application_install.set_workspace('$workspace');
      apex_application_install.remove_application($app_id);
    ELSIF l_id_count > 0 THEN
      raise_application_error(-20021, 'Application ID $app_id exists but does not match the probe identity');
    END IF;
  END;
END;
/
PROMPT APEX_BG_PROBE_APP_REMOVED
EXIT SUCCESS COMMIT
SQL
    sqlcl "@$state_dir/remove.sql" | tee "$state_dir/remove.log"
    require_line APEX_BG_PROBE_APP_REMOVED "$state_dir/remove.log"
    (cd "$probe_dir" && sqlcl @uninstall.sql) | tee "$state_dir/uninstall.log"
    require_line APEX_BG_PROBE_UNINSTALLED "$state_dir/uninstall.log"
    printf 'Local .run files were retained for inspection; remove them manually after review.\n'
    ;;
esac
