#!/usr/bin/env python3
"""Evaluate migration checks read-only without applying migration SQL."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .db_targets import (
    TargetResolutionError,
    batch_schema,
    flat_migrations_apply,
    resolve_target,
)
from .migrate import (
    MigrationApplyError,
    _assert_check_identity,
    _identity_for_receipt,
)
from .migration_checks import CheckReport, run_checks
from .migration_manifest import (
    Migration,
    MigrationManifestError,
    assert_single_layout,
    load_batch,
    verify_loaded_input_hashes,
)
from .schema_catalog import CatalogError, capture_inventory
from .sqlcl_session import safe_rmtree


ROOT = Path(__file__).resolve().parents[1]


def _new_run_dir(repo_root: Path) -> Path:
    """Create a private evidence directory beneath this checkout's ignored scratch root."""
    scratch = repo_root / "scratch"
    if scratch.is_symlink():
        raise OSError("scratch must not be a symbolic link")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        scratch.chmod(0o700)
    except OSError as error:
        raise OSError(f"could not secure scratch directory: {error}") from error
    run_dir = Path(tempfile.mkdtemp(prefix="migration-verify-", dir=scratch))
    try:
        run_dir.chmod(0o700)
    except OSError as error:
        safe_rmtree(run_dir)
        raise OSError(f"could not secure verification run directory: {error}") from error
    return run_dir


def _check_items(migrations: Sequence[Migration], phase: str) -> list[tuple[Migration, object]]:
    attribute = "preconditions" if phase == "preconditions" else "postconditions"
    return [(migration, check) for migration in migrations for check in getattr(migration, attribute)]


def _phase_records(
    items: Sequence[tuple[Migration, object]],
    report: CheckReport,
    *,
    identity_error: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Map batched results back to each folder/check in input order."""
    check_ids = report.coverage.get("sessionCheckIds")
    result_counts = report.coverage.get("sessionResultCounts")
    if not isinstance(check_ids, list) or not isinstance(result_counts, list):
        check_ids = [[item[1].id for item in items]] if items else []
        result_counts = [len(report.results)] if items else []

    records: list[dict] = []
    errors: list[dict] = []
    item_offset = 0
    result_offset = 0
    global_errors = [
        error for error in report.errors
        if error.get("code") != "CHECK_FAILED"
    ]
    unmatched_errors = list(global_errors)

    for session_index, session_ids in enumerate(check_ids):
        if not isinstance(session_ids, list):
            continue
        session_items = items[item_offset:item_offset + len(session_ids)]
        item_offset += len(session_ids)
        raw_count = result_counts[session_index] if session_index < len(result_counts) else 0
        count = raw_count if type(raw_count) is int and raw_count >= 0 else 0
        session_results = report.results[result_offset:result_offset + count]
        result_offset += count
        for check_index, (migration, check) in enumerate(session_items):
            result = session_results[check_index] if check_index < len(session_results) else None
            observed = result.get("value") if isinstance(result, Mapping) else None
            error_message = None
            if identity_error:
                status = "error"
                error_message = identity_error
            elif result is None:
                status = "error"
                matching = next(
                    (error for error in unmatched_errors if error.get("check") == check.id),
                    None,
                )
                error_message = (
                    matching.get("message") if matching else
                    "check result was not available; the SQLcl session did not complete or returned an incomplete payload"
                )
                if matching:
                    unmatched_errors.remove(matching)
            elif result.get("id") != check.id:
                status = "error"
                error_message = "check result identity/order does not match the requested check"
            elif result.get("error"):
                status = "error"
                error_message = str(result["error"])
            elif result.get("passed") is True:
                status = "true"
            else:
                status = "false"

            record = {
                "folder": migration.folder.name,
                "phase": report.coverage.get("phase"),
                "id": check.id,
                "expected": check.expected,
                "observed": observed,
                "status": status,
                "error": error_message,
            }
            records.append(record)
            if status == "error":
                errors.append({
                    "code": "CHECK_ERROR",
                    "folder": migration.folder.name,
                    "phase": report.coverage.get("phase"),
                    "id": check.id,
                    "message": error_message,
                })

    if item_offset < len(items):
        for migration, check in items[item_offset:]:
            message = "check was not included in a completed SQLcl session"
            records.append({
                "folder": migration.folder.name,
                "phase": report.coverage.get("phase"),
                "id": check.id,
                "expected": check.expected,
                "observed": None,
                "status": "error",
                "error": message,
            })
            errors.append({
                "code": "CHECK_ERROR", "folder": migration.folder.name,
                "phase": report.coverage.get("phase"), "id": check.id,
                "message": message,
            })

    if identity_error is None:
        for error in global_errors:
            errors.append({
                "code": error.get("code", "CHECK_ERROR"),
                "phase": report.coverage.get("phase"),
                "message": error.get("message", "check session failed"),
            })
    return records, errors


def _render_text(report: Mapping, *, only_failed: bool) -> str:
    target = report.get("target")
    lines = []
    if isinstance(target, Mapping):
        lines.append(
            f"Target {target['environment'].upper()}: schema={target['schema']} "
            f"expected_user={target['expectedUser']}"
        )
    lines.append(
        f"Phase: {report['phase']}; SQLcl sessions: {report['summary']['sessions']}; "
        f"{report['summary']['total']} checks"
    )
    lines.append("PHASE          MIGRATION                         ID                 EXPECTED OBSERVED STATUS")
    rows = report["checks"]
    if rows:
        for item in rows:
            observed = "—" if item["observed"] is None else str(item["observed"])
            lines.append(
                f"{item['phase']:<14} {item['folder']:<32} {item['id']:<18} "
                f"{item['expected']!s:<8} {observed:<8} {item['status']}"
            )
    elif only_failed:
        lines.append("(no failed checks)")
    summary = report["summary"]
    lines.append(
        f"Summary: {summary['total']} checks; {summary['passed']} true, "
        f"{summary['failed']} false, {summary['errors']} errors"
    )
    for error in report["errors"]:
        location = "/".join(
            str(error[key]) for key in ("folder", "phase", "id") if error.get(key)
        )
        prefix = f"{location}: " if location else ""
        lines.append(f"ERROR: {prefix}{error.get('message', error.get('code', 'verification failed'))}")
    if report.get("evidenceDir"):
        lines.append(f"Evidence retained: {report['evidenceDir']}")
    return "\n".join(lines)


def _emit(report: dict, output_format: str, *, only_failed: bool) -> None:
    if only_failed:
        report["checks"] = [item for item in report["checks"] if item["status"] != "true"]
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(_render_text(report, only_failed=only_failed))


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path = ROOT,
    expected_input_hashes: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*")
    parser.add_argument("--env", action="append", choices=("dev", "staging", "prod"))
    parser.add_argument("--schema")
    parser.add_argument("--phase", choices=("pre", "post", "both"), default="both")
    parser.add_argument("--only-failed", action="store_true", help="omit checks that returned the expected value")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--jobs", type=int, default=1, help="maximum concurrent SQLcl check sessions")
    args = parser.parse_args(argv)

    status = 2
    run_dir: Path | None = None
    report: dict = {
        "schemaVersion": 1,
        "environment": (args.env or [None])[0],
        "phase": args.phase,
        "status": "error",
        "exitCode": 2,
        "target": None,
        "checks": [],
        "summary": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "sessions": 0},
        "errors": [],
        "evidenceDir": None,
    }

    try:
        if not args.folders:
            raise MigrationManifestError("select one or more migration folders")
        if len(args.env or []) != 1:
            raise TargetResolutionError("specify exactly one --env dev|staging|prod")
        if args.jobs < 1:
            raise ValueError("--jobs must be a positive integer")

        root = Path(repo_root).resolve()
        migrations = load_batch(root, args.folders)
        if expected_input_hashes is not None:
            verify_loaded_input_hashes(migrations, root, expected_input_hashes)
        values = os.environ if environ is None else environ
        environment = args.env[0]
        requested = args.schema or values.get("PROJECT_SCHEMA") or None
        schema = batch_schema(
            [migration.schema for migration in migrations],
            requested,
            values,
            environment,
        )
        target = resolve_target(values, environment, "migration", schema=schema)
        assert_single_layout(
            root,
            migrations,
            target.schema,
            flat_folders_apply=flat_migrations_apply(values, environment),
        )
        report["environment"] = environment
        report["target"] = {
            "environment": environment,
            "schema": target.schema,
            "expectedUser": target.expected_user,
        }

        selected_phases = (
            ("preconditions", "postconditions")
            if args.phase == "both"
            else (("preconditions",) if args.phase == "pre" else ("postconditions",))
        )
        phase_items = {phase: _check_items(migrations, phase) for phase in selected_phases}
        requested_items = sum(len(items) for items in phase_items.values())
        report["summary"]["total"] = requested_items

        if requested_items:
            run_dir = _new_run_dir(root)
            inventory = capture_inventory(target, run_dir)
            expected_identity = _identity_for_receipt(inventory, target)
            for phase in selected_phases:
                items = phase_items[phase]
                checks = tuple(check for _migration, check in items)
                phase_report = run_checks(
                    target,
                    checks,
                    run_dir / phase,
                    phase=phase,
                    jobs=args.jobs,
                )
                identity_error = None
                try:
                    _assert_check_identity(expected_identity, phase_report)
                except MigrationApplyError as error:
                    identity_error = str(error)
                phase_records, phase_errors = _phase_records(
                    items,
                    phase_report,
                    identity_error=identity_error,
                )
                if identity_error:
                    phase_errors.insert(0, {
                        "code": "IDENTITY_MISMATCH",
                        "phase": phase,
                        "message": identity_error,
                    })
                report["checks"].extend(phase_records)
                report["errors"].extend(phase_errors)
                report["summary"]["sessions"] += int(phase_report.coverage.get("sessions", 0))
                if not phase_report.complete and not phase_errors:
                    report["errors"].append({
                        "code": "CHECK_INCOMPLETE",
                        "phase": phase,
                        "message": "one or more read-only check sessions did not complete",
                    })

        report["summary"]["passed"] = sum(item["status"] == "true" for item in report["checks"])
        report["summary"]["failed"] = sum(item["status"] == "false" for item in report["checks"])
        report["summary"]["errors"] = sum(item["status"] == "error" for item in report["checks"])
        if report["errors"] or report["summary"]["errors"]:
            status = 2
        elif report["summary"]["failed"]:
            status = 1
        else:
            status = 0
    except KeyboardInterrupt:
        report["errors"].append({"code": "INTERRUPTED", "message": "verification was interrupted"})
        status = 2
    except (
        CatalogError,
        MigrationApplyError,
        MigrationManifestError,
        OSError,
        RuntimeError,
        TargetResolutionError,
        ValueError,
    ) as error:
        report["errors"].append({"code": "VERIFY_ERROR", "message": str(error)})
        status = 2

    if run_dir is not None:
        if status == 0:
            safe_rmtree(run_dir)
            if run_dir.exists():
                report["errors"].append({
                    "code": "SCRATCH_CLEANUP_FAILED",
                    "message": "successful verification scratch files could not be removed",
                })
                status = 2
        if status != 0 and run_dir.exists():
            report["evidenceDir"] = str(run_dir)

    report["status"] = {0: "passed", 1: "failed", 2: "error"}[status]
    report["exitCode"] = status
    _emit(report, args.format, only_failed=args.only_failed)
    if report.get("evidenceDir"):
        print(f"verification evidence retained in {report['evidenceDir']}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
