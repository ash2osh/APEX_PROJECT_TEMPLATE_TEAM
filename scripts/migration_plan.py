#!/usr/bin/env python3
"""Build a local-only inventory and summary for migration folders."""

from __future__ import annotations

import html
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import migration_manifest as manifest
from .migration_revision import LockReport, MigrationRevisionError, inspect_migration_lock


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
__all__ = ["build_migration_plan", "render_migration_plan"]


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
        return _safe_text(path.relative_to(root).as_posix())
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
                    loaded[migration.folder.relative_to(root).as_posix()] = migration
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
        "Migration plan (offline)",
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
    for schema, group in _group_records(report["migrations"]):
        lines.append(f"  {schema}:")
        for record in group:
            files = len(record["files"])
            family = record["family"] or "invalid"
            revision = f"r{record['revision']:03d}" if isinstance(record["revision"], int) else "unknown revision"
            reasons = "; ".join(record["localEvidence"]["reasons"])
            lines.append(f"    {record['path']} — {family} {revision}, {files} SQL file(s), evidence {record['localEvidence']['status']} ({reasons})")
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
        f"- Mode: `{_markdown_cell(report['mode'])}`",
        f"- Selection: `{_markdown_cell(report['selection'])}`",
        f"- Order: `{_markdown_cell(report['orderStatus'])}`",
        "",
        "## Summary",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
    ]
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
    if report["diagnostics"]:
        lines.extend(("", "## Diagnostics", "", "| Code | Path | Message |", "| --- | --- | --- |"))
        for item in report["diagnostics"]:
            lines.append("| " + " | ".join(_markdown_cell(item.get(key) or "") for key in ("code", "path", "message")) + " |")
    lines.extend(("", "## Limitations", ""))
    lines.extend(f"- {_markdown_cell(item)}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def render_migration_plan(report: Mapping, output_format: str) -> str:
    """Render a report as plain text, JSON or Markdown; JSON omits the internal exit code."""
    if output_format == "json":
        payload = {key: value for key, value in report.items() if key != "exitCode"}
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output_format == "text":
        return _text_report(report)
    if output_format == "markdown":
        return _markdown_report(report)
    raise ValueError("output format must be text, json, or markdown")
