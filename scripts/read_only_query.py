#!/usr/bin/env python3
"""Run one bounded, guarded SELECT through a configured SQLcl profile."""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence

from .dev_test_bindings import validate_bindings
from .db_targets import Target, TargetResolutionError, looks_like_production_identity, resolve_target
from .local_config import ConfigError, read_project_env
from .migration_manifest import SAFE_FUNCTIONS, SAFE_SQL_FORMS
from .sqlcl_session import SqlclError, run_sqlcl


DEFAULT_TIMEOUT_SECONDS = 60
MAX_QUERY_BYTES = 32768
MAX_CAPTURE_BYTES = 1024 * 1024
MAX_RESULT_ROWS = 500
_ALLOWED_FUNCTIONS = SAFE_FUNCTIONS - {"cast"}
_FORBIDDEN_WORDS = {
    "alter", "begin", "call", "commit", "connect", "create", "currval", "declare", "delete", "disconnect",
    "drop", "edit", "execute", "exec", "exit", "function", "grant", "host", "insert", "lock", "merge",
    "nextval", "prompt", "procedure", "quit", "rollback", "revoke", "run", "script", "set", "spool",
    "start", "truncate", "update", "whenever",
}
_RESERVED_BEFORE_PAREN = {
    "all", "and", "any", "between", "by", "connect", "distinct", "else", "from", "group", "having",
    "intersect", "is", "like", "minus", "not", "of", "on", "or", "order", "prior", "select", "start",
    "then", "to", "union", "unique", "where", "with",
}
_FROM_TERMINATORS = {
    "connect", "fetch", "for", "group", "having", "intersect", "match_recognize", "minus", "model", "offset",
    "order", "start", "union", "where",
}
_ALIAS_RESERVED = _FROM_TERMINATORS | {
    "as", "cross", "full", "inner", "join", "left", "natural", "on", "outer", "right", "using",
}
_JOIN_MODIFIERS = {"cross", "full", "inner", "left", "natural", "outer", "right"}
_ORDS_KEYS = ("ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER")
_PROFILE_KEYS = {
    "tables": ("TABLES_SCHEMA", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER"),
    "code": ("CODE_SCHEMA", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER"),
    "apex": ("APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER"),
    "ords": _ORDS_KEYS,
}
_IDENTITY_ATTRIBUTES = {"session_user", "current_user", "current_schema", "sessionid", "db_name", "service_name"}
_MARKER_RE = re.compile(r"ROQ_[a-f0-9]{32}\Z")
_SQL_PRODUCTION_MARKER = (
    r"(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)"
    r"|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)"
    r"|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])"
)
_SQL_NON_PRODUCTION_MARKER = r"(pre|non)[-_.]?(prod|prd)"


class QueryError(ValueError):
    """A query was refused or its read-only result could not be verified."""


@dataclass(frozen=True)
class _Token:
    kind: str
    raw: str
    value: str
    start: int
    end: int
    national: bool = False


def _read_quoted(source: str, start: int, quote: str, prefix_length: int = 0) -> tuple[int, str]:
    index = start + prefix_length + 1
    value: list[str] = []
    while index < len(source):
        char = source[index]
        if char == quote:
            if index + 1 < len(source) and source[index + 1] == quote:
                value.append(quote)
                index += 2
                continue
            return index + 1, "".join(value)
        value.append(char)
        index += 1
    raise QueryError("query has an unterminated quoted token")


def _read_q_quoted(source: str, start: int, national: bool) -> tuple[int, str]:
    prefix_length = 2 if national else 1
    quote_index = start + prefix_length
    if quote_index + 1 >= len(source) or source[quote_index] != "'":
        raise QueryError("query has an invalid q-quoted literal")
    delimiter = source[quote_index + 1]
    if delimiter.isspace() or ord(delimiter) < 32 or delimiter == "'":
        raise QueryError("query has an invalid q-quote delimiter")
    closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(delimiter, delimiter)
    ending = closing + "'"
    end = source.find(ending, quote_index + 2)
    if end < 0:
        raise QueryError("query has an unterminated q-quoted literal")
    return end + len(ending), source[quote_index + 2 : end]


def _scan(sql: str) -> tuple[list[_Token], list[tuple[str, str]]]:
    if not isinstance(sql, str) or not sql.strip():
        raise QueryError("query must be a nonempty SELECT or WITH-SELECT statement")
    try:
        encoded = sql.encode("utf-8")
    except UnicodeEncodeError:
        raise QueryError("query must contain valid UTF-8 text") from None
    if len(encoded) > MAX_QUERY_BYTES:
        raise QueryError(f"query exceeds the {MAX_QUERY_BYTES}-byte input limit")

    tokens: list[_Token] = []
    parts: list[tuple[str, str]] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in " \t\r\n":
            parts.append(("space", " "))
            index += 1
            continue
        if ord(char) < 32:
            raise QueryError("query contains a control character outside a string literal")
        if sql.startswith("--", index):
            end = index + 2
            while end < len(sql) and sql[end] not in "\r\n":
                end += 1
            if end < len(sql):
                end += 2 if sql.startswith("\r\n", end) else 1
            parts.append(("space", " "))
            index = end
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise QueryError("query has an unterminated block comment")
            parts.append(("space", " "))
            index = end + 2
            continue

        start = index
        if char in "nN" and index + 2 < len(sql) and sql[index + 1] in "qQ" and sql[index + 2] == "'":
            index, value = _read_q_quoted(sql, start, True)
            token = _Token("string", sql[start:index], value, start, index, True)
        elif char in "qQ" and index + 1 < len(sql) and sql[index + 1] == "'":
            index, value = _read_q_quoted(sql, start, False)
            token = _Token("string", sql[start:index], value, start, index)
        elif char in "nN" and index + 1 < len(sql) and sql[index + 1] == "'":
            index, value = _read_quoted(sql, start, "'", prefix_length=1)
            token = _Token("string", sql[start:index], value, start, index, True)
        elif char == "'":
            index, value = _read_quoted(sql, start, "'")
            token = _Token("string", sql[start:index], value, start, index)
        elif char == '"':
            index, value = _read_quoted(sql, start, '"')
            if any(ord(value_char) < 32 for value_char in value):
                raise QueryError("quoted identifiers may not contain control characters")
            token = _Token("quoted_identifier", sql[start:index], value, start, index)
        elif char.isascii() and (char.isalpha() or char == "_"):
            index += 1
            while index < len(sql) and sql[index].isascii() and (sql[index].isalnum() or sql[index] in "_$#"):
                index += 1
            raw = sql[start:index]
            token = _Token("word", raw, raw.casefold(), start, index)
        elif char.isdigit():
            index += 1
            while index < len(sql) and sql[index].isascii() and (sql[index].isalnum() or sql[index] in "._+-"):
                if sql[index] in "+-" and sql[index - 1] not in "eE":
                    break
                index += 1
            raw = sql[start:index]
            token = _Token("number", raw, raw, start, index)
        elif not char.isascii():
            raise QueryError("unquoted identifiers must use ASCII; quote non-ASCII names")
        else:
            two = sql[index : index + 2]
            if two in {"||", ">=", "<=", "!=", "<>", "**"}:
                index += 2
                raw = two
            elif char in "(),.*+-/=<>;:":
                index += 1
                raw = char
            else:
                raise QueryError("query contains an unsupported token")
            token = _Token("symbol", raw, raw, start, index)
        tokens.append(token)
        parts.append((token.kind, token.raw))

    for token in tokens:
        if token.kind != "symbol" or token.raw != "/":
            continue
        line_start = max(sql.rfind("\n", 0, token.start), sql.rfind("\r", 0, token.start)) + 1
        line_end_candidates = [position for position in (sql.find("\n", token.end), sql.find("\r", token.end)) if position >= 0]
        line_end = min(line_end_candidates) if line_end_candidates else len(sql)
        if sql[line_start:line_end].strip() == "/":
            raise QueryError("SQLcl slash commands are not allowed")

    return tokens, parts


def _identifier(token: _Token) -> str | None:
    if token.kind == "word":
        return token.value
    if token.kind == "quoted_identifier":
        return token.value.casefold()
    return None


def _prepare_literal(token: _Token) -> str:
    if "\n" not in token.value and "\r" not in token.value:
        return token.raw
    pieces: list[str] = []
    chunk: list[str] = []
    index = 0
    while index < len(token.value):
        char = token.value[index]
        if char not in "\r\n":
            chunk.append(char)
            index += 1
            continue
        prefix = "N" if token.national else ""
        pieces.append(prefix + "'" + "".join(chunk).replace("'", "''") + "'")
        chunk.clear()
        pieces.append(("NCHR" if token.national else "CHR") + f"({ord(char)})")
        index += 1
    prefix = "N" if token.national else ""
    pieces.append(prefix + "'" + "".join(chunk).replace("'", "''") + "'")
    return "(" + " || ".join(pieces) + ")"


def _build_prepared(parts: list[tuple[str, str]], tokens: Sequence[_Token]) -> str:
    final_semicolon = tokens[-1] if tokens and tokens[-1].kind == "symbol" and tokens[-1].raw == ";" else None
    result: list[str] = []
    pending_space = False
    string_tokens = iter(token for token in tokens if token.kind == "string")
    for kind, raw in parts:
        if final_semicolon is not None and kind == "symbol" and raw == ";":
            # There is exactly one legal semicolon and it is the final token.
            continue
        if kind == "space":
            pending_space = True
            continue
        cooked = raw
        if kind == "string":
            literal = next(string_tokens)
            cooked = _prepare_literal(literal)
        if pending_space and result and not result[-1].endswith(" "):
            result.append(" ")
        result.append(cooked)
        pending_space = False
    return "".join(result).strip()


def _collect_sources(tokens: Sequence[_Token]) -> tuple[set[str], set[str]]:
    """Return allowed relation qualifiers and token offsets for relation names."""
    qualifiers: set[str] = set()
    source_offsets: set[int] = set()
    depths: list[int] = []
    depth = 0
    for token in tokens:
        depths.append(depth)
        if token.kind == "symbol" and token.raw == "(":
            depth += 1
        elif token.kind == "symbol" and token.raw == ")":
            depth -= 1

    for start, token in enumerate(tokens):
        if token.kind != "word" or token.value != "from":
            continue
        clause_depth = depths[start]
        index = start + 1
        while index < len(tokens):
            current = tokens[index]
            current_depth = depths[index]
            if current_depth < clause_depth:
                break
            if current_depth == clause_depth and current.kind == "word" and current.value in _FROM_TERMINATORS:
                break
            if current_depth == clause_depth and current.kind == "word" and current.value in _JOIN_MODIFIERS:
                index += 1
                continue
            if current_depth == clause_depth and current.kind == "word" and current.value in {"on", "using"}:
                index += 1
                while index < len(tokens):
                    following = tokens[index]
                    if depths[index] == clause_depth and following.kind == "word" and following.value == "join":
                        break
                    if depths[index] == clause_depth and following.kind == "symbol" and following.raw == ",":
                        break
                    if depths[index] == clause_depth and following.kind == "word" and following.value in _FROM_TERMINATORS:
                        break
                    index += 1
                continue
            if current_depth == clause_depth and current.kind == "word" and current.value == "join":
                index += 1
                continue
            if current_depth == clause_depth and current.kind == "symbol" and current.raw == ",":
                index += 1
                continue
            if current_depth != clause_depth or _identifier(current) is None:
                index += 1
                continue

            source_start = index
            if current.kind == "word" and current.value in {"only", "lateral"}:
                # These Oracle source modifiers have a parenthesized source form;
                # accepting the source itself remains subject to the same alias gate.
                source_offsets.add(index)
                index += 1
                if index < len(tokens) and tokens[index].raw == "(":
                    close = index + 1
                    while close < len(tokens) and not (tokens[close].raw == ")" and depths[close] == clause_depth + 1):
                        close += 1
                    if close >= len(tokens):
                        break
                    source_offsets.update(range(index, close + 1))
                    index = close + 1
                continue

            if current.raw == "(":
                source_offsets.add(index)
                close = index + 1
                while close < len(tokens) and not (tokens[close].raw == ")" and depths[close] == clause_depth + 1):
                    close += 1
                if close >= len(tokens):
                    break
                source_offsets.update(range(index, close + 1))
                index = close + 1
                if index < len(tokens) and tokens[index].kind == "word" and tokens[index].value == "as":
                    index += 1
                alias = _identifier(tokens[index]) if index < len(tokens) and depths[index] == clause_depth else None
                if alias is not None and alias not in _ALIAS_RESERVED:
                    qualifiers.add(alias)
                    source_offsets.add(index)
                    index += 1
                continue

            components = [_identifier(current)]
            source_offsets.add(index)
            index += 1
            while index + 1 < len(tokens) and tokens[index].raw == "." and _identifier(tokens[index + 1]) is not None:
                source_offsets.update((index, index + 1))
                components.append(_identifier(tokens[index + 1]))
                index += 2
            if components[0] is not None:
                qualifiers.add(components[-1])
                if len(components) >= 2:
                    qualifiers.add(".".join(str(part) for part in components[-2:]))
            if index < len(tokens) and tokens[index].kind == "word" and tokens[index].value == "as":
                index += 1
            alias = _identifier(tokens[index]) if index < len(tokens) and depths[index] == clause_depth else None
            if alias is not None and alias not in _ALIAS_RESERVED:
                qualifiers.add(alias)
                source_offsets.add(index)
                index += 1
            if index <= source_start:
                index = source_start + 1
    return qualifiers, source_offsets


def _validate_tokens(tokens: Sequence[_Token], *, allowed_binds: set[str] | None = None) -> None:
    if not tokens:
        raise QueryError("query must be a nonempty SELECT or WITH-SELECT statement")
    semicolons = [index for index, token in enumerate(tokens) if token.kind == "symbol" and token.raw == ";"]
    if len(semicolons) > 1 or (semicolons and semicolons[0] != len(tokens) - 1):
        raise QueryError("query must contain exactly one statement and at most one final semicolon")
    if semicolons:
        tokens = tokens[:-1]
    if not tokens or tokens[0].kind != "word" or tokens[0].value not in {"select", "with"}:
        raise QueryError("only one SELECT or SELECT-producing WITH query is allowed")

    for index, token in enumerate(tokens):
        if token.kind == "symbol" and token.raw == ":":
            if allowed_binds is None:
                raise QueryError("query contains an unsupported token")
            if index + 1 >= len(tokens):
                raise QueryError("bind variable colon must be followed by a name")
            next_token = tokens[index + 1]
            if next_token.start != token.end:
                raise QueryError("bind variable colon must precede name without whitespace")
            if next_token.kind == "number":
                raise QueryError("positional bind variables are not allowed")
            if next_token.kind != "word":
                raise QueryError(f"invalid bind variable token {next_token.raw!r}")
            name = next_token.raw.upper()
            if not name.startswith("DEVTEST_"):
                raise QueryError(f"bind variable :{name} is not in the allowed DEVTEST_ namespace")
            if name not in allowed_binds:
                raise QueryError(f"unbound bind variable :{name}")

    words = [token.value for token in tokens if token.kind == "word"]
    if set(words) & _FORBIDDEN_WORDS:
        raise QueryError("query contains a forbidden SQL or SQLcl operation")
    for token in tokens:
        if token.kind == "quoted_identifier" and token.value.casefold() in {"nextval", "currval"}:
            raise QueryError("sequence NEXTVAL and CURRVAL are not allowed")
    if any(word in {"nextval", "currval"} for word in words):
        raise QueryError("sequence NEXTVAL and CURRVAL are not allowed")
    if tokens[0].value == "with" and len(tokens) > 1 and tokens[1].kind == "word" and tokens[1].value in {"function", "procedure"}:
        raise QueryError("WITH FUNCTION and WITH PROCEDURE declarations are not allowed")

    depth = 0
    top_level_select = False
    for token in tokens:
        if token.kind == "symbol" and token.raw == "(":
            depth += 1
        elif token.kind == "symbol" and token.raw == ")":
            depth -= 1
            if depth < 0:
                raise QueryError("query has unbalanced parentheses")
        elif depth == 0 and token.kind == "word" and token.value == "select":
            top_level_select = True
    if depth:
        raise QueryError("query has unbalanced parentheses")
    if not top_level_select:
        raise QueryError("WITH query does not produce a SELECT result")

    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        if token.kind == "word" and next_token.kind == "symbol" and next_token.raw == "(":
            name = token.value
            if name not in _ALLOWED_FUNCTIONS and not _is_allowed_sql_form(name, index, tokens) and name not in _RESERVED_BEFORE_PAREN:
                raise QueryError("query calls an unsupported or user-defined function")
            if name == "sys_context":
                arguments = tokens[index + 2 : index + 6]
                if (
                    len(arguments) != 4
                    or arguments[0].kind != "string"
                    or arguments[0].value.casefold() != "userenv"
                    or arguments[1].raw != ","
                    or arguments[2].kind != "string"
                    or arguments[2].value.casefold() not in _IDENTITY_ATTRIBUTES
                    or arguments[3].raw != ")"
                ):
                    raise QueryError("SYS_CONTEXT may inspect only approved USERENV session attributes")
            if index > 0 and tokens[index - 1].raw == ".":
                raise QueryError("qualified package, member, and user-defined calls are not allowed")
        if token.kind == "quoted_identifier" and next_token.raw == "(":
            raise QueryError("quoted user-defined function calls are not allowed")

    qualifiers, source_offsets = _collect_sources(tokens)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if index > 0 and tokens[index - 1].kind == "symbol" and tokens[index - 1].raw == ":":
            index += 1
            continue
        if _identifier(token) is None or index in source_offsets:
            index += 1
            continue
        components = [_identifier(token)]
        cursor = index + 1
        while cursor + 1 < len(tokens) and tokens[cursor].raw == "." and _identifier(tokens[cursor + 1]) is not None:
            components.append(_identifier(tokens[cursor + 1]))
            cursor += 2
        if cursor < len(tokens) and tokens[cursor].raw == "." and cursor + 1 < len(tokens) and tokens[cursor + 1].raw == "*":
            if components[0] not in qualifiers:
                raise QueryError("qualified column reference does not name a selected relation")
            index = cursor + 2
            continue
        if len(components) > 1:
            prefix = ".".join(str(part) for part in components[:-1])
            if len(components) > 3 or prefix not in qualifiers:
                raise QueryError("qualified references must use a relation selected by the query")
            index = cursor
            continue
        index += 1


def _is_allowed_sql_form(name: str, index: int, tokens: Sequence[_Token]) -> bool:
    """Allow only grammar-shaped uses of words shared with migration checks.

    SAFE_SQL_FORMS is a vocabulary, not a function allowlist. In particular,
    accepting every listed word before ``(`` would make expressions such as
    FILTER(1) or CAST(value AS custom_type) look safe to this lexical gate.
    """
    if name not in SAFE_SQL_FORMS or index + 2 >= len(tokens):
        return False
    if name == "exists":
        first = tokens[index + 2]
        return first.kind == "word" and first.value in {"select", "with"}
    if name == "in":
        if index == 0:
            return False
        previous = tokens[index - 1]
        if previous.raw == ")" or previous.kind in {"quoted_identifier", "string", "number"}:
            return True
        if previous.kind != "word":
            return False
        if previous.value not in _RESERVED_BEFORE_PAREN:
            return True
        return (
            previous.value == "not"
            and index > 1
            and (tokens[index - 2].raw == ")" or _identifier(tokens[index - 2]) is not None)
        )
    if name == "over":
        if index == 0 or tokens[index - 1].raw != ")":
            return False
        depth = 1
        opening = index - 2
        while opening >= 0:
            if tokens[opening].raw == ")":
                depth += 1
            elif tokens[opening].raw == "(":
                depth -= 1
                if depth == 0:
                    break
            opening -= 1
        if opening <= 0:
            return False
        function_name = _identifier(tokens[opening - 1])
        return function_name in _ALLOWED_FUNCTIONS
    if name == "as":
        if index < 2 or _identifier(tokens[index - 1]) is None:
            return False
        previous = tokens[index - 2]
        if previous.kind == "word" and previous.value == "with":
            return True
        if previous.raw != ",":
            return False
        depth = 0
        for token in tokens[:index - 2]:
            if token.raw == "(":
                depth += 1
            elif token.raw == ")":
                depth -= 1
        # A comma starts another CTE only when an earlier WITH exists at the
        # same nesting depth and no same-level SELECT has started its body.
        for cursor in range(index - 3, -1, -1):
            candidate = tokens[cursor]
            if candidate.raw == ")":
                depth += 1
            elif candidate.raw == "(":
                depth -= 1
            elif depth == 0 and candidate.kind == "word" and candidate.value == "select":
                return False
            elif depth == 0 and candidate.kind == "word" and candidate.value == "with":
                return True
        return False
    # FILTER and WITHIN are deliberately excluded: FILTER support varies by
    # Oracle release, while WITHIN GROUP is recognized through its full syntax.
    return False

def validate_query(sql: str, *, bindings: Mapping[str, int | str] | None = None) -> None:
    validated = validate_bindings(bindings) if bindings is not None else None
    allowed = set(validated.keys()) if validated is not None else None
    tokens, _ = _scan(sql)
    _validate_tokens(tokens, allowed_binds=allowed)


def prepare_query(sql: str, *, bindings: Mapping[str, int | str] | None = None) -> str:
    validated = validate_bindings(bindings) if bindings is not None else None
    allowed = set(validated.keys()) if validated is not None else None
    tokens, parts = _scan(sql)
    _validate_tokens(tokens, allowed_binds=allowed)
    prepared = _build_prepared(parts, tokens)
    if not prepared:
        raise QueryError("query has no executable SQL")
    return prepared


def resolve_query_target(
    values: Mapping[str, str], environment: str, profile: str, schema: str | None = None
) -> Target:
    """Resolve a named profile; never accept a saved alias from the command line."""
    if profile not in {*_PROFILE_KEYS, "migration"}:
        raise QueryError("--connection must name one configured profile: tables, code, apex, ords, or migration")
    if environment not in {"dev", "staging", "prod"}:
        raise QueryError("--env must be dev, staging, or prod")
    if profile == "ords" and environment != "dev":
        raise QueryError("ORDS queries require a configured DEV ORDS owner profile")
    try:
        if environment == "dev" and profile in _PROFILE_KEYS:
            schema_key, connection_key, user_key = _PROFILE_KEYS[profile]
            if not all(values.get(key, "").strip() for key in (schema_key, connection_key, user_key)):
                raise QueryError(f"the {profile} query profile is not completely configured")
            selected_values = dict(values)
            selected_values.update({
                "CODE_SCHEMA": values[schema_key],
                "CODE_SQLCL_CONNECTION": values[connection_key],
                "CODE_EXPECTED_USER": values[user_key],
            })
            return resolve_target(selected_values, environment, "read", schema=schema)
        operation = "migration" if profile == "migration" else "read"
        return resolve_target(values, environment, operation, schema=schema)
    except TargetResolutionError as error:
        raise QueryError(str(error)) from None


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_driver(
    target: Target,
    prepared_query: str,
    marker: str,
    *,
    bindings: Mapping[str, int | str] | None = None,
    max_rows: int = MAX_RESULT_ROWS,
) -> str:
    if _MARKER_RE.fullmatch(marker) is None:
        raise QueryError("internal query marker is invalid")
    validated_bindings = validate_bindings(bindings) if bindings is not None else None
    # Target identifiers come from strict environment/profile validation. The
    # string literal in the identity check is the random per-run marker only.
    identity_attributes = (
        ("DB_NAME", "db_name"),
        ("DB_UNIQUE_NAME", "db_unique_name"),
        ("SERVICE_NAME", "service_name"),
    )
    identity_conditions = [
        "UPPER(SYS_CONTEXT('USERENV','SESSION_USER')) = " + _sql_literal(target.expected_user),
        "UPPER(SYS_CONTEXT('USERENV','CURRENT_SCHEMA')) = " + _sql_literal(target.schema),
    ]
    identity_expressions = {
        key: f"SYS_CONTEXT('USERENV','{attribute}')" for attribute, key in identity_attributes
    }
    identity_conditions.extend(
        f"TRIM({identity_expressions[key]}) IS NOT NULL" for _, key in identity_attributes
    )
    if target.environment != "prod":
        production_checks = []
        for _, key in identity_attributes:
            expression = identity_expressions[key]
            production_checks.append(
                "REGEXP_LIKE(REGEXP_REPLACE("
                + expression
                + ", "
                + _sql_literal(_SQL_NON_PRODUCTION_MARKER)
                + ", ' ', 1, 0, 'i'), "
                + _sql_literal(_SQL_PRODUCTION_MARKER)
                + ", 'i')"
            )
        identity_conditions.append("NOT (" + " OR ".join(production_checks) + ")")
    target_identity_check = (
        "SELECT CASE WHEN "
        + " AND ".join(identity_conditions)
        + " THEN 1 ELSE TO_NUMBER('INVALID_QUERY_TARGET') END FROM DUAL;"
    )
    identity_query = (
        "SELECT '" + marker + "_I:' || UPPER(SYS_CONTEXT('USERENV','SESSION_USER')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','CURRENT_SCHEMA')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','DB_NAME')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','SERVICE_NAME')) FROM DUAL;"
    )
    variable_lines: list[str] = []
    assignment_lines: list[str] = []
    if validated_bindings:
        for name in sorted(validated_bindings.keys()):
            val = validated_bindings[name]
            clean_name = name.lstrip(":")
            if isinstance(val, int):
                variable_lines.append(f"VARIABLE {clean_name} NUMBER")
                assignment_lines.append(f"  :{clean_name} := {val};")
            elif isinstance(val, str):
                variable_lines.append(f"VARIABLE {clean_name} VARCHAR2(4000)")
                hex_raw = val.encode("utf-8").hex().upper()
                assignment_lines.append(
                    f"  :{clean_name} := UTL_I18N.RAW_TO_CHAR(HEXTORAW('{hex_raw}'), 'AL32UTF8');"
                )

    lines: list[str] = [
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "SET DEFINE OFF",
        "SET ECHO OFF",
        "SET VERIFY OFF",
        "SET FEEDBACK OFF",
        "SET HEADING OFF",
        "SET PAGESIZE 0",
        "SET TRIMSPOOL ON",
        "SET TRIMOUT ON",
        "SET LINESIZE 32767",
        "SET LONG 1048576",
        "SET LONGCHUNKSIZE 32767",
        "SET SQLFORMAT DEFAULT",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "ALTER SESSION DISABLE COMMIT IN PROCEDURE;",
        "SET TRANSACTION READ ONLY;",
        target_identity_check,
        f"PROMPT {marker}_IDENTITY_BEGIN",
        identity_query,
        f"PROMPT {marker}_IDENTITY_END",
    ]
    if variable_lines:
        lines.extend(variable_lines)
        lines.append("BEGIN")
        lines.extend(assignment_lines)
        lines.append("END;")
        lines.append("/")
    lines.extend([
        f"PROMPT {marker}_DATA_BEGIN",
        "SET SQLFORMAT CSV",
        f"SELECT * FROM ( {prepared_query} ) WHERE ROWNUM <= {max_rows + 1};",
        f"PROMPT {marker}_DATA_END",
        "SET SQLFORMAT DEFAULT",
        f"PROMPT {marker}_SUCCESS",
        "EXIT SUCCESS ROLLBACK",
        "",
    ])
    return "\n".join(lines)


def _frame_positions(output: str, marker: str, frame: str) -> list[tuple[int, int]]:
    wanted = marker + frame
    matches: list[tuple[int, int]] = []
    offset = 0
    for line in output.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if content == wanted:
            matches.append((offset, offset + len(line)))
        offset += len(line)
    return matches


def parse_query_output(
    output: str, marker: str, target: Target, *, max_rows: int = MAX_RESULT_ROWS
) -> str:
    if _MARKER_RE.fullmatch(marker) is None or not isinstance(output, str):
        raise QueryError("query output framing is invalid")
    frames = {
        name: _frame_positions(output, marker, suffix)
        for name, suffix in (
            ("identity_begin", "_IDENTITY_BEGIN"),
            ("identity_end", "_IDENTITY_END"),
            ("data_begin", "_DATA_BEGIN"),
            ("data_end", "_DATA_END"),
            ("success", "_SUCCESS"),
        )
    }
    if any(len(positions) != 1 for positions in frames.values()):
        raise QueryError("query output is missing or duplicates a required verification frame")
    identity_begin = frames["identity_begin"][0]
    identity_end = frames["identity_end"][0]
    data_begin = frames["data_begin"][0]
    data_end = frames["data_end"][0]
    success = frames["success"][0]
    if not identity_begin[1] <= identity_end[0] < data_begin[0] < data_begin[1] <= data_end[0] < success[0]:
        raise QueryError("query output verification frames are out of order")
    identity_text = output[identity_begin[1] : identity_end[0]]
    identity_lines = [line.strip() for line in identity_text.splitlines() if line.strip()]
    prefix = marker + "_I:"
    if len(identity_lines) != 1 or not identity_lines[0].startswith(prefix):
        raise QueryError("SQLcl target identity frame is invalid")
    identity = identity_lines[0][len(prefix) :].split("|")
    if (
        len(identity) != 5
        or identity[0].upper() != target.expected_user.upper()
        or identity[1].upper() != target.schema.upper()
        or any(not value.strip() for value in identity[2:])
    ):
        raise QueryError("SQLcl target identity did not match the configured user, schema, database, and service")
    if target.environment != "prod" and looks_like_production_identity(*identity[2:]):
        raise QueryError("observed database or service identity resembles production for a non-prod target")
    result = output[data_begin[1] : data_end[0]]
    try:
        row_count = sum(1 for _ in csv.reader(io.StringIO(result, newline="")))
    except csv.Error:
        raise QueryError("query output is not complete CSV data") from None
    # SQLcl CSV output may include a heading row; suppressing it is required in
    # the generated driver, so every record here is a data row.
    if row_count > max_rows:
        raise QueryError(f"query result exceeded the {max_rows}-row limit; no partial result is shown")
    return result


def read_query_input(*, sql: str | None, file: str | None) -> str:
    if (sql is None) == (file is None):
        raise QueryError("provide exactly one inline SQL statement or --file PATH")
    if sql is not None:
        query = sql
    else:
        assert file is not None
        path = Path(file)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise QueryError("query file must be a regular file")
                raw = source.read(MAX_QUERY_BYTES + 1)
        except QueryError:
            raise
        except OSError:
            raise QueryError("query file could not be read") from None
        if len(raw) > MAX_QUERY_BYTES:
            raise QueryError(f"query file exceeds the {MAX_QUERY_BYTES}-byte input limit")
        try:
            query = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise QueryError("query file must contain UTF-8 SQL") from None
    validate_query(query)
    return query


def run_query(
    values: Mapping[str, str],
    environment: str,
    profile: str,
    schema: str | None,
    query: str,
    *,
    bindings: Mapping[str, int | str] | None = None,
    max_rows: int = MAX_RESULT_ROWS,
) -> str:
    prepared = prepare_query(query, bindings=bindings)
    target = resolve_query_target(values, environment, profile, schema)
    marker = "ROQ_" + secrets.token_hex(16)
    root = Path(__file__).resolve().parents[1]
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="read-only-query-", dir=scratch) as temporary:
        run_dir = Path(temporary)
        driver = run_dir / "query.sql"
        driver.write_text(
            build_driver(target, prepared, marker, bindings=bindings, max_rows=max_rows),
            encoding="utf-8",
            newline="\n",
        )
        try:
            result = run_sqlcl(
                target,
                driver,
                run_dir,
                phase="preconditions",
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                output_limit_bytes=MAX_CAPTURE_BYTES,
            )
        except SqlclError:
            raise QueryError("SQLcl did not complete within the limit; the query result is unknown and withheld") from None
        return parse_query_output(result.output, marker, target, max_rows=max_rows)


def build_parser() -> argparse.ArgumentParser:
    parser = _QueryArgumentParser(
        prog="team.sh query",
        description="Run one conservative, bounded SELECT using a configured SQLcl profile.",
    )
    parser.add_argument("--env", required=True, choices=("dev", "staging", "prod"))
    parser.add_argument("--connection", required=True, choices=("tables", "code", "apex", "ords", "migration"))
    parser.add_argument("--schema")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("sql", nargs="?")
    return parser


class _QueryArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally includes rejected positional/choice values in its
        # diagnostics. Those values may be complete SQL statements or literals.
        del message
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: invalid arguments (values suppressed); "
            "check --env, --connection, and SQL/file usage, or run --help.\n",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        query = read_query_input(sql=args.sql, file=args.file)
        env_path = Path(os.environ.get("PROJECT_ENV_FILE", Path(__file__).resolve().parents[1] / ".env"))
        try:
            values = read_project_env(env_path)
        except ConfigError:
            raise QueryError("project database configuration is missing or invalid") from None
        schema = args.schema or os.environ.get("PROJECT_SCHEMA") or None
        result = run_query(values, args.env, args.connection, schema, query)
        sys.stdout.write(result)
        if result and not result.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    except QueryError as error:
        print(f"query error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
