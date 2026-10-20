#!/usr/bin/env python3
"""Build local inventory and explicit live preflight reports without applying SQL."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import migration_manifest as manifest
from .migration_runtime import MigrationRuntimeError, require_migration_sqlcl_version
from .migration_revision import LockReport, MigrationRevisionError, inspect_migration_lock
from .path_utils import relative_to_root


SUMMARY_FIELDS = (
    "folderCount",
    "sqlFileCount",
    "preconditionCount",
    "postconditionCount",
    "singleSqlFileFolderCount",
    "immutableFolderCount",
    "unknownEvidenceFolderCount",
)
LIMITATIONS = (
    "Local-only evidence covers this checkout and cannot prove no attempt exists elsewhere.",
    "Migrations pending in other repositories are not visible to this report.",
    "No dependency or execution order is inferred from folder display order.",
    "A verified local receipt is historical evidence, not a read of current database shape.",
)
__all__ = [
    "build_live_migration_plan",
    "build_migration_plan",
    "main",
    "render_migration_plan",
    "require_migration_sqlcl_version",
]

_ORACLE_ERROR_RE = re.compile(r"\b(?:ORA|SP2)-\d{4,5}\b", re.IGNORECASE)
_SAFE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z", re.ASCII)
_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}\Z", re.ASCII)
_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,255}\Z", re.ASCII)
_IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)
_SAFE_PREFLIGHT_MESSAGES = {
    "CHECK_FAILED": "Live migration check failed; database details omitted.",
    "INCOMPLETE_VISIBILITY": "Live catalog observation is incomplete.",
    "LIVE_PREFLIGHT_UNAVAILABLE": "Live preflight could not complete; raw details omitted.",
    "UNSUPPORTED_OPERATION": "SQL operation needs explicit review; source text omitted.",
    "CHECK_TOO_LARGE": "A check exceeds the configured single-query byte limit.",
    "SQLCL_TIMEOUT": "SQLcl timed out while evaluating the preflight check.",
    "CHECK_UNAVAILABLE": "A live migration check could not complete; raw details omitted.",
}
_LIVE_ENVIRONMENTS = frozenset({"dev", "staging", "prod"})


def _canonical_environment(value: object) -> str | None:
    """Keep refusal envelopes to the three public environment labels."""
    return value if isinstance(value, str) and value in _LIVE_ENVIRONMENTS else None


def _safe_text(value: object, *, limit: int = 500) -> str:
    def printable(character: str) -> str:
        if character.isprintable():
            return character
        value = ord(character)
        return f"\\u{value:04x}" if value <= 0xFFFF else f"\\U{value:08x}"

    text = "".join(printable(character) for character in str(value))
    return text[:limit]


def _error_text(error: BaseException, root: Path | None = None) -> str:
    message = str(error)
    if root is not None:
        message = message.replace(str(root), "<repo>")
    if "SQL-only migration rejects '" in message:
        return "Migration SQL failed validation; source text is omitted."
    return _safe_text(message)


def _path_label(path: Path, root: Path) -> str:
    try:
        return _safe_text(relative_to_root(path, root).as_posix())
    except ValueError:
        return _safe_text(path.as_posix())


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
            return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _first_link_or_reparse(root: Path, parts: Sequence[str]) -> Path | None:
    current = root
    for part in parts:
        current = current / part
        if _is_link_or_reparse(current):
            return current
    return None


def _diagnostic(code: str, path: str | None, message: object) -> dict[str, str | None]:
    return {"code": code, "path": None if path is None else _safe_text(path), "message": _safe_text(message)}


def _candidate_fields(root: Path, folder: Path, schema_hint: str | None = None) -> dict[str, Any]:
    path = _path_label(folder, root)
    parts = Path(path).parts
    owner: str | None = None
    source = "unresolved"
    if len(parts) == 3 and not manifest.FOLDER_RE.fullmatch(parts[1]):
        owner = parts[1]
        source = "directory"
    elif schema_hint is not None:
        owner = schema_hint
        source = "caller-asserted"
    try:
        _date, family, revision = manifest._folder_parts(folder.name)
    except manifest.MigrationManifestError:
        family = None
        revision = None
    return {
        "path": path,
        "schema": owner,
        "schemaSource": source,
        "family": family,
        "revision": revision,
    }


def _empty_record(candidate: Mapping[str, Any], reasons: Sequence[str]) -> dict[str, Any]:
    return {
        "path": candidate["path"],
        "schema": candidate["schema"],
        "schemaSource": candidate["schemaSource"],
        "family": candidate["family"],
        "revision": candidate["revision"],
        "payloadDigest": None,
        "checksSha256": None,
        "files": [],
        "preconditionCount": 0,
        "postconditionCount": 0,
        "localEvidence": {"status": "unknown", "reasons": [_safe_text(reason) for reason in reasons]},
    }


def _receipt_statuses(root: Path, migration: manifest.Migration) -> tuple[bool, list[str]]:
    """Validate present receipts against this exact local payload, never their target as observed state."""
    found = False
    problems: list[str] = []
    for environment in sorted(manifest.STATUS_ENVIRONMENTS):
        receipt_path = migration.folder / f"status.{environment}.json"
        if _is_link_or_reparse(receipt_path):
            found = True
            problems.append(f"Receipt for {environment} is a symbolic link or reparse point.")
            continue
        try:
            exists = receipt_path.exists()
        except OSError:
            exists = True
        if not exists:
            continue
        found = True
        try:
            raw = manifest.decode_json(receipt_path.read_bytes(), "local receipt")
            target = raw.get("target") if isinstance(raw, dict) and isinstance(raw.get("target"), dict) else {}
            manifest.validate_receipt(receipt_path, migration, target)
        except (OSError, manifest.MigrationManifestError, TypeError, ValueError) as error:
            problems.append(f"Receipt for {environment} is invalid: {_error_text(error, root)}")
    return found, problems


def _local_evidence_for_path(
    root: Path,
    relative_path: str,
    migration: manifest.Migration,
) -> tuple[dict[str, Any], list[dict[str, str | None]]]:
    diagnostics: list[dict[str, str | None]] = []
    try:
        lock = inspect_migration_lock(root, relative_path)
    except (MigrationRevisionError, OSError, ValueError) as error:
        lock = LockReport("unknown", (_error_text(error, root),))
    has_receipt, receipt_problems = _receipt_statuses(root, migration)
    if receipt_problems:
        diagnostics.extend(_diagnostic("receipt-invalid", relative_path, reason) for reason in receipt_problems)
        return {"status": "unknown", "reasons": receipt_problems}, diagnostics
    if lock.status == "unknown":
        reasons = [_safe_text(reason.replace(str(root), "<repo>")) for reason in lock.states] or ["Local attempt evidence is incomplete or unreadable."]
        diagnostics.extend(_diagnostic("unknown-local-evidence", relative_path, reason) for reason in reasons)
        return {"status": "unknown", "reasons": reasons}, diagnostics
    if lock.status == "locked" or has_receipt:
        reasons = [_safe_text(reason.replace(str(root), "<repo>")) for reason in lock.states if reason != "no-attempt-or-receipt"]
        if not reasons:
            reasons = ["A locally verified receipt exists; it is historical evidence only."]
        return {"status": "locked", "reasons": reasons}, diagnostics
    reasons = [_safe_text(reason.replace(str(root), "<repo>")) for reason in lock.states]
    if reasons == ["no-attempt-or-receipt"]:
        reasons = ["No local attempt or receipt evidence was found."]
    if not reasons:
        reasons = ["No local attempt or receipt evidence was found."]
    return {"status": "unlocked", "reasons": reasons}, diagnostics


def _migration_record(
    root: Path,
    migration: manifest.Migration,
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str | None]]]:
    evidence, diagnostics = _local_evidence_for_path(root, candidate["path"], migration)
    return {
        "path": candidate["path"],
        "schema": candidate["schema"],
        "schemaSource": candidate["schemaSource"],
        "family": migration.family,
        "revision": migration.revision,
        "payloadDigest": migration.payload_digest,
        "checksSha256": migration.checks_sha256,
        "files": [
            {"name": item.name, "sequence": item.sequence, "sha256": item.sha256}
            for item in migration.files
        ],
        "preconditionCount": len(migration.preconditions),
        "postconditionCount": len(migration.postconditions),
        "localEvidence": evidence,
    }, diagnostics


def _discover_candidates(
    root: Path,
    schema: str | None,
    diagnostics: list[dict[str, str | None]],
) -> list[dict[str, Any]]:
    migrations_dir = root / "migrations"
    if _is_link_or_reparse(migrations_dir):
        diagnostics.append(_diagnostic("unsafe-path", "migrations", "Migrations directory is a symbolic link or reparse point."))
        return []
    if not migrations_dir.is_dir():
        diagnostics.append(_diagnostic("missing-migrations-directory", "migrations", "Migrations directory is missing or unreadable."))
        return []

    candidates: list[dict[str, Any]] = []

    def add_candidate(folder: Path, owner: str | None) -> None:
        if owner is not None and schema is not None and owner != schema:
            return
        candidate = _candidate_fields(root, folder, schema if owner is None else None)
        if owner is not None:
            candidate["schema"] = owner
            candidate["schemaSource"] = "directory"
        candidates.append(candidate)

    try:
        entries = sorted(migrations_dir.iterdir(), key=lambda item: item.name)
    except OSError as error:
        diagnostics.append(_diagnostic("unreadable-migrations-directory", "migrations", _error_text(error, root)))
        return []
    for entry in entries:
        relative = _path_label(entry, root)
        if _is_link_or_reparse(entry):
            diagnostics.append(_diagnostic("unsafe-path", relative, "Migration input is a symbolic link or reparse point."))
            add_candidate(entry, None)
            continue
        try:
            is_directory = entry.is_dir()
            is_file = entry.is_file()
        except OSError as error:
            diagnostics.append(_diagnostic("unreadable-path", relative, _error_text(error, root)))
            continue
        if is_file:
            if entry.name == ".gitkeep":
                continue
            if entry.suffix.lower() == ".sql":
                diagnostics.append(_diagnostic("invalid-migration-directory", relative, "Root-level SQL files are not migration folders."))
            continue
        if not is_directory:
            continue
        if manifest.FOLDER_RE.fullmatch(entry.name):
            add_candidate(entry, None)
            continue
        if manifest.SCHEMA_DIRECTORY_RE.fullmatch(entry.name) is None:
            diagnostics.append(_diagnostic("invalid-migration-directory", relative, "Directory is not a dated migration folder or uppercase schema owner."))
            add_candidate(entry, None)
            continue
        if schema is not None and entry.name != schema:
            continue
        try:
            children = sorted(entry.iterdir(), key=lambda item: item.name)
        except OSError as error:
            diagnostics.append(_diagnostic("unreadable-schema-directory", relative, _error_text(error, root)))
            continue
        for child in children:
            child_path = _path_label(child, root)
            if _is_link_or_reparse(child):
                diagnostics.append(_diagnostic("unsafe-path", child_path, "Migration input is a symbolic link or reparse point."))
                add_candidate(child, entry.name)
                continue
            try:
                child_is_dir = child.is_dir()
                child_is_file = child.is_file()
            except OSError as error:
                diagnostics.append(_diagnostic("unreadable-path", child_path, _error_text(error, root)))
                continue
            if child_is_dir:
                if manifest.FOLDER_RE.fullmatch(child.name) is None:
                    diagnostics.append(_diagnostic("invalid-migration-directory", child_path, "Schema directory contains a non-dated migration folder."))
                add_candidate(child, entry.name)
            elif child_is_file and child.suffix.lower() == ".sql":
                diagnostics.append(_diagnostic("invalid-migration-directory", child_path, "SQL files must be inside a dated migration folder."))
    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate["schema"], []).append(candidate)
    ordered: list[dict[str, Any]] = []
    for owner in sorted(grouped, key=lambda value: value or ""):
        ordered.extend(sorted(grouped[owner], key=lambda item: (item["path"].split("/")[-1], item["path"]), reverse=True))
    return ordered


def _summary(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "folderCount": len(records),
        "sqlFileCount": sum(len(record["files"]) for record in records),
        "preconditionCount": sum(record["preconditionCount"] for record in records),
        "postconditionCount": sum(record["postconditionCount"] for record in records),
        "singleSqlFileFolderCount": sum(len(record["files"]) == 1 for record in records),
        "immutableFolderCount": sum(record["localEvidence"]["status"] == "locked" for record in records),
        "unknownEvidenceFolderCount": sum(record["localEvidence"]["status"] == "unknown" for record in records),
    }


def build_migration_plan(repo_root: Path, folders: Sequence[str] = (), *, schema: str | None = None) -> dict:
    """Inventory local migration files without reading configuration or contacting a database."""
    root = Path(repo_root).resolve()
    diagnostics: list[dict[str, str | None]] = []
    records: list[dict[str, Any]] = []
    if schema is not None and (not isinstance(schema, str) or manifest.SCHEMA_DIRECTORY_RE.fullmatch(schema) is None):
        diagnostics.append(_diagnostic("invalid-schema", None, "Schema must be an uppercase Oracle identifier."))
        schema = None

    explicit = bool(folders)
    if isinstance(folders, (str, bytes)):
        diagnostics.append(_diagnostic("invalid-selection", None, "Folders must be a sequence of repository-relative paths."))
        folders = ()
        explicit = False

    if not explicit:
        candidates = _discover_candidates(root, schema, diagnostics)
        for candidate in candidates:
            path = candidate["path"]
            full_path = root / Path(path)
            if _is_link_or_reparse(full_path):
                records.append(_empty_record(candidate, ["Migration path is a symbolic link or reparse point."]))
                continue
            try:
                migration = manifest.load_migration(root, path)
            except (manifest.MigrationManifestError, OSError, ValueError) as error:
                message = _error_text(error, root)
                diagnostics.append(_diagnostic("migration-invalid", path, message))
                records.append(_empty_record(candidate, [message]))
                continue
            record, evidence_diagnostics = _migration_record(root, migration, candidate)
            records.append(record)
            diagnostics.extend(evidence_diagnostics)
    else:
        requested = list(folders)
        normalized: list[str] = []
        candidate_by_path: dict[str, dict[str, Any]] = {}
        record_by_path: dict[str, dict[str, Any]] = {}
        groups: dict[str | None, list[str]] = {}
        for raw in requested:
            if not isinstance(raw, str):
                diagnostics.append(_diagnostic("invalid-selection", None, "Each selected folder must be a repository-relative path."))
                continue
            try:
                parts = manifest._validate_relative_folder(raw)
            except manifest.MigrationManifestError as error:
                diagnostics.append(_diagnostic("invalid-path", raw, _safe_text(error)))
                continue
            relative = "/".join(parts)
            if relative in normalized:
                diagnostics.append(_diagnostic("duplicate-selection", relative, "A migration folder may be selected only once."))
                continue
            normalized.append(relative)
            unsafe_component = _first_link_or_reparse(root, parts)
            if unsafe_component is not None:
                component_path = _path_label(unsafe_component, root)
                reason = f"Selected path component {component_path} is a symbolic link or reparse point."
                diagnostics.append(_diagnostic("unsafe-path", relative, reason))
                candidate = _candidate_fields(root, root / relative, schema)
                record_by_path[relative] = _empty_record(candidate, [reason])
                continue
            path_schema = parts[1] if len(parts) == 3 else None
            if path_schema is not None and schema is not None and path_schema != schema:
                diagnostics.append(_diagnostic("schema-mismatch", relative, f"Selected folder belongs to schema {path_schema}, not {schema}."))
                candidate = _candidate_fields(root, root / relative, schema)
                candidate_by_path[relative] = candidate
                record_by_path[relative] = _empty_record(candidate, ["Selected folder schema does not match the supplied schema."])
                continue
            candidate = _candidate_fields(root, root / relative, schema)
            candidate_by_path[relative] = candidate
            groups.setdefault(path_schema, []).append(relative)

        loaded: dict[str, manifest.Migration] = {}
        for _owner, group in groups.items():
            try:
                batch = manifest.load_batch(root, group)
                for migration in batch:
                    loaded[relative_to_root(migration.folder, root).as_posix()] = migration
            except (manifest.MigrationManifestError, OSError, ValueError) as error:
                diagnostics.append(_diagnostic("selection-invalid", group[0] if group else None, _error_text(error, root)))
                # Preserve valid individual records when a batch-level ordering or content error occurs.
                for relative in group:
                    try:
                        loaded[relative] = manifest.load_migration(root, relative)
                    except (manifest.MigrationManifestError, OSError, ValueError) as item_error:
                        message = _error_text(item_error, root)
                        diagnostics.append(_diagnostic("migration-invalid", relative, message))
                        record_by_path[relative] = _empty_record(candidate_by_path[relative], [message])

        for relative in normalized:
            if relative not in candidate_by_path or relative not in loaded:
                continue
            candidate = candidate_by_path[relative]
            migration = loaded[relative]
            owner = migration.schema
            if owner is not None:
                candidate["schema"] = owner
                candidate["schemaSource"] = "directory"
            elif schema is not None:
                candidate["schema"] = schema
                candidate["schemaSource"] = "caller-asserted"
            record, evidence_diagnostics = _migration_record(root, migration, candidate)
            record_by_path[relative] = record
            diagnostics.extend(evidence_diagnostics)
        records.extend(record_by_path[relative] for relative in normalized if relative in record_by_path)

    # Discovery order is defined for display, while explicit records retain caller order.
    if diagnostics or any(record["localEvidence"]["status"] == "unknown" for record in records):
        exit_code = 2
    else:
        exit_code = 0
    return {
        "schemaVersion": 1,
        "mode": "offline",
        "selection": "explicit" if explicit else "discovered",
        "orderStatus": "caller-supplied" if explicit else "sorted-display-only",
        "environment": None,
        "target": None,
        "summary": _summary(records),
        "migrations": records,
        "preflight": None,
        "diagnostics": diagnostics,
        "limitations": list(LIMITATIONS),
        "exitCode": exit_code,
    }


def _live_report_from_offline(report: Mapping[str, Any], environment: str | None) -> dict[str, Any]:
    """Copy the stable offline inventory envelope for a live refusal."""
    result = dict(report)
    result.update({"mode": "live", "environment": _canonical_environment(environment), "target": None, "preflight": None})
    result["diagnostics"] = list(report.get("diagnostics", []))
    result["limitations"] = list(report.get("limitations", []))
    result["exitCode"] = 2
    return result


def _input_hashes_from_report(records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for record in records:
        folder = record.get("path")
        if not isinstance(folder, str):
            continue
        for source_file in record.get("files", []):
            if isinstance(source_file, Mapping) and isinstance(source_file.get("name"), str) and isinstance(source_file.get("sha256"), str):
                expected[f"{folder}/{source_file['name']}"] = source_file["sha256"]
        checks_sha256 = record.get("checksSha256")
        if isinstance(checks_sha256, str):
            expected[f"{folder}/checks.json"] = checks_sha256
    return expected


def _changed_input_path(
    repo_root: Path,
    folders: Sequence[str],
    expected_hashes: Mapping[str, str],
) -> str | None:
    """Compare the current selected bytes with an earlier inventory, without exposing them."""
    root = Path(repo_root).resolve()
    try:
        current = manifest.load_batch(root, folders)
    except (manifest.MigrationManifestError, OSError, ValueError):
        return _first_selected_folder_path(root, folders)

    observed_hashes: dict[str, str] = {}
    for migration in current:
        folder = relative_to_root(migration.folder.resolve(), root).as_posix()
        observed_hashes.update({f"{folder}/{source_file.name}": source_file.sha256 for source_file in migration.files})
        observed_hashes[f"{folder}/checks.json"] = migration.checks_sha256

    for relative_path in sorted(expected_hashes.keys() | observed_hashes.keys()):
        if expected_hashes.get(relative_path) != observed_hashes.get(relative_path):
            return relative_path
    return None


def _first_selected_folder_path(repo_root: Path, folders: Sequence[str]) -> str:
    if not folders:
        return "migrations"
    try:
        selected = Path(folders[0])
        candidate = selected if selected.is_absolute() else repo_root / selected
        return relative_to_root(candidate.resolve(), repo_root).as_posix()
    except (OSError, RuntimeError, TypeError, ValueError):
        return "migrations"


def build_live_migration_plan(
    repo_root: Path,
    folders: Sequence[str],
    *,
    environment: str,
    schema: str | None,
    values: Mapping[str, str],
) -> dict:
    """Build an explicitly selected live preflight report without applying SQL."""
    root = Path(repo_root).resolve()
    offline = build_migration_plan(root, folders, schema=schema)
    report = _live_report_from_offline(offline, environment)

    if not folders:
        report["diagnostics"].append(
            _diagnostic("live-selection-required", None, "Live reporting requires explicitly selected migration folders.")
        )
        report["selection"] = offline["selection"]
        return report
    if environment not in {"dev", "staging", "prod"}:
        report["diagnostics"].append(
            _diagnostic("live-environment-required", None, "Select dev, staging, or prod for live migration preflight.")
        )
        return report
    if offline["exitCode"] != 0 or offline["selection"] != "explicit":
        report["diagnostics"].append(
            _diagnostic("live-selection-invalid", None, "Live preflight requires a valid explicit selection with known local evidence.")
        )
        return report
    if not isinstance(values, Mapping):
        report["diagnostics"].append(
            _diagnostic("live-target-unavailable", None, "A validated file mapping is required to resolve the live migration target.")
        )
        return report

    # Snapshot the caller's validated mapping so a mutable mapping cannot change
    # between target validation and the read-only adapter.
    effective_values = dict(values)
    selected_paths = [record["path"] for record in offline["migrations"]]
    from .db_targets import TargetResolutionError, batch_schema, flat_migrations_apply, resolve_target

    try:
        selected = manifest.load_batch(root, selected_paths)

        requested_schema = schema or effective_values.get("PROJECT_SCHEMA") or None
        owner = batch_schema([migration.schema for migration in selected], requested_schema, effective_values, environment)
        target = resolve_target(effective_values, environment, "migration", schema=owner)
        manifest.assert_single_layout(
            root,
            selected,
            target.schema,
            flat_folders_apply=flat_migrations_apply(effective_values, environment),
        )
    except (manifest.MigrationManifestError, TargetResolutionError, OSError, ValueError):
        # Configuration and connection names stay out of report diagnostics.
        report["diagnostics"].append(
            _diagnostic("live-target-unavailable", None, "The selected owner and supplied profile do not resolve to one safe migration target.")
        )
        return report

    expected_hashes = _input_hashes_from_report(offline["migrations"])
    if not expected_hashes:
        report["diagnostics"].append(
            _diagnostic("live-selection-invalid", None, "Selected migration SQL/check hashes are incomplete.")
        )
        return report

    from . import migration_checks

    try:
        require_migration_sqlcl_version()
    except MigrationRuntimeError:
        report["diagnostics"].append(
            _diagnostic("sqlcl-version-unavailable", None, "SQLcl 26.3.0.0 or newer is required for a live report.")
        )
        return report

    preflight = migration_checks.preflight_selected(
        root,
        selected_paths,
        environment=environment,
        schema=target.schema,
        values=effective_values,
    )
    preflight_payload = preflight.to_dict()
    coverage = dict(preflight.coverage)
    preflight_payload["coverage"] = coverage
    report["preflight"] = preflight_payload
    report["limitations"].append(preflight.limitation)

    changed_path = _changed_input_path(root, selected_paths, expected_hashes)
    if changed_path is not None:
        message = "selected SQL/check inputs changed or became unreadable during live preflight; source omitted"
        report["diagnostics"].append(_diagnostic("selected-input-changed", changed_path, message))
        coverage["complete"] = False
        coverage["selectedInputHashesVerified"] = False
        errors = list(preflight_payload.get("errors", []))
        if not any(error.get("code") == "SELECTED_INPUT_CHANGED" for error in errors):
            errors.append({"code": "SELECTED_INPUT_CHANGED", "path": changed_path, "message": message})
        preflight_payload["errors"] = errors
        preflight_payload["exit_code"] = 2
        report["preflight"] = preflight_payload
        return report

    coverage.setdefault("selectedInputHashesVerified", True)
    locked = any(record["localEvidence"]["status"] == "locked" for record in offline["migrations"])
    observed_identity = migration_checks.complete_observed_identity(coverage.get("observedIdentity"))
    identity_error_reported = any(
        isinstance(error, Mapping) and error.get("code") == "OBSERVED_IDENTITY_INCOMPLETE"
        for error in preflight_payload.get("errors", ())
    )
    # Exit 2 also covers failures before a snapshot was captured. Diagnose
    # incomplete identity only when the adapter says capture completed or
    # already identified the captured identity as incomplete.
    identity_capture_completed = (
        preflight.exit_code in (0, 1)
        or coverage.get("complete") is True
        or identity_error_reported
    )
    identity_incomplete = observed_identity is None and identity_capture_completed
    if observed_identity is None:
        coverage.pop("observedIdentity", None)
    if identity_incomplete:
        coverage["complete"] = False
        message = migration_checks.OBSERVED_IDENTITY_INCOMPLETE_MESSAGE
        errors = list(preflight_payload.get("errors", []))
        if not any(error.get("code") == "OBSERVED_IDENTITY_INCOMPLETE" for error in errors):
            errors.append({"code": "OBSERVED_IDENTITY_INCOMPLETE", "message": message})
        preflight_payload["errors"] = errors
        preflight_payload["exit_code"] = 2
        report["diagnostics"].append(
            _diagnostic("live-target-identity-incomplete", None, message)
        )

    if identity_incomplete or preflight.exit_code == 2:
        report["exitCode"] = 2
    elif preflight.exit_code == 1 or locked:
        report["exitCode"] = 1
    else:
        report["exitCode"] = 0

    if (
        report["exitCode"] in (0, 1)
        and preflight.exit_code in (0, 1)
        and coverage.get("complete") is True
        and not preflight.errors
        and observed_identity is not None
    ):
        report["target"] = observed_identity
    return report


def _markdown_cell(value: object) -> str:
    text = html.escape(_safe_text(value, limit=2000), quote=False)
    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\n", "<br>")
        .replace("\r", "")
    )


def _safe_report_path(value: object) -> str | None:
    """Keep useful relative paths while excluding controls and traversal."""
    if not isinstance(value, str) or not value or len(value) > 1000:
        return None
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/")):
        return None
    return _safe_text(normalized, limit=1000)


def _safe_identifier(value: object, *, component: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    pattern = _SAFE_COMPONENT_RE if component else _SAFE_IDENTIFIER_RE
    return value if pattern.fullmatch(value) else None


def _safe_oracle_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _ORACLE_ERROR_RE.fullmatch(value)
    return match.group(0).upper() if match else None


def _safe_preflight_message(code: str, error_code: str | None) -> str:
    if error_code:
        return f"Oracle returned {error_code}; raw details omitted."
    return _SAFE_PREFLIGHT_MESSAGES.get(code, "Preflight condition needs review; raw details omitted.")


def _safe_preflight_item(item: object, *, conflict: bool) -> dict[str, Any]:
    """Project one untrusted preflight row onto fields safe for public reports."""
    source = item if isinstance(item, Mapping) else {}
    raw_code = source.get("code")
    code = raw_code if isinstance(raw_code, str) and _SAFE_CODE_RE.fullmatch(raw_code) else "UNKNOWN_CONDITION"
    safe: dict[str, Any] = {"code": code}

    path = _safe_report_path(source.get("path"))
    if path is not None:
        safe["path"] = path
    for field in ("migration", "file", "check"):
        value = _safe_identifier(source.get(field), component=True)
        if value is not None:
            safe[field] = value
    for field in ("name", "table", "column", "view", "required_by", "namespace"):
        value = _safe_identifier(source.get(field))
        if value is not None:
            safe[field] = value
    existing_types = source.get("existing_types")
    if isinstance(existing_types, (list, tuple)):
        safe["existing_types"] = [
            value for value in existing_types
            if isinstance(value, str) and _SAFE_IDENTIFIER_RE.fullmatch(value)
        ][:16]
    columns = source.get("columns")
    if isinstance(columns, (list, tuple)):
        safe["columns"] = [
            value for value in columns
            if isinstance(value, str) and _SAFE_IDENTIFIER_RE.fullmatch(value)
        ][:64]
    for field in ("statement_index", "sequence"):
        value = source.get(field)
        if type(value) is int and value >= 0:
            safe[field] = value
    for field in ("expected", "observedValue"):
        value = source.get(field)
        if type(value) in {int, float} and (type(value) is int or math.isfinite(value)):
            safe[field] = value

    error_code = _safe_oracle_code(source.get("errorCode"))
    if error_code is None:
        raw_message = source.get("message")
        if isinstance(raw_message, str):
            match = _ORACLE_ERROR_RE.search(raw_message)
            error_code = match.group(0).upper() if match else None
    if error_code is not None:
        safe["errorCode"] = error_code
    safe["message"] = _safe_preflight_message(code, error_code)
    if not conflict and isinstance(source.get("reason"), str):
        safe["reason"] = _safe_preflight_message(code, error_code)
    return safe


def _safe_identity(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    identity: dict[str, str] = {}
    for field in _IDENTITY_FIELDS:
        item = value.get(field)
        if isinstance(item, (str, int)) and str(item).strip():
            identity[field] = _safe_text(item, limit=255)
    return identity or None


def _safe_folder_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        item for item in value
        if isinstance(item, str) and _SAFE_COMPONENT_RE.fullmatch(item)
    ][:256]


def _safe_preflight(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    safe: dict[str, Any] = {}
    exit_code = value.get("exit_code")
    if type(exit_code) is int and exit_code in {0, 1, 2}:
        safe["exit_code"] = exit_code
    for field in ("passed", "complete"):
        if type(value.get(field)) is bool:
            safe[field] = value[field]
    conflicts = value.get("conflicts", [])
    errors = value.get("errors", [])
    safe["conflicts"] = [
        _safe_preflight_item(item, conflict=True)
        for item in conflicts[:1000]
    ] if isinstance(conflicts, (list, tuple)) else []
    safe["errors"] = [
        _safe_preflight_item(item, conflict=False)
        for item in errors[:1000]
    ] if isinstance(errors, (list, tuple)) else []

    raw_coverage = value.get("coverage")
    coverage: dict[str, Any] = {}
    if isinstance(raw_coverage, Mapping):
        for field in ("complete", "live_state_checked", "selectedInputHashesVerified", "ownerComplete"):
            if type(raw_coverage.get(field)) is bool:
                coverage[field] = raw_coverage[field]
        for field in ("structural_operations", "opaque_operations"):
            count = raw_coverage.get(field)
            if type(count) is int and count >= 0:
                coverage[field] = count
        for field in ("mode", "analysis", "environment"):
            item = raw_coverage.get(field)
            if isinstance(item, str) and _SAFE_COMPONENT_RE.fullmatch(item):
                coverage[field] = item
        selected = _safe_folder_names(raw_coverage.get("selected_migrations"))
        if selected:
            coverage["selected_migrations"] = selected
        failed = _safe_folder_names(raw_coverage.get("failedCheckIds"))
        if failed:
            coverage["failedCheckIds"] = failed
        deferred = _safe_folder_names(raw_coverage.get("deferredPreconditionFolders"))
        deferred_info = raw_coverage.get("deferred_preconditions")
        if not deferred and isinstance(deferred_info, Mapping):
            deferred = _safe_folder_names(deferred_info.get("folders"))
        if deferred:
            coverage["deferredPreconditionFolders"] = deferred
            coverage["deferred_preconditions"] = {
                "folders": deferred,
                "reason": "Checked when each folder reaches its own apply boundary.",
            }
        identity = _safe_identity(raw_coverage.get("observedIdentity"))
        if identity is not None:
            coverage["observedIdentity"] = identity
        reviews = raw_coverage.get("manual_review_required")
        if isinstance(reviews, (list, tuple)):
            safe_reviews = []
            for review in reviews[:256]:
                if not isinstance(review, Mapping):
                    continue
                raw_reason = review.get("reason")
                reason = (
                    "CREATE OR REPLACE VIEW requires review of its declared precondition."
                    if isinstance(raw_reason, str) and "create or replace view" in raw_reason.casefold()
                    else "Opaque SQL requires manual review; source text omitted."
                )
                row: dict[str, str] = {"reason": reason}
                migration = _safe_identifier(review.get("migration"), component=True)
                filename = _safe_identifier(review.get("file"), component=True)
                if migration is not None:
                    row["migration"] = migration
                if filename is not None:
                    row["file"] = filename
                safe_reviews.append(row)
            if safe_reviews:
                coverage["manual_review_required"] = safe_reviews
        inventory = raw_coverage.get("live_inventory")
        if isinstance(inventory, Mapping):
            safe_inventory: dict[str, Any] = {}
            owner_complete = inventory.get("ownerComplete")
            if type(owner_complete) is bool:
                safe_inventory["ownerComplete"] = owner_complete
            catalogs = _safe_folder_names(inventory.get("catalogs"))
            if catalogs:
                safe_inventory["catalogs"] = catalogs
            if safe_inventory:
                coverage["live_inventory"] = safe_inventory
    safe["coverage"] = coverage

    safe["limitation"] = "Live preflight is a point-in-time read-only observation; other repositories are not included."
    return safe


def _safe_report_for_render(report: Mapping[str, Any]) -> dict[str, Any]:
    """Remove raw preflight details at the report's public serialization boundary."""
    safe = dict(report)
    safe["target"] = _safe_identity(report.get("target"))
    safe["preflight"] = _safe_preflight(report.get("preflight"))
    return safe


def _group_records(records: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        owner = record.get("schema")
        label = str(owner) if owner is not None else "unresolved"
        groups.setdefault(label, []).append(record)
    return [(owner, groups[owner]) for owner in sorted(groups)]


def _text_report(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"Migration plan ({report['mode']})",
        f"Mode: {report['mode']}",
        f"Selection: {report['selection']}",
        f"Order: {report['orderStatus']}",
        f"Folders: {summary['folderCount']}",
        f"SQL files: {summary['sqlFileCount']}",
        f"Preconditions: {summary['preconditionCount']}",
        f"Postconditions: {summary['postconditionCount']}",
        f"Single-SQL-file folders: {summary['singleSqlFileFolderCount']}",
        f"Immutable folders: {summary['immutableFolderCount']}",
        f"Unknown-evidence folders: {summary['unknownEvidenceFolderCount']}",
        "Migrations by schema:",
    ]
    if report.get("environment"):
        lines.insert(2, f"Environment: {report['environment']}")
    target = report.get("target")
    if target:
        lines.insert(3, "Target: " + ", ".join(f"{key}={value}" for key, value in target.items()))
    for schema, group in _group_records(report["migrations"]):
        lines.append(f"  {schema}:")
        for record in group:
            files = len(record["files"])
            family = record["family"] or "invalid"
            revision = f"r{record['revision']:03d}" if isinstance(record["revision"], int) else "unknown revision"
            reasons = "; ".join(record["localEvidence"]["reasons"])
            lines.append(f"    {record['path']} — {family} {revision}, {files} SQL file(s), evidence {record['localEvidence']['status']} ({reasons})")
    preflight = report.get("preflight")
    if report["mode"] == "live":
        lines.append("Live preflight:")
        if preflight is None:
            lines.append("  Preflight: unavailable")
        else:
            conflicts = preflight.get("conflicts", [])
            errors = preflight.get("errors", [])
            lines.append(f"  Preflight exit: {preflight.get('exit_code', 'unknown')} ({len(conflicts)} conflict(s), {len(errors)} error(s))")
            for heading, items in (("Conflicts", conflicts), ("Errors", errors)):
                if items:
                    lines.append(f"  Preflight {heading.lower()}:")
                    lines.extend(
                        "    [" + item["code"] + "] " + "; ".join(
                            f"{key}={item[key]}" for key in ("path", "migration", "file", "check", "name", "table", "column", "namespace", "errorCode") if key in item
                        ) + (": " if any(key in item for key in ("path", "migration", "file", "check", "name", "table", "column", "namespace", "errorCode")) else " ") + item["message"]
                        for item in items
                    )
            coverage = preflight.get("coverage", {})
            deferred = coverage.get("deferredPreconditionFolders", [])
            if deferred:
                lines.append("  Deferred preconditions: " + ", ".join(deferred) + " (checked when each folder reaches its apply boundary).")
            reviews = coverage.get("manual_review_required", [])
            for review in reviews:
                where = "/".join(value for value in (review.get("migration"), review.get("file")) if value)
                lines.append(f"  Manual review: {where}: {review['reason']}")
    if report["diagnostics"]:
        lines.append("Diagnostics:")
        lines.extend(
            f"  [{item['code']}] {item['path'] or '(report)'}: {item['message']}"
            for item in report["diagnostics"]
        )
    lines.append("Limitations:")
    lines.extend(f"  - {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _markdown_report(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Migration plan",
        "",
        f"- Mode: {_markdown_cell(report['mode'])}",
        f"- Selection: `{_markdown_cell(report['selection'])}`",
        f"- Order: `{_markdown_cell(report['orderStatus'])}`",
        "",
        "## Summary",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
    ]
    if report.get("environment"):
        lines.insert(4, f"- Environment: `{_markdown_cell(report['environment'])}`")
    target = report.get("target")
    if target:
        lines.insert(5 if report.get("environment") else 4, "- Target: " + ", ".join(
            f"`{_markdown_cell(key)}={_markdown_cell(value)}`" for key, value in target.items()
        ))
    for key in SUMMARY_FIELDS:
        lines.append(f"| {_markdown_cell(key)} | {summary[key]} |")
    lines.extend(("", "## Migrations by schema"))
    for schema, group in _group_records(report["migrations"]):
        lines.extend(("", f"### {_markdown_cell(schema)}", "", "| Path | Family | Revision | SQL files | Preconditions | Postconditions | Local evidence |", "| --- | --- | ---: | ---: | ---: | ---: | --- |"))
        for record in group:
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value)
                    for value in (
                        record["path"],
                        record["family"] or "invalid",
                        f"r{record['revision']:03d}" if isinstance(record["revision"], int) else "unknown",
                        len(record["files"]),
                        record["preconditionCount"],
                        record["postconditionCount"],
                        f"{record['localEvidence']['status']}: {'; '.join(record['localEvidence']['reasons'])}",
                    )
                ) + " |"
            )
    preflight = report.get("preflight")
    if report["mode"] == "live":
        lines.extend(("", "## Live preflight", ""))
        if preflight is None:
            lines.append("Preflight: unavailable.")
        else:
            conflicts = preflight.get("conflicts", [])
            errors = preflight.get("errors", [])
            lines.append(f"Preflight exit: {preflight.get('exit_code', 'unknown')} ({len(conflicts)} conflict(s), {len(errors)} error(s)).")
            for heading, items in (("Conflicts", conflicts), ("Errors", errors)):
                if items:
                    lines.extend(("", f"### {heading}", ""))
                    for item in items:
                        details = "; ".join(
                            f"{key}={item[key]}" for key in ("path", "migration", "file", "check", "name", "table", "column", "namespace", "errorCode") if key in item
                        )
                        lines.append(f"- `{_markdown_cell(item['code'])}` — {_markdown_cell(details)}: {_markdown_cell(item['message'])}")
            coverage = preflight.get("coverage", {})
            deferred = coverage.get("deferredPreconditionFolders", [])
            if deferred:
                lines.extend(("", "Deferred preconditions: " + ", ".join(f"`{_markdown_cell(item)}`" for item in deferred) + " (checked when each folder reaches its apply boundary)."))
            for review in coverage.get("manual_review_required", []):
                where = "/".join(value for value in (review.get("migration"), review.get("file")) if value)
                lines.append(f"- Manual review: `{_markdown_cell(where)}` — {_markdown_cell(review['reason'])}")
    if report["diagnostics"]:
        lines.extend(("", "## Diagnostics", "", "| Code | Path | Message |", "| --- | --- | --- |"))
        for item in report["diagnostics"]:
            lines.append("| " + " | ".join(_markdown_cell(item.get(key) or "") for key in ("code", "path", "message")) + " |")
    lines.extend(("", "## Limitations", ""))
    lines.extend(f"- {_markdown_cell(item)}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def render_migration_plan(report: Mapping, output_format: str) -> str:
    """Render a report as plain text, JSON or Markdown; JSON omits the internal exit code."""
    safe_report = _safe_report_for_render(report)
    if output_format == "json":
        payload = {key: value for key, value in safe_report.items() if key != "exitCode"}
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output_format == "text":
        return _text_report(safe_report)
    if output_format == "markdown":
        return _markdown_report(safe_report)
    raise ValueError("output format must be text, json, or markdown")


class _ReportArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("invalid migration-plan arguments")


def _error_report(*, mode: str, environment: str | None, message: str, code: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "mode": mode,
        "selection": "explicit",
        "orderStatus": "caller-supplied",
        "environment": _canonical_environment(environment),
        "target": None,
        "summary": _summary([]),
        "migrations": [],
        "preflight": None,
        "diagnostics": [_diagnostic(code, None, message)],
        "limitations": list(LIMITATIONS),
        "exitCode": 2,
    }


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    """Run the local report CLI; environment and SQLcl are touched only for valid live input."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _ReportArgumentParser(
        prog="scripts/team.sh migration-plan",
        description="Inventory migrations or run an explicitly selected read-only live preflight.",
        allow_abbrev=False,
    )
    parser.add_argument("folders", nargs="*", help="migration folders, in caller-supplied order")
    parser.add_argument("--repo-root", type=Path, default=repo_root, help=argparse.SUPPRESS)
    parser.add_argument("--schema", help="filter migration owner or assert the owner of flat folders")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--live", action="store_true", help="run the guarded read-only preflight")
    parser.add_argument("--env", help="live target environment: dev, staging, or prod")

    if "--help" in arguments or "-h" in arguments:
        print(parser.format_help(), end="")
        return 0
    selected_format = "text"
    mode = "offline"
    requested_environment = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument == "--live":
            mode = "live"
        elif argument in {"--env", "--schema", "--repo-root"}:
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                if argument == "--env":
                    requested_environment = arguments[index + 1]
                index += 2
            else:
                index += 1
            continue
        elif argument.startswith("--env="):
            requested_environment = argument.split("=", 1)[1]
        elif argument == "--format":
            if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                candidate = arguments[index + 1]
                if candidate in {"text", "json", "markdown"}:
                    selected_format = candidate
                index += 2
            else:
                index += 1
            continue
        elif argument.startswith("--format="):
            candidate = argument.split("=", 1)[1]
            if candidate in {"text", "json", "markdown"}:
                selected_format = candidate
        index += 1
    try:
        args = parser.parse_args(arguments)
    except (ValueError, argparse.ArgumentError):
        report = _error_report(
            mode=mode,
            environment=requested_environment if mode == "live" else None,
            message="Invalid migration-plan arguments; see --help.",
            code="invalid-arguments",
        )
        print(render_migration_plan(report, selected_format), end="")
        return 2

    root = Path(args.repo_root if args.repo_root is not None else Path(__file__).resolve().parents[1]).resolve()
    if args.env is not None and not args.live:
        report = build_migration_plan(root, args.folders, schema=args.schema)
        report["diagnostics"].append(_diagnostic("environment-requires-live", None, "--env requires --live."))
        report["exitCode"] = 2
    elif args.live and (args.env not in {"dev", "staging", "prod"} or not args.folders):
        offline = build_migration_plan(root, args.folders, schema=args.schema)
        report = _live_report_from_offline(offline, args.env)
        if args.env not in {"dev", "staging", "prod"}:
            report["diagnostics"].append(_diagnostic("live-environment-required", None, "Live reporting requires --env dev, staging, or prod."))
        if not args.folders:
            report["diagnostics"].append(_diagnostic("live-selection-required", None, "Live reporting requires explicitly selected migration folders."))
        report["exitCode"] = 2
    elif args.live:
        offline = build_migration_plan(root, args.folders, schema=args.schema)
        if offline["exitCode"] != 0:
            report = _live_report_from_offline(offline, args.env)
            report["diagnostics"].append(_diagnostic("live-selection-invalid", None, "Live preflight requires valid selected folders with known local evidence."))
        else:
            from .local_config import ConfigError, read_project_env

            env_path = Path(os.environ.get("PROJECT_ENV_FILE", root / ".env"))
            try:
                values = read_project_env(env_path)
            except (ConfigError, OSError, ValueError):
                report = _live_report_from_offline(offline, args.env)
                report["diagnostics"].append(
                    _diagnostic("live-configuration-unavailable", None, "Live migration configuration is unavailable or invalid; values omitted.")
                )
            else:
                report = build_live_migration_plan(root, args.folders, environment=args.env, schema=args.schema, values=values)
    else:
        report = build_migration_plan(root, args.folders, schema=args.schema)

    print(render_migration_plan(report, args.format), end="")
    return report["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
