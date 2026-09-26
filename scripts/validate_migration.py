#!/usr/bin/env python3
"""Reject SQLcl client commands in migrations before SQLcl opens a session."""

from __future__ import annotations

import re
import sys
from pathlib import Path


SQL_STARTERS = {
    "ALTER",
    "ANALYZE",
    "AUDIT",
    "CALL",
    "COMMENT",
    "CREATE",
    "DELETE",
    "DROP",
    "FLASHBACK",
    "GRANT",
    "INSERT",
    "LOCK",
    "MERGE",
    "NOAUDIT",
    "PURGE",
    "RENAME",
    "REVOKE",
    "SELECT",
    "TRUNCATE",
    "UPDATE",
    "WITH",
}


def mask_comments_and_literals(source: str) -> str:
    """Blank comments and quoted values while preserving offsets and newlines."""
    characters = list(source)
    index = 0
    length = len(source)

    def blank(start: int, end: int) -> None:
        for offset in range(start, end):
            if characters[offset] not in "\r\n":
                characters[offset] = " "

    while index < length:
        if source.startswith("--", index):
            end = source.find("\n", index)
            if end < 0:
                end = length
            blank(index, end)
            index = end
            continue

        if source.startswith("/*", index):
            start = index
            comment_end = source.find("*/", index + 2)
            if comment_end < 0:
                raise ValueError(f"unterminated block comment at line {source.count(chr(10), 0, start) + 1}")
            index = comment_end + 2
            blank(start, index)
            continue

        java_header = re.match(
            r"CREATE(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
            r"(?:OR(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
            r"REPLACE(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+)?"
            r"(?:AND(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
            r"(?:RESOLVE|COMPILE)(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+)?"
            r"(?:NOFORCE(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+)?"
            r"JAVA(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
            r"(?:IF(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
            r"NOT(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
            r"EXISTS(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+)?"
            r"(?:SOURCE|CLASS|RESOURCE)\b",
            source[index:],
            flags=re.IGNORECASE | re.ASCII | re.DOTALL,
        )
        if java_header is not None:
            payload_start = index + java_header.end()
            for comment in re.finditer(r"/\*.*?\*/|--[^\r\n]*", source[index:payload_start], flags=re.DOTALL):
                blank(index + comment.start(), index + comment.end())
            slash = re.search(
                r"(?m)^[ \t]*/[ \t]*(?:--[^\r\n]*)?(?:\r?\n|$)",
                source[payload_start:],
            )
            if slash is not None:
                payload_end = payload_start + slash.start()
                java_payload = mask_java_quoted_values(source[payload_start:payload_end])
                buffer_terminator = re.search(r"(?m)^[ \t]*;[ \t]*(?:\r?\n|$)", java_payload)
                if buffer_terminator is not None:
                    terminator_line = source.count("\n", 0, payload_start + buffer_terminator.start()) + 1
                    raise ValueError(
                        f"SQL-only migration rejects SQLcl buffer terminator in Java source at line {terminator_line}"
                    )
                blank(payload_start, payload_end)
                index = payload_end
                continue

        if source[index] in "qQ" and index + 1 < length and source[index + 1] == "'":
            start = index
            if index + 2 >= length:
                raise ValueError(f"unterminated Oracle quoted literal at line {source.count(chr(10), 0, start) + 1}")
            opening = source[index + 2]
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            terminator = closing + "'"
            end = source.find(terminator, index + 3)
            if end < 0:
                raise ValueError(f"unterminated Oracle quoted literal at line {source.count(chr(10), 0, start) + 1}")
            index = end + len(terminator)
            blank(start, index)
            continue

        if source[index] in "'\"":
            start = index
            quote = source[index]
            index += 1
            while index < length:
                if source[index] == quote:
                    if index + 1 < length and source[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError(f"unterminated quoted value at line {source.count(chr(10), 0, start) + 1}")
            blank(start, index)
            continue

        index += 1
    return "".join(characters)


def mask_java_quoted_values(source: str) -> str:
    """Blank Java strings and text blocks, preserving comments and line layout."""
    characters = list(source)
    index = 0
    length = len(source)

    def blank(start: int, end: int) -> None:
        for offset in range(start, end):
            if characters[offset] not in "\r\n":
                characters[offset] = " "

    while index < length:
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = length if end < 0 else end
            continue

        if source.startswith("/*", index):
            comment_end = source.find("*/", index + 2)
            if comment_end < 0:
                line = source.count("\n", 0, index) + 1
                raise ValueError(f"unterminated Java block comment at line {line}")
            index = comment_end + 2
            continue

        if source.startswith('"""', index):
            start = index
            index += 3
            while index < length:
                if source[index] == "\\":
                    index += 2
                elif source.startswith('"""', index):
                    index += 3
                    break
                else:
                    index += 1
            else:
                line = source.count("\n", 0, start) + 1
                raise ValueError(f"unterminated Java text block at line {line}")
            blank(start, index)
            continue

        if source[index] in "'\"":
            start = index
            quote = source[index]
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                elif source[index] == quote:
                    index += 1
                    break
                elif source[index] in "\r\n":
                    line = source.count("\n", 0, start) + 1
                    raise ValueError(f"unterminated Java quoted value at line {line}")
                else:
                    index += 1
            else:
                line = source.count("\n", 0, start) + 1
                raise ValueError(f"unterminated Java quoted value at line {line}")
            blank(start, index)
            continue

        index += 1
    return "".join(characters)


def validate_sql_only(source: str) -> None:
    """Allow terminated SQL statements and PL/SQL blocks, but no SQLcl commands."""
    masked = mask_comments_and_literals(source)
    index = 0
    while index < len(masked):
        while index < len(masked) and masked[index].isspace():
            index += 1
        if index >= len(masked):
            break

        statement_start = index
        line_number = masked.count("\n", 0, statement_start) + 1
        head = masked[index:]
        first_match = re.match(r"([A-Za-z][A-Za-z0-9_$#]*)", head, flags=re.ASCII)
        if first_match is None:
            raise ValueError(
                f"SQL-only migration rejects a client command or non-SQL token at line {line_number}"
            )
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_$#]*", head[:240], flags=re.ASCII)
        first = first_match.group(1).upper()

        is_plsql_block = first in {"BEGIN", "DECLARE"}
        if first == "CREATE":
            create_head = " ".join(token.upper() for token in tokens[:8])
            is_plsql_block = bool(
                re.match(
                    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:AND\s+(?:RESOLVE|COMPILE)\s+)?(?:NOFORCE\s+)?"
                    r"(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?(?:FORCE\s+)?"
                    r"(?:PACKAGE(?:\s+BODY)?|TYPE(?:\s+BODY)?|LIBRARY|JAVA|MLE\s+MODULE|PROCEDURE|FUNCTION|TRIGGER)\b",
                    create_head,
                )
            )

        if is_plsql_block:
            slash = re.search(r"(?m)^[ \t]*/[ \t]*(?:\r?\n|$)", masked[index:])
            if slash is None:
                raise ValueError(f"SQL-only migration PL/SQL block must end with a standalone slash at line {line_number}")
            block = masked[index:index + slash.start()]
            buffer_terminator = re.search(r"(?m)^[ \t]*\.[ \t]*(?:\r?\n|$)", block)
            if buffer_terminator is not None:
                terminator_line = line_number + block.count("\n", 0, buffer_terminator.start())
                raise ValueError(
                    f"SQL-only migration rejects SQLcl buffer terminator in PL/SQL block at line {terminator_line}"
                )
            index += slash.end()
            continue

        statement_end = masked.find(";", index)
        if statement_end < 0:
            raise ValueError(f"SQL-only migration statement must end with a semicolon at line {line_number}")
        words = [word.upper() for word in re.findall(r"[A-Za-z][A-Za-z0-9_$#]*", masked[index:statement_end])]
        if not words:
            raise ValueError(f"SQL-only migration has an empty statement at line {line_number}")
        first = words[0]
        allowed = first in SQL_STARTERS
        if first == "SET":
            allowed = len(words) > 1 and words[1] in {"CONSTRAINTS", "TRANSACTION"}
        if not allowed:
            raise ValueError(
                f"SQL-only migration rejects SQLcl/client command or unsupported statement "
                f"'{first}' at line {line_number}"
            )
        index = statement_end + 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: validate_migration.py <migration.sql>", file=sys.stderr)
        return 2
    path = Path(arguments[0])
    try:
        validate_sql_only(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"migration validation error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
