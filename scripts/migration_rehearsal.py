#!/usr/bin/env python3
"""Rehearse transaction-safe migration data changes and prove rollback."""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import migrate as migration_runner
from .db_targets import Target, TargetResolutionError
from .migration_checks import (
    CheckReport,
    analyze_batch,
    _parse_check_output,
    render_in_session_check_driver,
    run_checks,
)
from .migration_manifest import Migration, MigrationManifestError
from .rehearsal_metrics import MetricCapture, parse_metric_frame, render_metric_driver
from .path_utils import relative_to_root
from .schema_catalog import CatalogError, capture_inventory
from .sqlcl_session import SqlclError, run_sqlcl, safe_rmtree
from .sqlcl_transport import lint_sqlcl_transport


DML_VERBS = {"INSERT", "UPDATE", "DELETE", "MERGE"}
IMPLICIT_COMMIT_VERBS = {"ALTER", "CREATE", "DROP", "GRANT", "RENAME", "REVOKE", "TRUNCATE"}
ROW_COUNT_RE = re.compile(r"^\s*(\d+)\s+rows?\s+(?:inserted|updated|deleted|merged)\.?\s*$", re.IGNORECASE)
AUTOCOMMIT_OFF_RE = re.compile(r"\bAUTOCOMMIT\s+(?:IS\s+)?OFF\b", re.IGNORECASE)
RESERVED_SCRATCH_EVIDENCE_PREFIXES = (
    "migration-attempt-",
    "migration-verify-",
    "migration-rehearsal-",
    "rollout-runs",
    "rollout-receipts",
    "apex-publish.",
    "apex-publish-",
    "apex-partial.",
    "apex-partial-",
    "apexlang-upgrade.",
    "apexlang-upgrade-",
    "apex-export.",
    "apex-export-",
    "apex-lookup.",
    "apex-lookup-",
    "sqlcl-doctor.",
    "sqlcl-doctor-",
    "db-backup.",
    "db-backup-",
)


def _new_run_dir(repo_root: Path) -> Path:
    scratch = repo_root / "scratch"
    if scratch.is_symlink():
        raise MigrationManifestError("scratch must not be a symbolic link")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        scratch.chmod(0o700)
    except OSError as error:
        raise MigrationManifestError(f"could not secure scratch directory: {error}") from error
    run_dir = Path(tempfile.mkdtemp(prefix="migration-rehearsal-", dir=scratch))
    try:
        run_dir.chmod(0o700)
    except OSError as error:
        safe_rmtree(run_dir)
        raise MigrationManifestError(f"could not secure rehearsal directory: {error}") from error
    return run_dir


def _classify_files(migrations: Sequence[Migration], target_schema: str) -> list[list[dict]]:
    """Allow only files whose analyzer-classified statements are DML."""
    operations = analyze_batch(migrations, target_schema)
    by_file: dict[tuple[str, str], list[dict]] = {}
    for operation in operations:
        key = (str(operation.get("migration", "")), str(operation.get("file", "")))
        by_file.setdefault(key, []).append(operation)

    classified: list[list[dict]] = []
    for migration in migrations:
        migration_files: list[dict] = []
        for file in migration.files:
            statements = sorted(
                by_file.get((migration.folder.name, file.name), ()),
                key=lambda operation: operation.get("statement_index", 0),
            )
            details: list[dict] = []
            reasons: list[str] = []
            if not statements:
                reasons.append("the analyzer found no complete SQL statement")
            for operation in statements:
                statement = str(operation.get("statement", "")).split()
                verb = statement[0].upper() if statement else "UNKNOWN"
                kind = str(operation.get("kind", "UNKNOWN"))
                detail = {
                    "statementIndex": operation.get("statement_index"),
                    "kind": verb,
                    "operation": kind,
                    "reason": operation.get("reason"),
                }
                details.append(detail)
                if kind == "REVIEWED_OPERATION" and verb in DML_VERBS:
                    if not migration.preconditions or not migration.postconditions:
                        reasons.append("data-changing files require explicit preconditions and postconditions")
                    continue
                if verb in IMPLICIT_COMMIT_VERBS or kind.startswith(("CREATE_", "ALTER_", "REVIEWED_ALTER")):
                    reasons.append(f"{verb} ({kind}) can commit implicitly and is not rehearsable")
                else:
                    reason = operation.get("reason") or "the analyzer cannot prove this statement is transaction-safe"
                    reasons.append(f"{verb} ({kind}) is not transaction-safe: {reason}")
            migration_files.append({
                "file": file.name,
                "status": "rehearsed" if statements and not reasons else "not rehearsable",
                "reason": "; ".join(dict.fromkeys(reasons)) if reasons else None,
                "statements": details,
            })
        classified.append(migration_files)
    return classified


def _write_check_driver(
    path: Path,
    target: Target,
    checks: Sequence,
    phase: str,
    *,
    fail_on_false: bool = False,
    fail_on_error: bool = False,
) -> None:
    path.write_text(
        render_in_session_check_driver(
            target, checks, phase, fail_on_false=fail_on_false, fail_on_error=fail_on_error,
        ),
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o600)


def _write_metric_driver(path: Path, target: Target, migration: Migration, phase: str) -> None:
    path.write_text(
        render_metric_driver(target, migration.rehearsal_metrics, phase),
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o600)


def _render_driver(
    run_dir: Path,
    target: Target,
    expected_identity: Mapping[str, str],
    frozen: Sequence,
    classified: Sequence[Sequence[Mapping]],
) -> Path:
    session_dir = run_dir / "session"
    session_dir.mkdir(mode=0o700)
    script_root = Path(__file__).resolve().parent
    for trusted in ("migrate.sql", "verify_migration_access.sql"):
        shutil.copyfile(script_root / trusted, session_dir / trusted)
        (session_dir / trusted).chmod(0o600)

    pre_paths: dict[str, str] = {}
    post_paths: dict[str, str] = {}
    rollback_paths: dict[str, str] = {}
    rollback_start_paths: dict[str, str] = {}
    metric_paths: dict[tuple[str, str], str] = {}
    for folder_index, item in enumerate(frozen, start=1):
        migration = item.staged
        if migration.preconditions:
            pre_name = f"pre-{folder_index:03d}.sql"
            rollback_name = f"rollback-pre-{folder_index:03d}.sql"
            rollback_start_name = f"rollback-start-{folder_index:03d}.sql"
            _write_check_driver(session_dir / pre_name, target, migration.preconditions, "preconditions", fail_on_false=True)
            _write_check_driver(session_dir / rollback_name, target, migration.preconditions, "preconditions")
            _write_check_driver(
                session_dir / rollback_start_name, target, migration.preconditions, "rollback-start", fail_on_error=True,
            )
            pre_paths[migration.folder.name] = pre_name
            rollback_paths[migration.folder.name] = rollback_name
            rollback_start_paths[migration.folder.name] = rollback_start_name
        if migration.postconditions:
            post_name = f"post-{folder_index:03d}.sql"
            _write_check_driver(session_dir / post_name, target, migration.postconditions, "postconditions")
            post_paths[migration.folder.name] = post_name
        if migration.rehearsal_metrics:
            for phase in ("metric-before", "metric-after", "metric-rollback"):
                metric_name = f"{phase}-{folder_index:03d}.sql"
                _write_metric_driver(session_dir / metric_name, target, migration, phase)
                metric_paths[(migration.folder.name, phase)] = metric_name

    lines = [
        "SET ECHO OFF",
        "SET SQLBLANKLINES ON",
        "SET FEEDBACK ON",
        "SET AUTOCOMMIT OFF",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "SET DEFINE ON",
        f"@@migrate.sql {target.schema} {target.environment} {target.expected_user}",
        "SET DEFINE OFF",
        "SET AUTOCOMMIT OFF",
        "ALTER SESSION DISABLE COMMIT IN PROCEDURE;",
        *migration_runner._identity_guard_lines(expected_identity),
    ]
    for item in frozen:
        migration = item.staged
        if migration.folder.name in rollback_start_paths:
            lines.append(f"@@{rollback_start_paths[migration.folder.name]}")
    for folder_index, item in enumerate(frozen, start=1):
        migration = item.staged
        if migration.folder.name in pre_paths:
            lines.append(f"@@{pre_paths[migration.folder.name]}")
        before_metric = metric_paths.get((migration.folder.name, "metric-before"))
        if before_metric:
            lines.extend((
                f"PROMPT MIGRATION_REHEARSAL_METRIC_FRAME_BEGIN:metric-before:{folder_index:03d}",
                f"@@{before_metric}",
                f"PROMPT MIGRATION_REHEARSAL_METRIC_FRAME_END:metric-before:{folder_index:03d}",
            ))
        for file_index, (file, plan) in enumerate(zip(migration.files, classified[folder_index - 1], strict=True), start=1):
            if plan.get("status") != "rehearsed":
                continue
            marker = f"{folder_index:03d}:{file_index:03d}"
            lines.extend((
                "SET AUTOCOMMIT OFF",
                "SET DEFINE OFF",
                "SET FEEDBACK ON",
                "SET SQLBLANKLINES ON",
                f"PROMPT MIGRATION_REHEARSAL_FILE_BEGIN:{marker}",
                f"@@../payload/{migration.folder.name}/{file.name}",
                f"PROMPT MIGRATION_REHEARSAL_FILE_END:{marker}",
            ))
        after_metric = metric_paths.get((migration.folder.name, "metric-after"))
        if after_metric:
            lines.extend((
                f"PROMPT MIGRATION_REHEARSAL_METRIC_FRAME_BEGIN:metric-after:{folder_index:03d}",
                f"@@{after_metric}",
                f"PROMPT MIGRATION_REHEARSAL_METRIC_FRAME_END:metric-after:{folder_index:03d}",
            ))
        if migration.folder.name in post_paths:
            lines.append(f"@@{post_paths[migration.folder.name]}")
    lines.extend(("ROLLBACK;", "PROMPT MIGRATION_REHEARSAL_ROLLBACK_COMPLETED"))
    for folder_index, item in enumerate(frozen, start=1):
        migration = item.staged
        rollback_metric = metric_paths.get((migration.folder.name, "metric-rollback"))
        if rollback_metric:
            lines.extend((
                f"PROMPT MIGRATION_REHEARSAL_METRIC_FRAME_BEGIN:metric-rollback:{folder_index:03d}",
                f"@@{rollback_metric}",
                f"PROMPT MIGRATION_REHEARSAL_METRIC_FRAME_END:metric-rollback:{folder_index:03d}",
            ))
        if migration.folder.name in rollback_paths:
            lines.append(f"@@{rollback_paths[migration.folder.name]}")
    lines.extend(("EXIT SUCCESS ROLLBACK", ""))
    driver = session_dir / "rehearsal-driver.sql"
    driver.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    driver.chmod(0o600)
    return driver


def _verify_autocommit_off(run_dir: Path, target: Target, session_runner: Callable) -> None:
    probe_dir = run_dir / "autocommit-probe"
    probe_dir.mkdir(mode=0o700)
    probe = probe_dir / "autocommit-probe.sql"
    probe.write_text("\n".join((
        "SET ECHO OFF",
        "SET AUTOCOMMIT OFF",
        "PROMPT MIGRATION_REHEARSAL_AUTOCOMMIT_BEGIN",
        "SHOW AUTOCOMMIT",
        "PROMPT MIGRATION_REHEARSAL_AUTOCOMMIT_END",
        "EXIT SUCCESS ROLLBACK",
        "",
    )), encoding="utf-8", newline="\n")
    probe.chmod(0o600)
    try:
        result = session_runner(target, probe, probe_dir, phase="apply")
    except (SqlclError, OSError, RuntimeError, ValueError) as error:
        raise MigrationManifestError(f"could not verify SQLcl autocommit is off; rehearsal refused: {error}") from error
    try:
        start = result.output.index("MIGRATION_REHEARSAL_AUTOCOMMIT_BEGIN")
        finish = result.output.index("MIGRATION_REHEARSAL_AUTOCOMMIT_END", start + 1)
    except ValueError as error:
        raise MigrationManifestError("SQLcl did not report its autocommit setting; rehearsal refused before payload") from error
    if AUTOCOMMIT_OFF_RE.search(result.output[start + len("MIGRATION_REHEARSAL_AUTOCOMMIT_BEGIN"):finish]) is None:
        raise MigrationManifestError("SQLcl autocommit is not confirmed off; rehearsal refused before payload")


def _extract_frames(output: str, phase: str) -> list[str]:
    lines = output.splitlines()
    begin = f"CHECK_PAYLOAD_BEGIN:{phase}"
    end = f"CHECK_PAYLOAD_END:{phase}"
    verified = f"CHECK_VERIFIED:{phase}"
    frames: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index] != begin:
            index += 1
            continue
        try:
            finish = lines.index(end, index + 1)
            sentinel = lines.index(verified, finish + 1)
        except ValueError:
            frames.append("\n".join(lines[index:]))
            break
        frames.append("\n".join(lines[index:sentinel + 1]))
        index = sentinel + 1
    return frames


def _parse_frame(frame: str | None, checks: Sequence, phase: str) -> CheckReport:
    if not checks:
        return CheckReport(True, True, (), (), {"phase": phase, "complete": True})
    return _parse_check_output(frame or "", checks, phase)


def _check_records(checks: Sequence, report: CheckReport) -> dict:
    result_by_id: dict[str, dict] = {}
    for result in report.results:
        if isinstance(result.get("id"), str):
            result_by_id[result["id"]] = result
    records = []
    has_error = not report.complete
    has_false = False
    for check in checks:
        result = result_by_id.get(check.id)
        if result is None:
            records.append({"id": check.id, "expected": check.expected, "observed": None, "status": "error"})
            has_error = True
            continue
        error = result.get("error")
        passed = result.get("passed") is True
        if error:
            status = "error"
            has_error = True
        elif passed:
            status = "true"
        else:
            status = "false"
            has_false = True
        records.append({
            "id": check.id,
            "expected": check.expected,
            "observed": result.get("value"),
            "status": status,
            **({"error": str(error)} if error else {}),
        })
    status = "error" if has_error else "false" if has_false else "true"
    return {"status": status, "checks": records}


def _attach_frames(
    folders: list[dict],
    migrations: Sequence[Migration],
    output: str,
    phase: str,
    *,
    start_after_rollback: bool = False,
) -> None:
    marker = "MIGRATION_REHEARSAL_ROLLBACK_COMPLETED"
    phase_output = output
    if start_after_rollback:
        index = output.find(marker)
        phase_output = output[index + len(marker):] if index >= 0 else ""
    elif marker in output:
        phase_output = output[:output.find(marker)]
    frames = iter(_extract_frames(phase_output, phase))
    for folder, migration in zip(folders, migrations, strict=True):
        checks = migration.preconditions if phase == "preconditions" else migration.postconditions
        key = "rollbackProof" if start_after_rollback else "preconditions" if phase == "preconditions" else "postconditions"
        if not checks:
            folder[key] = {"status": "not needed", "checks": []}
            continue
        frame = next(frames, None)
        if frame is None:
            folder[key] = {"status": "not run", "checks": []}
            continue
        report = _parse_frame(frame, checks, phase)
        if isinstance(report.coverage.get("identity"), Mapping) and folder.get("targetIdentity"):
            migration_runner._assert_same_target(folder["targetIdentity"], report.coverage["identity"])
        folder[key] = _check_records(checks, report)


def _check_frame_error(report: CheckReport, expected_identity: Mapping[str, str]) -> str | None:
    if not report.complete:
        return "check frame is missing, incomplete, or malformed"
    identity = report.coverage.get("identity")
    if not isinstance(identity, Mapping):
        return "check frame has no database identity"
    try:
        migration_runner._assert_same_target(expected_identity, identity)
    except migration_runner.MigrationApplyError as error:
        return str(error)
    return None


def _numeric_check_value(result: Mapping | None) -> tuple[object | None, str | None]:
    if not isinstance(result, Mapping):
        return None, "check result is missing"
    if result.get("error"):
        return None, str(result["error"])
    value = result.get("value")
    if (
        result.get("row_count") != 1
        or result.get("column_count") != 1
        or result.get("numeric") is not True
        or type(value) not in {int, float}
    ):
        return None, "check did not return exactly one numeric value"
    return value, None


def _rollback_start_values(
    folders: list[dict],
    migrations: Sequence[Migration],
    output: str,
    expected_identity: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Capture the initial value of every rollback check before any payload runs."""
    first_payload = output.find("MIGRATION_REHEARSAL_FILE_BEGIN:")
    start_output = output if first_payload < 0 else output[:first_payload]
    frames = iter(_extract_frames(start_output, "rollback-start"))
    values: dict[str, dict[str, object]] = {}
    for folder, migration in zip(folders, migrations, strict=True):
        checks = migration.preconditions
        if not checks:
            folder["rollbackStart"] = {"status": "not needed", "checks": []}
            continue
        frame = next(frames, None)
        parsed = _parse_frame(frame, checks, "rollback-start")
        frame_error = "initial rollback-check frame is missing" if frame is None else _check_frame_error(parsed, expected_identity)
        result_by_id = {
            result["id"]: result for result in parsed.results if isinstance(result.get("id"), str)
        }
        folder_values: dict[str, object] = {}
        records = []
        has_error = frame_error is not None
        for check in checks:
            value, value_error = _numeric_check_value(result_by_id.get(check.id))
            error = frame_error or value_error
            if error:
                has_error = True
            else:
                folder_values[check.id] = value
            records.append({
                "id": check.id,
                "startingValue": value,
                "status": "error" if error else "captured",
                **({"error": error} if error else {}),
            })
        values[migration.folder.name] = folder_values
        folder["rollbackStart"] = {
            "status": "error" if has_error else "captured",
            "checks": records,
            **({"error": frame_error} if frame_error else {}),
        }
    return values


def _rollback_proof_in_session(
    folders: list[dict],
    migrations: Sequence[Migration],
    output: str,
    expected_identity: Mapping[str, str],
    starting_values: Mapping[str, Mapping[str, object]],
) -> dict:
    marker = "MIGRATION_REHEARSAL_ROLLBACK_COMPLETED"
    marker_index = output.find(marker)
    final_output = output[marker_index + len(marker):] if marker_index >= 0 else ""
    frames = iter(_extract_frames(final_output, "preconditions"))
    any_error = False
    any_false = False
    any_checked = False
    for folder, migration in zip(folders, migrations, strict=True):
        checks = migration.preconditions
        if not checks:
            folder["rollbackProof"] = {"status": "not needed", "checks": []}
            continue
        any_checked = True
        frame = next(frames, None)
        parsed = _parse_frame(frame, checks, "preconditions")
        frame_error = "final rollback-check frame is missing" if frame is None else _check_frame_error(parsed, expected_identity)
        result_by_id = {
            result["id"]: result for result in parsed.results if isinstance(result.get("id"), str)
        }
        records = []
        folder_error = frame_error is not None
        folder_false = False
        initial_values = starting_values.get(migration.folder.name, {})
        for check in checks:
            initial = initial_values.get(check.id)
            final_value, value_error = _numeric_check_value(result_by_id.get(check.id))
            error = frame_error or value_error
            if initial is None:
                error = error or "initial rollback-check value is unavailable"
            if error:
                folder_error = True
            elif final_value != initial:
                folder_false = True
            records.append({
                "id": check.id,
                "startingValue": initial,
                "observed": final_value,
                "status": "error" if error else "false" if final_value != initial else "true",
                **({"error": error} if error else {}),
            })
        status = "error" if folder_error else "false" if folder_false else "true"
        any_error |= folder_error
        any_false |= folder_false
        folder["rollbackProof"] = {"status": status, "checks": records}
    status = "error" if any_error else "false" if any_false else "true" if any_checked else "not needed"
    return {
        "status": status,
        "method": "initial rollback-check values compared with values after ROLLBACK in the rehearsal session",
        "explicitRollback": True,
    }


def _row_counts(output: str, frozen: Sequence, classified: Sequence[Sequence[Mapping]]) -> dict[tuple[int, int], list[int]]:
    lines = output.splitlines()
    counts: dict[tuple[int, int], list[int]] = {}
    for folder_index, _item in enumerate(frozen, start=1):
        for file_index, plan in enumerate(classified[folder_index - 1], start=1):
            if plan.get("status") != "rehearsed":
                continue
            marker = f"{folder_index:03d}:{file_index:03d}"
            begin = f"MIGRATION_REHEARSAL_FILE_BEGIN:{marker}"
            end = f"MIGRATION_REHEARSAL_FILE_END:{marker}"
            try:
                start = lines.index(begin)
            except ValueError:
                continue
            try:
                finish = lines.index(end, start + 1)
            except ValueError:
                finish = len(lines)
            counts[(folder_index, file_index)] = [
                int(match.group(1))
                for line in lines[start + 1:finish]
                if (match := ROW_COUNT_RE.match(line)) is not None
            ]
    return counts


def _update_statement_rows(folders: list[dict], frozen: Sequence, classified: Sequence[Sequence[Mapping]], output: str) -> None:
    counts = _row_counts(output, frozen, classified)
    for folder_index, (folder, _item) in enumerate(zip(folders, frozen, strict=True), start=1):
        total = 0
        complete = True
        count_statements = 0
        reported_statements = 0
        statement_map = {
            (statement.get("file"), statement.get("statementIndex")): statement
            for statement in folder["statements"]
        }
        for file_index, plan in enumerate(classified[folder_index - 1], start=1):
            file_counts = iter(counts.get((folder_index, file_index), ()))
            if plan.get("status") != "rehearsed":
                continue
            for detail in plan.get("statements", ()):
                row_count = next(file_counts, None)
                count_statements += 1
                statement = statement_map[(plan["file"], detail.get("statementIndex"))]
                statement["status"] = "rehearsed" if row_count is not None else "not reported"
                statement["rowsAffected"] = row_count
                if row_count is None:
                    complete = False
                else:
                    reported_statements += 1
                    total += row_count
        folder["rowsAffected"] = total if reported_statements else None
        folder["rowsAffectedComplete"] = complete if count_statements else False


def _report_target(path: Path | str | None, repo_root: Path) -> Path | None:
    if path is None:
        return None
    requested = Path(path)
    if not requested.is_absolute():
        requested = repo_root / requested
    cursor = Path(requested.anchor)
    for component in requested.parts[1:]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise MigrationManifestError(f"rehearsal report path cannot contain a symbolic-link component: {cursor}")
    try:
        resolved = requested.resolve(strict=False)
    except OSError as error:
        raise MigrationManifestError(f"rehearsal report path cannot be resolved safely: {error}") from error
    root = repo_root.resolve()
    protected_roots = (root / "migrations", root / ".sync-state")
    for protected in protected_roots:
        if resolved == protected or protected in resolved.parents:
            raise MigrationManifestError(
                f"rehearsal report cannot be written inside protected source or publish evidence: {resolved}"
            )
    try:
        scratch_relative = relative_to_root(resolved, root / "scratch")
    except ValueError:
        scratch_relative = None
    if scratch_relative and scratch_relative.parts:
        first = scratch_relative.parts[0]
        if any(first == prefix or first.startswith(prefix) for prefix in RESERVED_SCRATCH_EVIDENCE_PREFIXES):
            raise MigrationManifestError(f"rehearsal report cannot be written inside protected recovery evidence: {resolved}")
    if requested.name.startswith("status.") and requested.name.endswith(".json"):
        raise MigrationManifestError("rehearsal report cannot replace a migration receipt")
    return requested


def _write_report(path: Path | None, report: Mapping) -> None:
    if path is not None:
        migration_runner._atomic_write_json(path, report)


def _folder_report(migration: Migration, plan: Sequence[Mapping], expected_identity: Mapping[str, str]) -> dict:
    files = [dict(item) for item in plan]
    statements = []
    for file in files:
        for detail in file["statements"]:
            statements.append({
                "file": file["file"],
                "statementIndex": detail.get("statementIndex"),
                "kind": detail.get("kind"),
                "status": file["status"],
                "rowsAffected": None,
                **({"reason": file["reason"]} if file["reason"] else {}),
            })
    return {
        "folder": migration.folder.name,
        "files": files,
        "statements": statements,
        "rowsAffected": None,
        "rowsAffectedComplete": False,
        "preconditions": {"status": "not run", "checks": []},
        "postconditions": {"status": "not run", "checks": []},
        "rollbackProof": {"status": "not run", "checks": []},
        "rehearsalMetrics": {
            "status": "not needed" if not migration.rehearsal_metrics else "not run",
            **{
                phase: {
                    "status": "not needed" if not migration.rehearsal_metrics else "not run",
                    "metrics": [
                        {"id": metric.id, "expected": getattr(metric, expected_field), "observed": None, "status": "not run"}
                        for metric in migration.rehearsal_metrics
                    ],
                }
                for phase, expected_field in (
                    ("before", "expected_before"),
                    ("after", "expected_after"),
                    ("rollback", "expected_rollback"),
                )
            },
        },
        "targetIdentity": {key: expected_identity[key] for key in migration_runner.IDENTITY_FIELDS},
    }


def _metric_frame_output(output: str, phase: str, folder_index: int) -> str | None:
    lines = output.splitlines()
    marker = f"{phase}:{folder_index:03d}"
    begin = f"MIGRATION_REHEARSAL_METRIC_FRAME_BEGIN:{marker}"
    end = f"MIGRATION_REHEARSAL_METRIC_FRAME_END:{marker}"
    if lines.count(begin) != 1 or lines.count(end) != 1:
        return None
    start, finish = lines.index(begin), lines.index(end)
    if start >= finish:
        return None
    return "\n".join(lines[start + 1:finish])


def _metric_phase_report(migration: Migration, phase: str, capture: MetricCapture | None) -> dict:
    if not migration.rehearsal_metrics:
        return {"status": "not needed", "metrics": []}
    if capture is None or not capture.complete:
        errors = capture.errors if capture is not None else ("metric result frame is missing",)
        return {
            "status": "error",
            "metrics": [
                {
                    "id": metric.id,
                    "expected": getattr(metric, {
                        "before": "expected_before",
                        "after": "expected_after",
                        "rollback": "expected_rollback",
                    }[phase]),
                    "observed": None,
                    "status": "error",
                    "error": errors[0] if errors else "metric capture is incomplete",
                }
                for metric in migration.rehearsal_metrics
            ],
        }
    expected_field = {
        "before": "expected_before",
        "after": "expected_after",
        "rollback": "expected_rollback",
    }[phase]
    records = []
    for metric in migration.rehearsal_metrics:
        expected = getattr(metric, expected_field)
        observed = capture.values[metric.id]
        records.append({
            "id": metric.id,
            "expected": expected,
            "observed": observed,
            "status": "true" if observed == expected else "false",
        })
    status = "false" if any(record["status"] == "false" for record in records) else "true"
    return {"status": status, "metrics": records}


def _attach_metric_frames(
    folders: list[dict],
    migrations: Sequence[Migration],
    output: str,
    expected_identity: Mapping[str, str],
) -> None:
    phases = {
        "before": "metric-before",
        "after": "metric-after",
        "rollback": "metric-rollback",
    }
    for folder_index, (folder, migration) in enumerate(zip(folders, migrations, strict=True), start=1):
        groups = {}
        for label, phase in phases.items():
            frame = _metric_frame_output(output, phase, folder_index)
            capture = None
            if frame is not None and migration.rehearsal_metrics:
                capture = parse_metric_frame(frame, migration.rehearsal_metrics, phase, expected_identity)
            groups[label] = _metric_phase_report(migration, label, capture)
        statuses = [group["status"] for group in groups.values()]
        status = "error" if "error" in statuses else "false" if "false" in statuses else (
            "not needed" if all(value == "not needed" for value in statuses) else "true"
        )
        folder["rehearsalMetrics"] = {"status": status, **groups}


def _merge_metric_rollback_proof(report: dict) -> None:
    metric_groups = [
        folder.get("rehearsalMetrics", {}).get("rollback", {})
        for folder in report.get("folders", ())
        if folder.get("rehearsalMetrics", {}).get("rollback", {}).get("status") != "not needed"
    ]
    if not metric_groups:
        return
    statuses = [group.get("status") for group in metric_groups]
    existing = report.get("rollbackProof", {})
    existing_status = existing.get("status")
    if existing_status in {"error", "not verifiable"} or "error" in statuses:
        status = "error"
    elif existing_status == "false" or "false" in statuses:
        status = "false"
    elif existing_status in {"true", "not needed"} and all(value == "true" for value in statuses):
        status = "true"
    else:
        status = "error"
    existing["status"] = status
    if existing.get("explicitRollback") is not True:
        metric_method = "; declared rehearsal metrics not verified because SQLcl did not confirm the final ROLLBACK"
    elif any(value in {"error", "not run"} for value in statuses):
        metric_method = "; declared rehearsal metrics could not be fully verified after SQLcl confirmed the final ROLLBACK"
    else:
        metric_method = "; declared rehearsal metrics compared after the explicit final ROLLBACK"
    existing["method"] = (
        str(existing.get("method", "rollback observation"))
        + metric_method
    )
    if status != "true":
        existing["metricStatus"] = status


def _check_rollback_separately(
    target: Target,
    migrations: Sequence[Migration],
    folders: list[dict],
    run_dir: Path,
    expected_identity: Mapping[str, str],
    starting_values: Mapping[str, Mapping[str, object]],
    run_checks_fn: Callable,
) -> dict:
    any_unproven = False
    any_false = False
    any_error = False
    for index, (migration, folder) in enumerate(zip(migrations, folders, strict=True), start=1):
        if not migration.preconditions:
            folder["rollbackProof"] = {"status": "not needed", "checks": []}
            continue
        try:
            report = run_checks_fn(target, migration.preconditions, run_dir / f"rollback-proof-{index:03d}", phase="preconditions")
            migration_runner._assert_check_identity(expected_identity, report)
        except (migration_runner.MigrationApplyError, SqlclError, OSError, RuntimeError, ValueError) as error:
            report = None
            report_error = str(error)
        else:
            report_error = None if report.complete else "final rollback-check report is incomplete"
        results_by_id = {}
        if report is not None:
            results_by_id = {
                result["id"]: result for result in report.results
                if isinstance(result.get("id"), str)
            }
        checks = []
        folder_error = report_error is not None
        folder_false = False
        initial_values = starting_values.get(migration.folder.name, {})
        for check in migration.preconditions:
            initial = initial_values.get(check.id)
            final_value, value_error = _numeric_check_value(results_by_id.get(check.id))
            error = report_error or value_error
            if initial is None:
                error = error or "initial rollback-check value is unavailable"
            if error:
                folder_error = True
            elif final_value != initial:
                folder_false = True
            checks.append({
                "id": check.id,
                "startingValue": initial,
                "observed": final_value,
                "status": "error" if error else "false" if final_value != initial else "true",
                **({"error": error} if error else {}),
            })
        result = {
            "status": "error" if folder_error else "false" if folder_false else "true",
            "checks": checks,
            **({"error": report_error} if report_error else {}),
        }
        folder["rollbackProof"] = result
        any_false |= folder_false
        any_error |= folder_error
        any_unproven |= result["status"] not in {"true", "not needed"}
    status = "error" if any_error else "false" if any_false else "true" if not any_unproven else "not verifiable"
    return {
        "status": status,
        "method": "initial rollback-check values compared with preconditions after SQLcl exit",
        "explicitRollback": False,
    }


def _session_error_lines(output: str) -> list[str]:
    matches = [
        line.strip() for line in output.splitlines()
        if re.search(r"\b(?:ORA|SP2|SQLcl Error)-\d{4,5}\b|^Error starting at line|^Error report -", line, re.IGNORECASE)
    ]
    return matches[:20]


def _print_report(report: Mapping) -> None:
    for folder in report.get("folders", ()):
        statements = [
            statement for statement in folder.get("statements", ())
            if statement.get("status") in {"rehearsed", "not reported"}
        ]
        counts = len(statements)
        rows = folder.get("rowsAffected")
        row_label = "unknown" if rows is None else str(rows)
        reported_count = sum(statement.get("rowsAffected") is not None for statement in folder.get("statements", ()))
        if rows is not None and not folder.get("rowsAffectedComplete", False) and reported_count:
            row_label = f"{rows} reported; total unknown"
        print(
            f"Rehearsal {folder['folder']}: {counts} statements, {row_label} rows affected; "
            f"postconditions {folder['postconditions']['status']}; "
            f"rollback proof {folder['rollbackProof']['status']}; "
            f"metrics before/after/rollback "
            f"{folder['rehearsalMetrics']['before']['status']}/"
            f"{folder['rehearsalMetrics']['after']['status']}/"
            f"{folder['rehearsalMetrics']['rollback']['status']}"
        )
        for file in folder.get("files", ()):
            if file.get("status") == "not rehearsable":
                print(f"  not rehearsable: {file['file']}: {file['reason']}")
        for statement in statements:
            rows = statement.get("rowsAffected")
            row_label = "unknown" if rows is None else str(rows)
            if statement.get("status") == "not reported" and rows is None:
                row_label = "unknown (SQLcl did not report)"
            if statement.get("status") == "rehearsed" or statement.get("status") == "not reported":
                print(
                    f"  {statement['file']} statement {statement['statementIndex']} "
                    f"{statement['kind']}: {row_label} rows affected"
                )
    if report.get("rollbackProof", {}).get("status") == "true":
        print(f"Rollback proof: true ({report['rollbackProof'].get('method', 'in-session preconditions')})")
    elif report.get("rollbackProof"):
        print(f"Rollback proof: {report['rollbackProof']['status']}")
    for error in report.get("errors", ()):
        print(f"Rehearsal error: {error}", file=sys.stderr)


def rehearse_batch(
    repo_root: Path,
    migrations: Sequence[Migration],
    target: Target,
    confirm: Callable[[str], bool],
    *,
    capture_inventory_fn: Callable | None = None,
    run_session_fn: Callable | None = None,
    run_checks_fn: Callable | None = None,
    report_path: Path | str | None = None,
) -> int:
    """Run rehearsable data files in order, roll back, then verify preconditions."""
    repo_root = Path(repo_root).resolve()
    if not migrations:
        print("migration error: select one or more folders in execution order", file=sys.stderr)
        return 2
    inventory_runner = capture_inventory_fn or capture_inventory
    session_runner = run_session_fn or run_sqlcl
    check_runner = run_checks_fn or run_checks
    run_dir: Path | None = None
    session_output = ""
    report: dict = {
        "schemaVersion": 1,
        "command": "migrate --rehearse",
        "environment": target.environment,
        "status": "error",
        "target": None,
        "folders": [],
        "rollbackProof": {"status": "not needed", "method": "no data-changing statement executed", "explicitRollback": False},
        "errors": [],
    }
    requested_report: Path | None = None
    retain_evidence = False
    exit_code = 2
    try:
        requested_report = _report_target(report_path, repo_root)
        run_dir = _new_run_dir(repo_root)
        frozen = migration_runner._freeze_batch(migrations, run_dir)
        staged = tuple(item.staged for item in frozen)
        classified = _classify_files(staged, target.schema)
        inventory = inventory_runner(target, run_dir / "identity")
        expected_identity = migration_runner._identity_for_receipt(inventory, target)
        report["target"] = migration_runner._identity_summary(expected_identity)
        report["folders"] = [
            _folder_report(item.original, plan, expected_identity)
            for item, plan in zip(frozen, classified, strict=True)
        ]

        if target.environment in {"staging", "prod"}:
            migration_runner._print_target_summary(target, expected_identity, frozen)
            prompt = f"Rehearsing against {target.environment.upper()}. Proceed? [y/N]"
            try:
                approved = bool(confirm(prompt))
            except (EOFError, OSError):
                approved = False
            if not approved:
                report["status"] = "declined"
                print(f"Rehearsal against {target.environment.upper()} declined; no data changes were attempted.")
                exit_code = 1
                _write_report(requested_report, report)
                return exit_code

        for item in frozen:
            relative = relative_to_root(item.original.folder, repo_root).as_posix()
            current = migration_runner.load_migration(repo_root, relative)
            if current.payload_digest != item.original.payload_digest:
                raise MigrationManifestError(f"source changed after rehearsal review: {item.original.folder.name}")
            migration_runner._verify_staged_payload(item.staged)

        for migration, plans in zip(staged, classified, strict=True):
            for file, plan in zip(migration.files, plans, strict=True):
                if plan.get("status") != "rehearsed":
                    continue
                for finding in lint_sqlcl_transport(file.source.decode("utf-8"), "direct"):
                    message = f"{file.name}:{finding.line} ({finding.code}): {finding.message}"
                    if finding.severity == "error":
                        raise MigrationManifestError(f"SQLcl transport preflight refused: {message}")
                    print(f"migration warning: SQLcl transport review: {message}", file=sys.stderr)

        _verify_autocommit_off(run_dir, target, session_runner)
        driver = _render_driver(run_dir, target, expected_identity, frozen, classified)
        try:
            result = session_runner(target, driver, driver.parent, phase="apply")
            session_output = result.output
            session_error = None
        except (SqlclError, OSError, RuntimeError, ValueError) as error:
            session_output = error.output if isinstance(error, SqlclError) else ""
            session_error = str(error)

        rollback_marker = "MIGRATION_REHEARSAL_ROLLBACK_COMPLETED"
        precondition_failure = "CHECK_REHEARSAL_FAILED:preconditions" in session_output
        if session_error and not precondition_failure:
            report["errors"].append(session_error)
            report["errors"].extend(_session_error_lines(session_output))
            retain_evidence = True

        try:
            apply_identity = migration_runner._apply_identity_from_output(session_output)
            migration_runner._assert_same_target(expected_identity, apply_identity)
        except migration_runner.MigrationApplyError as error:
            if not any("MIGRATION_IDENTITY" in str(item) for item in report["errors"]):
                report["errors"].append(str(error))
            retain_evidence = True

        _attach_frames(report["folders"], staged, session_output, "preconditions")
        _attach_frames(report["folders"], staged, session_output, "postconditions")
        _update_statement_rows(report["folders"], frozen, classified, session_output)
        _mark_file_results(report["folders"], frozen, classified, session_output)
        _attach_metric_frames(report["folders"], staged, session_output, expected_identity)
        if any(
            group["status"] == "error"
            for folder in report["folders"]
            for group in (
                folder["rehearsalMetrics"]["before"],
                folder["rehearsalMetrics"]["after"],
                folder["rehearsalMetrics"]["rollback"],
            )
        ):
            retain_evidence = True
        starting_values = _rollback_start_values(
            report["folders"], staged, session_output, expected_identity,
        )

        if rollback_marker in session_output:
            ran_data = any(file.get("executionStatus") in {"complete", "failed"} for folder in report["folders"] for file in folder["files"])
            if ran_data:
                report["rollbackProof"] = _rollback_proof_in_session(
                    report["folders"], staged, session_output, expected_identity, starting_values,
                )
            else:
                for folder in report["folders"]:
                    folder["rollbackProof"] = {"status": "not needed", "checks": []}
                report["rollbackProof"] = {
                    "status": "not needed", "method": "no data-changing statement executed", "explicitRollback": True,
                }
        elif session_error or precondition_failure:
            report["rollbackProof"] = _check_rollback_separately(
                target, staged, report["folders"], run_dir, expected_identity, starting_values, check_runner,
            )
        else:
            report["errors"].append("SQLcl did not confirm the final ROLLBACK; rollback proof is unavailable")
            report["rollbackProof"] = _check_rollback_separately(
                target, staged, report["folders"], run_dir, expected_identity, starting_values, check_runner,
            )
            retain_evidence = True

        _merge_metric_rollback_proof(report)

        for item in frozen:
            relative = relative_to_root(item.original.folder, repo_root).as_posix()
            current = migration_runner.load_migration(repo_root, relative)
            if current.payload_digest != item.original.payload_digest:
                report["errors"].append(f"local source changed during rehearsal: {item.original.folder.name}")
                retain_evidence = True

        has_unrehearsable = any(file["status"] == "not rehearsable" for folder in report["folders"] for file in folder["files"])
        has_false_checks = any(
            check_group["status"] == "false"
            for folder in report["folders"]
            for check_group in (folder["preconditions"], folder["postconditions"], folder["rollbackProof"])
        ) or any(
            group["status"] == "false"
            for folder in report["folders"]
            for group in (
                folder["rehearsalMetrics"]["before"],
                folder["rehearsalMetrics"]["after"],
                folder["rehearsalMetrics"]["rollback"],
            )
        )
        has_check_error = any(
            check_group["status"] == "error"
            for folder in report["folders"]
            for check_group in (folder["preconditions"], folder["postconditions"], folder["rollbackProof"])
        ) or any(
            group["status"] == "error"
            for folder in report["folders"]
            for group in (
                folder["rehearsalMetrics"]["before"],
                folder["rehearsalMetrics"]["after"],
                folder["rehearsalMetrics"]["rollback"],
            )
        )
        if session_error and not precondition_failure:
            report["status"] = "failed"
            exit_code = 2
        elif report["errors"] or has_check_error:
            report["status"] = "error"
            exit_code = 2
        elif has_unrehearsable or has_false_checks or report["rollbackProof"]["status"] != "true":
            report["status"] = "incomplete"
            exit_code = 1
        else:
            report["status"] = "rehearsed"
            exit_code = 0
        _print_report(report)
        try:
            _write_report(requested_report, report)
        except (OSError, TypeError, ValueError) as error:
            print(f"migration rehearsal report could not be written: {error}", file=sys.stderr)
            return 2
        return exit_code
    except (MigrationManifestError, CatalogError, TargetResolutionError, OSError, RuntimeError, ValueError) as error:
        report["errors"].append(str(error))
        report["status"] = "error"
        print(f"migration rehearsal error: {error}", file=sys.stderr)
        exit_code = 2
        try:
            _write_report(requested_report, report)
        except (OSError, TypeError, ValueError) as report_error:
            print(f"migration rehearsal report could not be written: {report_error}", file=sys.stderr)
        return exit_code
    finally:
        if run_dir is not None:
            if not retain_evidence:
                safe_rmtree(run_dir)


def _mark_file_results(folders: list[dict], frozen: Sequence, classified: Sequence[Sequence[Mapping]], output: str) -> None:
    lines = output.splitlines()
    for folder_index, (folder, _item) in enumerate(zip(folders, frozen, strict=True), start=1):
        for file_index, file in enumerate(folder["files"], start=1):
            if file["status"] != "rehearsed":
                continue
            marker = f"{folder_index:03d}:{file_index:03d}"
            begin = f"MIGRATION_REHEARSAL_FILE_BEGIN:{marker}"
            end = f"MIGRATION_REHEARSAL_FILE_END:{marker}"
            if end in lines:
                file["executionStatus"] = "complete"
            elif begin in lines:
                file["executionStatus"] = "failed"
            else:
                file["executionStatus"] = "not run"
            if file["executionStatus"] == "not run":
                for statement in folder["statements"]:
                    if statement["file"] == file["file"]:
                        statement["status"] = "not run"
