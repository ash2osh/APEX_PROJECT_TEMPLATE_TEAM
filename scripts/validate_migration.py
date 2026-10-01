#!/usr/bin/env python3
"""Reject SQLcl client commands in migrations before SQLcl opens a session."""

from __future__ import annotations

import re
import sys
from bisect import bisect_left
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


# SQLcl ends a SQL statement or PL/SQL block at a line holding only '/' or '.'
# and splices the file named by a line starting with '@' into the statement.
# Neither is SQL, so the validator, which splits statements at ';', would see
# one statement while SQLcl runs the following lines as client commands.
LONE_TERMINATOR = re.compile(r"(?m)^[ \t]*([/.])[ \t\r]*$")
AT_LINE = re.compile(r"(?m)^[ \t]*@")


# CREATE [OR REPLACE] [AND RESOLVE|COMPILE] [NOFORCE] JAVA [IF NOT EXISTS]
# SOURCE|CLASS|RESOURCE, with comments allowed between the words.
_GAP = r"(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
JAVA_HEADER = re.compile(
    rf"CREATE{_GAP}"
    rf"(?:OR{_GAP}REPLACE{_GAP})?"
    rf"(?:AND{_GAP}(?:RESOLVE|COMPILE){_GAP})?"
    rf"(?:NOFORCE{_GAP})?"
    rf"JAVA{_GAP}"
    rf"(?:IF{_GAP}NOT{_GAP}EXISTS{_GAP})?"
    r"(?:SOURCE|CLASS|RESOURCE)\b",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)
FIRST_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*", re.ASCII)
STANDALONE_SLASH = re.compile(r"(?m)^[ \t]*/[ \t]*(?:\r?\n|$)")


def line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def reject_sqlcl_line_hazards(masked: str, start: int, end: int, *, plsql: bool) -> None:
    """Refuse lines inside one statement that SQLcl does not read as part of it."""
    for terminator in LONE_TERMINATOR.finditer(masked, start, end):
        line = line_at(masked, terminator.start())
        if plsql and terminator.group(1) == ".":
            raise ValueError(f"SQL-only migration rejects SQLcl buffer terminator in PL/SQL block at line {line}")
        raise ValueError(
            f"SQL-only migration rejects a line holding only '{terminator.group(1)}' inside a "
            f"{'PL/SQL block' if plsql else 'SQL statement'} at line {line}; SQLcl ends the statement there"
        )
    at_line = AT_LINE.search(masked, start, end)
    if at_line is not None:
        line = line_at(masked, at_line.start())
        raise ValueError(
            f"SQL-only migration rejects a line starting with '@' at line {line}; "
            "SQLcl would splice the named file into the statement"
        )


def mask_comments_and_literals(source: str, quoted_spans: list[tuple[int, int]] | None = None) -> str:
    """Blank comments and quoted values while preserving offsets and newlines.

    ``quoted_spans``, when given, receives the (start, end) of every string,
    q-quoted literal and quoted identifier that was blanked."""
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

        if source.startswith("/*/", index):
            # A valid Oracle comment opener, but SQLcl's parser throws on it and
            # then runs the comment's lines as commands.
            raise ValueError(
                f"SQL-only migration rejects the comment opener '/*/' at line {line_at(source, index)}; "
                "SQLcl cannot parse it and would run the comment's lines as commands. Write '/* /' or '/**/' instead"
            )
        if source.startswith("/*", index):
            start = index
            comment_end = source.find("*/", index + 2)
            if comment_end < 0:
                raise ValueError(f"unterminated block comment at line {line_at(source, start)}")
            index = comment_end + 2
            # SQLcl ends the statement or PL/SQL block at a line holding only
            # '/', even inside a comment, and runs the rest as commands.
            for lone in LONE_TERMINATOR.finditer(source, start, index):
                if lone.group(1) == "/":
                    raise ValueError(
                        "SQL-only migration rejects a line holding only '/' inside a comment at line "
                        f"{line_at(source, lone.start())}; SQLcl ends the statement there"
                    )
            blank(start, index)
            continue

        # Matching in place, and only where a CREATE can start, keeps the scan
        # linear: slicing the rest of the source at every character is quadratic.
        java_header = JAVA_HEADER.match(source, index) if source[index] in "cC" else None
        if java_header is not None:
            payload_start = java_header.end()
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
                    terminator_line = line_at(source, payload_start + buffer_terminator.start())
                    raise ValueError(
                        f"SQL-only migration rejects SQLcl buffer terminator in Java source at line {terminator_line}"
                    )
                blank(payload_start, payload_end)
                index = payload_end
                continue

        if source[index] in "qQ" and index + 1 < length and source[index + 1] == "'":
            start = index
            if index + 2 >= length:
                raise ValueError(f"unterminated Oracle quoted literal at line {line_at(source, start)}")
            opening = source[index + 2]
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            terminator = closing + "'"
            end = source.find(terminator, index + 3)
            if end < 0:
                raise ValueError(f"unterminated Oracle quoted literal at line {line_at(source, start)}")
            index = end + len(terminator)
            blank(start, index)
            if quoted_spans is not None:
                quoted_spans.append((start, index))
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
                raise ValueError(f"unterminated quoted value at line {line_at(source, start)}")
            # SQLcl does not keep a quoted identifier together across lines: a
            # line holding only '/' inside one ends the statement. A string
            # literal is kept whole, so only identifiers are refused.
            if quote == '"' and "\n" in source[start:index]:
                raise ValueError(
                    f"SQL-only migration rejects a quoted identifier that spans lines at line {line_at(source, start)}; "
                    "keep it on one line"
                )
            blank(start, index)
            if quoted_spans is not None:
                quoted_spans.append((start, index))
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
                line = line_at(source, index)
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
                line = line_at(source, start)
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
                    line = line_at(source, start)
                    raise ValueError(f"unterminated Java quoted value at line {line}")
                else:
                    index += 1
            else:
                line = line_at(source, start)
                raise ValueError(f"unterminated Java quoted value at line {line}")
            blank(start, index)
            continue

        index += 1
    return "".join(characters)


def validate_sql_only(source: str) -> None:
    """Allow terminated SQL statements and PL/SQL blocks, but no SQLcl commands."""
    statement_spans(source)


def statement_spans(source: str) -> list[tuple[int, int]]:
    """Validate SQL-only source and return each statement's (start, end) offsets.

    The end excludes the ';' or the standalone '/' that terminates it.
    """
    spans: list[tuple[int, int]] = []
    quoted: list[tuple[int, int]] = []
    masked = mask_comments_and_literals(source, quoted)
    index = 0
    previous_end = 0
    while index < len(masked):
        while index < len(masked) and masked[index].isspace():
            index += 1
        # Blanked quoted text between statements (or after the last one) would
        # be skipped here, but SQLcl reads it as a command ("Unknown Command")
        # and carries on. The spans are in source order and never overlap, so
        # only the first one at or after the previous statement can lie in the gap.
        gap_quoted = bisect_left(quoted, (previous_end,))
        if gap_quoted < len(quoted) and quoted[gap_quoted][1] <= index:
            raise ValueError(
                f"SQL-only migration rejects a quoted value outside any statement at line "
                f"{line_at(source, quoted[gap_quoted][0])}; SQLcl would read it as a command"
            )
        if index >= len(masked):
            break

        statement_start = index
        first_match = FIRST_WORD.match(masked, index)
        if first_match is None:
            raise ValueError(
                f"SQL-only migration rejects a client command or non-SQL token at line {line_at(masked, statement_start)}"
            )
        # SQLcl decides what a statement is from its first whitespace-delimited
        # word. 'DECLARE,~' is an unknown command to it, so it runs the lines
        # after it one by one while this validator would read the keyword and
        # skip the rest as one block. Judge the source, not the masked text, so
        # a comment glued to the word ('DECLARE/**/') does not count as a gap.
        word_end = first_match.end()
        if word_end < len(source) and source[word_end] not in " \t\r\n":
            raise ValueError(
                f"SQL-only migration rejects '{source[statement_start:word_end + 1].strip()}' at line "
                f"{line_at(masked, statement_start)}: SQLcl reads a statement's first word up to the next space, "
                f"so put a space after '{first_match.group()}'"
            )
        tokens = FIRST_WORD.findall(masked[index:index + 240])
        first = first_match.group().upper()

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
            slash = STANDALONE_SLASH.search(masked, index)
            if slash is None:
                raise ValueError(
                    f"SQL-only migration PL/SQL block must end with a standalone slash at line {line_at(masked, statement_start)}"
                )
            reject_sqlcl_line_hazards(masked, index, slash.start(), plsql=True)
            spans.append((statement_start, slash.start()))
            index = slash.end()
            previous_end = index
            continue

        # Name a client command before asking for a semicolon: HOST, DEFINE, START
        # and the like end at the newline, so "add a semicolon" would be wrong.
        allowed = first in SQL_STARTERS
        if first == "SET":
            # Only SET CONSTRAINTS and SET TRANSACTION are SQL; read the second word
            # from this statement, not from the one after a ';'.
            statement_words = FIRST_WORD.findall(masked[index:index + 240].split(";", 1)[0])
            allowed = len(statement_words) > 1 and statement_words[1].upper() in {"CONSTRAINTS", "TRANSACTION"}
        if not allowed:
            raise ValueError(
                f"SQL-only migration rejects SQLcl/client command or unsupported statement "
                f"'{first}' at line {line_at(masked, statement_start)}"
            )
        statement_end = masked.find(";", index)
        if statement_end < 0:
            raise ValueError(
                f"SQL-only migration statement must end with a semicolon at line {line_at(masked, statement_start)}"
            )
        reject_sqlcl_line_hazards(masked, statement_start, statement_end, plsql=False)
        spans.append((statement_start, statement_end))
        index = statement_end + 1
        previous_end = index
    return spans


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
