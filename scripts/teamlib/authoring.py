"""Offline migration scaffolding and dependency editing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Sequence

from .migration_bundle import BundleError, bundle_checksum, load_bundles


@dataclass(frozen=True)
class AuthoringResult:
    migration_id: str
    sql_path: Path
    verify_path: Path


_PART_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}$")


def _validate_part(name: str, field: str) -> None:
    if not isinstance(name, str) or not _PART_RE.fullmatch(name):
        raise BundleError(f"{field} must use lowercase letters, digits and hyphens")


def new_migration(
    directory: str | Path,
    author: str,
    slug: str,
    target: str,
    *,
    timestamp: str | None = None,
) -> AuthoringResult:
    _validate_part(author, "author")
    _validate_part(slug, "slug")
    if target not in {"tables", "code"}:
        raise BundleError("migration target must be tables or code")
    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    if not _STAMP_RE.fullmatch(stamp):
        raise BundleError("timestamp must be UTC YYYYMMDDTHHMMSS")
    try:
        datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise BundleError("timestamp is not a valid UTC calendar value") from exc
    migration_id = f"{stamp}__{author}__{slug}"
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    sql_path = root / f"{migration_id}.sql"
    verify_path = root / f"{migration_id}.verify.sql"
    if sql_path.exists() or verify_path.exists():
        raise BundleError(f"migration already exists: {migration_id}")
    sql_path.write_text(
        f"-- migration-version: 1\n-- target: {target}\n-- destructive: false\n\n",
        encoding="utf-8",
        newline="\n",
    )
    verify_path.write_bytes(b"")
    return AuthoringResult(migration_id, sql_path, verify_path)


def add_dependency(directory: str | Path, migration_id: str, dependency_id: str) -> AuthoringResult:
    bundles = load_bundles(directory)
    if migration_id not in bundles:
        raise BundleError(f"migration not found: {migration_id}")
    if dependency_id not in bundles:
        raise BundleError(f"dependency migration not found: {dependency_id}")
    if migration_id == dependency_id:
        raise BundleError("a migration cannot depend on itself")
    migration = bundles[migration_id]
    checksum = bundles[dependency_id].checksum
    if migration.sql_path is None or migration.verify_path is None:
        raise BundleError("migration paths are unavailable")
    text = migration.sql_bytes.decode("utf-8")
    lines = text.splitlines(keepends=True)
    directive = f"-- depends-on: {dependency_id} sha256:{checksum}\n"
    dependency_line = re.compile(rf"^\s*--\s*depends-on:\s*{re.escape(dependency_id)}\s+sha256:[0-9a-f]{{64}}\s*$")
    replaced = False
    for index, line in enumerate(lines):
        if dependency_line.fullmatch(line.rstrip("\r\n")):
            ending = "\n" if line.endswith("\n") or not line else ""
            lines[index] = directive if ending else directive.rstrip("\n")
            replaced = True
            break
    if not replaced:
        insertion = 0
        while insertion < len(lines) and lines[insertion].lstrip().startswith("--"):
            insertion += 1
        lines.insert(insertion, directive)
    migration.sql_path.write_text("".join(lines), encoding="utf-8", newline="")
    return AuthoringResult(migration_id, migration.sql_path, migration.verify_path)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="migration-authoring")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new-migration")
    new.add_argument("--directory", default="migrations")
    new.add_argument("--author", required=True)
    new.add_argument("--slug", required=True)
    new.add_argument("--target", choices=("tables", "code"), required=True)
    dep = sub.add_parser("add-dependency")
    dep.add_argument("migration_id")
    dep.add_argument("--on", dest="dependency_id", required=True)
    dep.add_argument("--directory", default="migrations")
    args = parser.parse_args(list(argv or []))
    if args.command == "new-migration":
        result = new_migration(args.directory, args.author, args.slug, args.target)
    else:
        result = add_dependency(args.directory, args.migration_id, args.dependency_id)
    print(result.migration_id)
    return 0

