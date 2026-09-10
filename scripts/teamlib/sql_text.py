"""Shared, Oracle-aware SQL text helpers.

These were previously private helpers in ``migration_store`` that
``control_store`` reached into by underscore name, and two independent
comment/literal lexers in ``migration_bundle`` and ``sqlcl``. One correct
implementation lives here so a fix reaches every caller.
"""

from __future__ import annotations

import base64
import re


class SqlTextError(ValueError):
    """Raised when a value cannot be embedded in generated SQL."""


_CLOB_CHUNK = 1000
_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$#"
)
# Oracle alternative quoting closes a bracket-style delimiter with its mate and
# every other delimiter with itself.
_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


def sql_literal(value: str) -> str:
    """Quote a value as a SQL string literal.

    Generated drivers always run under ``SET DEFINE OFF``, so doubling the
    single quote is sufficient; ``&`` substitution is never active.
    """
    if not isinstance(value, str) or "\x00" in value:
        raise SqlTextError("SQL value is invalid")
    return "'" + value.replace("'", "''") + "'"


def clob_builder(variable: str, value: str, *, indent: str = "  ") -> str:
    """Emit PL/SQL that fills ``variable`` from bounded lines.

    A single ``TO_CLOB(..) || TO_CLOB(..)`` expression puts the whole value on
    one line, which a large schema inventory pushes past what SQLcl will read.
    One append per line keeps every line under about 1050 characters, and
    building the value once lets a caller reference it more than once without
    re-emitting it.

    The temporary LOB is not freed: each ``run_sqlcl`` call is a fresh
    single-purpose session, and the session end reclaims it. Freeing it here
    would need an exception handler that would swallow the ORA-20xxx codes the
    callers match on.
    """
    if not isinstance(variable, str) or not variable.isidentifier():
        raise SqlTextError("CLOB variable name is invalid")
    if not isinstance(value, str) or "\x00" in value:
        raise SqlTextError("CLOB value is invalid")
    lines = [f"{indent}DBMS_LOB.CREATETEMPORARY({variable}, TRUE);"]
    for index in range(0, len(value), _CLOB_CHUNK):
        piece = sql_literal(value[index:index + _CLOB_CHUNK])
        lines.append(f"{indent}DBMS_LOB.APPEND({variable}, TO_CLOB({piece}));")
    return "\n".join(lines)


def b64_sql(column: str) -> str:
    """Base64-encode a column so wrapped output stays safely re-joinable."""
    return (
        "UTL_RAW.CAST_TO_VARCHAR2(UTL_ENCODE.BASE64_ENCODE("
        f"UTL_RAW.CAST_TO_RAW(NVL({column}, CHR(1)))))"
    )


def _decode_b64(value: str) -> str:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
        return "" if decoded == "\x01" else decoded
    except (ValueError, UnicodeError) as exc:
        raise SqlTextError("metadata row contains invalid encoded text") from exc


def row_lines(stdout: str, prefix: str) -> list[list[str]]:
    """Parse prefixed, base64-framed metadata rows from SQLcl output."""
    rows: list[list[str]] = []
    pending: str | None = None

    def consume(value: str) -> None:
        parts = value.split("|")[1:]
        if not parts or any(not part for part in parts):
            raise SqlTextError(f"malformed {prefix} metadata row")
        rows.append([_decode_b64(part) for part in parts])

    for raw in stdout.splitlines():
        line = raw.strip().rstrip("\r")
        if line.startswith(prefix):
            if pending is not None:
                consume(pending)
            pending = line
            continue
        # SQLcl may wrap a long SELECT expression at its terminal width even
        # after LINESIZE is raised. The encoded payload deliberately contains
        # only base64 characters and separators, so continuation lines can be
        # joined without accepting arbitrary diagnostic output.
        if pending is not None and re.fullmatch(r"[A-Za-z0-9+/=|]+", line):
            pending += line
    if pending is not None:
        consume(pending)
    return rows


def _scan(text: str) -> tuple[list[tuple[str, int, int]], bool]:
    """Locate every comment and literal span, Oracle alternative quoting included.

    Returns ``(spans, terminated)``. ``terminated`` is False when a construct
    runs off the end of the text, which every caller treats as a refusal: an
    unterminated literal means the rest of the file is not what it looks like.
    Spans are ascending, non-overlapping, and ``stop`` is exclusive.
    """
    spans: list[tuple[str, int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        current = text[index]
        following = text[index + 1] if index + 1 < length else ""

        if current == "-" and following == "-":
            end = text.find("\n", index)
            if end == -1:
                end = length
            spans.append(("line-comment", index, end))
            index = end
            continue

        if current == "/" and following == "*":
            end = text.find("*/", index + 2)
            if end == -1:
                spans.append(("block-comment", index, length))
                return spans, False
            spans.append(("block-comment", index, end + 2))
            index = end + 2
            continue

        if current in {"q", "Q"} and following == "'":
            previous = text[index - 1] if index else ""
            if previous in {"n", "N"} and (index < 2 or text[index - 2] not in _IDENTIFIER_CHARS):
                start = index - 1
            elif previous not in _IDENTIFIER_CHARS:
                start = index
            else:
                start = None
            if start is not None and index + 2 < length:
                delimiter = text[index + 2]
                closer = _Q_CLOSERS.get(delimiter, delimiter)
                end = text.find(closer + "'", index + 3)
                if end == -1:
                    spans.append(("literal", start, length))
                    return spans, False
                spans.append(("literal", start, end + 2))
                index = end + 2
                continue

        if current in {"'", '"'}:
            quote = current
            cursor = index + 1
            while cursor < length:
                if text[cursor] == quote:
                    if cursor + 1 < length and text[cursor + 1] == quote:
                        cursor += 2
                        continue
                    spans.append(("literal", index, cursor + 1))
                    index = cursor + 1
                    break
                cursor += 1
            else:
                spans.append(("literal", index, length))
                return spans, False
            continue

        index += 1
    return spans, True


def mask_sql(text: str) -> tuple[str, bool]:
    """Blank comments and literals, preserving offsets and newlines.

    Returns the masked text and whether every construct was terminated. Oracle
    alternative quoting (``q'[...]'``, ``nq'{...}'``) is recognised, so an
    embedded apostrophe neither ends the literal early nor leaks the rest of
    the literal's text into the masked output as if it were code.
    """
    spans, terminated = _scan(text)
    chars = list(text)
    for _, start, stop in spans:
        for index in range(start, stop):
            if text[index] != "\n":
                chars[index] = " "
    return "".join(chars), terminated


def comment_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return ``(start, stop)`` offsets of every real ``--`` line comment.

    A ``--`` inside a string literal -- including a q-quoted literal whose body
    contains an apostrophe -- is data and is not reported. Callers that parse
    directives out of comments must use this rather than scanning raw lines.
    """
    spans, terminated = _scan(text)
    if not terminated:
        raise SqlTextError("unterminated SQL comment or literal")
    return tuple((start, stop) for kind, start, stop in spans if kind == "line-comment")


# A PL/SQL block is one statement whose body may contain any number of
# semicolons. It ends at a line containing only "/", never at a ";".
BLOCK_START_RE = re.compile(
    r"^(?:DECLARE|BEGIN)\b"
    r"|^CREATE(?:\s+OR\s+REPLACE)?(?:\s+(?:EDITIONABLE|NONEDITIONABLE))?\s+"
    r"(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\b",
    re.IGNORECASE,
)
# SQLcl client commands are terminated by the end of the line, not by ";".
# Treating them as SQL made one unterminated "SET DEFINE OFF" swallow every
# following statement, so nothing after it was ever offered to a guard.
_LINE_TERMINATED_RE = re.compile(
    r"^(?:@{1,2}|!)"
    r"|^(?:SET|SHOW|SPOOL|PROMPT|WHENEVER|EXIT|QUIT|CONNECT|CONN|DISCONNECT|DISC"
    r"|HOST|HO|START|SCRIPT|DEFINE|DEF|UNDEFINE|UNDEF|COLUMN|COL|CLEAR|ACCEPT|ACC"
    r"|PAUSE|TIMING|REM|REMARK|DESCRIBE|DESC|EXECUTE|EXEC|ALIAS|APEX|LIQUIBASE|LB"
    r"|CD|INFO|HISTORY)\b",
    re.IGNORECASE,
)


def statement_starts(masked: str):
    """Yield ``(line_number, text)`` for every top-level statement.

    ``masked`` must be :func:`mask_sql` output, so a ``;`` it still contains
    really does terminate a statement rather than sitting inside a literal or a
    comment. ``text`` runs from the statement's first non-blank character to the
    next top-level ``;`` or the end of that line -- enough to match a leading
    keyword, which is all any guard in this repository inspects.

    Two properties matter and are why this replaced the per-line walkers:
    a second statement after an inline ``;`` is offered, and a client command
    with no ``;`` does not hide the lines that follow it.
    """
    pending = True
    in_block = False
    for number, raw in enumerate(masked.splitlines(), start=1):
        line = raw.strip()
        if in_block:
            if line == "/":
                in_block = False
                pending = True
            continue
        if not line:
            continue
        if line == "/":
            pending = True
            continue
        position = 0
        line_length = len(line)
        while position < line_length:
            semicolon = line.find(";", position)
            piece = (line[position:semicolon] if semicolon >= 0 else line[position:]).strip()
            if pending and piece:
                yield number, piece
                if BLOCK_START_RE.match(piece):
                    in_block = True
                    pending = False
                    break
                if _LINE_TERMINATED_RE.match(piece):
                    # The command ended with this line regardless of any ";".
                    pending = True
                    break
            if semicolon < 0:
                pending = False
                break
            pending = True
            position = semicolon + 1
