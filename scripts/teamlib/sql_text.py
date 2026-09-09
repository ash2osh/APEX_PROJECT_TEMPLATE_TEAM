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


def _mask_span(chars: list[str], text: str, start: int, stop: int) -> None:
    for index in range(start, stop):
        if text[index] != "\n":
            chars[index] = " "


def mask_sql(text: str) -> tuple[str, bool]:
    """Blank comments and literals, preserving offsets and newlines.

    Returns the masked text and whether every construct was terminated. Oracle
    alternative quoting (``q'[...]'``, ``nq'{...}'``) is recognised, so an
    embedded apostrophe neither ends the literal early nor leaks the rest of
    the literal's text into the masked output as if it were code.
    """
    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        current = text[index]
        following = text[index + 1] if index + 1 < length else ""

        if current == "-" and following == "-":
            while index < length and text[index] != "\n":
                chars[index] = " "
                index += 1
            continue

        if current == "/" and following == "*":
            chars[index] = chars[index + 1] = " "
            index += 2
            while index < length:
                if text[index] == "*" and index + 1 < length and text[index + 1] == "/":
                    chars[index] = chars[index + 1] = " "
                    index += 2
                    break
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            else:
                return "".join(chars), False
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
                    _mask_span(chars, text, start, length)
                    return "".join(chars), False
                _mask_span(chars, text, start, end + 2)
                index = end + 2
                continue

        if current in {"'", '"'}:
            quote = current
            chars[index] = " "
            index += 1
            while index < length:
                if text[index] == quote:
                    if index + 1 < length and text[index + 1] == quote:
                        chars[index] = chars[index + 1] = " "
                        index += 2
                        continue
                    chars[index] = " "
                    index += 1
                    break
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            else:
                return "".join(chars), False
            continue

        index += 1
    return "".join(chars), True
