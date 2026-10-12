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

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.application_lifecycle import capture_lifecycle
from scripts.db_targets import Target, TargetResolutionError, resolve_target
from scripts.deployment_descriptor import DescriptorError, read_descriptor
from scripts.local_config import ConfigError, read_project_env
from scripts.read_only_query import QueryError, resolve_query_target


AUTOMATION_STATUSES = frozenset({"ACTIVE", "DISABLED", "ERROR"})
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
        "service": identity.get("service_name"),
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "check"))
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
    if args.action == "list" and args.expected_status is not None:
        parser.error("--expected-status is valid only with check")
    if args.action == "check":
        if not args.automation:
            parser.error("check requires one or more --automation static IDs")
        if args.expected_status not in AUTOMATION_STATUSES:
            parser.error("--expected-status must be ACTIVE, DISABLED, or ERROR for APEX 26.2")

    env_value = args.env_file or Path(os.environ.get("PROJECT_ENV_FILE", repo_root / ".env"))
    env_path = env_value if env_value.is_absolute() else (repo_root / env_value).resolve()
    try:
        values = read_project_env(env_path)
        _app_id, snapshot, _run_dir, scope = _validate_scope(values, repo_root, args.env, schema, args.app_id)
        rows = list_automations(snapshot)
        selected = select_automations(rows, args.automation) if args.automation else rows
        if args.action == "check":
            matched = [row for row in selected if row["status"].upper() == args.expected_status]
            status = "pass" if len(matched) == len(selected) else "mismatch"
            report = _result(args.action, status, scope, selected, expectedStatus=args.expected_status)
            report["mismatches"] = [row["staticId"] for row in selected if row["status"].upper() != args.expected_status]
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
        if report.get("reason"):
            print("Reason: " + report["reason"], file=sys.stderr)
    if report["status"] == "unavailable":
        return 2
    if report["status"] == "mismatch":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
