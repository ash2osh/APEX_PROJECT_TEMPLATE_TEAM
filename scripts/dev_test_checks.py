#!/usr/bin/env python3
"""Self-contained read-only SQL check runner and script validator for dev-test."""

from __future__ import annotations

import json
import os
import re
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .db_targets import Target
from .dev_test_bindings import validate_bindings
from .read_only_query import SAFE_FUNCTIONS, SAFE_SQL_FORMS
from .sqlcl_session import SqlclError, SqlclTimeout, driver_epilogue, run_sqlcl
from .sqlcl_transport import mask_comments_and_literals


CHECK_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
IDENTIFIER_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
SQL_TOKEN_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_$#]*|[0-9]+(?:\.[0-9]+)?|.)", re.ASCII | re.DOTALL)
HEX_CHUNK_SIZE = 3000
DEFAULT_CHECK_DRIVER_BYTES = 2 * 1024 * 1024

OBSERVED_IDENTITY_FIELDS = (
    "session_user",
    "current_schema",
    "db_name",
    "db_unique_name",
    "service_name",
    "container_id",
    "container_name",
    "edition",
    "database_version",
)

_RESERVED_BEFORE_PAREN = {
    "all", "and", "any", "between", "by", "connect", "distinct", "else", "from", "group", "having",
    "intersect", "is", "like", "minus", "not", "of", "on", "or", "order", "prior", "select", "start",
    "then", "to", "union", "unique", "where", "with",
}
_CONTEXTUAL_BEFORE_PAREN = {
    "case": None, "when": "case", "join": "from", "using": "from", "lateral": "from", "some": "from",
}
_FORBIDDEN_WORDS = {
    "alter", "begin", "commit", "create", "currval", "delete", "drop", "execute", "exec", "exit",
    "grant", "host", "insert", "lock", "merge", "nextval", "prompt", "rollback", "revoke", "set",
    "truncate", "update", "whenever",
}
_SQL_STARTERS = {
    "ALTER", "ANALYZE", "AUDIT", "CALL", "COMMENT", "CREATE", "DELETE", "DROP", "FLASHBACK",
    "GRANT", "INSERT", "LOCK", "MERGE", "NOAUDIT", "PURGE", "RENAME", "REVOKE", "SELECT",
    "TRUNCATE", "UPDATE", "WITH",
}
_LONE_TERMINATOR = re.compile(r"(?m)^[ \t]*([/.])[ \t\r]*$")
_AT_LINE = re.compile(r"(?m)^[ \t]*@")
_FIRST_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*", re.ASCII)
_STANDALONE_SLASH = re.compile(r"(?m)^[ \t]*/[ \t]*(?:\r?\n|$)")
_CURRENT_SCHEMA_CHANGE = re.compile(r"(?is)\bALTER\s+SESSION\s+SET\s+CURRENT_SCHEMA\b")


class CheckValidationError(ValueError):
    """Raised when a check query or script fails static validation."""


@dataclass(frozen=True)
class QueryCheck:
    id: str
    sql: str
    expected: int


@dataclass(frozen=True)
class CheckReport:
    passed: bool
    complete: bool
    results: tuple[dict, ...]
    errors: tuple[dict, ...]
    coverage: dict

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "complete": self.complete,
            "results": list(self.results),
            "errors": list(self.errors),
            "coverage": dict(self.coverage),
        }


class _CheckDriverTooLarge(ValueError):
    def __init__(self, check_id: str, size: int, budget: int) -> None:
        self.check_id = check_id
        super().__init__(f"check {check_id} driver ({size} bytes) exceeds the {budget}-byte budget")


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _reject_sqlcl_line_hazards(masked: str, start: int, end: int, *, plsql: bool) -> None:
    for terminator in _LONE_TERMINATOR.finditer(masked, start, end):
        line = _line_at(masked, terminator.start())
        if plsql and terminator.group(1) == ".":
            raise ValueError(f"SQL-only script rejects SQLcl buffer terminator in PL/SQL block at line {line}")
        raise ValueError(
            f"SQL-only script rejects a line holding only '{terminator.group(1)}' inside a "
            f"{'PL/SQL block' if plsql else 'SQL statement'} at line {line}; SQLcl ends the statement there"
        )
    at_line = _AT_LINE.search(masked, start, end)
    if at_line is not None:
        line = _line_at(masked, at_line.start())
        raise ValueError(
            f"SQL-only script rejects a line starting with '@' at line {line}; "
            "SQLcl would splice the named file into the statement"
        )


def statement_spans(source: str, *, allow_transaction_control: bool = False) -> list[tuple[int, int]]:
    """Validate SQL-only source and return each statement's (start, end) offsets."""
    spans: list[tuple[int, int]] = []
    quoted: list[tuple[int, int]] = []
    masked = mask_comments_and_literals(source, quoted)
    index = 0
    previous_end = 0
    while index < len(masked):
        while index < len(masked) and masked[index].isspace():
            index += 1
        gap_quoted = bisect_left(quoted, (previous_end,))
        if gap_quoted < len(quoted) and quoted[gap_quoted][1] <= index:
            raise ValueError(
                f"SQL-only script rejects a quoted value outside any statement at line "
                f"{_line_at(source, quoted[gap_quoted][0])}; SQLcl would read it as a command"
            )
        if index >= len(masked):
            break
        statement_start = index
        first_match = _FIRST_WORD.match(masked, index)
        if first_match is None:
            raise ValueError(
                f"SQL-only script rejects a client command or non-SQL token at line {_line_at(masked, statement_start)}"
            )
        word_end = first_match.end()
        first = first_match.group().upper()
        if first in {"COMMIT", "ROLLBACK"} and not allow_transaction_control:
            raise ValueError(
                f"SQL-only script does not allow transaction control ({first}) at line {_line_at(masked, statement_start)}"
            )
        transaction_control = allow_transaction_control and first in {"COMMIT", "ROLLBACK"}
        transaction_terminator = transaction_control and source[word_end:word_end + 1] == ";"
        if word_end < len(source) and source[word_end] not in " \t\r\n" and not transaction_terminator:
            raise ValueError(
                f"SQL-only script rejects '{source[statement_start:word_end + 1].strip()}' at line "
                f"{_line_at(masked, statement_start)}: put a space after '{first_match.group()}'"
            )
        tokens = _FIRST_WORD.findall(masked[index:index + 240])
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
            slash = _STANDALONE_SLASH.search(masked, index)
            if slash is None:
                raise ValueError(
                    f"SQL-only PL/SQL block must end with a standalone slash at line {_line_at(masked, statement_start)}"
                )
            _reject_sqlcl_line_hazards(masked, index, slash.start(), plsql=True)
            spans.append((statement_start, slash.start()))
            index = slash.end()
            previous_end = index
            continue
        allowed = first in _SQL_STARTERS or transaction_control
        if first == "SET":
            statement_words = _FIRST_WORD.findall(masked[index:index + 240].split(";", 1)[0])
            allowed = len(statement_words) > 1 and statement_words[1].upper() in {"CONSTRAINTS", "TRANSACTION"}
        if not allowed:
            raise ValueError(
                f"SQL-only script rejects SQLcl/client command or unsupported statement "
                f"'{first}' at line {_line_at(masked, statement_start)}"
            )
        statement_end = masked.find(";", index)
        if statement_end < 0:
            raise ValueError(
                f"SQL-only statement must end with a semicolon at line {_line_at(masked, statement_start)}"
            )
        _reject_sqlcl_line_hazards(masked, statement_start, statement_end, plsql=False)
        if transaction_control:
            transaction_text = masked[index:statement_end].strip()
            if not re.fullmatch(r"(?:COMMIT|ROLLBACK)(?:\s+WORK)?", transaction_text, re.IGNORECASE | re.ASCII):
                raise ValueError("SQL-only transaction control accepts only COMMIT or ROLLBACK [WORK]")
        spans.append((statement_start, statement_end))
        index = statement_end + 1
        previous_end = index
    return spans


def validate_sql_script(source: str, number: int = 1) -> None:
    if not source.strip():
        raise CheckValidationError(f"step {number} SQL script is empty")
    try:
        statement_spans(source, allow_transaction_control=True)
    except ValueError as error:
        raise CheckValidationError(f"step {number} SQL script is not safe SQL-only source: {error}") from error
    if _CURRENT_SCHEMA_CHANGE.search(source):
        raise CheckValidationError(f"step {number} SQL script cannot change CURRENT_SCHEMA after the identity guard")


def _strip_sql_comments_and_tokenize(source: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise CheckValidationError("check query has an unterminated block comment")
            index = end + 2
            continue
        if char in "'\"":
            quote = char
            start = index
            index += 1
            while index < len(source):
                if source[index] == quote:
                    if index + 1 < len(source) and source[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise CheckValidationError("check query has an unterminated quoted value")
            tokens.append(("STRING:" if quote == "'" else "QIDENT:") + source[start:index])
            continue
        if char in "qQ" and index + 1 < len(source) and source[index + 1] == "'":
            raise CheckValidationError("Oracle q-quoted literals are not supported in checks")
        if not char.isascii():
            raise CheckValidationError(
                f"check query has a non-ASCII character outside a quoted value: {char!r}; "
                "put the text in a string literal or quote the name"
            )
        match = SQL_TOKEN_RE.match(source, index)
        if match is None:
            raise CheckValidationError("check query contains an invalid token")
        token = match.group(0)
        if token == "@":
            raise CheckValidationError("database links are not allowed in checks")
        if token == ";":
            tokens.append(token)
        elif token.isascii() and token.isalpha() or re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", token, re.ASCII):
            tokens.append("WORD:" + token.casefold())
        elif token[0].isdigit():
            tokens.append("NUMBER:" + token)
        else:
            tokens.append(token)
        index = match.end()
    return tokens


def validate_check_query(sql: str, *, allowed_bind_names: Iterable[str] | None = None) -> None:
    """Allow one read-only SELECT/WITH-SELECT with a restricted function set."""
    if not isinstance(sql, str) or not sql.strip() or len(sql.encode("utf-8")) > 32768:
        raise CheckValidationError("check SQL must be a nonempty UTF-8 query no longer than 32768 bytes")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in sql):
        raise CheckValidationError("check SQL contains a control character")
    tokens = _strip_sql_comments_and_tokenize(sql)
    if not tokens:
        raise CheckValidationError("check SQL is empty")
    if tokens[-1] == ";":
        tokens.pop()
    if ";" in tokens or "\\" in tokens or any(
        token == "/" and (index in (0, len(tokens) - 1) or "/" in (tokens[index - 1], tokens[index + 1]))
        for index, token in enumerate(tokens)
    ):
        raise CheckValidationError("checks must contain exactly one SQL query and no SQLcl terminator")

    allowed_names = {"target_schema"}
    if allowed_bind_names is not None:
        try:
            for name in allowed_bind_names:
                if not isinstance(name, str):
                    raise CheckValidationError("allowed check binding names must be strings")
                normalized = name[1:] if name.startswith(":") else name
                if not normalized or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", normalized, re.ASCII):
                    raise CheckValidationError("allowed check binding name is invalid")
                allowed_names.add(normalized.casefold())
        except TypeError as error:
            raise CheckValidationError("allowed check binding names must be iterable strings") from error

    for index, token in enumerate(tokens):
        if token == ":" and (index + 1 >= len(tokens) or tokens[index + 1] != "WORD:target_schema"):
            if (
                index + 1 >= len(tokens)
                or not tokens[index + 1].startswith("WORD:")
                or tokens[index + 1][5:] not in allowed_names
            ):
                if allowed_bind_names is None:
                    raise CheckValidationError("checks may bind only :target_schema")
                raise CheckValidationError("check query contains an undeclared or positional bind")

    words = [token[5:] for token in tokens if token.startswith("WORD:")]
    if not words or words[0] not in {"select", "with"} or "select" not in words:
        raise CheckValidationError("checks must be one SELECT or WITH-SELECT query")
    forbidden = sorted(_FORBIDDEN_WORDS.intersection(words))
    if forbidden:
        raise CheckValidationError(f"check query contains forbidden operation/token {forbidden[0].upper()}")
    for token in tokens:
        if token in {'QIDENT:"NEXTVAL"', 'QIDENT:"CURRVAL"'}:
            raise CheckValidationError(f"check query contains forbidden operation/token {token[8:-1]}")
    if "for" in words and any(words[i + 1] == "update" for i, word in enumerate(words[:-1]) if word == "for"):
        raise CheckValidationError("FOR UPDATE is not allowed in checks")

    depth = 0
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth < 0:
                raise CheckValidationError("check query has unbalanced parentheses")
    if depth:
        raise CheckValidationError("check query has unbalanced parentheses")

    for index, token in enumerate(tokens[:-1]):
        if token.startswith("WORD:") and tokens[index + 1] == "(":
            name = token[5:]
            qualified = index > 0 and tokens[index - 1] == "."
            keyword = not qualified and (
                name in _RESERVED_BEFORE_PAREN
                or (name in _CONTEXTUAL_BEFORE_PAREN and (
                    _CONTEXTUAL_BEFORE_PAREN[name] is None
                    or f"WORD:{_CONTEXTUAL_BEFORE_PAREN[name]}" in tokens[:index]
                ))
            )
            if name not in SAFE_FUNCTIONS and name not in SAFE_SQL_FORMS and not keyword:
                raise CheckValidationError(f"check query calls unsupported or user-defined function {name.upper()}")
            if name == "sys_context":
                arguments = tokens[index + 2 : index + 6]
                if arguments != ["STRING:'USERENV'", ",", "STRING:'SESSION_USER'", ")"] and arguments != [
                    "STRING:'USERENV'", ",", "STRING:'CURRENT_SCHEMA'", ")"
                ]:
                    raise CheckValidationError("SYS_CONTEXT checks may inspect only USERENV session identity")
        if token.startswith("QIDENT:") and tokens[index + 1] == "(" and not (index > 0 and tokens[index - 1] == "."):
            raise CheckValidationError(f"check query calls unsupported or user-defined function {token[7:]}")
        if token == "." and index + 2 < len(tokens) and tokens[index + 2] == "(":
            raise CheckValidationError("schema-qualified function calls are not allowed in checks")


def _check_binds(sql: str, allowed_bind_names: Iterable[str] | None = None) -> list[str]:
    tokens = _strip_sql_comments_and_tokenize(sql)
    allowed_names = {"target_schema"}
    if allowed_bind_names is not None:
        allowed_names.update((name[1:] if name.startswith(":") else name).casefold() for name in allowed_bind_names)
    binds: list[str] = []
    for index, token in enumerate(tokens):
        if token == ":":
            if index + 1 >= len(tokens) or not tokens[index + 1].startswith("WORD:"):
                raise CheckValidationError("checks may bind only named, declared bindings")
            name = tokens[index + 1][5:]
            if name not in allowed_names:
                if allowed_bind_names is None:
                    raise CheckValidationError("checks may bind only :target_schema")
                raise CheckValidationError(f"check query contains undeclared binding :{name}")
            if name not in binds:
                binds.append(name)
    return binds


def _hex_chunks(text: str) -> list[str]:
    raw = text.encode("utf-8")
    result: list[str] = []
    offset = 0
    byte_limit = HEX_CHUNK_SIZE // 2
    while offset < len(raw):
        end = min(offset + byte_limit, len(raw))
        while end < len(raw) and raw[end] & 0xC0 == 0x80:
            end -= 1
        if end == offset:
            raise ValueError("UTF-8 check query could not be chunked safely")
        result.append(raw[offset:end].hex().upper())
        offset = end
    return result or [""]


def _render_check_driver(
    target: Target,
    checks: Sequence[QueryCheck],
    phase: str,
    *,
    bindings: Mapping[str, int | str] | None = None,
) -> str:
    if phase not in {"preconditions", "postconditions"}:
        raise ValueError("check phase must be preconditions or postconditions")
    if IDENTIFIER_RE.fullmatch(target.schema) is None:
        raise ValueError("target schema must be an uppercase Oracle identifier")
    validated_bindings = validate_bindings(bindings) if bindings is not None else None
    allowed_bind_names = set(validated_bindings) if validated_bindings is not None else None
    sql_lines = [
        "SET ENCODING UTF-8",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "ALTER SESSION DISABLE COMMIT IN PROCEDURE;",
        "SET TRANSACTION READ ONLY;",
        "DECLARE",
        "  l_payload JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_results JSON_ARRAY_T := JSON_ARRAY_T();",
        "  l_sql CLOB;",
        "  l_cursor INTEGER;",
        "  l_columns INTEGER;",
        "  l_description DBMS_SQL.DESC_TAB2;",
        "  l_rows INTEGER;",
        "  l_value NUMBER;",
        "  l_result JSON_OBJECT_T;",
        "  l_chunk VARCHAR2(32767);",
        "  l_offset INTEGER;",
        "  l_error VARCHAR2(4000);",
        "BEGIN",
        "  l_payload.put('schemaVersion', 1);",
        f"  l_payload.put('phase', '{phase}');",
        "  l_payload.put('complete', TRUE);",
        "  l_result := JSON_OBJECT_T();",
        "  l_result.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));",
        "  l_result.put('current_schema', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'));",
        "  l_result.put('db_name', SYS_CONTEXT('USERENV', 'DB_NAME'));",
        "  l_result.put('db_unique_name', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'));",
        "  l_result.put('service_name', NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>'));",
        "  l_result.put('container_id', NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0'));",
        "  l_result.put('container_name', NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB'));",
        "  l_result.put('edition', NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>'));",
        "  l_result.put('database_version', TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE));",
        "  l_payload.put('identity', l_result);",
    ]
    for check in checks:
        validate_check_query(check.sql, allowed_bind_names=allowed_bind_names)
        if not isinstance(check.id, str) or CHECK_ID_RE.fullmatch(check.id) is None:
            raise CheckValidationError("check id must use lowercase kebab-case")
        used_binds = _check_binds(check.sql, allowed_bind_names=allowed_bind_names)
        bind_statements: list[str] = []
        if "target_schema" in used_binds:
            bind_statements.append(f"DBMS_SQL.BIND_VARIABLE(l_cursor, ':target_schema', '{target.schema}');")
        if validated_bindings is not None:
            for bind_name, bind_value in validated_bindings.items():
                if bind_name.casefold() not in used_binds:
                    continue
                if isinstance(bind_value, int):
                    bind_statements.append(f"DBMS_SQL.BIND_VARIABLE(l_cursor, ':{bind_name}', {bind_value});")
                else:
                    value_hex = bind_value.encode("utf-8").hex().upper()
                    bind_statements.append(
                        f"DBMS_SQL.BIND_VARIABLE(l_cursor, ':{bind_name}', "
                        f"UTL_I18N.RAW_TO_CHAR(HEXTORAW('{value_hex}'), 'AL32UTF8'));"
                    )
        bind_statement = "\n      ".join(bind_statements) if bind_statements else "NULL;"
        sql_lines.extend([
            "  l_sql := EMPTY_CLOB();",
            "  DBMS_LOB.CREATETEMPORARY(l_sql, TRUE);",
        ])
        for chunk in _hex_chunks(check.sql.strip().rstrip(";").strip()):
            if chunk:
                sql_lines.append(
                    f"  DBMS_LOB.WRITEAPPEND(l_sql, LENGTH2(UTL_I18N.RAW_TO_CHAR(HEXTORAW('{chunk}'), 'AL32UTF8')), UTL_I18N.RAW_TO_CHAR(HEXTORAW('{chunk}'), 'AL32UTF8'));"
                )
        sql_lines.extend([
            "  l_result := JSON_OBJECT_T();",
            f"  l_result.put('id', '{check.id}');",
            "  l_result.put('row_count', 0);",
            "  l_result.put('column_count', 0);",
            "  l_result.put('numeric', FALSE);",
            "  l_value := NULL;",
            "  l_rows := 0;",
            "  l_columns := 0;",
            "  l_error := NULL;",
            "  l_cursor := DBMS_SQL.OPEN_CURSOR;",
            "  BEGIN",
            "    DBMS_SQL.PARSE(l_cursor, l_sql, DBMS_SQL.NATIVE);",
            "    DBMS_SQL.DESCRIBE_COLUMNS2(l_cursor, l_columns, l_description);",
            "    l_result.put('column_count', l_columns);",
            "    IF l_columns = 1 AND l_description(1).col_type = 2 THEN",
            "      DBMS_SQL.DEFINE_COLUMN(l_cursor, 1, l_value);",
            "      " + bind_statement,
            "      l_rows := DBMS_SQL.EXECUTE(l_cursor);",
            "      l_rows := 0;",
            "      WHILE DBMS_SQL.FETCH_ROWS(l_cursor) > 0 AND l_rows < 2 LOOP",
            "        l_rows := l_rows + 1;",
            "        IF l_rows = 1 THEN DBMS_SQL.COLUMN_VALUE(l_cursor, 1, l_value); END IF;",
            "      END LOOP;",
            "      l_result.put('row_count', l_rows);",
            "      l_result.put('numeric', TRUE);",
            "      IF l_rows = 1 AND l_value IS NOT NULL THEN l_result.put('value', l_value); END IF;",
            "    END IF;",
            "  EXCEPTION WHEN OTHERS THEN",
            "    l_error := SQLERRM;",
            "    l_result.put('error', l_error);",
            "  END;",
            "  IF DBMS_SQL.IS_OPEN(l_cursor) THEN DBMS_SQL.CLOSE_CURSOR(l_cursor); END IF;",
            "  DBMS_LOB.FREETEMPORARY(l_sql);",
            "  l_results.append(l_result);",
        ])
    sql_lines.extend([
        "  l_payload.put('results', l_results);",
        f"  DBMS_OUTPUT.PUT_LINE('CHECK_PAYLOAD_BEGIN:{phase}');",
        "  l_sql := l_payload.to_clob();",
        "  l_offset := 1;",
        "  WHILE l_offset <= DBMS_LOB.GETLENGTH(l_sql) LOOP",
        "    l_chunk := DBMS_LOB.SUBSTR(l_sql, 8000, l_offset);",
        "    DBMS_OUTPUT.PUT_LINE(l_chunk);",
        "    l_offset := l_offset + LENGTH2(l_chunk);",
        "  END LOOP;",
        f"  DBMS_OUTPUT.PUT_LINE('CHECK_PAYLOAD_END:{phase}');",
        f"  DBMS_OUTPUT.PUT_LINE('CHECK_VERIFIED:{phase}');",
        "END;",
        "/",
        driver_epilogue(False),
        "",
    ])
    return "\n".join(sql_lines)


def _driver_for_checks(
    run_dir: Path,
    target: Target,
    checks: Sequence[QueryCheck],
    phase: str,
    *,
    bindings: Mapping[str, int | str] | None = None,
) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    driver = run_dir / "dev-test-checks.sql"
    driver.write_text(_render_check_driver(target, checks, phase, bindings=bindings), encoding="utf-8", newline="\n")
    try:
        driver.chmod(0o600)
    except OSError:
        pass
    return driver


def _check_output_available(output: str, phase: str) -> bool:
    return f"CHECK_PAYLOAD_BEGIN:{phase}" in output or f"CHECK_PAYLOAD_END:{phase}" in output


def _failed_check_ids(errors: Sequence[Mapping]) -> list[str]:
    return list(dict.fromkeys(error["check"] for error in errors if isinstance(error.get("check"), str)))


def _parse_check_output(output: str, checks: Sequence[QueryCheck], phase: str) -> CheckReport:
    begin = f"CHECK_PAYLOAD_BEGIN:{phase}"
    end = f"CHECK_PAYLOAD_END:{phase}"
    sentinel = f"CHECK_VERIFIED:{phase}"
    lines = output.splitlines()
    coverage: dict = {
        "phase": phase,
        "complete": False,
        "outputAvailable": _check_output_available(output, phase),
        "failedCheckIds": [],
    }
    try:
        start = lines.index(begin)
        finish = lines.index(end, start + 1)
    except ValueError:
        return CheckReport(False, False, (), ({"code": "MISSING_FRAME", "message": "check result frame is missing or truncated"},), coverage)
    try:
        payload = json.loads("".join(lines[start + 1 : finish]))
    except json.JSONDecodeError as error:
        return CheckReport(False, False, (), ({"code": "INVALID_JSON", "message": f"check result JSON is malformed: {error}"},), coverage)
    if sentinel not in lines[finish + 1 :] or not isinstance(payload, dict) or payload.get("schemaVersion") != 1 or payload.get("phase") != phase or payload.get("complete") is not True:
        return CheckReport(False, False, (), ({"code": "UNVERIFIED_FRAME", "message": "check result is missing its verified completion sentinel"},), coverage)
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != len(checks):
        return CheckReport(False, False, (), ({"code": "RESULT_COUNT", "message": "check result count does not match the requested checks"},), coverage)
    results: list[dict] = []
    errors: list[dict] = []
    for check, result in zip(checks, raw_results, strict=True):
        if not isinstance(result, dict) or result.get("id") != check.id:
            errors.append({"code": "RESULT_IDENTITY", "check": check.id, "message": "check result identity/order does not match the requested check"})
            continue
        passed = (
            result.get("row_count") == 1
            and result.get("column_count") == 1
            and result.get("numeric") is True
            and type(result.get("value")) in {int, float}
            and result.get("value") == check.expected
            and not result.get("error")
        )
        normalized = {**result, "passed": passed}
        results.append(normalized)
        if not passed:
            message = result.get("error") or "check must return exactly one row and one numeric column equal to 1"
            failure = {
                "code": "CHECK_FAILED",
                "check": check.id,
                "expected": check.expected,
                "observedValue": normalized.get("value"),
                "message": message,
                "observed": normalized,
            }
            error_code = re.search(r"\b(?:ORA|SP2)-\d{4,5}\b", str(result.get("error", "")))
            if error_code is not None:
                failure["errorCode"] = error_code.group(0)
            errors.append(failure)
    complete = len(results) == len(checks) and not any(error["code"] == "RESULT_IDENTITY" for error in errors)
    coverage["complete"] = complete
    coverage["failedCheckIds"] = _failed_check_ids(errors)
    identity = payload.get("identity")
    if isinstance(identity, dict) and all(isinstance(value, str) for value in identity.values()):
        coverage["identity"] = dict(identity)
    return CheckReport(complete and not errors, complete, tuple(results), tuple(errors), coverage)


def _check_driver_budget() -> int:
    raw = os.environ.get("MIGRATION_CHECK_BATCH_BYTES")
    if raw is None:
        return DEFAULT_CHECK_DRIVER_BYTES
    try:
        value = int(raw.strip(), 10)
    except (AttributeError, ValueError) as error:
        raise ValueError("MIGRATION_CHECK_BATCH_BYTES must be a positive integer") from error
    if value <= 0:
        raise ValueError("MIGRATION_CHECK_BATCH_BYTES must be a positive integer")
    return value


def _partition_checks(
    target: Target,
    checks: Sequence[QueryCheck],
    phase: str,
    byte_budget: int,
    *,
    bindings: Mapping[str, int | str] | None = None,
) -> tuple[tuple[QueryCheck, ...], ...]:
    batches: list[tuple[QueryCheck, ...]] = []
    current: list[QueryCheck] = []
    for check in checks:
        candidate = (*current, check)
        candidate_size = len(_render_check_driver(target, candidate, phase, bindings=bindings).encode("utf-8"))
        if candidate_size <= byte_budget:
            current.append(check)
            continue
        if current:
            batches.append(tuple(current))
        single_size = len(_render_check_driver(target, (check,), phase, bindings=bindings).encode("utf-8"))
        if single_size > byte_budget:
            raise _CheckDriverTooLarge(check.id, single_size, byte_budget)
        current = [check]
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def run_checks(
    target: Target,
    checks: Sequence[QueryCheck],
    run_dir: Path,
    *,
    phase: str = "preconditions",
    jobs: int = 1,
    bindings: Mapping[str, int | str] | None = None,
    _runner: Callable = run_sqlcl,
) -> CheckReport:
    """Execute validated SELECT checks under a read-only SQLcl transaction."""
    if type(jobs) is not int or jobs < 1:
        return CheckReport(False, False, (), ({
            "code": "CHECK_CONFIGURATION", "message": "jobs must be a positive integer",
        },), {
            "phase": phase, "complete": False, "count": len(checks), "sessions": 0,
            "outputAvailable": False, "failedCheckIds": [],
        })
    if not checks:
        return CheckReport(True, True, (), (), {
            "phase": phase, "complete": True, "count": 0, "sessions": 0,
            "outputAvailable": True, "failedCheckIds": [],
        })
    run_dir = Path(run_dir)
    try:
        byte_budget = _check_driver_budget()
    except ValueError as error:
        return CheckReport(False, False, (), ({"code": "CHECK_CONFIGURATION", "message": str(error)},), {
            "phase": phase, "complete": False, "count": len(checks), "sessions": 0,
            "outputAvailable": False, "failedCheckIds": [],
        })
    try:
        batches = _partition_checks(target, checks, phase, byte_budget, bindings=bindings)
    except _CheckDriverTooLarge as error:
        return CheckReport(False, False, (), ({
            "code": "CHECK_TOO_LARGE", "check": error.check_id,
            "expected": next(check.expected for check in checks if check.id == error.check_id),
            "message": str(error),
        },), {
            "phase": phase, "complete": False, "count": len(checks), "sessions": 0,
            "outputAvailable": False, "failedCheckIds": [error.check_id],
        })
    except (OSError, RuntimeError, ValueError, CheckValidationError) as error:
        return CheckReport(False, False, (), ({"code": "CHECK_UNAVAILABLE", "message": str(error)},), {
            "phase": phase, "complete": False, "count": len(checks), "sessions": 0,
            "outputAvailable": False, "failedCheckIds": [],
        })

    def run_batch(index: int, batch: Sequence[QueryCheck]) -> CheckReport:
        batch_dir = run_dir if len(batches) == 1 else run_dir / f"batch-{index:03d}"
        evidence = batch_dir / "sqlcl-output.log"
        attempted = False
        try:
            driver = _driver_for_checks(batch_dir, target, batch, phase, bindings=bindings)
            attempted = True
            result = _runner(target, driver, batch_dir, phase=phase)
            report = _parse_check_output(result.output, batch, phase)
        except SqlclTimeout as error:
            report = CheckReport(False, False, (), ({
                "code": "SQLCL_TIMEOUT",
                "phase": phase,
                "timeoutSeconds": error.timeout_seconds,
                "elapsedSeconds": error.elapsed_seconds,
                "message": f"{phase} checks did not finish within {error.timeout_seconds:g} s",
            },), {
                "phase": phase, "complete": False,
                "outputAvailable": _check_output_available(error.output, phase),
                "failedCheckIds": [],
            })
        except (OSError, RuntimeError, ValueError, TypeError, CheckValidationError) as error:
            output = error.output if isinstance(error, SqlclError) else ""
            report = CheckReport(False, False, (), ({
                "code": "CHECK_UNAVAILABLE", "phase": phase, "message": str(error),
            },), {
                "phase": phase, "complete": False,
                "outputAvailable": _check_output_available(output, phase),
                "failedCheckIds": [],
            })
        coverage = {
            **report.coverage,
            "sessionAttempted": attempted,
            "evidence": str(evidence),
        }
        return CheckReport(report.passed, report.complete, report.results, report.errors, coverage)

    if jobs == 1 or len(batches) == 1:
        reports = [run_batch(index, batch) for index, batch in enumerate(batches, start=1)]
    else:
        with ThreadPoolExecutor(max_workers=min(jobs, len(batches))) as executor:
            reports = list(executor.map(lambda item: run_batch(*item), enumerate(batches, start=1)))

    errors = tuple(error for report in reports for error in report.errors)
    results = tuple(result for report in reports for result in report.results)
    evidence_paths = [report.coverage["evidence"] for report in reports]
    identities = [report.coverage.get("identity") for report in reports]
    coverage = {
        "phase": phase,
        "complete": all(report.complete for report in reports),
        "count": len(checks),
        "sessions": len(reports),
        "outputAvailable": any(report.coverage.get("outputAvailable") is True for report in reports),
        "noOutputEvidence": [
            report.coverage["evidence"] for report in reports
            if report.coverage.get("sessionAttempted") is True and report.coverage.get("outputAvailable") is not True
        ],
        "failedCheckIds": _failed_check_ids(errors),
        "sessionIdentities": identities,
        "sessionCheckIds": [[check.id for check in batch] for batch in batches],
        "sessionResultCounts": [len(report.results) for report in reports],
        "evidence": evidence_paths[0] if len(evidence_paths) == 1 else evidence_paths,
    }
    observed_identities = [identity for identity in identities if isinstance(identity, dict)]
    if observed_identities:
        coverage["identity"] = dict(observed_identities[0])
    return CheckReport(
        all(report.passed for report in reports),
        coverage["complete"],
        results,
        errors,
        coverage,
    )
