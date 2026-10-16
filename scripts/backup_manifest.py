#!/usr/bin/env python3
"""Strict validation of SQLcl database backup manifests and staged mirrors."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

SCOPE_TYPES: dict[str, tuple[str, ...]] = {
    "tables": ("TABLE",),
    "code": ("FUNCTION", "PACKAGE", "PACKAGE BODY", "PROCEDURE", "SYNONYM", "TRIGGER", "VIEW"),
}

SCOPE_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "tables": ("tables",),
    "code": ("views", "packages", "procedures", "functions", "triggers", "synonyms"),
}

MAX_COUNT = 9223372036854775807  # 2^63 - 1
ASCII_WHITESPACE = " \t\r\n\v\f"


class BackupManifestError(ValueError):
    """The backup manifest is malformed or its file counts do not match staged files."""


def parse_manifest(source: str, scope: str) -> dict[str, int]:
    if scope not in SCOPE_TYPES:
        raise BackupManifestError(f"unsupported backup scope: {scope}")
    if source.startswith("\ufeff"):
        raise BackupManifestError("database backup manifest must be UTF-8 without a byte-order mark")

    required_types = set(SCOPE_TYPES[scope])
    counts: dict[str, int] = {}
    total = 0

    # Only LF/CRLF end records. splitlines() also splits vertical tabs, form
    # feeds and Unicode separators, changing the promised field grammar.
    lines = source.split("\n")
    non_empty_count = 0

    for line_num, line in enumerate(lines, start=1):
        stripped = line.strip(ASCII_WHITESPACE)
        if not stripped:
            continue
        non_empty_count += 1

        if line.count("=") != 1:
            raise BackupManifestError(
                f"database backup manifest line {line_num} is malformed: expected exactly one '=' separator"
            )

        left, right = line.split("=")
        obj_type = left.strip(ASCII_WHITESPACE)
        count_str = right.strip(ASCII_WHITESPACE)

        if obj_type not in required_types:
            if any(obj_type in types for s, types in SCOPE_TYPES.items() if s != scope):
                raise BackupManifestError(
                    f"database backup manifest contains cross-scope type '{obj_type}' for {scope}"
                )
            raise BackupManifestError(
                f"database backup manifest contains unknown object type '{obj_type}' for {scope}"
            )
        if obj_type in counts:
            raise BackupManifestError(
                f"database backup manifest contains duplicate row for '{obj_type}'"
            )

        if not count_str or not count_str.isascii() or not count_str.isdigit():
            raise BackupManifestError(
                f"database backup manifest row for '{obj_type}' has invalid count: {count_str!r}"
            )

        try:
            val = int(count_str, 10)
        except ValueError as error:
            raise BackupManifestError(
                f"database backup manifest row for '{obj_type}' count is not a valid integer: {count_str!r}"
            ) from error

        if val < 0 or val > MAX_COUNT:
            raise BackupManifestError(
                f"database backup manifest row for '{obj_type}' count {val} overflows 64-bit integer range"
            )

        total += val
        if total > MAX_COUNT:
            raise BackupManifestError(
                f"database backup manifest total count {total} overflows 64-bit integer range"
            )

        counts[obj_type] = val

    if non_empty_count == 0:
        raise BackupManifestError("database backup manifest has no readable object counts; the mirror was not replaced")

    missing = [t for t in SCOPE_TYPES[scope] if t not in counts]
    if missing:
        missing_type = missing[0]
        raise BackupManifestError(
            f"database backup manifest is missing the {missing_type} row; the mirror was not replaced"
        )

    return counts


def verify_scope(manifest: Path, mirror: Path, scope: str, schema: str = "") -> None:
    schema_label = f" for {schema}" if schema else ""
    try:
        raw = manifest.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise BackupManifestError(
                f"database backup manifest{schema_label} ({scope}) must be UTF-8 without a byte-order mark"
            )
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BackupManifestError(
            f"database backup manifest{schema_label} ({scope}) is not strict UTF-8: {error}"
        ) from error
    except OSError as error:
        raise BackupManifestError(
            f"cannot read database backup manifest{schema_label} ({scope}): {error}"
        ) from error

    try:
        counts = parse_manifest(text, scope)
    except BackupManifestError as error:
        msg = str(error)
        if schema and "database backup manifest" in msg and f"for {schema}" not in msg:
            msg = msg.replace("database backup manifest", f"database backup manifest for {schema} ({scope})", 1)
        raise BackupManifestError(msg) from error

    expected = sum(counts.values())

    actual = 0
    if mirror.is_dir():
        for scope_dir in SCOPE_DIRECTORIES.get(scope, ()):
            dir_path = mirror / scope_dir
            if dir_path.is_dir():
                actual += sum(1 for p in dir_path.iterdir() if p.is_file() and p.name.endswith(".sql"))

    if expected != actual:
        prefix = f"database backup is incomplete for {schema} ({scope})" if schema else f"database backup is incomplete ({scope})"
        raise BackupManifestError(
            f"{prefix}: manifest expects {expected} object file(s) but {actual} were written; the mirror was not replaced"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a backup manifest and its staged mirror.")
    parser.add_argument("--scope", choices=["tables", "code"], required=True, help="Backup scope (tables or code)")
    parser.add_argument("--manifest", type=Path, required=True, help="Path to manifest file")
    parser.add_argument("--mirror", type=Path, required=True, help="Path to staged mirror directory")
    parser.add_argument("--schema", default="", help="Optional schema name for diagnostics")
    args = parser.parse_args(argv)

    try:
        verify_scope(args.manifest, args.mirror, args.scope, schema=args.schema)
        return 0
    except BackupManifestError as error:
        print(f"{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
