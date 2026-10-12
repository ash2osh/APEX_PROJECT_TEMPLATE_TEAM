#!/usr/bin/env python3
"""Qualify explicit APEX 26.2 automation actions on a disposable local app."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.apex_compatibility import require_sqlcl_version
from scripts.application_lifecycle import capture_lifecycle, validate_snapshot
from scripts.automation_operations import execute_automation_action, list_automations
from scripts.db_targets import Target
from scripts.path_utils import relative_to_root
from scripts.validate_app_source import validate_import_effects
from tools.probe_apex_26_2 import (
    Probe,
    ProbeError,
    ProbeTarget,
    command_path,
    literal,
    tree_hashes,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DEFAULT = ROOT / "tests/fixtures/apex_26_2/canonical_26_2"
SELECTED_STATIC_ID = "TEMPLATE-AUTOMATION-SELECTED"
UNSELECTED_STATIC_ID = "TEMPLATE-AUTOMATION-UNSELECTED"
AUTOMATION_STATUSES = {"ACTIVE", "DISABLED", "ERROR"}


class QualificationError(ValueError):
    """A local automation qualification did not reach a safe verified result."""


def _output_path(output: Path, repo_root: Path) -> Path:
    root = repo_root.resolve()
    scratch = root / "scratch"
    candidate = Path(os.path.abspath(output if output.is_absolute() else root / output))
    try:
        relative_to_root(candidate, scratch)
    except ValueError as error:
        raise ValueError("--report-dir must be a fresh directory under this checkout's scratch/") from error
    if candidate == scratch or candidate.exists() or candidate.is_symlink():
        raise ValueError("qualification requires a fresh report directory")
    for path in (candidate, *candidate.parents):
        if path == root:
            break
        if path.is_symlink():
            raise ValueError("qualification output cannot contain symlinked parents")
    if not relative_to_root(candidate.resolve(), scratch):
        raise ValueError("qualification output escaped scratch/")
    return candidate


def prepare_fixture_source(
    source: Path,
    destination: Path,
    *,
    app_alias: str,
    workspace: str,
    app_id: int,
    schema: str,
) -> Path:
    """Build a fresh APEXlang fixture with two disabled, no-op automations."""
    source = Path(source)
    if source.is_symlink():
        raise QualificationError("fixture source cannot be a symbolic link")
    source = source.resolve(strict=True)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise QualificationError("fixture staging directory must be new")
    if any(char in str(destination) for char in ('"', "&", "\0", "\r", "\n")):
        raise QualificationError("fixture path cannot be passed safely through SQLcl")
    if not re.fullmatch(r"APEX262-PROBE-[1-9][0-9]{0,17}-[A-F0-9]{12}", app_alias, re.ASCII):
        raise QualificationError("fixture application alias is not run-owned")
    if type(app_id) is not int or not 0 < app_id < 10**18:
        raise QualificationError("fixture application ID must be a positive numeric ID")
    if not workspace or not schema:
        raise QualificationError("fixture workspace and parsing schema are required")
    hashes = tree_hashes(source)
    if not hashes:
        raise QualificationError("fixture source is empty")
    metadata_path = source / ".apex/apexlang.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not str(metadata.get("mmdVersion", "")).startswith("26.2."):
        raise QualificationError("automation qualification requires an APEX 26.2 source fixture")
    validate_import_effects(source)
    application_path = source / "application.apx"
    application_text = application_path.read_text(encoding="utf-8")
    if re.search(r"(?i)initializ", application_text):
        raise QualificationError("fixture application initialization must remain empty")

    shutil.copytree(source, destination)
    application_text, changed = re.subn(
        r"(?m)^app [^\s]+ \(", f"app {app_alias} (", application_text, count=1
    )
    if changed != 1:
        raise QualificationError("fixture application must contain one application declaration")
    (destination / "application.apx").write_text(application_text, encoding="utf-8", newline="\n")

    automation_source = (ROOT / "tests/fixtures/lifecycle/runtime/automation.apx").read_text(encoding="utf-8")
    if "begin null; end;" not in automation_source or "scheduleStatus: disabled" not in automation_source:
        raise QualificationError("reviewed no-op automation fixture is unavailable")
    automation_source = automation_source.replace(
        "FREQ=DAILY;BYHOUR=0;BYMINUTE=0;BYSECOND=0",
        "FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=31;BYHOUR=23;BYMINUTE=59;BYSECOND=0",
    )
    automation_dir = destination / "shared-components/automations"
    automation_dir.mkdir(parents=True, exist_ok=True)
    for static_id, name, filename in (
        (SELECTED_STATIC_ID, "Automation qualification selected", "automation-selected.apx"),
        (UNSELECTED_STATIC_ID, "Automation qualification unselected", "automation-unselected.apx"),
    ):
        contents = automation_source.replace("TEMPLATE_LIFECYCLE_PROBE", static_id)
        contents = contents.replace("Template lifecycle probe", name)
        (automation_dir / filename).write_text(contents, encoding="utf-8", newline="\n")

    deployment = {
        "workspace": {"name": workspace},
        "app": {"id": app_id, "databaseSession": {"parsingSchema": schema}},
    }
    deployment_path = destination / "deployments/automation-qualification.json"
    deployment_path.parent.mkdir(parents=True, exist_ok=True)
    deployment_path.write_text(json.dumps(deployment, indent=2) + "\n", encoding="utf-8")
    return destination


def cleanup_policy(action_status: str) -> str:
    """Only a fully verified action run may automatically remove its fixture."""
    return "cleanup" if action_status == "verified" else "retain"


def _fixture_guard(target: ProbeTarget, alias: str, developer: str) -> str:
    return f'''declare n number; v_workspace_id number; begin
 select count(*) into n from apex_applications
  where application_id={target.app_id} or upper(alias)=upper({literal(alias)});
 if n<>0 then raise_application_error(-20085,'Disposable application identity occupied'); end if;
 v_workspace_id:=apex_util.find_security_group_id({literal(target.workspace)});
 if v_workspace_id is null then raise_application_error(-20081,'Workspace unavailable'); end if;
 select count(*) into n from apex_workspace_apex_users
  where workspace_id=v_workspace_id and upper(user_name)=upper({literal(developer)})
    and (is_admin='Yes' or is_application_developer='Yes');
 if n<>1 then raise_application_error(-20084,'Existing workspace developer is required'); end if;
end;
/
'''


def _assert_snapshot(snapshot: Mapping, workspace: str, app_id: int, *, selected: str, unselected: str) -> dict[str, str]:
    validate_snapshot(snapshot)
    if snapshot.get("workspace") != workspace or snapshot.get("appId") != app_id or snapshot.get("applicationPresent") is not True:
        raise QualificationError("fixture lifecycle scope did not match the exact app/workspace")
    rows = list_automations(snapshot)
    indexed = {row["staticId"].upper(): row["status"].upper() for row in rows}
    if set(indexed) != {SELECTED_STATIC_ID, UNSELECTED_STATIC_ID}:
        raise QualificationError("fixture did not expose exactly its two owned automations")
    if indexed.get(UNSELECTED_STATIC_ID) != "DISABLED":
        raise QualificationError("unselected fixture automation is not disabled")
    if indexed.get(SELECTED_STATIC_ID) != selected.upper():
        raise QualificationError("selected fixture automation has an unexpected schedule state")
    if selected.upper() not in AUTOMATION_STATUSES or unselected.upper() != "DISABLED":
        raise QualificationError("qualification expectation is not a known APEX 26.2 state")
    return indexed


def _write_report(directory: Path, report: Mapping) -> Path:
    path = directory / "qualification.json"
    temporary = directory / "qualification.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
    return path


def qualify(
    target: ProbeTarget,
    output: Path,
    *,
    source: Path = SOURCE_DEFAULT,
    developer: str,
    repo_root: Path = ROOT,
    probe_factory: Callable = Probe,
    lifecycle_capture: Callable = capture_lifecycle,
    action_executor: Callable = execute_automation_action,
) -> dict:
    directory = _output_path(Path(output), Path(repo_root))
    directory.mkdir(parents=True, mode=0o700)
    probe = probe_factory(target, directory / "fixture")
    report = {
        "schemaVersion": 1,
        "status": "unknown",
        "commit": None,
        "sqlclVersion": None,
        "identity": None,
        "appId": target.app_id,
        "workspace": target.workspace,
        "schema": target.schema,
        "selectedStaticId": SELECTED_STATIC_ID,
        "unselectedStaticId": UNSELECTED_STATIC_ID,
        "actions": [],
        "fixtureCleanup": "not-started",
        "cleanupRequired": False,
        "automationLogBaseline": None,
        "expectedAutomationLogs": [],
        "retainedAutomationLogs": [],
    }
    try:
        version = probe.version_reader(directory / "fixture" / "version")
        require_sqlcl_version(version)
        report["sqlclVersion"] = version
        identity = probe.identify()
        report["identity"] = {
            key: identity.get(key)
            for key in ("sessionUser", "currentSchema", "serverHost", "service", "workspace", "workspaceId", "apexVersion")
        }
        if any(identity.get(key) != expected for key, expected in {
            "sessionUser": target.expected_user,
            "currentSchema": target.schema,
            "serverHost": target.server_host,
            "service": target.service,
            "workspace": target.workspace,
        }.items()):
            raise QualificationError("fresh SQLcl identity did not match the local disposable target")
        if identity.get("appCount") != 0:
            report["status"] = "refused"
            report["reason"] = "requested app ID is already occupied; no fixture write was attempted"
            probe.report.update(report)
            probe.save_report()
            _write_report(directory, report)
            return report

        privilege_output = probe.execute(
            "automation-privilege-preflight",
            "select 'APEX_AUTOMATION_CREATE_JOB:'||count(*) from user_sys_privs where privilege='CREATE JOB';\n",
        )
        privilege_rows = [
            line.strip().split(":", 1)[1]
            for line in privilege_output.splitlines()
            if line.strip().startswith("APEX_AUTOMATION_CREATE_JOB:")
        ]
        if privilege_rows != ["1"]:
            report["status"] = "unavailable"
            report["reason"] = "CREATE JOB is not directly granted to the parsing schema; no app fixture was imported"
            probe.report.update(report)
            probe.save_report()
            _write_report(directory, report)
            return report

        log_output = probe.execute(
            "automation-log-preflight",
            f"select 'APEX_AUTOMATION_LOG_COUNT:'||count(*) from apex_automation_log where application_id={target.app_id};\n",
        )
        log_rows = [
            line.strip().split(":", 1)[1]
            for line in log_output.splitlines()
            if line.strip().startswith("APEX_AUTOMATION_LOG_COUNT:")
        ]
        if log_rows != ["0"]:
            report["status"] = "unavailable"
            report["reason"] = "the unused app ID has retained APEX automation history; choose another unused ID"
            probe.report.update(report)
            probe.save_report()
            _write_report(directory, report)
            return report
        report["automationLogBaseline"] = 0
        probe.report.update(report)
        probe.save_report()

        try:
            revision = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            result = revision.stdout.strip() if revision.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            result = ""
        report["commit"] = result if re.fullmatch(r"[0-9a-f]{40,64}", result) else None
        fixture_alias = probe.alias
        staged = prepare_fixture_source(
            source,
            directory / "fixture-source",
            app_alias=fixture_alias,
            workspace=target.workspace,
            app_id=target.app_id,
            schema=target.schema,
        )
        descriptor = staged / "deployments/automation-qualification.json"
        probe.execute("fixture-preflight", _fixture_guard(target, fixture_alias, developer))
        probe.execute(
            "fixture-validate",
            f"apex validate -input {command_path(staged)} -deployment {command_path(descriptor)}\n",
            success_line="Validation successful.",
        )
        probe.report.update(
            scenario="lifecycle",
            developers=[developer],
            fixtures=[{"kind": "application", "id": target.app_id, "alias": fixture_alias}],
            fixtureWriteAttempted=True,
            cleanupRequired=True,
            identity=identity,
        )
        report["cleanupRequired"] = True
        probe.save_report()
        probe.execute(
            "fixture-import",
            _fixture_guard(target, fixture_alias, developer)
            + f"apex import -input {command_path(staged)} -deployment {command_path(descriptor)}\n",
            success_line="Import successful.",
        )

        live_target = Target("dev", target.connection, target.expected_user, target.schema, "development")
        snapshot = lifecycle_capture(live_target, target.workspace, target.app_id, directory / "lifecycle-initial")
        _assert_snapshot(snapshot, target.workspace, target.app_id, selected="DISABLED", unselected="DISABLED")
        automation_rows = list_automations(snapshot)
        selected_static_id = next(row["staticId"] for row in automation_rows if row["staticId"].upper() == SELECTED_STATIC_ID)
        unselected_static_id = next(row["staticId"] for row in automation_rows if row["staticId"].upper() == UNSELECTED_STATIC_ID)
        steps = (("enable", "ACTIVE"), ("run-once", "ACTIVE"), ("disable", "DISABLED"))
        for action, expected_selected_status in steps:
            action_report = action_executor(
                live_target,
                target.workspace,
                target.app_id,
                selected_static_id,
                action,
                snapshot,
                directory / "actions" / action,
                workspace_user=developer,
            )
            entry = {
                "action": action,
                "status": action_report.get("status"),
                "beforeStatus": (action_report.get("before") or {}).get("status"),
                "afterStatus": (action_report.get("after") or {}).get("status"),
                "unselectedStatus": next(
                    (value for key, value in (action_report.get("after") or {}).get("unselected", {}).items()
                     if key.upper() == unselected_static_id.upper()),
                    None,
                ),
            }
            if action == "run-once" and isinstance(action_report.get("runLog"), Mapping):
                run_log = action_report["runLog"]
                entry["runLog"] = {
                    key: run_log.get(key)
                    for key in (
                        "id", "workspaceId", "staticId", "statusCode", "status",
                        "startedAt", "endedAt", "successfulRows", "errorRows",
                    )
                }
            report["actions"].append(entry)
            if action_report.get("status") != "verified":
                report["status"] = action_report.get("status", "unknown")
                report["reason"] = "an explicit automation action did not reach verified status; fixture retained"
                probe.report.update(report)
                probe.save_report()
                _write_report(directory, report)
                return report
            snapshot = lifecycle_capture(
                live_target,
                target.workspace,
                target.app_id,
                directory / f"lifecycle-after-{action}",
            )
            statuses = _assert_snapshot(
                snapshot,
                target.workspace,
                target.app_id,
                selected=expected_selected_status,
                unselected="DISABLED",
            )
            if statuses[UNSELECTED_STATIC_ID] != "DISABLED":
                raise QualificationError("unselected automation changed schedule state")

        once = report["actions"][1].get("runLog")
        if not isinstance(once, Mapping) or once.get("statusCode", "").upper() != "SUCCESS" or once.get("errorRows") != 0 or not once.get("endedAt"):
            report["status"] = "unknown"
            report["reason"] = "run-once lacks a completed successful APEX execution log; fixture retained"
            probe.report.update(report)
            probe.save_report()
            _write_report(directory, report)
            return report

        report["status"] = "pass"
        report["fixtureCleanup"] = "running-owned-cleanup"
        report["expectedAutomationLogs"] = [
            {
                "id": entry["runLog"]["id"],
                "workspaceId": entry["runLog"]["workspaceId"],
                "staticId": entry["runLog"]["staticId"],
                "statusCode": entry["runLog"]["statusCode"],
            }
            for entry in report["actions"]
            if isinstance(entry.get("runLog"), Mapping)
        ]
        probe.report.update(report)
        probe.save_report()
        cleanup_result = probe.cleanup(probe.directory / "report.json")
        report["retainedAutomationLogs"] = cleanup_result.get("retainedAutomationLogs", [])
        report["fixtureCleanup"] = cleanup_result.get("cleanupStatus", "pass")
        report["cleanupRequired"] = False
        probe.report.update(report)
        probe.save_report()
        _write_report(directory, report)
        return report
    except Exception as error:
        if report["cleanupRequired"]:
            report["status"] = "unknown"
            report["fixtureCleanup"] = "pending-explicit-recovery"
            report["reason"] = f"qualification stopped after fixture write ({type(error).__name__}); retain fixture and inspect private evidence"
        else:
            report["status"] = "unavailable"
            report["reason"] = f"qualification could not start ({type(error).__name__}); no fixture cleanup is required"
        try:
            probe.report.update(report)
            probe.save_report()
            _write_report(directory, report)
        except Exception:
            pass
        return report


def cleanup_fixture(
    target: ProbeTarget,
    output: Path,
    previous_report: Path,
    *,
    repo_root: Path = ROOT,
    probe_factory: Callable = Probe,
) -> dict:
    """Remove only the exact application recorded in a successful write attempt."""
    directory = _output_path(Path(output), Path(repo_root))
    directory.mkdir(parents=True, mode=0o700)
    report = {
        "schemaVersion": 1,
        "status": "unknown",
        "appId": target.app_id,
        "cleanupRequired": True,
        "cleanupOf": str(previous_report),
    }
    try:
        if Path(previous_report).is_symlink() or not Path(previous_report).is_file():
            raise QualificationError("cleanup requires the retained fixture/report.json ownership record")
        probe = probe_factory(target, directory / "cleanup")
        cleaned = probe.cleanup(Path(previous_report))
        report["status"] = "pass" if cleaned.get("cleanupRequired") is False else "unknown"
        report["cleanupRequired"] = cleaned.get("cleanupRequired") is not False
        report["checks"] = cleaned.get("checks", {})
        report["retainedAutomationLogs"] = cleaned.get("retainedAutomationLogs", [])
        report["cleanupStatus"] = cleaned.get("cleanupStatus", "pass")
        if report["status"] != "pass":
            report["reason"] = "owned cleanup did not verify zero remaining fixture rows"
    except Exception as error:
        report["reason"] = f"cleanup could not be verified ({type(error).__name__}); inspect private diagnostics"
    _write_report(directory, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("connection", "expected-user", "schema", "workspace", "server-host", "service"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--app-id", required=True, type=int, help="unused positive numeric application ID in the selected workspace")
    parser.add_argument("--developer", help="existing APEX workspace developer used for the disposable app session")
    parser.add_argument("--source", type=Path, default=SOURCE_DEFAULT, help="reviewed APEX 26.2 fixture source")
    parser.add_argument("--report-dir", required=True, type=Path, help="fresh ignored scratch directory")
    parser.add_argument("--cleanup-report", type=Path, help="exact retained fixture/report.json to clean up")
    parser.add_argument("--allow-writes", action="store_true", help="confirm this invocation may import or remove only its disposable fixture")
    args = parser.parse_args(argv)
    try:
        if not args.allow_writes:
            raise QualificationError("local qualification and fixture cleanup require --allow-writes")
        target = ProbeTarget(
            args.connection, args.expected_user, args.schema, args.workspace,
            args.app_id, args.server_host, args.service,
        )
        if args.cleanup_report:
            report = cleanup_fixture(target, args.report_dir, args.cleanup_report)
        else:
            if not args.developer:
                raise QualificationError("provide --developer for the disposable APEX session")
            report = qualify(target, args.report_dir, source=args.source, developer=args.developer)
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
        return 0 if report["status"] == "pass" else 2
    except (ProbeError, ValueError, OSError) as error:
        print(f"automation qualification: {type(error).__name__}; inspect {args.report_dir}/qualification.json", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
