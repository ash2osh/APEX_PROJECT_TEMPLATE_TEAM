#!/usr/bin/env python3
"""Plan and archive released or superseded local migration folders."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence

from .migration_manifest import (
    SCHEMA_DIRECTORY_RE,
    MigrationManifestError,
    _migration_directories,
    decode_json,
    load_archived_index,
    load_migration,
    validate_receipt,
)
from .migration_revision import inspect_migration_lock
from .path_utils import relative_to_root


DEFAULT_DESTINATION = "migrations-archive"
DEFAULT_REASON = "released or superseded"


class MigrationArchiveError(ValueError):
    """A local migration archive operation could not be completed safely."""


@dataclass(frozen=True)
class ArchiveEntry:
    folder: Path
    relative_folder: str
    schema: str | None
    family: str
    revision: int
    payload_digest: str
    evidence: tuple[str, ...]
    destination: Path
    relative_destination: str


@dataclass(frozen=True)
class ArchiveIndex:
    path: Path
    schema: str | None
    records: tuple[dict, ...]
    original_bytes: bytes | None


@dataclass(frozen=True)
class ArchivePlan:
    repo_root: Path
    destination_root: Path
    reason: str
    allow_unverified_attempts: bool
    entries: tuple[ArchiveEntry, ...]
    indexes: tuple[ArchiveIndex, ...]
    family_state: tuple[tuple[str | None, str, tuple[tuple[int, str], ...]], ...]


def _label(value: str) -> str:
    return "".join(character if character.isprintable() else f"\\u{ord(character):04x}" for character in value)


def _schema_directories(migrations_dir: Path) -> tuple[tuple[str, Path], ...]:
    found = []
    for path in sorted(migrations_dir.iterdir(), key=lambda entry: entry.name):
        if path.is_symlink():
            raise MigrationArchiveError(f"symbolic link is not allowed in migrations/: {path.name}")
        if path.is_dir() and SCHEMA_DIRECTORY_RE.fullmatch(path.name):
            found.append((path.name, path))
    return tuple(found)


def _resolve_archive_root(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MigrationArchiveError("--destination must be a repository path outside migrations/")
    try:
        destination = Path(value)
        if not destination.is_absolute():
            destination = root / destination
        resolved = destination.resolve(strict=False)
        relative_to_root(resolved, root)
        migrations = (root / "migrations").resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise MigrationArchiveError("--destination must be inside the repository and outside migrations/") from exc
    if resolved == migrations or resolved.is_relative_to(migrations):
        raise MigrationArchiveError("destination must be outside migrations/")
    if os.path.lexists(resolved) and not resolved.is_dir():
        raise MigrationArchiveError("archive destination is not a directory")
    return resolved


def _parse_selector(value: str) -> tuple[str | None, str, bool]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MigrationArchiveError(f"invalid migration folder selector: {_label(str(value))}")
    cleaned = value.removesuffix("/")
    if cleaned.startswith("migrations/"):
        parts = cleaned.split("/")
        if len(parts) == 2:
            return None, parts[1], False
        if len(parts) == 3 and SCHEMA_DIRECTORY_RE.fullmatch(parts[1]):
            return parts[1], parts[2], True
        raise MigrationArchiveError(f"use migrations/<folder> or migrations/<SCHEMA>/<folder>: {_label(value)}")
    parts = cleaned.split("/")
    if len(parts) == 1:
        return None, parts[0], False
    if len(parts) == 2 and SCHEMA_DIRECTORY_RE.fullmatch(parts[0]):
        return parts[0], parts[1], True
    raise MigrationArchiveError(f"use migrations/<folder> or migrations/<SCHEMA>/<folder>: {_label(value)}")


def _resolve_selector(
    value: str,
    directories: Sequence[tuple[Path, str | None, str, int]],
    *,
    schema: str | None,
    allow_flat_schema: bool = False,
) -> tuple[Path, str | None, str, int, bool]:
    explicit_schema, name, schema_named = _parse_selector(value)
    if not name or name in {".", ".."}:
        raise MigrationArchiveError(f"invalid migration folder selector: {_label(value)}")
    if explicit_schema is not None and schema is not None and explicit_schema != schema:
        raise MigrationArchiveError(f"--schema {schema} does not match folder path schema {explicit_schema}")
    wanted_schema = explicit_schema if explicit_schema is not None else schema
    matches = [
        item for item in directories
        if item[0].name == name
        and (
            wanted_schema is None
            or item[1] == wanted_schema
            or (explicit_schema is None and allow_flat_schema and item[1] is None)
        )
        and (explicit_schema is None or item[1] == explicit_schema)
    ]
    if not matches:
        raise MigrationArchiveError(f"unknown migration folder: {_label(value)}")
    if len(matches) != 1:
        raise MigrationArchiveError(f"migration folder selector is ambiguous; name its schema: {_label(value)}")
    return (*matches[0], schema_named)


def _validated_reason(reason: str | None) -> str:
    selected = DEFAULT_REASON if reason is None else reason.strip()
    if not selected or any(not character.isprintable() for character in selected):
        raise MigrationArchiveError("--reason must be one nonempty printable line")
    return selected


def _folder_evidence(
    root: Path, relative_folder: str, migration, *, allow_unverified_attempts: bool = False
) -> tuple[tuple[str, ...], bool, bool]:
    evidence: list[str] = []
    verified_receipt = False
    for environment in ("dev", "staging", "prod"):
        path = migration.folder / f"status.{environment}.json"
        if not path.exists() and not path.is_symlink():
            continue
        try:
            raw = decode_json(path.read_bytes(), str(path))
            if isinstance(raw, dict) and isinstance(raw.get("target"), dict):
                validate_receipt(path, migration, raw["target"])
                evidence.append(f"{path.name} (verified)")
                verified_receipt = True
            else:
                evidence.append(f"{path.name} (unverified)")
        except (OSError, MigrationManifestError, TypeError, ValueError):
            evidence.append(f"{path.name} (unverified)")

    lock = inspect_migration_lock(root, relative_folder)
    attempt_states = tuple(
        state for state in lock.states
        if state.startswith(("attempt:", "attempt-", "scratch-"))
    )
    if lock.status == "unknown" and attempt_states and not allow_unverified_attempts:
        states = "; ".join(lock.states)
        raise MigrationArchiveError(f"attempt or receipt evidence is unknown for {migration.folder.name}: {states}")
    if attempt_states:
        evidence.extend(attempt_states)
    if not evidence:
        evidence.append("no-attempt-or-receipt")
    if attempt_states and not verified_receipt:
        if not allow_unverified_attempts:
            raise MigrationArchiveError(
                f"attempt record for {migration.folder.name} has no verified receipt for any environment; state unknown "
                "(--allow-unverified-attempts archives it as a failed or abandoned attempt)"
            )
        evidence.append("attempt-without-verified-receipt (accepted)")
    return tuple(evidence), bool(attempt_states), verified_receipt


def _validate_prefix_selection(
    selected: Sequence[tuple[Path, str | None, str, int, bool]],
    directories: Sequence[tuple[Path, str | None, str, int]],
) -> None:
    groups = {(schema, family) for _path, schema, family, _revision, _named in selected}
    for schema, family in groups:
        visible = sorted(
            revision for _path, found_schema, found_family, revision in directories
            if found_schema == schema and found_family == family
        )
        # The index prefix is already validated by manifest discovery. Keep the
        # full-family exception explicit; partial selection must remove a low prefix.
        selected_revisions = sorted(
            revision for _path, found_schema, found_family, revision, _named in selected
            if found_schema == schema and found_family == family
        )
        if selected_revisions == visible:
            continue
        if selected_revisions != visible[: len(selected_revisions)]:
            raise MigrationArchiveError(
                f"selected folders for {family} must be the contiguous low prefix of the family or the whole family"
            )


def build_archive_plan(
    repo_root: Path,
    folders: Sequence[str] = (),
    *,
    all_folders: bool = False,
    keep: Sequence[str] = (),
    schema: str | None = None,
    destination: str = DEFAULT_DESTINATION,
    reason: str | None = None,
    allow_unverified_attempts: bool = False,
) -> ArchivePlan:
    """Validate selection and return a no-write move plan."""
    root = Path(repo_root).resolve()
    migrations_dir = root / "migrations"
    if migrations_dir.is_symlink() or not migrations_dir.is_dir():
        raise MigrationArchiveError(f"migrations directory does not exist or is unsafe: {migrations_dir}")
    if schema is not None and (not isinstance(schema, str) or SCHEMA_DIRECTORY_RE.fullmatch(schema) is None):
        raise MigrationArchiveError("--schema must be an uppercase Oracle identifier")
    if all_folders and folders:
        raise MigrationArchiveError("--all does not accept positional migration folders")
    if not all_folders and not folders:
        raise MigrationArchiveError("select migration folders or use --all")
    if keep and not all_folders:
        raise MigrationArchiveError("--keep is only valid with --all")

    try:
        directories = tuple(_migration_directories(root))
    except MigrationManifestError as exc:
        raise MigrationArchiveError(str(exc)) from exc
    schema_directories = _schema_directories(migrations_dir)
    allow_flat_schema = not schema_directories or (
        len(schema_directories) == 1 and schema is not None and schema_directories[0][0] == schema
    )
    destination_root = _resolve_archive_root(root, destination)
    selected: list[tuple[Path, str | None, str, int, bool]] = []
    if all_folders:
        selected.extend(
            (path, found_schema, family, revision, False)
            for path, found_schema, family, revision in directories
            if schema is None or found_schema == schema or (allow_flat_schema and found_schema is None)
        )
        kept: set[Path] = set()
        for value in keep:
            path, _found_schema, _family, _revision, _named = _resolve_selector(
                value, directories, schema=schema, allow_flat_schema=allow_flat_schema
            )
            kept.add(path)
        selected = [item for item in selected if item[0] not in kept]
    else:
        for value in folders:
            resolved = _resolve_selector(value, directories, schema=schema, allow_flat_schema=allow_flat_schema)
            if any(existing[0] == resolved[0] for existing in selected):
                raise MigrationArchiveError(f"migration folder selected more than once: {_label(value)}")
            selected.append(resolved)
    if not selected:
        raise MigrationArchiveError("no migration folders remain selected")

    selected_schemas = {found_schema for _path, found_schema, _family, _revision, _named in selected}
    multi_schema_project = len(schema_directories) > 1
    if (
        schema is None
        and multi_schema_project
        and len(selected_schemas) > 1
        and not all(item[4] for item in selected)
    ):
        raise MigrationArchiveError("select two schemas with --schema or schema-qualified folder paths")

    _validate_prefix_selection(selected, directories)
    selected.sort(key=lambda item: ("" if item[1] is None else item[1], item[2], item[3]))
    reason_text = _validated_reason(reason)
    entries: list[ArchiveEntry] = []
    index_records: dict[tuple[str | None, Path], list[dict]] = {}
    index_originals: dict[tuple[str | None, Path], bytes | None] = {}
    family_state: dict[tuple[str | None, str], tuple[tuple[int, str], ...]] = {}

    for path, found_schema, family, revision, _named in selected:
        relative_folder = relative_to_root(path, root).as_posix()
        try:
            migration = load_migration(root, relative_folder)
        except MigrationManifestError as exc:
            raise MigrationArchiveError(str(exc)) from exc
        evidence, _has_attempt, _verified = _folder_evidence(
            root, relative_folder, migration, allow_unverified_attempts=allow_unverified_attempts
        )
        destination = destination_root / (found_schema if found_schema is not None else "") / path.name
        relative_destination = relative_to_root(destination, root).as_posix()
        if os.path.lexists(destination):
            raise MigrationArchiveError(f"archive target already exists: {relative_destination}")
        entries.append(
            ArchiveEntry(
                folder=path,
                relative_folder=relative_folder,
                schema=found_schema,
                family=family,
                revision=revision,
                payload_digest=migration.payload_digest,
                evidence=evidence,
                destination=destination,
                relative_destination=relative_destination,
            )
        )

        index_path = migrations_dir / found_schema / ".archived.json" if found_schema is not None else migrations_dir / ".archived.json"
        key = (found_schema, index_path)
        if key not in index_records:
            try:
                records = load_archived_index(index_path, found_schema)
                original = index_path.read_bytes() if index_path.exists() else None
            except (MigrationManifestError, OSError) as exc:
                raise MigrationArchiveError(f"archived index is invalid: {exc}") from exc
            index_records[key] = list(records)
            index_originals[key] = original
        family_key = (found_schema, family)
        if family_key not in family_state:
            family_state[family_key] = tuple(
                (found_revision, relative_to_root(found_path, root).as_posix())
                for found_path, found_schema2, found_family, found_revision in directories
                if found_schema2 == found_schema and found_family == family
            )

    index_plans = tuple(
        ArchiveIndex(path=path, schema=index_schema, records=tuple(records), original_bytes=index_originals[(index_schema, path)])
        for (index_schema, path), records in sorted(index_records.items(), key=lambda item: str(item[0][1]))
    )
    return ArchivePlan(
        repo_root=root,
        destination_root=destination_root,
        reason=reason_text,
        allow_unverified_attempts=allow_unverified_attempts,
        entries=tuple(entries),
        indexes=index_plans,
        family_state=tuple(
            (schema_name, family, state)
            for (schema_name, family), state in sorted(family_state.items(), key=lambda item: (item[0][0] or "", item[0][1]))
        ),
    )


def _atomic_write(path: Path, source: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _index_path_for_entry(migrations_dir: Path, entry: ArchiveEntry) -> Path:
    if entry.schema is not None:
        return migrations_dir / entry.schema / ".archived.json"
    return migrations_dir / ".archived.json"


def _index_matches_original(index: ArchiveIndex) -> bool:
    try:
        if index.original_bytes is None:
            return not os.path.lexists(index.path)
        return not index.path.is_symlink() and index.path.read_bytes() == index.original_bytes
    except OSError:
        return False


def execute_archive_plan(plan: ArchivePlan) -> None:
    """Move each selected tree and atomically update its schema index."""
    root = plan.repo_root
    migrations_dir = root / "migrations"
    try:
        current_directories = tuple(_migration_directories(root))
    except MigrationManifestError as exc:
        raise MigrationArchiveError(f"archive plan is stale: {exc}") from exc
    for schema, family, expected_state in plan.family_state:
        current_state = tuple(
            (revision, relative_to_root(path, root).as_posix())
            for path, found_schema, found_family, revision in current_directories
            if found_schema == schema and found_family == family
        )
        if current_state != expected_state:
            raise MigrationArchiveError(f"archive plan is stale; visible revisions changed for {family}")
    for index in plan.indexes:
        current = index.path.read_bytes() if index.path.exists() else None
        if current != index.original_bytes:
            raise MigrationArchiveError(f"archive plan is stale; archived index changed: {index.path.name}")
    for entry in plan.entries:
        if entry.folder.is_symlink() or not entry.folder.is_dir():
            raise MigrationArchiveError(f"archive plan is stale; migration folder changed: {entry.relative_folder}")
        if os.path.lexists(entry.destination):
            raise MigrationArchiveError(f"archive target already exists: {entry.relative_destination}")
        try:
            migration = load_migration(root, entry.relative_folder)
        except MigrationManifestError as exc:
            raise MigrationArchiveError(f"archive plan is stale; {exc}") from exc
        if migration.payload_digest != entry.payload_digest:
            raise MigrationArchiveError(f"archive plan is stale; migration payload changed: {entry.relative_folder}")
        _folder_evidence(root, entry.relative_folder, migration, allow_unverified_attempts=plan.allow_unverified_attempts)

    archived_at = datetime.now(timezone.utc).date().isoformat()
    records_by_path: dict[Path, list[dict]] = {index.path: list(index.records) for index in plan.indexes}
    entries_by_index: dict[Path, list[ArchiveEntry]] = {index.path: [] for index in plan.indexes}
    for entry in plan.entries:
        index_path = _index_path_for_entry(migrations_dir, entry)
        entries_by_index[index_path].append(entry)
        records_by_path[index_path].append(
            {
                "folder": entry.folder.name,
                "family": entry.family,
                "revision": entry.revision,
                "payloadDigest": entry.payload_digest,
                "archivedAt": archived_at,
                "reason": plan.reason,
                "destination": entry.relative_destination,
            }
        )
    archive_bytes_by_path = {
        index.path: (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "archived": sorted(
                        records_by_path[index.path],
                        key=lambda record: (record["family"], record["revision"]),
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        for index in plan.indexes
    }

    created_directories: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    try:
        if not plan.destination_root.exists():
            plan.destination_root.mkdir(parents=True)
            created_directories.append(plan.destination_root)
        for entry in plan.entries:
            parent = entry.destination.parent
            missing: list[Path] = []
            cursor = parent
            while not cursor.exists():
                missing.append(cursor)
                cursor = cursor.parent
            parent.mkdir(parents=True, exist_ok=True)
            created_directories.extend(reversed(missing))
            os.replace(entry.folder, entry.destination)
            moved.append((entry.folder, entry.destination))

        for index in plan.indexes:
            _atomic_write(index.path, archive_bytes_by_path[index.path])
    except (OSError, MigrationManifestError, MigrationArchiveError) as exc:
        rollback_errors = []
        indexes_not_restored: set[Path] = set()
        retained_index_reasons: dict[Path, str] = {}
        entries_by_folder = {entry.folder: entry for entry in plan.entries}
        for index in reversed(plan.indexes):
            if _index_matches_original(index):
                continue
            try:
                planned_index_is_current = (
                    not index.path.is_symlink()
                    and index.path.read_bytes() == archive_bytes_by_path[index.path]
                )
            except OSError:
                planned_index_is_current = False
            if not planned_index_is_current:
                indexes_not_restored.add(index.path)
                retained_index_reasons[index.path] = (
                    "index contents do not match the original or planned archive index"
                )
                continue
            try:
                if index.original_bytes is None:
                    index.path.unlink(missing_ok=True)
                else:
                    _atomic_write(index.path, index.original_bytes)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
            if not _index_matches_original(index):
                indexes_not_restored.add(index.path)
                retained_index_reasons[index.path] = "the original index could not be restored"
        folder_rollback_failures: set[Path] = set()
        for source, destination in reversed(moved):
            index_path = _index_path_for_entry(migrations_dir, entries_by_folder[source])
            if index_path in indexes_not_restored:
                continue
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
                folder_rollback_failures.add(index_path)
        for index_path in sorted(folder_rollback_failures, key=str):
            recovery_failed = False
            for entry in entries_by_index[index_path]:
                source_exists = os.path.lexists(entry.folder)
                destination_exists = os.path.lexists(entry.destination)
                if source_exists and destination_exists:
                    rollback_errors.append(
                        f"both source and archive folders exist for {entry.relative_folder}"
                    )
                    recovery_failed = True
                    continue
                if not destination_exists:
                    if not source_exists:
                        rollback_errors.append(
                            f"both source and archive folders are missing for {entry.relative_folder}"
                        )
                        recovery_failed = True
                        continue
                    try:
                        entry.destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(entry.folder, entry.destination)
                    except OSError as recovery_error:
                        rollback_errors.append(str(recovery_error))
                        recovery_failed = True
            if not recovery_failed:
                try:
                    _atomic_write(index_path, archive_bytes_by_path[index_path])
                except OSError as recovery_error:
                    rollback_errors.append(str(recovery_error))
                    recovery_failed = True
            indexes_not_restored.add(index_path)
            retained_index_reasons[index_path] = (
                "folder rollback failed; matching archived folders and index were retained"
                if not recovery_failed
                else "folder rollback recovery failed; inspect the folders and index"
            )
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        retained = sorted(
            (path.relative_to(root).as_posix(), retained_index_reasons[path])
            for path in indexes_not_restored
        )
        details = []
        if rollback_errors:
            details.append(f"rollback errors: {'; '.join(rollback_errors)}")
        if retained:
            details.append("archived folders retained for recovery: " + ", ".join(
                f"{path} ({reason})" for path, reason in retained
            ))
        if details:
            detail = "; " + "; ".join(details)
            message = f"archive move failed; rollback is incomplete: {exc}{detail}"
        else:
            message = f"archive move failed and was rolled back: {exc}"
        raise MigrationArchiveError(message) from exc


def archive_migrations(
    repo_root: Path,
    folders: Sequence[str] = (),
    *,
    all_folders: bool = False,
    keep: Sequence[str] = (),
    schema: str | None = None,
    destination: str = DEFAULT_DESTINATION,
    reason: str | None = None,
    allow_unverified_attempts: bool = False,
    yes: bool = False,
) -> ArchivePlan:
    """Build a plan and apply it only when ``yes`` is true."""
    plan = build_archive_plan(
        repo_root,
        folders,
        all_folders=all_folders,
        keep=keep,
        schema=schema,
        destination=destination,
        reason=reason,
        allow_unverified_attempts=allow_unverified_attempts,
    )
    if yes:
        execute_archive_plan(plan)
    return plan


def _print_plan(plan: ArchivePlan, *, dry_run: bool) -> None:
    if dry_run:
        print("Archive plan (dry run; pass --yes to move):")
    else:
        print("Archive plan:")
    for entry in plan.entries:
        print(
            f"  {entry.relative_folder}: {entry.family} r{entry.revision:03d}; "
            f"evidence: {', '.join(entry.evidence)}; destination: {entry.relative_destination}"
        )


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/team.sh archive-migrations")
    parser.add_argument("folders", nargs="*", help="migration folder names or repository-relative paths")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="select every visible folder in the chosen schema scope")
    parser.add_argument("--keep", action="append", default=[], help="keep a folder when using --all; may be repeated")
    parser.add_argument("--schema", help="scope selection to one schema folder")
    parser.add_argument("--destination", default=DEFAULT_DESTINATION, help=f"archive directory (default: {DEFAULT_DESTINATION}/)")
    parser.add_argument("--reason", help=f"one-line archive reason (default: {DEFAULT_REASON})")
    parser.add_argument("--allow-unverified-attempts", action="store_true", help="also archive folders with an attempt record but no verified receipt (failed or abandoned attempts)")
    parser.add_argument("--yes", action="store_true", help="move folders and update the archived index")
    parser.add_argument("--repo-root", type=Path, default=repo_root or Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        plan = archive_migrations(
            arguments.repo_root,
            arguments.folders,
            all_folders=arguments.all,
            keep=arguments.keep,
            schema=arguments.schema,
            destination=arguments.destination,
            reason=arguments.reason,
            allow_unverified_attempts=arguments.allow_unverified_attempts,
            yes=arguments.yes,
        )
    except (MigrationArchiveError, MigrationManifestError, OSError) as exc:
        print(f"migration archive error: {exc}", file=sys.stderr)
        return 2
    _print_plan(plan, dry_run=not arguments.yes)
    if arguments.yes:
        for entry in plan.entries:
            print(f"Archived: {entry.relative_destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
