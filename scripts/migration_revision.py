#!/usr/bin/env python3
"""Create migration follow-up folders and inspect local migration locks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from .migration_manifest import (
    FOLDER_RE,
    MigrationManifestError,
    STATUS_ENVIRONMENTS,
    _folder_parts,
    _validate_relative_folder,
    decode_json,
    load_migration,
)


DEFAULT_REASON = "follow-up revision"
RUN_MANIFEST_NAME = "run-manifest.json"
RUN_ID_RE = re.compile(r"migration-attempt-[A-Za-z0-9_-]+\Z", re.ASCII)
NOT_STARTED_CONNECTION_ERRORS = re.compile(r"(?m)^ORA-(?:01017|12154|12514|12537|12541|12545|12547|12560|12637)\b")
IDENTITY_GUARD_ERRORS = re.compile(r"(?m)^ORA-2098[0-5]\b")


class MigrationRevisionError(ValueError):
    """A local revision or attempt-state operation could not be completed safely."""


@dataclass(frozen=True)
class RevisionResult:
    folder: Path
    order_hint: str


@dataclass(frozen=True)
class LockReport:
    status: str
    states: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return {"unlocked": 0, "locked": 1, "unknown": 2}[self.status]


def not_started_reason(output: str) -> str | None:
    """Return a reason only for explicit pre-payload failures; missing output locks."""
    if not isinstance(output, str):
        return None
    lines = {line.strip() for line in output.splitlines()}
    if "MIGRATION_APPLY_COMPLETED" in lines:
        return None
    if any(line.startswith("MIGRATION_PAYLOAD_STARTED:") for line in lines):
        return None
    if (
        "MIGRATION_IDENTITY_VERIFIED" in lines
        and "MIGRATION_IDENTITY_GUARD_REFUSED" in output
        and re.search(r"(?m)^ORA-20987\b", output)
    ):
        return "identity-guard-refused"
    if "MIGRATION_IDENTITY_VERIFIED" not in lines and IDENTITY_GUARD_ERRORS.search(output):
        return "migration-identity-guard-refused"
    if NOT_STARTED_CONNECTION_ERRORS.search(output) or re.search(r"(?m)^SP2-0640\b", output):
        return "connection-failed-before-payload"
    if "unable to start sqlcl:" in output.casefold():
        return "sqlcl-start-failed"
    return None


def attempt_record_is_locked(record: dict) -> bool:
    """Only explicit untouched states with a false write bit are unlocked."""
    return not (
        record.get("writeAttempted") is False
        and record.get("state") in {"frozen", "apply-not-started"}
    )


def _safe_label(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return "".join(character if character.isprintable() else f"\\u{ord(character):04x}" for character in value)


def _family_revisions(parent: Path, family: str) -> dict[int, Path]:
    found: dict[int, Path] = {}
    try:
        children = tuple(parent.iterdir())
    except OSError as error:
        raise MigrationRevisionError(f"could not read migration family directory: {error}") from error
    for child in children:
        match = FOLDER_RE.fullmatch(child.name)
        if match is None or match.group("family") != family:
            continue
        if child.is_symlink():
            raise MigrationRevisionError(f"symbolic link in migration revision family is not allowed: {child.name}")
        _date, _family, revision = _folder_parts(child.name)
        if revision in found:
            raise MigrationRevisionError(f"duplicate migration revision r{revision:03d} in {parent}")
        found[revision] = child
    return found


def revise_folder(repo_root: Path, relative_folder: str, reason: str | None = None) -> RevisionResult:
    """Copy a migration into its next family revision without changing its source."""
    root = Path(repo_root).resolve()
    try:
        parts = _validate_relative_folder(relative_folder)
    except MigrationManifestError as error:
        raise MigrationRevisionError(str(error)) from error
    source = root.joinpath(*parts)
    _date, family, revision = _folder_parts(source.name)
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise MigrationRevisionError("symbolic links are not allowed in migration paths")
    try:
        source.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise MigrationRevisionError("migration folder is missing or outside the repository") from error

    revisions = _family_revisions(source.parent, family)
    newer = sorted(number for number in revisions if number > revision)
    if newer:
        raise MigrationRevisionError(
            f"newer revision r{newer[-1]:03d} already exists for {family}; revise the latest revision instead"
        )
    expected_revisions = set(range(1, revision + 1))
    if set(revisions) != expected_revisions:
        raise MigrationRevisionError(f"revision history for {family} is incomplete; revisions must be consecutive from r001")
    if revision >= 999:
        raise MigrationRevisionError("migration revision r999 cannot be incremented")

    migration = load_migration(root, "/".join(parts))
    family_prefix = source.name.rsplit("-r", 1)[0]
    next_name = f"{family_prefix}-r{revision + 1:03d}"
    destination = source.parent / next_name
    if destination.exists() or destination.is_symlink():
        raise MigrationRevisionError(f"next revision folder already exists: {destination.relative_to(root).as_posix()}")

    selected_reason = DEFAULT_REASON if reason is None else reason.strip()
    if not selected_reason:
        raise MigrationRevisionError("--reason must contain nonempty text")
    if any(not character.isprintable() for character in selected_reason):
        raise MigrationRevisionError("--reason must be one printable line")

    relative_destination = destination.relative_to(root).as_posix()
    header = f"Supersedes {source.name}: {selected_reason}\n".encode("utf-8")
    original_readme = source / "README.md"
    try:
        readme_bytes = original_readme.read_bytes() if original_readme.exists() else b""
        readme_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MigrationRevisionError(f"migration README.md must be readable UTF-8: {error}") from error

    try:
        with tempfile.TemporaryDirectory(prefix=".migration-revision-", dir=root) as temporary:
            staged = Path(temporary) / next_name
            staged.mkdir()
            for migration_file in migration.files:
                (staged / migration_file.name).write_bytes(migration_file.source)
            (staged / "checks.json").write_bytes(migration.checks_source)
            if original_readme.exists():
                (staged / "README.md").write_bytes(header + readme_bytes)
            else:
                (staged / "README.md").write_bytes(header)
            if destination.exists() or destination.is_symlink():
                raise MigrationRevisionError(f"next revision folder already exists: {relative_destination}")
            os.rename(staged, destination)
    except MigrationRevisionError:
        raise
    except OSError as error:
        raise MigrationRevisionError(f"could not create revision {relative_destination}: {error}") from error

    hint_paths = [revisions[number].relative_to(root).as_posix() for number in sorted(revisions)]
    hint_paths.append(relative_destination)
    order_hint = (
        "Supply any required revisions explicitly in ascending order: "
        + " -> ".join(hint_paths)
        + "; omit folders that already have verified receipts."
    )
    return RevisionResult(destination, order_hint)


def inspect_migration_lock(repo_root: Path, relative_folder: str) -> LockReport:
    """Inspect receipt and retained attempt files without connecting to SQLcl."""
    root = Path(repo_root).resolve()
    states: list[str] = []
    problems: list[str] = []
    try:
        parts = _validate_relative_folder(relative_folder)
        folder = root.joinpath(*parts)
        candidate = root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise MigrationRevisionError("symbolic links are not allowed in migration paths")
        try:
            folder.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as error:
            raise MigrationRevisionError("migration folder is missing or outside the repository") from error
        if not folder.is_dir():
            raise MigrationRevisionError("migration path is not a folder")
        _folder_parts(folder.name)
    except (MigrationManifestError, MigrationRevisionError, OSError, ValueError) as error:
        return LockReport("unknown", (f"folder-invalid:{_safe_label(str(error))}",))
    explicit_schema = parts[1] if len(parts) == 3 else None

    locked = False
    for environment in sorted(STATUS_ENVIRONMENTS):
        receipt_path = folder / f"status.{environment}.json"
        if not receipt_path.exists() and not receipt_path.is_symlink():
            continue
        if receipt_path.is_symlink():
            problems.append(f"receipt-symlink:{environment}")
            continue
        try:
            receipt = decode_json(receipt_path.read_bytes(), str(receipt_path))
        except (OSError, MigrationManifestError) as error:
            problems.append(f"receipt-unreadable:{environment}:{_safe_label(str(error))}")
            continue
        if not isinstance(receipt, dict) or receipt.get("schemaVersion") != 1 or receipt.get("state") != "verified" or receipt.get("environment") != environment:
            problems.append(f"receipt-invalid:{environment}")
            continue
        locked = True
        states.append(f"receipt:verified:{environment}")

    scratch = root / "scratch"
    if scratch.is_symlink():
        problems.append("scratch-is-a-symbolic-link")
    elif scratch.exists() and not scratch.is_dir():
        problems.append("scratch-is-not-a-directory")
    elif scratch.is_dir():
        try:
            attempt_dirs = tuple(path for path in scratch.iterdir() if path.name.startswith("migration-attempt-"))
        except OSError as error:
            attempt_dirs = ()
            problems.append(f"scratch-unreadable:{_safe_label(str(error))}")
        for attempt_dir in attempt_dirs:
            if attempt_dir.is_symlink() or not attempt_dir.is_dir() or RUN_ID_RE.fullmatch(attempt_dir.name) is None:
                problems.append(f"attempt-directory-unsafe:{_safe_label(attempt_dir.name)}")
                continue
            manifest_path = attempt_dir / RUN_MANIFEST_NAME
            if not manifest_path.exists() and not manifest_path.is_symlink():
                problems.append(f"attempt-manifest-missing:{_safe_label(attempt_dir.name)}")
                continue
            if manifest_path.is_symlink():
                problems.append(f"attempt-manifest-symlink:{_safe_label(attempt_dir.name)}")
                continue
            try:
                manifest = decode_json(manifest_path.read_bytes(), str(manifest_path))
            except (OSError, MigrationManifestError) as error:
                problems.append(f"attempt-manifest-unreadable:{_safe_label(str(error))}")
                continue
            if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("migrations"), list):
                problems.append(f"attempt-manifest-invalid:{_safe_label(attempt_dir.name)}")
                continue
            target = manifest.get("target")
            if (
                not isinstance(target, dict)
                or target.get("environment") not in STATUS_ENVIRONMENTS
                or not isinstance(target.get("current_schema"), str)
            ):
                problems.append(f"attempt-target-invalid:{_safe_label(attempt_dir.name)}")
                continue
            if explicit_schema is not None:
                if target.get("current_schema") != explicit_schema:
                    continue
            for record in manifest["migrations"]:
                if not isinstance(record, dict):
                    problems.append(f"attempt-record-invalid:{_safe_label(attempt_dir.name)}")
                    continue
                if record.get("folder") != folder.name:
                    continue
                state = _safe_label(record.get("state"))
                environment = _safe_label(target.get("environment"))
                if record.get("writeAttempted") is False and record.get("state") not in {"frozen", "apply-not-started"}:
                    problems.append(f"attempt-state-uncertain:{state}:{environment}")
                    continue
                if attempt_record_is_locked(record):
                    locked = True
                states.append(f"attempt:{state}:{environment}")

    if problems:
        states.extend(problems)
        return LockReport("locked" if locked else "unknown", tuple(states))
    if not states:
        states.append("no-attempt-or-receipt")
    return LockReport("locked" if locked else "unlocked", tuple(states))


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/team.sh revise")
    parser.add_argument("--repo-root", type=Path, default=repo_root or Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="inspect whether the folder is locked by local evidence")
    parser.add_argument("folder", nargs="?", help="repository-relative migrations/<folder> path")
    parser.add_argument("--reason", help=f"one-line reason for the README header (default: {DEFAULT_REASON})")
    arguments = parser.parse_args(argv)
    if not arguments.folder:
        parser.error("expected a migration folder")
    if arguments.check and arguments.reason is not None:
        parser.error("--check cannot be combined with --reason")
    try:
        if arguments.check:
            report = inspect_migration_lock(arguments.repo_root, arguments.folder)
            print(f"{report.status.upper()} {arguments.folder}: {'; '.join(report.states)}")
            return report.exit_code
        result = revise_folder(arguments.repo_root, arguments.folder, arguments.reason)
    except (MigrationRevisionError, MigrationManifestError, OSError, ValueError) as error:
        print(f"migration revision error: {error}", file=sys.stderr)
        return 2
    print(f"Created revision: {result.folder.relative_to(arguments.repo_root.resolve()).as_posix()}")
    print(f"Migration order hint: {result.order_hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
