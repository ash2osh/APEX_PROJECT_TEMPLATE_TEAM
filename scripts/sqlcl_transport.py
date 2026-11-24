#!/usr/bin/env python3
"""Conservative diagnostics for SQL delivered through SQLcl's script reader."""

from __future__ import annotations

import re
from dataclasses import dataclass


_LONE_TERMINATOR = re.compile(r"(?m)^[ \t]*([/.])[ \t\r]*$")
_GAP = r"(?:\s+|/\*.*?\*/|--[^\r\n]*(?:\r?\n|$))+"
_JAVA_HEADER = re.compile(
    rf"CREATE{_GAP}"
    rf"(?:OR{_GAP}REPLACE{_GAP})?"
    rf"(?:AND{_GAP}(?:RESOLVE|COMPILE){_GAP})?"
    rf"(?:NOFORCE{_GAP})?"
    rf"JAVA{_GAP}"
    rf"(?:IF{_GAP}NOT{_GAP}EXISTS{_GAP})?"
    r"(?:SOURCE|CLASS|RESOURCE)\b",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _mask_java_quoted_values(source: str) -> str:
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
                raise ValueError(f"unterminated Java block comment at line {_line_at(source, index)}")
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
                raise ValueError(f"unterminated Java text block at line {_line_at(source, start)}")
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
                    raise ValueError(f"unterminated Java quoted value at line {_line_at(source, start)}")
                else:
                    index += 1
            else:
                raise ValueError(f"unterminated Java quoted value at line {_line_at(source, start)}")
            blank(start, index)
            continue
        index += 1
    return "".join(characters)


def mask_comments_and_literals(source: str, quoted_spans: list[tuple[int, int]] | None = None) -> str:
    """Blank comments and quoted values while preserving offsets and newlines.

    ``quoted_spans``, when given, receives the (start, end) of every string,
    q-quoted literal and quoted identifier that was blanked.
    """
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
            raise ValueError(
                f"SQL-only script rejects the comment opener '/*/' at line {_line_at(source, index)}; "
                "SQLcl cannot parse it and would run the comment's lines as commands. Write '/* /' or '/**/' instead"
            )
        if source.startswith("/*", index):
            start = index
            comment_end = source.find("*/", index + 2)
            if comment_end < 0:
                raise ValueError(f"unterminated block comment at line {_line_at(source, start)}")
            index = comment_end + 2
            for lone in _LONE_TERMINATOR.finditer(source, start, index):
                if lone.group(1) == "/":
                    raise ValueError(
                        "SQL-only script rejects a line holding only '/' inside a comment at line "
                        f"{_line_at(source, lone.start())}; SQLcl ends the statement there"
                    )
            blank(start, index)
            continue
        java_header = _JAVA_HEADER.match(source, index) if source[index] in "cC" else None
        if java_header is not None:
            payload_start = java_header.end()
            for comment in re.finditer(r"/\*.*?\*/|--[^\r\n]*", source[index:payload_start], flags=re.DOTALL):
                blank(index + comment.start(), index + comment.end())
            slash = re.search(r"(?m)^[ \t]*/[ \t]*(?:--[^\r\n]*)?(?:\r?\n|$)", source[payload_start:])
            if slash is not None:
                payload_end = payload_start + slash.start()
                java_payload = _mask_java_quoted_values(source[payload_start:payload_end])
                buffer_terminator = re.search(r"(?m)^[ \t]*;[ \t]*(?:\r?\n|$)", java_payload)
                if buffer_terminator is not None:
                    terminator_line = _line_at(source, payload_start + buffer_terminator.start())
                    raise ValueError(
                        f"SQL-only script rejects SQLcl buffer terminator in Java source at line {terminator_line}"
                    )
                blank(payload_start, payload_end)
                index = payload_end
                continue
        if source[index] in "qQ" and index + 1 < length and source[index + 1] == "'":
            start = index
            if index + 2 >= length:
                raise ValueError(f"unterminated Oracle quoted literal at line {_line_at(source, start)}")
            opening = source[index + 2]
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            terminator = closing + "'"
            end = source.find(terminator, index + 3)
            if end < 0:
                raise ValueError(f"unterminated Oracle quoted literal at line {_line_at(source, start)}")
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
                raise ValueError(f"unterminated quoted value at line {_line_at(source, start)}")
            if quote == '"' and "\n" in source[start:index]:
                raise ValueError(
                    f"SQL-only script rejects a quoted identifier that spans lines at line {_line_at(source, start)}; "
                    "keep it on one line"
                )
            blank(start, index)
            if quoted_spans is not None:
                quoted_spans.append((start, index))
            continue
        index += 1
    return "".join(characters)


@dataclass(frozen=True)
class TransportFinding:
    code: str
    line: int
    severity: str
    message: str


_GENERATED_CLOB_HEADER = re.compile(
    r"-- (?:Exact stored source for .+|Stored DDL for .+) assembled as a CLOB\.\Z"
)


def generated_clob_assembly(sql: str) -> bool:
    """Recognize the baseline generator's explicit source/DDL CLOB wrapper."""
    lines = sql.splitlines()
    if not lines or _GENERATED_CLOB_HEADER.fullmatch(lines[0]) is None:
        return False
    if re.search(r"(?is)\bDECLARE\b.*?\bTO_CLOB\s*\(.*?\bEXECUTE\s+IMMEDIATE\b.*?\bDBMS_LOB\.FREETEMPORARY\s*\(", sql) is None:
        return False
    return re.search(r"(?im)^\s*/\s*$", sql) is not None


def _line_ranges(source: str):
    start = 0
    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        end = start + len(line)
        content_end = end
        while content_end > start and source[content_end - 1] in "\r\n":
            content_end -= 1
        yield line_number, start, content_end, end
        start = end
    if not source or (source and source[-1] not in "\r\n"):
        return


def _inside(offset: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in spans)


def lint_sqlcl_transport(sql: str, delivery_mode: str) -> tuple[TransportFinding, ...]:
    """Report transport risks without changing the SQL bytes.

    ``direct`` describes source sent as an included SQL script. ``assembled``
    describes executable SQL that reconstructs source from data such as CLOB
    chunks or ``CHR`` expressions; it does not describe the original data.
    Only a direct physical newline that splits a quoted value immediately after
    a hyphen is an error. Less certain blank-line and code-format cases remain
    warnings for human review.
    """
    if delivery_mode not in {"direct", "assembled"}:
        raise ValueError("delivery_mode must be 'direct' or 'assembled'")
    if not isinstance(sql, str):
        raise TypeError("SQLcl transport input must be text")

    quoted_spans: list[tuple[int, int]] = []
    try:
        mask_comments_and_literals(sql, quoted_spans)
    except ValueError:
        # The normal SQL/migration validator owns syntax failures. Do not let a
        # best-effort transport pass turn malformed input into an acceptance.
        return ()

    findings: list[TransportFinding] = []
    masked = mask_comments_and_literals(sql)
    ranges = list(_line_ranges(sql))
    for index, (line_number, start, content_end, end) in enumerate(ranges):
        line = sql[start:content_end]
        nonspace_end = content_end
        while nonspace_end > start and sql[nonspace_end - 1] in " \t":
            nonspace_end -= 1

        if nonspace_end > start and sql[nonspace_end - 1] == "-":
            tail = nonspace_end - 1
            in_literal = _inside(tail, quoted_spans)
            if in_literal and end > content_end and index + 1 < len(ranges):
                next_start = ranges[index + 1][1]
                next_end = ranges[index + 1][3]
                if _inside(next_start, quoted_spans) or _inside(max(next_start, next_end - 1), quoted_spans):
                    severity = "error" if delivery_mode == "direct" else "warning"
                    findings.append(TransportFinding(
                        "quoted-trailing-hyphen",
                        line_number,
                        severity,
                        "physical newline after a quoted trailing hyphen may be lost by SQLcl; "
                        "assemble the value with explicit CLOB chunks or character expressions",
                    ))
                    continue
            if not in_literal and masked[tail] == "-":
                findings.append(TransportFinding(
                    "code-trailing-hyphen",
                    line_number,
                    "warning",
                    "SQL text ends a physical line with a hyphen; review SQLcl continuation "
                    "semantics and the emitted driver",
                ))

        # Whitespace-only lines inside quoted source may carry meaningful data.
        # Current evidence does not justify refusing every such direct script.
        if not line.strip(" \t"):
            if _inside(start, quoted_spans) or (content_end > start and _inside(content_end - 1, quoted_spans)):
                findings.append(TransportFinding(
                    "quoted-blank-line",
                    line_number,
                    "warning",
                    "whitespace-only physical line occurs inside a quoted value; "
                    "review exact stored bytes or use explicit assembly",
                ))

    # The same executable assembly driver is still read by SQLcl. It is safe to
    # inspect its SQL syntax, but data represented by CHR/hex expressions is not
    # treated as physical source text by this checker.
    return tuple(findings)
