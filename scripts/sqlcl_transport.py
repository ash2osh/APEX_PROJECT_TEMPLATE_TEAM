#!/usr/bin/env python3
"""Conservative diagnostics for SQL delivered through SQLcl's script reader."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .validate_migration import mask_comments_and_literals


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
