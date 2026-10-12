#!/usr/bin/env python3
"""Read and check app-scoped APEX automation schedule states without mutation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from collections.abc import Mapping, Sequence
from datetime import datetime

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.application_lifecycle import capture_lifecycle, validate_snapshot
from scripts.db_targets import Target, TargetResolutionError, resolve_target
from scripts.deployment_descriptor import DescriptorError, read_descriptor
from scripts.local_config import ConfigError, read_project_env
from scripts.read_only_query import QueryError, resolve_query_target
from scripts.sqlcl_session import SqlclError, run_sqlcl


AUTOMATION_STATUSES = frozenset({"ACTIVE", "DISABLED", "ERROR"})
WRITE_ACTIONS = frozenset({"enable", "disable", "run-once"})
ACTION_IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_unique_name", "container_id", "container_name",
    "edition", "server_host", "service_name",
)
REFUSAL_REASONS = {
    "TARGET_IDENTITY": "the live database identity changed after confirmation",
    "WORKSPACE": "the selected APEX workspace is unavailable or changed",
    "APPLICATION": "the application scope is absent, changed, or locked",
    "APPLICATION_LOCKED": "application is locked by another Builder operation",
    "AUTOMATION": "the selected automation is absent or its schedule status changed",
    "PRIVILEGE": "the parsing schema lacks the required CREATE JOB system privilege",
    "SESSION": "the APEX workspace session could not be established",
}
ACTION_PHASES = frozenset({"enable", "disable", "run-once"})
MAX_AUTOMATION_ROWS = 500
APP_ID_RE = re.compile(r"[1-9][0-9]{0,17}\Z", re.ASCII)


class AutomationOperationError(ValueError):
    """The automation selection or its read-only evidence is incomplete."""


def list_automations(snapshot: Mapping) -> tuple[dict, ...]:
    """Return the lifecycle adapter's app-scoped automation rows in order."""
    rows = snapshot.get("automations") if isinstance(snapshot, Mapping) else None
    if not isinstance(rows, list) or len(rows) > MAX_AUTOMATION_ROWS:
        raise AutomationOperationError("automation visibility is incomplete or exceeds the output limit")
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise AutomationOperationError("automation visibility returned a malformed row")
        static_id, status = row.get("staticId"), row.get("status")
        if not _valid_static_id(static_id) or static_id in seen:
            raise AutomationOperationError("automation visibility returned an invalid or duplicate static ID")
        if not isinstance(status, str) or not status:
            raise AutomationOperationError("automation visibility returned an invalid schedule status")
        if status.upper() not in AUTOMATION_STATUSES:
            raise AutomationOperationError("automation visibility returned an unqualified schedule status")
        seen.add(static_id)
        result.append({"staticId": static_id, "status": status})
    return tuple(result)


def _valid_static_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= 255
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def select_automations(rows: Sequence[Mapping], static_ids: Sequence[str]) -> tuple[dict, ...]:
    """Select an exact, unique set of app-owned static IDs."""
    if not static_ids or len(static_ids) != len(set(static_ids)):
        raise AutomationOperationError("select one or more unique automation static IDs")
    if any(not _valid_static_id(value) for value in static_ids):
        raise AutomationOperationError("automation static IDs must be nonempty single-line text")
    indexed = {row["staticId"]: row for row in rows}
    missing = [value for value in static_ids if value not in indexed]
    if missing:
        raise AutomationOperationError("one or more selected automations are not present in this application")
    return tuple(indexed[value] for value in static_ids)


def _target(values: Mapping[str, str], environment: str, schema: str) -> Target:
    if environment == "dev":
        return resolve_query_target(values, environment, "apex", schema=schema)
    return resolve_target(values, environment, "read", schema=schema)


def _capture(repo_root: Path, values: Mapping[str, str], environment: str, schema: str, app_id: int) -> tuple[dict, Path, dict]:
    descriptor_path = repo_root / "apps" / schema / str(app_id) / "deployments" / f"{environment}.json"
    descriptor = read_descriptor(descriptor_path, app_id)
    parsing_schema = descriptor["app"]["databaseSession"]["parsingSchema"]
    if parsing_schema != schema:
        raise AutomationOperationError("descriptor parsing schema does not match the selected schema")
    target = _target(values, environment, schema)
    if target.schema != parsing_schema:
        raise AutomationOperationError("configured database target does not match the descriptor parsing schema")

    scratch_root = repo_root / "scratch"
    if scratch_root.is_symlink():
        raise AutomationOperationError("scratch directory cannot be a symbolic link")
    scratch = scratch_root / "automation-operations"
    if scratch.is_symlink():
        raise AutomationOperationError("automation evidence directory cannot be a symbolic link")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch.chmod(0o700)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{environment}-{app_id}-{uuid.uuid4().hex[:8]}-", dir=scratch))
    run_dir.chmod(0o700)
    try:
        snapshot = capture_lifecycle(target, descriptor["workspace"]["name"], app_id, run_dir / "lifecycle")
    except Exception as error:
        # SQLcl output and captured database payloads remain in ignored private
        # evidence; callers receive only a bounded status, never raw SQLcl text.
        raise AutomationOperationError(f"automation observation is unavailable; inspect private evidence at {run_dir}") from error
    identity = snapshot.get("identity", {})
    summary = {
        "environment": environment,
        "connection": target.connection,
        "sessionUser": identity.get("session_user"),
        "currentSchema": identity.get("current_schema"),
        "dbUniqueName": identity.get("db_unique_name"),
        "containerName": identity.get("container_name"),
        "service": identity.get("service_name"),
        "serverHost": identity.get("server_host"),
        "workspace": snapshot.get("workspace"),
        "appId": snapshot.get("appId"),
        "release": snapshot.get("release"),
    }
    return snapshot, run_dir, summary


def _validate_scope(values: Mapping[str, str], repo_root: Path, environment: str, schema: str, app_text: str) -> tuple[int, dict, Path, dict]:
    if environment not in {"dev", "staging", "prod"}:
        raise AutomationOperationError("--env must be dev, staging, or prod")
    if re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", schema, re.ASCII) is None:
        raise AutomationOperationError("--schema must be an uppercase Oracle identifier")
    if APP_ID_RE.fullmatch(app_text) is None:
        raise AutomationOperationError("application ID must be a positive numeric ID")
    app_id = int(app_text)
    snapshot, run_dir, scope = _capture(repo_root, values, environment, schema, app_id)
    if snapshot.get("applicationPresent") is not True:
        raise AutomationOperationError(f"application {app_id} is not visible in the selected workspace; private evidence: {run_dir}")
    if snapshot.get("coverage", {}).get("automations") is not True:
        raise AutomationOperationError(f"automation visibility is incomplete; private evidence: {run_dir}")
    return app_id, snapshot, run_dir, scope


def _result(action: str, status: str, scope: Mapping, rows: Sequence[Mapping], **extra) -> dict:
    return {
        "schemaVersion": 1,
        "action": action,
        "status": status,
        "scope": dict(scope),
        "count": len(rows),
        "automations": [dict(row) for row in rows],
        **extra,
    }


def _valid_workspace_user(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.encode("utf-8")) <= 255
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _iso_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def render_action_driver(
    target: Target,
    workspace: str,
    app_id: int,
    static_id: str,
    action: str,
    expected_status: str,
    workspace_user: str,
    expected_identity: Mapping[str, str],
    workspace_id: str,
    run_dir: Path,
) -> Path:
    """Write a private, argument-safe driver for one explicit APEX action."""
    if action not in ACTION_PHASES:
        raise AutomationOperationError("unknown automation action")
    if target.environment not in {"dev", "staging", "prod"}:
        raise AutomationOperationError("invalid automation database environment")
    if re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", target.schema, re.ASCII) is None:
        raise AutomationOperationError("invalid automation parsing schema")
    if re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", target.expected_user, re.ASCII) is None:
        raise AutomationOperationError("invalid automation database user")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", target.connection, re.ASCII) is None:
        raise AutomationOperationError("automation target must use a saved SQLcl connection name")
    if APP_ID_RE.fullmatch(str(app_id)) is None:
        raise AutomationOperationError("application ID must be a positive numeric ID")
    if not _valid_static_id(static_id):
        raise AutomationOperationError("automation static ID must be nonempty single-line text")
    if expected_status.upper() not in AUTOMATION_STATUSES:
        raise AutomationOperationError("selected automation has an unqualified schedule status")
    if not _valid_workspace_user(workspace_user):
        raise AutomationOperationError("set APEX_WORKSPACE_USERNAME to an existing workspace developer")
    if not isinstance(workspace, str) or not workspace or len(workspace.encode("utf-8")) > 255 or any(ord(c) < 32 for c in workspace):
        raise AutomationOperationError("invalid APEX workspace")
    if re.fullmatch(r"[1-9][0-9]{0,17}", str(workspace_id), re.ASCII) is None:
        raise AutomationOperationError("invalid APEX workspace identity")
    if any(not isinstance(expected_identity.get(field), str) or not expected_identity[field].strip() for field in ACTION_IDENTITY_FIELDS):
        raise AutomationOperationError("confirmed database identity is incomplete")
    if (
        expected_identity["session_user"].upper() != target.expected_user.upper()
        or expected_identity["current_schema"].upper() != target.schema.upper()
    ):
        raise AutomationOperationError("confirmed database identity does not match the selected target")

    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        raise AutomationOperationError("automation action directory cannot be a symbolic link")
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    adapter = Path(__file__).with_suffix(".sql").resolve()
    if any(char in str(adapter) for char in ('"', "\n", "\r")):
        raise AutomationOperationError("unsupported SQL adapter path")

    def hex_arg(value: str) -> str:
        return value.encode("utf-8").hex().upper()

    arguments = (
        action.upper(), str(app_id), hex_arg(static_id), hex_arg(expected_status.upper()),
        hex_arg(workspace_user), hex_arg(workspace), hex_arg(target.expected_user),
        hex_arg(target.schema), str(workspace_id),
        *(hex_arg(expected_identity[field]) for field in ACTION_IDENTITY_FIELDS),
    )
    driver = run_dir / "automation-action-driver.sql"
    lines = [
        "SET ECHO OFF", "SET VERIFY OFF", "SET FEEDBACK OFF", "SET HEADING OFF",
        "SET LINESIZE 32767", "SET SERVEROUTPUT ON SIZE UNLIMITED", "SET DEFINE ON",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK", "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        f'@"{adapter}" ' + " ".join(arguments),
        "SET DEFINE OFF", "EXIT SUCCESS ROLLBACK", "",
    ]
    driver.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    driver.chmod(0o600)
    return driver


def _strict_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("automation action result contains duplicate JSON keys")
        result[key] = value
    return result


def _parse_action_output(output: str, action: str, app_id: int, static_id: str) -> dict:
    lines = output.splitlines()
    begin, end, verified = (
        "APEX_AUTOMATION_ACTION_PAYLOAD_BEGIN",
        "APEX_AUTOMATION_ACTION_PAYLOAD_END",
        "APEX_AUTOMATION_ACTION_VERIFIED",
    )
    if lines.count(begin) != 1 or lines.count(end) != 1 or lines.count(verified) != 1:
        raise AutomationOperationError("automation action result frame is missing or duplicated")
    start, finish, marker = lines.index(begin), lines.index(end), lines.index(verified)
    if not start < finish < marker or finish <= start + 1:
        raise AutomationOperationError("automation action result frame is malformed")
    try:
        payload_text = "".join(lines[start + 1 : finish])
        payload = json.loads(payload_text, object_pairs_hook=_strict_json_pairs)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise AutomationOperationError("automation action result JSON is invalid") from error
    required = {
        "schemaVersion", "action", "appId", "workspace", "workspaceId", "staticId",
        "beforeStatus", "afterStatus", "apiReturned", "startedAt", "runLogCount", "sessionDeleted",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) not in (required, required | {"runLog"})
        or type(payload.get("schemaVersion")) is not int
        or payload["schemaVersion"] != 1
        or not isinstance(payload.get("action"), str)
        or payload["action"].casefold() != action.casefold()
        or type(payload.get("appId")) is not int
        or payload["appId"] != app_id
        or payload.get("staticId") != static_id
        or not isinstance(payload.get("workspace"), str)
        or not payload["workspace"]
        or re.fullmatch(r"[1-9][0-9]{0,17}", payload.get("workspaceId", ""), re.ASCII) is None
        or payload.get("apiReturned") is not True
        or payload.get("sessionDeleted") is not True
        or not isinstance(payload.get("beforeStatus"), str)
        or payload["beforeStatus"].upper() not in AUTOMATION_STATUSES
        or not isinstance(payload.get("afterStatus"), str)
        or payload["afterStatus"].upper() not in AUTOMATION_STATUSES
        or not _iso_timestamp(payload.get("startedAt"))
        or type(payload.get("runLogCount")) is not int
        or payload["runLogCount"] not in {0, 1}
    ):
        raise AutomationOperationError("automation action result does not match the requested scope")
    payload["action"] = action
    payload.setdefault("runLog", None)
    run_log = payload.get("runLog")
    if payload["runLogCount"] == 0:
        if run_log is not None:
            raise AutomationOperationError("automation action returned an unexpected execution log")
    else:
        if not isinstance(run_log, dict) or set(run_log) != {
            "id", "applicationId", "workspaceId", "workspace", "staticId", "statusCode",
            "status", "startedAt", "endedAt", "successfulRows", "errorRows",
        }:
            raise AutomationOperationError("automation execution log frame is malformed")
        if (
            not isinstance(run_log.get("id"), str)
            or re.fullmatch(r"[1-9][0-9]{0,17}", run_log["id"], re.ASCII) is None
            or type(run_log.get("applicationId")) is not int
            or run_log["applicationId"] != app_id
            or run_log.get("workspaceId") != payload["workspaceId"]
            or run_log.get("workspace") != payload["workspace"]
            or run_log.get("staticId") != static_id
            or not isinstance(run_log.get("statusCode"), str)
            or not run_log["statusCode"]
            or not isinstance(run_log.get("status"), str)
            or not run_log["status"]
            or not _iso_timestamp(run_log.get("startedAt"))
            or (run_log.get("endedAt") is not None and not _iso_timestamp(run_log["endedAt"]))
            or type(run_log.get("successfulRows")) is not int
            or run_log["successfulRows"] < 0
            or type(run_log.get("errorRows")) is not int
            or run_log["errorRows"] < 0
        ):
            raise AutomationOperationError("automation execution log scope or values are invalid")
    return payload


def _snapshot_scope_matches(left: Mapping, right: Mapping, target: Target, workspace: str, app_id: int) -> bool:
    try:
        validate_snapshot(left)
        validate_snapshot(right)
    except (TypeError, ValueError):
        return False
    return all(
        left.get(key) == right.get(key)
        for key in ("identity", "release", "workspace", "workspaceId", "appId", "applicationPresent")
    ) and (
        left.get("identity", {}).get("session_user", "").upper() == target.expected_user.upper()
        and left.get("identity", {}).get("current_schema", "").upper() == target.schema.upper()
        and left.get("workspace") == workspace
        and left.get("appId") == app_id
        and left.get("applicationPresent") is True
    )


def _automation_index(snapshot: Mapping) -> dict[str, dict]:
    return {row["staticId"]: row for row in list_automations(snapshot)}


def _store_action_evidence(run_dir: Path, report: Mapping) -> None:
    path = Path(run_dir) / "automation-action.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _action_result(action: str, status: str, scope: Mapping, static_id: str, before=None, after=None, **extra) -> dict:
    return {
        "schemaVersion": 1,
        "action": action,
        "status": status,
        "scope": dict(scope),
        "staticId": static_id,
        "before": before,
        "after": after,
        **extra,
    }


def _completed_run_log(run_log: Mapping | None, started_at: str, app_id: int, static_id: str, workspace_id: str) -> bool:
    if not isinstance(run_log, Mapping):
        return False
    if (
        run_log.get("applicationId") != app_id
        or run_log.get("staticId") != static_id
        or run_log.get("workspaceId") != workspace_id
        or run_log.get("statusCode", "").upper() != "SUCCESS"
        or run_log.get("status", "").casefold() not in {"success", "successful"}
        or run_log.get("endedAt") is None
        or run_log.get("errorRows") != 0
        or not _iso_timestamp(run_log.get("startedAt"))
        or not _iso_timestamp(run_log.get("endedAt"))
        or not _iso_timestamp(started_at)
    ):
        return False
    try:
        return datetime.fromisoformat(run_log["startedAt"].replace("Z", "+00:00")) >= datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False


def execute_automation_action(
    target: Target,
    workspace: str,
    app_id: int,
    static_id: str,
    action: str,
    expected_snapshot: Mapping,
    run_dir: Path,
    *,
    workspace_user: str | None = None,
) -> dict:
    """Apply one confirmed, app-scoped APEX action and independently read it back."""
    scope = {
        "environment": target.environment,
        "connection": target.connection,
        "sessionUser": target.expected_user,
        "currentSchema": target.schema,
        "workspace": workspace,
        "appId": app_id,
    }
    if action not in WRITE_ACTIONS:
        return _action_result(action, "refused", scope, static_id, reason="unknown automation action")
    if not _valid_static_id(static_id):
        return _action_result(action, "refused", scope, static_id, reason="invalid automation static ID")
    try:
        validate_snapshot(expected_snapshot)
        expected_rows = _automation_index(expected_snapshot)
        expected_selected = expected_rows.get(static_id)
        if expected_selected is None:
            raise AutomationOperationError("selected automation is not present in the confirmed application")
        if (
            expected_snapshot.get("appId") != app_id
            or expected_snapshot.get("workspace") != workspace
            or expected_snapshot.get("applicationPresent") is not True
        ):
            raise AutomationOperationError("confirmed application scope does not match the requested action")
        user = workspace_user if workspace_user is not None else os.environ.get("APEX_WORKSPACE_USERNAME")
        if not _valid_workspace_user(user):
            raise AutomationOperationError("set APEX_WORKSPACE_USERNAME to an existing workspace developer")
    except (AutomationOperationError, TypeError, ValueError) as error:
        return _action_result(action, "refused", scope, static_id, reason=str(error))

    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        return _action_result(action, "refused", scope, static_id, reason="automation action evidence directory is a symbolic link")
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    try:
        live_before = capture_lifecycle(target, workspace, app_id, run_dir / "live-before")
    except (OSError, SqlclError, ValueError) as error:
        report = _action_result(action, "refused", scope, static_id, before=expected_selected,
                                apiOutcome="not-started",
                                reason=f"live scope recheck is unavailable ({type(error).__name__})")
        _store_action_evidence(run_dir, report)
        return report
    try:
        live_rows = _automation_index(live_before)
        live_selected = live_rows.get(static_id)
        same_scope = _snapshot_scope_matches(expected_snapshot, live_before, target, workspace, app_id)
        same_selection = live_selected == expected_selected
    except (AutomationOperationError, KeyError, TypeError, ValueError):
        live_selected = None
        same_scope = False
        same_selection = False
    if not same_scope or not same_selection:
        report = _action_result(
            action, "refused", scope, static_id,
            before=expected_selected,
            liveBefore=live_selected,
            reason="target, application, or selected automation changed after confirmation",
        )
        _store_action_evidence(run_dir, report)
        return report

    driver_dir = run_dir / "action-session"
    try:
        driver = render_action_driver(
            target, workspace, app_id, static_id, action, live_selected["status"], user,
            live_before["identity"], live_before["workspaceId"], driver_dir,
        )
    except (AutomationOperationError, OSError, ValueError) as error:
        report = _action_result(
            action, "refused", scope, static_id, before=live_selected,
            apiOutcome="not-started", reason=f"action preflight failed ({type(error).__name__})",
            evidence=str(driver_dir),
        )
        _store_action_evidence(run_dir, report)
        return report
    try:
        result = run_sqlcl(target, driver, driver_dir)
        payload = _parse_action_output(result.output, action, app_id, static_id)
    except SqlclError as error:
        refusal_match = re.search(r"APEX_AUTOMATION_ACTION_REFUSED:([A-Z_]+)", error.output)
        refusal = refusal_match.group(1) if refusal_match else None
        report = _action_result(
            action,
            "refused" if refusal else "unknown",
            scope,
            static_id,
            before=live_selected,
            apiOutcome="not-started" if refusal else "unknown",
            reason=REFUSAL_REASONS[refusal] if refusal else "SQLcl did not confirm the automation action; do not replay until reconciled",
            evidence=str(error.run_dir),
        )
        _store_action_evidence(run_dir, report)
        return report
    except (AutomationOperationError, OSError, ValueError) as error:
        report = _action_result(
            action, "unknown", scope, static_id, before=live_selected, apiOutcome="unknown",
            reason=f"automation action evidence is incomplete ({type(error).__name__})",
            evidence=str(run_dir),
        )
        _store_action_evidence(run_dir, report)
        return report

    try:
        after_snapshot = capture_lifecycle(target, workspace, app_id, run_dir / "live-after")
        after_rows = _automation_index(after_snapshot)
        after_selected = after_rows.get(static_id)
        same_after_scope = _snapshot_scope_matches(expected_snapshot, after_snapshot, target, workspace, app_id)
        unselected_before = {key: row for key, row in live_rows.items() if key != static_id}
        unselected_after = {key: row for key, row in after_rows.items() if key != static_id}
    except (OSError, SqlclError, AutomationOperationError, TypeError, ValueError):
        after_selected = None
        same_after_scope = False
        unselected_before = {}
        unselected_after = {"<unavailable>": {}}

    action_frame_matches = (
        payload.get("workspace") == workspace
        and payload.get("workspaceId") == live_before.get("workspaceId")
        and payload.get("beforeStatus", "").upper() == live_selected["status"].upper()
        and payload.get("afterStatus", "").upper() in AUTOMATION_STATUSES
        and after_selected is not None
        and payload.get("afterStatus", "").upper() == after_selected["status"].upper()
        and payload.get("sessionDeleted") is True
    )
    unchanged_unselected = unselected_before == unselected_after
    status = "unknown"
    reason = "post-action scope or selected state could not be verified"
    run_log = payload.get("runLog")
    if same_after_scope and action_frame_matches and unchanged_unselected and after_selected is not None:
        if action == "enable" and after_selected["status"].upper() == "ACTIVE":
            status, reason = "verified", None
        elif action == "disable" and after_selected["status"].upper() == "DISABLED":
            status, reason = "verified", None
        elif action == "run-once" and _completed_run_log(
            run_log, payload["startedAt"], app_id, static_id, live_before["workspaceId"]
        ) and after_selected["status"] == live_selected["status"]:
            status, reason = "verified", None

    after_record = None
    if after_selected:
        after_record = {
            "staticId": static_id,
            "status": after_selected.get("status"),
            "unselected": {key: row["status"] for key, row in unselected_after.items()},
        }
    report = _action_result(
        action,
        status,
        scope,
        static_id,
        before={"staticId": static_id, "status": live_selected["status"]},
        after=after_record,
        apiOutcome="returned",
        runLog=run_log,
        reason=reason,
    )
    _store_action_evidence(run_dir, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "check", "enable", "disable", "run-once"))
    parser.add_argument("app_id", help="numeric application ID")
    parser.add_argument("--env", required=True, choices=("dev", "staging", "prod"))
    parser.add_argument("--schema", help="application parsing schema; defaults to PROJECT_SCHEMA")
    parser.add_argument("--automation", action="append", default=[], help="exact static ID; repeat to select several")
    parser.add_argument("--expected-status", help="required by check; APEX 26.2: ACTIVE, DISABLED, or ERROR")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--env-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    schema = args.schema or os.environ.get("PROJECT_SCHEMA", "")
    if args.action != "check" and args.expected_status is not None:
        parser.error("--expected-status is valid only with check")
    if args.action == "check":
        if not args.automation:
            parser.error("check requires one or more --automation static IDs")
        if args.expected_status not in AUTOMATION_STATUSES:
            parser.error("--expected-status must be ACTIVE, DISABLED, or ERROR for APEX 26.2")
    elif args.action in WRITE_ACTIONS:
        if len(args.automation) != 1:
            parser.error(f"{args.action} requires exactly one --automation static ID")
    elif args.action == "list" and args.automation:
        parser.error("list does not accept --automation; use check to select exact IDs")

    env_value = args.env_file or Path(os.environ.get("PROJECT_ENV_FILE", repo_root / ".env"))
    env_path = env_value if env_value.is_absolute() else (repo_root / env_value).resolve()
    try:
        values = read_project_env(env_path)
        app_id, snapshot, run_dir, scope = _validate_scope(values, repo_root, args.env, schema, args.app_id)
        rows = list_automations(snapshot)
        selected = select_automations(rows, args.automation) if args.automation else rows
        if args.action == "check":
            matched = [row for row in selected if row["status"].upper() == args.expected_status]
            status = "pass" if len(matched) == len(selected) else "mismatch"
            report = _result(args.action, status, scope, selected, expectedStatus=args.expected_status)
            report["mismatches"] = [row["staticId"] for row in selected if row["status"].upper() != args.expected_status]
        elif args.action in WRITE_ACTIONS:
            selected_row = selected[0]
            if not _valid_workspace_user(values.get("APEX_WORKSPACE_USERNAME")):
                raise AutomationOperationError("set APEX_WORKSPACE_USERNAME to an existing workspace developer")
            confirmation = f"{args.action.upper()} {app_id} {selected_row['staticId']}"
            identity = snapshot["identity"]
            print(
                f"Target environment={scope['environment']} via {scope['connection']}: "
                f"user={scope['sessionUser']} schema={scope['currentSchema']} "
                f"db={identity['db_unique_name']} container={identity['container_name']} "
                f"host={identity['server_host']} service={identity['service_name']} "
                f"workspace={scope['workspace']} app={scope['appId']} APEX={scope['release']}",
                file=sys.stderr,
            )
            print(
                f"Action {args.action}: automation {selected_row['staticId']} "
                f"current status={selected_row['status']}",
                file=sys.stderr,
            )
            print(
                "This creates and deletes an APEX session; the application's configured session initialization and cleanup code may run.",
                file=sys.stderr,
            )
            print(f"Type exactly {confirmation!r} to continue: ", end="", file=sys.stderr, flush=True)
            try:
                entered = input().strip()
            except EOFError:
                entered = ""
            if entered != confirmation:
                report = _action_result(
                    args.action, "refused", scope, selected_row["staticId"],
                    before=selected_row,
                    reason="exact action confirmation was not supplied",
                )
            else:
                target = _target(values, args.env, schema)
                report = execute_automation_action(
                    target,
                    scope["workspace"],
                    app_id,
                    selected_row["staticId"],
                    args.action,
                    snapshot,
                    run_dir,
                    workspace_user=values["APEX_WORKSPACE_USERNAME"],
                )
        else:
            report = _result(args.action, "observed", scope, selected)
    except (ConfigError, QueryError, TargetResolutionError, DescriptorError, AutomationOperationError, OSError, ValueError) as error:
        report = {"schemaVersion": 1, "action": args.action, "status": "unavailable", "reason": str(error)}

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    else:
        print(f"Automations {report['action']}: {report['status']}")
        scope = report.get("scope", {})
        if scope:
            print(
                f"Target {scope['environment'].upper()} via {scope['connection']}: "
                f"user={scope['sessionUser']} schema={scope['currentSchema']} "
                f"workspace={scope['workspace']} app={scope['appId']} APEX={scope['release']}"
            )
        if "count" in report:
            print(f"Count: {report['count']}")
        for row in report.get("automations", []):
            print(f"  {json.dumps(row['staticId'], ensure_ascii=True)}: {row['status']}")
        if report.get("expectedStatus"):
            print(f"Expected status: {report['expectedStatus']}")
        if report.get("mismatches"):
            print("Mismatches: " + ", ".join(json.dumps(value, ensure_ascii=True) for value in report["mismatches"]))
        if report.get("staticId"):
            print(f"Selected automation: {json.dumps(report['staticId'], ensure_ascii=True)}")
        if report.get("before") is not None:
            print(f"Before: {json.dumps(report['before'], ensure_ascii=True, sort_keys=True)}")
        if report.get("after") is not None:
            print(f"After: {json.dumps(report['after'], ensure_ascii=True, sort_keys=True)}")
        if report.get("runLog"):
            print(f"Execution log: {json.dumps(report['runLog'], ensure_ascii=True, sort_keys=True)}")
        if report.get("reason"):
            print("Reason: " + report["reason"], file=sys.stderr)
    if report["status"] in {"unavailable", "refused", "unknown"}:
        return 2
    if report["status"] == "mismatch":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
