#!/usr/bin/env python3
"""Detect duplicate Oracle object declarations across developer migrations."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


IDENTIFIER = r'(?:"(?:""|[^"])+"|[A-Za-z][A-Za-z0-9_$#]*)'
QUALIFIED_NAME = rf"{IDENTIFIER}(?:\s*\.\s*{IDENTIFIER})?"
IDENTIFIER_RE = re.compile(IDENTIFIER)
TABLE_RE = re.compile(
    rf"\bCREATE\s+(?:(?:GLOBAL|PRIVATE)\s+TEMPORARY\s+)?TABLE\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{QUALIFIED_NAME})",
    re.IGNORECASE,
)
VIEW_RE = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?VIEW\s+(?P<name>{QUALIFIED_NAME})",
    re.IGNORECASE,
)
SEQUENCE_RE = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?SEQUENCE\s+(?P<name>{QUALIFIED_NAME})",
    re.IGNORECASE,
)
ALTER_TABLE_RE = re.compile(
    rf"\bALTER\s+TABLE\s+(?P<table>{QUALIFIED_NAME})\s+ADD\b",
    re.IGNORECASE,
)
NON_COLUMN_ADD = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}


@dataclass(frozen=True)
class Declaration:
    kind: str
    name: str
    developer: str
    path: str
    line: int
    table_name: str | None = None
    column_name: str | None = None

    @property
    def key(self) -> tuple[str, ...]:
        if self.kind == "COLUMN":
            return (self.kind, self.table_name or "", self.column_name or "")
        return (self.kind, self.name)


def _mask_comments_and_literals(source: str) -> str:
    """Blank comments and string contents while preserving offsets and quoted IDs."""
    chars = list(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if chars[position] not in "\r\n":
                chars[position] = " "

    index = 0
    while index < len(source):
        if source.startswith("--", index):
            end = index + 2
            while end < len(source) and source[end] not in "\r\n":
                end += 1
            blank(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            blank(index, end)
            index = end
            continue
        if source[index] == "'":
            end = index + 1
            while end < len(source):
                if source[end] == "'":
                    if end + 1 < len(source) and source[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            blank(index, end)
            index = end
            continue
        if source[index] == '"':
            index += 1
            while index < len(source):
                if source[index] == '"':
                    if index + 1 < len(source) and source[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        index += 1
    return "".join(chars)


def _canonical_identifier(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1].replace('""', '"')
    return token.upper()


def _canonical_object(reference: str) -> str:
    parts = IDENTIFIER_RE.findall(reference)
    if not parts:
        return reference.strip().upper()
    # Treat an unqualified name and the same schema-qualified name as a
    # possible collision; migration profiles do not define one default schema.
    return _canonical_identifier(parts[-1])


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _split_parenthesized_columns(masked: str, opening: int) -> list[tuple[str, int]]:
    depth = 0
    segment_start = opening + 1
    columns: list[tuple[str, int]] = []
    for index in range(opening, len(masked)):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                columns.append((masked[segment_start:index], segment_start))
                return columns
        elif char == "," and depth == 1:
            columns.append((masked[segment_start:index], segment_start))
            segment_start = index + 1
    return []


def parse_declarations(source: str, developer: str, relative_path: str) -> list[Declaration]:
    masked = _mask_comments_and_literals(source)
    declarations: list[Declaration] = []

    for kind, pattern in (
        ("TABLE", TABLE_RE),
        ("VIEW", VIEW_RE),
        ("SEQUENCE", SEQUENCE_RE),
    ):
        for match in pattern.finditer(masked):
            name = _canonical_object(match.group("name"))
            declarations.append(
                Declaration(
                    kind=kind,
                    name=name,
                    developer=developer,
                    path=relative_path,
                    line=_line_number(source, match.start()),
                )
            )

    for match in ALTER_TABLE_RE.finditer(masked):
        table_name = _canonical_object(match.group("table"))
        rest = match.end()
        while rest < len(masked) and masked[rest].isspace():
            rest += 1
        if masked[rest : rest + 6].upper() == "COLUMN" and (
            rest + 6 == len(masked) or not (masked[rest + 6].isalnum() or masked[rest + 6] == "_")
        ):
            rest += 6
            while rest < len(masked) and masked[rest].isspace():
                rest += 1

        if rest < len(masked) and masked[rest] == "(":
            column_segments = _split_parenthesized_columns(masked, rest)
        else:
            column_segments = [(masked[rest:], rest)]

        for segment, offset in column_segments:
            identifier_match = IDENTIFIER_RE.match(segment.lstrip())
            if identifier_match is None:
                continue
            token = identifier_match.group(0)
            column_name = _canonical_identifier(token)
            if column_name in NON_COLUMN_ADD:
                continue
            leading = len(segment) - len(segment.lstrip())
            declarations.append(
                Declaration(
                    kind="COLUMN",
                    name=f"{table_name}.{column_name}",
                    developer=developer,
                    path=relative_path,
                    line=_line_number(source, offset + leading),
                    table_name=table_name,
                    column_name=column_name,
                )
            )

    return declarations


def scan_migrations(migrations_dir: Path) -> tuple[list[Declaration], int]:
    declarations: list[Declaration] = []
    files = sorted(migrations_dir.glob("*/*.sql"))
    for path in files:
        developer = path.parent.name
        relative_path = path.relative_to(migrations_dir).as_posix()
        source = path.read_text(encoding="utf-8")
        declarations.extend(parse_declarations(source, developer, relative_path))
    return declarations, len(files)


def find_conflicts(declarations: list[Declaration]) -> list[list[Declaration]]:
    groups: dict[tuple[str, ...], list[Declaration]] = defaultdict(list)
    for declaration in declarations:
        groups[declaration.key].append(declaration)
    conflicts = [
        entries
        for entries in groups.values()
        if len({entry.developer.casefold() for entry in entries}) > 1
    ]
    return sorted(conflicts, key=lambda entries: entries[0].key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "migrations",
        help="migration root (defaults to this repository's migrations/)",
    )
    args = parser.parse_args(argv)
    migrations_dir = args.migrations_dir.resolve()
    if not migrations_dir.is_dir():
        print(f"conflict check error: migrations directory does not exist: {migrations_dir}", file=sys.stderr)
        return 2

    try:
        declarations, file_count = scan_migrations(migrations_dir)
    except (OSError, UnicodeError) as exc:
        print(f"conflict check error: could not read migrations: {exc}", file=sys.stderr)
        return 2

    conflicts = find_conflicts(declarations)
    if conflicts:
        print("Cross-developer migration conflicts found:")
        for entries in conflicts:
            declaration = entries[0]
            if declaration.kind == "COLUMN":
                label = f"COLUMN {declaration.table_name}.{declaration.column_name} (ADD)"
            else:
                label = f"{declaration.kind} {declaration.name}"
            print(f"  {label}:")
            for entry in sorted(entries, key=lambda item: (item.developer.casefold(), item.path, item.line)):
                print(f"    {entry.developer}/{Path(entry.path).name}:{entry.line} ({entry.path})")
        return 1

    print(
        f"No cross-developer conflicts found across {file_count} migration file(s) "
        f"and {len(declarations)} declaration(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
