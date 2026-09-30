#!/usr/bin/env python3
"""Conservative local migration analysis and read-only live checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .db_targets import Target
from .migration_manifest import (
    CHECK_ID_RE,
    Migration,
    MigrationManifestError,
    QueryCheck,
    assert_single_layout,
    _strip_sql_comments_and_tokenize,
    validate_check_query,
)
from .schema_catalog import ObjectKey, SchemaSnapshot
from .sqlcl_session import run_sqlcl
from .validate_migration import statement_spans


ROOT = Path(__file__).resolve().parents[1]
LIMITATION = (
    "This checks selected local migrations against observed live state. Other repositories' pending migrations "
    "are not visible; concurrent changes can occur after preflight."
)
COMMON_NAMESPACE_TYPES = {
    "TABLE", "VIEW", "SEQUENCE", "SYNONYM", "MATERIALIZED VIEW", "PROCEDURE", "FUNCTION",
    "PACKAGE", "PACKAGE BODY", "TYPE", "TYPE BODY", "CLUSTER", "JAVA SOURCE", "JAVA CLASS",
    "LIBRARY", "OPERATOR", "INDEXTYPE",
}
CONSTRAINT_SEGMENT_STARTERS = {
    "CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "SUPPLEMENTAL", "PERIOD",
}
IDENTIFIER_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
HEX_CHUNK_SIZE = 3000


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


@dataclass(frozen=True)
class PreflightReport:
    exit_code: int
    conflicts: tuple[dict, ...]
    errors: tuple[dict, ...]
    coverage: dict
    limitation: str = LIMITATION

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
            "coverage": dict(self.coverage),
            "limitation": self.limitation,
        }


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    start: int
    end: int


class MigrationAnalysisError(ValueError):
    """Selected SQL is malformed or cannot be scoped safely."""


def _is_word_start(char: str) -> bool:
    return char.isalpha() or char in "_$#" or ord(char) >= 128


def _is_word_continue(char: str) -> bool:
    return char.isalnum() or char in "_$#" or ord(char) >= 128


def _quoted_end(source: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(source):
        if source[index] == quote:
            if index + 1 < len(source) and source[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    raise MigrationAnalysisError("unterminated quoted identifier or string literal")


def _tokens(source: str) -> list[_Token]:
    return list(_iter_tokens(source))


def _leading_tokens(source: str) -> list[_Token]:
    """Tokens up to the first one this SQL tokenizer cannot read.

    A Java source body is not SQL, so a quote in a Java comment can end the
    SQL tokenizer early; a statement's leading words are all a unit needs.
    """
    result: list[_Token] = []
    try:
        for token in _iter_tokens(source):
            result.append(token)
    except MigrationAnalysisError:
        pass
    return result


def _iter_tokens(source: str) -> Iterator[_Token]:
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            index = length if end < 0 else end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise MigrationAnalysisError("unterminated SQL comment")
            index = end + 2
            continue
        prefix = 0
        if source[index : index + 3].lower() == "nq'":
            prefix = 3
        elif source[index : index + 2].lower() == "q'":
            prefix = 2
        if prefix:
            opening_index = index + prefix
            if opening_index >= length:
                raise MigrationAnalysisError("incomplete Oracle alternative-quoted literal")
            opening = source[opening_index]
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            terminator = closing + "'"
            end = source.find(terminator, opening_index + 1)
            if end < 0:
                raise MigrationAnalysisError("unterminated Oracle alternative-quoted literal")
            end += len(terminator)
            yield _Token("LITERAL", "", index, end)
            index = end
            continue
        if char in "nN" and index + 1 < length and source[index + 1] == "'":
            end = _quoted_end(source, index + 1, "'")
            yield _Token("LITERAL", "", index, end)
            index = end
            continue
        if char == "'":
            end = _quoted_end(source, index, "'")
            yield _Token("LITERAL", "", index, end)
            index = end
            continue
        if char == '"':
            end = _quoted_end(source, index, '"')
            value = source[index + 1 : end - 1].replace('""', '"')
            yield _Token("QIDENT", value, index, end)
            index = end
            continue
        if _is_word_start(char):
            end = index + 1
            while end < length and _is_word_continue(source[end]):
                end += 1
            yield _Token("WORD", source[index:end].upper(), index, end)
            index = end
            continue
        if char == "/":
            line_start = source.rfind("\n", 0, index) + 1
            line_end = source.find("\n", index + 1)
            if line_end < 0:
                line_end = length
            remainder = source[index + 1 : line_end].strip()
            if not source[line_start:index].strip() and (not remainder or remainder.startswith("--")):
                yield _Token("SLASH", "/", index, index + 1)
                index += 1
                continue
        yield _Token("SYMBOL", char, index, index + 1)
        index += 1


def _is_plsql_statement(tokens: Sequence[_Token]) -> bool:
    if not tokens:
        return False
    if tokens[0].value in {"BEGIN", "DECLARE"}:
        return True
    if tokens[0].value != "CREATE":
        return False
    index = 1
    if _word(tokens, index, "OR") and _word(tokens, index + 1, "REPLACE"):
        index += 2
    while index < len(tokens) and tokens[index].kind == "WORD":
        if _word(tokens, index, "AND") and (_word(tokens, index + 1, "RESOLVE") or _word(tokens, index + 1, "COMPILE")):
            index += 2
        elif any(_word(tokens, index, modifier) for modifier in ("NOFORCE", "EDITIONABLE", "NONEDITIONABLE", "FORCE")):
            index += 1
        else:
            break
    return any(_word(tokens, index, word) for word in ("PACKAGE", "PROCEDURE", "FUNCTION", "TRIGGER", "LIBRARY", "JAVA", "TYPE")) or (
        _word(tokens, index, "MLE") and _word(tokens, index + 1, "MODULE")
    )


def _split_statements(source: str) -> list[list[_Token]]:
    tokens = _tokens(source)
    result: list[list[_Token]] = []
    index = 0
    while index < len(tokens):
        if tokens[index].kind == "SLASH":
            index += 1
            continue
        start = index
        block = _is_plsql_statement(tokens[index : index + 20])
        if block:
            while index < len(tokens) and tokens[index].kind != "SLASH":
                index += 1
            if index == len(tokens):
                raise MigrationAnalysisError("PL/SQL statement has no standalone slash terminator")
            statement = tokens[start:index]
            index += 1
            if statement:
                result.append(statement)
            continue
        depth = 0
        while index < len(tokens):
            token = tokens[index]
            if token.kind == "SYMBOL" and token.value == "(":
                depth += 1
            elif token.kind == "SYMBOL" and token.value == ")":
                depth -= 1
                if depth < 0:
                    raise MigrationAnalysisError("unbalanced SQL parentheses")
            elif token.kind == "SYMBOL" and token.value == ";" and depth == 0:
                break
            index += 1
        if index == len(tokens):
            raise MigrationAnalysisError("SQL statement has no final semicolon")
        if depth:
            raise MigrationAnalysisError("unbalanced SQL parentheses")
        statement = tokens[start:index]
        if statement:
            result.append(statement)
        index += 1
    return result


def _value(token: _Token) -> str:
    if token.kind == "WORD":
        return token.value
    if token.kind == "QIDENT":
        return token.value
    raise MigrationAnalysisError("expected an ordinary or quoted Oracle identifier")


def _parse_name(tokens: Sequence[_Token], offset: int, target_schema: str) -> tuple[str, str, int]:
    if offset >= len(tokens) or tokens[offset].kind not in {"WORD", "QIDENT"}:
        raise MigrationAnalysisError("expected an Oracle object name")
    first = _value(tokens[offset])
    offset += 1
    if offset + 1 < len(tokens) and tokens[offset].kind == "SYMBOL" and tokens[offset].value == ".":
        if tokens[offset + 1].kind not in {"WORD", "QIDENT"}:
            raise MigrationAnalysisError("expected an object name after schema qualifier")
        owner, name = first, _value(tokens[offset + 1])
        offset += 2
    else:
        owner, name = target_schema, first
    return owner, name, offset


def _word(tokens: Sequence[_Token], index: int, expected: str) -> bool:
    return index < len(tokens) and tokens[index].kind == "WORD" and tokens[index].value == expected


def _skip_create_modifiers(tokens: Sequence[_Token], index: int) -> tuple[int, bool]:
    replace = False
    if _word(tokens, index, "OR") and _word(tokens, index + 1, "REPLACE"):
        replace = True
        index += 2
    changed = True
    while changed:
        changed = False
        for phrase in (("GLOBAL", "TEMPORARY"), ("PRIVATE", "TEMPORARY")):
            if all(_word(tokens, index + offset, word) for offset, word in enumerate(phrase)):
                index += len(phrase)
                changed = True
                break
        if changed:
            continue
        if any(_word(tokens, index, value) for value in ("IMMUTABLE", "BLOCKCHAIN", "EDITIONABLE", "NONEDITIONABLE", "FORCE", "EDITIONING")):
            index += 1
            changed = True
    return index, replace


def _segments(tokens: Sequence[_Token], opening: int) -> tuple[list[list[_Token]], int]:
    if opening >= len(tokens) or tokens[opening].value != "(":
        return [], opening
    depth = 0
    start = opening + 1
    segments: list[list[_Token]] = []
    for index in range(opening, len(tokens)):
        token = tokens[index]
        if token.value == "(":
            depth += 1
        elif token.value == ")":
            depth -= 1
            if depth == 0:
                segments.append(list(tokens[start:index]))
                return segments, index + 1
        elif token.value == "," and depth == 1:
            segments.append(list(tokens[start:index]))
            start = index + 1
    raise MigrationAnalysisError("unterminated parenthesized SQL clause")


def _column_names(segments: Sequence[Sequence[_Token]]) -> tuple[str, ...]:
    names: list[str] = []
    for segment in segments:
        if not segment or (segment[0].kind == "WORD" and segment[0].value in CONSTRAINT_SEGMENT_STARTERS):
            continue
        if segment[0].kind not in {"WORD", "QIDENT"}:
            raise MigrationAnalysisError("column declaration has no simple leading identifier")
        names.append(_value(segment[0]))
    return tuple(names)


def _dependencies(tokens: Sequence[_Token], target_schema: str) -> tuple[tuple[str, str], ...]:
    dependencies: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        if token.kind != "WORD" or token.value not in {"FROM", "JOIN"}:
            continue
        following = index + 1
        if following >= len(tokens) or tokens[following].value == "(":
            raise MigrationAnalysisError("view uses a subquery in FROM/JOIN; dependency analysis is incomplete")
        owner, name, _ = _parse_name(tokens, following, target_schema)
        dependencies.append((owner, name))
    return tuple(dict.fromkeys(dependencies))


def _parse_operation(statement: Sequence[_Token], target_schema: str) -> dict:
    words = [token.value if token.kind == "WORD" else "" for token in statement]
    if not statement:
        return {"kind": "EMPTY", "supported": True}
    base = {
        "statement": " ".join(words[:12]).strip(),
        "supported": False,
        "manual_checks_required": True,
        "owner": target_schema,
    }
    if _is_plsql_statement(statement):
        return {**base, "kind": "OPAQUE", "reason": "PL/SQL or a stored program unit requires reviewed checks"}

    if _word(statement, 0, "CREATE"):
        index, replace = _skip_create_modifiers(statement, 1)
        unique = False
        bitmap = False
        if _word(statement, index, "UNIQUE") or _word(statement, index, "BITMAP"):
            unique = _word(statement, index, "UNIQUE")
            bitmap = _word(statement, index, "BITMAP")
            index += 1
        object_type = None
        for candidate in ("TABLE", "VIEW", "SEQUENCE", "INDEX"):
            if _word(statement, index, candidate):
                object_type = candidate
                index += 1
                break
        if object_type is None:
            return {**base, "kind": "OPAQUE", "reason": "CREATE form is outside the structural analyzer"}
        if_not_exists = _word(statement, index, "IF") and _word(statement, index + 1, "NOT") and _word(statement, index + 2, "EXISTS")
        if if_not_exists:
            index += 3
        try:
            owner, name, index = _parse_name(statement, index, target_schema)
        except MigrationAnalysisError as error:
            return {**base, "kind": "UNKNOWN", "reason": str(error)}
        if owner != target_schema:
            return {**base, "kind": "UNKNOWN", "owner": owner, "name": name, "reason": "cross-schema migration effects are outside this target mapping"}
        namespace = "INDEX" if object_type == "INDEX" else "COMMON"
        operation = {
            **base,
            "kind": f"CREATE_{object_type}",
            "object_type": object_type,
            "owner": owner,
            "name": name,
            "key": ("INDEX" if object_type == "INDEX" else "COMMON", name),
            "namespace": namespace,
            "replace": replace,
            "if_not_exists": if_not_exists,
            "unique": unique,
            "bitmap": bitmap,
            "supported": True,
            "manual_checks_required": replace,
            "columns": (),
            "dependencies": (),
        }
        try:
            if object_type == "TABLE":
                if index >= len(statement) or statement[index].value != "(":
                    raise MigrationAnalysisError("CREATE TABLE AS SELECT or non-column table form needs reviewed checks")
                opening = index
                segments, _ = _segments(statement, opening)
                columns = _column_names(segments)
                if len(columns) != len(set(columns)):
                    raise MigrationAnalysisError("CREATE TABLE repeats a column identifier")
                operation["columns"] = columns
            elif object_type == "INDEX":
                on = next((position for position in range(index, len(statement)) if _word(statement, position, "ON")), None)
                if on is None:
                    raise MigrationAnalysisError("CREATE INDEX is missing its ON table")
                table_owner, table_name, after_table = _parse_name(statement, on + 1, target_schema)
                if table_owner != target_schema:
                    raise MigrationAnalysisError("cross-schema index target needs reviewed checks")
                operation["table"] = table_name
                if after_table >= len(statement) or statement[after_table].value != "(":
                    raise MigrationAnalysisError("CREATE INDEX has no simple indexed-column list")
                opening = after_table
                segments, _ = _segments(statement, opening)
                indexed = []
                for segment in segments:
                    if not segment or segment[0].kind not in {"WORD", "QIDENT"} or len(segment) != 1:
                        raise MigrationAnalysisError("function-based or expression index needs reviewed checks")
                    indexed.append(_value(segment[0]))
                operation["columns"] = tuple(indexed)
                operation["dependencies"] = ((table_owner, table_name),)
            elif object_type == "VIEW":
                if any(_word(statement, position, "WITH") for position in range(index, len(statement))):
                    raise MigrationAnalysisError("CREATE VIEW common-table expressions need reviewed checks")
                operation["dependencies"] = _dependencies(statement, target_schema)
        except MigrationAnalysisError as error:
            operation.update(kind="UNKNOWN", supported=False, reason=str(error), manual_checks_required=True)
        return operation

    if _word(statement, 0, "ALTER") and _word(statement, 1, "TABLE"):
        try:
            owner, table_name, index = _parse_name(statement, 2, target_schema)
            if owner != target_schema:
                raise MigrationAnalysisError("cross-schema ALTER TABLE is outside this target mapping")
            if _word(statement, index, "ADD"):
                index += 1
                if _word(statement, index, "COLUMN"):
                    index += 1
                if index < len(statement) and statement[index].value == "(":
                    segments, _ = _segments(statement, index)
                else:
                    segments = [statement[index:]]
                if any(segment and segment[0].kind == "WORD" and segment[0].value in CONSTRAINT_SEGMENT_STARTERS for segment in segments):
                    raise MigrationAnalysisError("ALTER TABLE ADD constraint forms need reviewed checks")
                names = _column_names(segments)
                if not names:
                    raise MigrationAnalysisError("ALTER TABLE ADD is not a simple column addition")
                return {
                    **base,
                    "kind": "ALTER_ADD_COLUMN",
                    "object_type": "COLUMN",
                    "owner": owner,
                    "table": table_name,
                    "columns": names,
                "name": table_name,
                "key": ("COLUMN", table_name),
                    "namespace": "COLUMN",
                    "supported": True,
                    "manual_checks_required": False,
                }
            return {**base, "kind": "REVIEWED_ALTER", "owner": owner, "table": table_name, "name": table_name, "reason": "ALTER TABLE form requires an explicit reviewed precondition and postcondition"}
        except MigrationAnalysisError as error:
            return {**base, "kind": "UNKNOWN", "reason": str(error)}

    if any(_word(statement, 0, word) for word in ("DROP", "RENAME", "TRUNCATE", "INSERT", "UPDATE", "DELETE", "MERGE", "GRANT", "REVOKE")):
        return {**base, "kind": "REVIEWED_OPERATION", "reason": "destructive, data, or privilege operation requires explicit reviewed checks"}
    return {**base, "kind": "OPAQUE", "reason": "SQL form is outside the structural analyzer"}


def analyze_batch(migrations: Sequence[Migration], target_schema: str) -> tuple[dict, ...]:
    """Extract ordered supported effects; do not inspect unselected folders."""
    if IDENTIFIER_RE.fullmatch(target_schema) is None:
        raise MigrationAnalysisError("target schema must be an uppercase Oracle identifier")
    operations: list[dict] = []
    for migration in migrations:
        for file in migration.files:
            source = file.source.decode("utf-8")
            try:
                statements = _split_statements(source)
            except MigrationAnalysisError as error:
                statements = []
                operations.append({
                    "kind": "UNKNOWN", "supported": False, "manual_checks_required": True,
                    "reason": str(error), "migration": migration.folder.name, "file": file.name,
                    "sequence": file.sequence, "statement_index": 1, "owner": target_schema,
                })
            for index, statement in enumerate(statements, start=1):
                operation = _parse_operation(statement, target_schema)
                if operation.get("kind") == "EMPTY":
                    continue
                operation.update(
                    migration=migration.folder.name,
                    migration_path=migration.folder.as_posix(),
                    file=file.name,
                    sequence=file.sequence,
                    statement_index=index,
                )
                operations.append(operation)
    return tuple(operations)


def _stored_unit_type(statement: Sequence[_Token], index: int, *, named: bool) -> tuple[str | None, int]:
    """Read a stored unit's object type at index; named expects CREATE JAVA SOURCE's NAMED."""
    if _word(statement, index, "JAVA") and _word(statement, index + 1, "SOURCE"):
        index += 2
        if named:
            if not _word(statement, index, "NAMED"):
                return None, index
            index += 1
        return "JAVA SOURCE", index
    if _word(statement, index, "MLE") and _word(statement, index + 1, "MODULE"):
        return "MLE MODULE", index + 2
    for candidate in ("PACKAGE", "TYPE"):
        if _word(statement, index, candidate):
            if _word(statement, index + 1, "BODY"):
                return f"{candidate} BODY", index + 2
            return candidate, index + 1
    for candidate in ("PROCEDURE", "FUNCTION", "TRIGGER", "VIEW", "LIBRARY"):
        if _word(statement, index, candidate):
            return candidate, index + 1
    return None, index


def _unit_name(statement: Sequence[_Token], index: int, schema: str) -> tuple[str, str, int] | None:
    if _word(statement, index, "IF") and _word(statement, index + 1, "NOT") and _word(statement, index + 2, "EXISTS"):
        index += 3
    elif _word(statement, index, "IF") and _word(statement, index + 1, "EXISTS"):
        index += 2
    try:
        return _parse_name(statement, index, schema)
    except MigrationAnalysisError:
        return None


def _compiled_unit(statement: Sequence[_Token], schema: str) -> list[tuple[str, str, str, bool]]:
    """Name the stored units a CREATE or ALTER ... COMPILE statement compiles.

    Each unit is (owner, type, name, required). A plain ALTER PACKAGE or ALTER
    TYPE ... COMPILE also recompiles a body when one exists, so that body is
    checked but not required to exist.
    """
    if not statement or statement[0].kind != "WORD":
        return []
    if statement[0].value == "CREATE":
        index = 1
        if _word(statement, index, "OR") and _word(statement, index + 1, "REPLACE"):
            index += 2
        while True:
            if _word(statement, index, "AND") and (_word(statement, index + 1, "RESOLVE") or _word(statement, index + 1, "COMPILE")):
                index += 2
            elif _word(statement, index, "NO") and _word(statement, index + 1, "FORCE"):
                index += 2
            elif any(_word(statement, index, modifier) for modifier in ("NOFORCE", "FORCE", "EDITIONABLE", "NONEDITIONABLE", "EDITIONING")):
                index += 1
            else:
                break
        alter = False
    elif statement[0].value == "ALTER":
        index = 1
        alter = True
    else:
        return []
    object_type, index = _stored_unit_type(statement, index, named=not alter)
    if object_type is None:
        return []
    parsed = _unit_name(statement, index, schema)
    if parsed is None:
        return []
    owner, name, index = parsed
    if alter:
        words = [token.value for token in statement[index:] if token.kind == "WORD"]
        if "COMPILE" not in words:
            return []
        after = words[words.index("COMPILE") + 1 :]
        if after[:1] == ["DEBUG"]:
            after = after[1:]
        if object_type in {"PACKAGE", "TYPE"}:
            if after[:1] == ["BODY"]:
                return [(owner, f"{object_type} BODY", name, True)]
            if after[:1] == ["SPECIFICATION"]:
                return [(owner, object_type, name, True)]
            return [(owner, object_type, name, True), (owner, f"{object_type} BODY", name, False)]
    return [(owner, object_type, name, True)]


def _dropped_units(statement: Sequence[_Token], schema: str) -> list[tuple[str, str, str]]:
    """Name the stored units a DROP statement removes."""
    if not _word(statement, 0, "DROP"):
        return []
    object_type, index = _stored_unit_type(statement, 1, named=False)
    if object_type is None:
        return []
    parsed = _unit_name(statement, index, schema)
    if parsed is None:
        return []
    owner, name, _index = parsed
    if object_type in {"PACKAGE", "TYPE"}:
        return [(owner, object_type, name), (owner, f"{object_type} BODY", name)]
    return [(owner, object_type, name)]


def _current_schema_change(statement: Sequence[_Token]) -> str | None:
    """The schema an ALTER SESSION SET CURRENT_SCHEMA statement switches to."""
    if not (_word(statement, 0, "ALTER") and _word(statement, 1, "SESSION") and _word(statement, 2, "SET")):
        return None
    for index in range(3, len(statement) - 2):
        if (
            _word(statement, index, "CURRENT_SCHEMA")
            and statement[index + 1].kind == "SYMBOL"
            and statement[index + 1].value == "="
            and statement[index + 2].kind in {"WORD", "QIDENT"}
        ):
            return statement[index + 2].value
    return None


def compiled_units(migration: Migration, target_schema: str) -> tuple[tuple[str, str, str, bool], ...]:
    """Stored units (owner, type, name, required) the migration's own statements compile.

    SQLcl reports their compilation errors as warnings, so the apply session
    checks ALL_ERRORS for exactly these units, never for unrelated objects a
    teammate may be compiling at the same time. Statements are split where the
    SQL-only migration validator splits them. Files run in one session, so an
    ALTER SESSION SET CURRENT_SCHEMA carries into later files, and a unit the
    migration drops again is not checked.
    """
    schema = target_schema
    units: dict[tuple[str, str, str], bool] = {}
    for file in migration.files:
        try:
            source = file.source.decode("utf-8")
            spans = statement_spans(source)
        except (UnicodeError, ValueError) as error:
            raise MigrationAnalysisError(f"the compile check cannot read {file.name}: {error}") from error
        for start, end in spans:
            statement = _leading_tokens(source[start:end])
            changed = _current_schema_change(statement)
            if changed is not None:
                schema = changed
                continue
            for unit in _dropped_units(statement, schema):
                units.pop(unit, None)
            for owner, object_type, name, required in _compiled_unit(statement, schema):
                key = (owner, object_type, name)
                units[key] = units.get(key, False) or required
    return tuple((*key, required) for key, required in units.items())


def _check_bind_target(sql: str) -> bool:
    tokens = _strip_sql_comments_and_tokenize(sql)
    binds = []
    for index, token in enumerate(tokens):
        if token == ":":
            if index + 1 >= len(tokens) or tokens[index + 1] != "WORD:target_schema":
                raise MigrationManifestError("checks may bind only :target_schema")
            binds.append(index)
    return bool(binds)


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


def _driver_for_checks(run_dir: Path, target: Target, checks: Sequence[QueryCheck], phase: str) -> Path:
    if phase not in {"preconditions", "postconditions"}:
        raise ValueError("check phase must be preconditions or postconditions")
    if IDENTIFIER_RE.fullmatch(target.schema) is None:
        raise ValueError("target schema must be an uppercase Oracle identifier")
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    sql_lines = [
        "SET ENCODING UTF-8",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
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
        "  l_chunk VARCHAR2(30000);",
        "  l_offset INTEGER;",
        "  l_error VARCHAR2(4000);",
        "BEGIN",
        "  l_payload.put('schemaVersion', 1);",
        f"  l_payload.put('phase', '{phase}');",
        "  l_payload.put('complete', TRUE);",
        # The session reports where it ran so the caller can refuse results
        # observed on a different database than the migration target.
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
        validate_check_query(check.sql)
        if not isinstance(check.id, str) or CHECK_ID_RE.fullmatch(check.id) is None:
            raise MigrationManifestError("check id must use lowercase kebab-case")
        bind_target = _check_bind_target(check.sql)
        check_id = check.id
        bind_statement = (
            f"DBMS_SQL.BIND_VARIABLE(l_cursor, ':target_schema', '{target.schema}');"
            if bind_target else "NULL;"
        )
        # Manifest IDs are constrained to lowercase kebab-case.
        sql_lines.extend([
            "  l_sql := EMPTY_CLOB();",
            "  DBMS_LOB.CREATETEMPORARY(l_sql, TRUE);",
        ])
        for chunk in _hex_chunks(check.sql.strip().rstrip(";").strip()):
            if chunk:
                sql_lines.append(
                    f"  DBMS_LOB.WRITEAPPEND(l_sql, LENGTH(UTL_I18N.RAW_TO_CHAR(HEXTORAW('{chunk}'), 'AL32UTF8')), UTL_I18N.RAW_TO_CHAR(HEXTORAW('{chunk}'), 'AL32UTF8'));"
                )
        sql_lines.extend([
            "  l_result := JSON_OBJECT_T();",
            f"  l_result.put('id', '{check_id}');",
            "  l_result.put('row_count', 0);",
            "  l_result.put('column_count', 0);",
            "  l_result.put('numeric', FALSE);",
            "  l_value := NULL;",
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
        "  DBMS_OUTPUT.PUT_LINE('CHECK_PAYLOAD_BEGIN:" + phase + "');",
        "  l_sql := l_payload.to_clob();",
        "  l_offset := 1;",
        "  WHILE l_offset <= DBMS_LOB.GETLENGTH(l_sql) LOOP",
        "    l_chunk := DBMS_LOB.SUBSTR(l_sql, 30000, l_offset);",
        "    DBMS_OUTPUT.PUT_LINE(l_chunk);",
        "    l_offset := l_offset + LENGTH(l_chunk);",
        "  END LOOP;",
        "  DBMS_OUTPUT.PUT_LINE('CHECK_PAYLOAD_END:" + phase + "');",
        "  DBMS_OUTPUT.PUT_LINE('CHECK_VERIFIED:" + phase + "');",
        "END;",
        "/",
        "EXIT SUCCESS ROLLBACK",
        "",
    ])
    driver = run_dir / "migration-checks.sql"
    driver.write_text("\n".join(sql_lines), encoding="utf-8", newline="\n")
    try:
        driver.chmod(0o600)
    except OSError:
        pass
    return driver


def _parse_check_output(output: str, checks: Sequence[QueryCheck], phase: str) -> CheckReport:
    begin = f"CHECK_PAYLOAD_BEGIN:{phase}"
    end = f"CHECK_PAYLOAD_END:{phase}"
    sentinel = f"CHECK_VERIFIED:{phase}"
    lines = output.splitlines()
    try:
        start = lines.index(begin)
        finish = lines.index(end, start + 1)
    except ValueError:
        return CheckReport(False, False, (), ({"code": "MISSING_FRAME", "message": "check result frame is missing or truncated"},), {"phase": phase, "complete": False})
    try:
        payload = json.loads("".join(lines[start + 1 : finish]))
    except json.JSONDecodeError as error:
        return CheckReport(False, False, (), ({"code": "INVALID_JSON", "message": f"check result JSON is malformed: {error}"},), {"phase": phase, "complete": False})
    if sentinel not in lines[finish + 1 :] or not isinstance(payload, dict) or payload.get("schemaVersion") != 1 or payload.get("phase") != phase or payload.get("complete") is not True:
        return CheckReport(False, False, (), ({"code": "UNVERIFIED_FRAME", "message": "check result is missing its verified completion sentinel"},), {"phase": phase, "complete": False})
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != len(checks):
        return CheckReport(False, False, (), ({"code": "RESULT_COUNT", "message": "check result count does not match the requested checks"},), {"phase": phase, "complete": False})
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
            errors.append({"code": "CHECK_FAILED", "check": check.id, "message": result.get("error") or "check must return exactly one row and one numeric column equal to 1", "observed": normalized})
    complete = len(results) == len(checks) and not any(error["code"] == "RESULT_IDENTITY" for error in errors)
    coverage: dict = {"phase": phase, "complete": complete}
    identity = payload.get("identity")
    if isinstance(identity, dict) and all(isinstance(value, str) for value in identity.values()):
        coverage["identity"] = dict(identity)
    return CheckReport(complete and not errors, complete, tuple(results), tuple(errors), coverage)


def run_checks(
    target: Target,
    checks: Sequence[QueryCheck],
    run_dir: Path,
    *,
    phase: str = "preconditions",
    _runner: Callable = run_sqlcl,
) -> CheckReport:
    """Execute validated SELECT checks under a read-only SQLcl transaction."""
    if not checks:
        return CheckReport(True, True, (), (), {"phase": phase, "complete": True, "count": 0})
    try:
        driver = _driver_for_checks(Path(run_dir), target, checks, phase)
        result = _runner(target, driver, Path(run_dir))
    except (OSError, RuntimeError, ValueError, MigrationManifestError) as error:
        return CheckReport(False, False, (), ({"code": "CHECK_UNAVAILABLE", "message": str(error)},), {"phase": phase, "complete": False})
    return _parse_check_output(result.output, checks, phase)


def _initial_objects(snapshot: SchemaSnapshot) -> dict[str, set[str]]:
    owner = str(snapshot.identity.get("current_schema", ""))
    found: dict[str, set[str]] = {}
    for key in snapshot.inventory:
        if key.owner == owner and key.object_type in COMMON_NAMESPACE_TYPES:
            found.setdefault(key.name, set()).add(key.object_type)
    return found


def _table_columns(snapshot: SchemaSnapshot, name: str) -> set[str] | None:
    owner = str(snapshot.identity.get("current_schema", ""))
    key = ObjectKey(owner, name, "TABLE")
    definition = snapshot.objects.get(key)
    if definition is None:
        return None
    columns = definition.attributes.get("columns")
    if not isinstance(columns, list):
        return None
    result = set()
    for column in columns:
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            return None
        result.add(column["name"])
    return result


def _has_explicit_reviews(migration: Migration) -> bool:
    return bool(migration.preconditions and migration.postconditions)


def preflight(migrations: Sequence[Migration], snapshot: SchemaSnapshot, checks: CheckReport) -> PreflightReport:
    """Compare only the selected effects with a complete current schema observation."""
    conflicts: list[dict] = []
    errors: list[dict] = []
    coverage = {
        "complete": True,
        "analysis": "migration-sql-v1",
        "live_inventory": dict(snapshot.coverage),
        "selected_migrations": [migration.folder.name for migration in migrations],
        "limitations": [LIMITATION],
    }
    if not migrations:
        errors.append({"code": "NO_MIGRATIONS", "message": "select one or more migration folders"})
    if not checks.complete:
        errors.extend(checks.errors or ({"code": "CHECKS_INCOMPLETE", "message": "live checks are incomplete"},))
        coverage["complete"] = False
    if not checks.passed:
        conflicts.extend(error for error in checks.errors if error.get("code") == "CHECK_FAILED")
        if not conflicts and checks.complete:
            conflicts.append({"code": "CHECK_FAILED", "message": "one or more live migration checks failed"})
    owner = str(snapshot.identity.get("current_schema", ""))
    if not owner or snapshot.coverage.get("ownerComplete") is not True or "ALL_OBJECTS" not in snapshot.coverage.get("catalogs", []):
        errors.append({"code": "INCOMPLETE_VISIBILITY", "message": "a complete ALL_OBJECTS inventory for the target schema is required"})
        coverage["complete"] = False
    if any(migration.folder.name == "" for migration in migrations):
        errors.append({"code": "MIGRATION_IDENTITY", "message": "selected migration folder identity is incomplete"})

    try:
        operations = analyze_batch(migrations, owner)
    except (MigrationAnalysisError, MigrationManifestError, UnicodeError) as error:
        operations = ()
        errors.append({"code": "ANALYSIS_INCOMPLETE", "message": str(error)})
        coverage["complete"] = False

    selected_names = [migration.folder.name for migration in migrations]
    if len(selected_names) != len(set(selected_names)):
        errors.append({"code": "DUPLICATE_SELECTION", "message": "a migration folder was selected more than once"})

    live_common = _initial_objects(snapshot)
    live_exact = {(key.name, key.object_type) for key in snapshot.inventory}
    staged_common: set[str] = set()
    staged_indexes: set[str] = set()
    staged_tables: dict[str, set[str]] = {}
    coverage["operations"] = []

    migrations_by_name = {migration.folder.name: migration for migration in migrations}
    for operation in operations:
        coverage["operations"].append({
            "migration": operation.get("migration"), "file": operation.get("file"),
            "sequence": operation.get("sequence"), "kind": operation.get("kind"),
            "supported": operation.get("supported", False),
        })
        migration = migrations_by_name.get(operation.get("migration"))
        if not operation.get("supported") or operation.get("kind") in {"OPAQUE", "UNKNOWN", "REVIEWED_OPERATION", "REVIEWED_ALTER"}:
            if migration is None or not _has_explicit_reviews(migration):
                errors.append({"code": "UNSUPPORTED_OPERATION", "migration": operation.get("migration"), "file": operation.get("file"), "reason": operation.get("reason", "SQL effect needs explicit reviewed preconditions and postconditions")})
                coverage["complete"] = False
            else:
                coverage.setdefault("manual_review_required", []).append({"migration": migration.folder.name, "file": operation.get("file"), "reason": operation.get("reason", "opaque SQL")})
            continue

        kind = operation["kind"]
        name = operation.get("name")
        migration_name = operation.get("migration")
        location = {"migration": migration_name, "file": operation.get("file"), "statement_index": operation.get("statement_index")}
        if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE"}:
            if name in staged_common:
                conflicts.append({"code": "BATCH_NAMESPACE_COLLISION", "name": name, "namespace": "COMMON", **location})
                continue
            existing = live_common.get(name, set())
            if existing:
                if kind == "CREATE_VIEW" and operation.get("replace") and existing == {"VIEW"} and migration is not None and migration.preconditions:
                    coverage.setdefault("manual_review_required", []).append({"migration": migration_name, "file": operation.get("file"), "reason": "CREATE OR REPLACE VIEW needs its declared precondition reviewed against the expected prior view"})
                else:
                    conflicts.append({"code": "LIVE_NAMESPACE_OCCUPIED", "name": name, "namespace": "COMMON", "existing_types": sorted(existing), **location})
                    continue
            staged_common.add(name)
            if kind == "CREATE_TABLE":
                staged_tables[name] = set(operation.get("columns", ()))
        elif kind == "CREATE_INDEX":
            if name in staged_indexes or (name, "INDEX") in live_exact:
                conflicts.append({"code": "LIVE_OR_BATCH_INDEX_COLLISION", "name": name, "namespace": "INDEX", **location})
                continue
            table_name = operation.get("table")
            columns = staged_tables.get(table_name)
            if columns is None:
                if (table_name, "TABLE") not in live_exact:
                    conflicts.append({"code": "MISSING_PREREQUISITE", "name": table_name, "required_by": name, **location})
                    continue
                columns = _table_columns(snapshot, table_name)
                if columns is None:
                    errors.append({"code": "UNKNOWN_TABLE_COLUMNS", "name": table_name, "required_by": name, **location})
                    coverage["complete"] = False
                    continue
            missing_columns = sorted(set(operation.get("columns", ())) - columns)
            if missing_columns:
                conflicts.append({"code": "MISSING_INDEX_COLUMNS", "name": name, "table": table_name, "columns": missing_columns, **location})
                continue
            staged_indexes.add(name)
        elif kind == "CREATE_VIEW":
            pass
        if kind == "CREATE_VIEW":
            for dependency_owner, dependency_name in operation.get("dependencies", ()):
                if dependency_owner != owner:
                    errors.append({"code": "EXTERNAL_VIEW_DEPENDENCY", "name": dependency_name, "view": name, **location})
                    coverage["complete"] = False
                elif dependency_name.upper() == "DUAL":
                    continue
                elif dependency_name not in staged_common and dependency_name not in live_common:
                    conflicts.append({"code": "MISSING_PREREQUISITE", "name": dependency_name, "required_by": name, **location})
        elif kind == "ALTER_ADD_COLUMN":
            table_name = operation["table"]
            columns = staged_tables.get(table_name)
            if columns is None:
                if (table_name, "TABLE") not in live_exact:
                    conflicts.append({"code": "MISSING_PREREQUISITE", "name": table_name, "required_by": operation.get("columns"), **location})
                    continue
                columns = _table_columns(snapshot, table_name)
                if columns is None:
                    errors.append({"code": "UNKNOWN_TABLE_COLUMNS", "name": table_name, **location})
                    coverage["complete"] = False
                    continue
                staged_tables[table_name] = set(columns)
            for column in operation["columns"]:
                if column in columns:
                    conflicts.append({"code": "COLUMN_ALREADY_EXISTS", "table": table_name, "column": column, **location})
                else:
                    columns.add(column)

    coverage["structural_operations"] = sum(bool(operation.get("supported")) for operation in operations)
    coverage["opaque_operations"] = sum(not bool(operation.get("supported")) for operation in operations)
    if not coverage["complete"] or errors:
        exit_code = 2
    elif conflicts:
        exit_code = 1
    else:
        exit_code = 0
    return PreflightReport(exit_code, tuple(conflicts), tuple(errors), coverage)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--env", choices=("dev", "staging", "prod"))
    parser.add_argument("--schema")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if not args.folders:
        print("usage: scripts/team.sh check-conflicts <migration-folder> [...] (--env dev|staging|prod | --local)", file=sys.stderr)
        return 2
    if bool(args.env) == bool(args.local):
        print("preflight error: select exactly one of --env dev|staging|prod or --local", file=sys.stderr)
        return 2
    try:
        from .migration_manifest import load_batch

        migrations = load_batch(args.repo_root, args.folders)
    except MigrationManifestError as error:
        print(f"preflight error: {error}", file=sys.stderr)
        return 2
    if args.local:
        report = _local_report(migrations, args.repo_root)
    else:
        report = _live_report(migrations, args.env, args.repo_root, args.schema)
    rendered = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) if args.format == "json" else _render_preflight(report, args.local)
    print(rendered)
    return report.exit_code


def _local_report(migrations: Sequence[Migration], repo_root: Path) -> PreflightReport:
    owner = "LOCAL_SCOPE"
    empty = SchemaSnapshot({"current_schema": owner}, {}, {}, {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]}, "", "")
    result = preflight(migrations, empty, CheckReport(True, True, (), (), {"complete": True}))
    unknown_live = [conflict for conflict in result.conflicts if conflict.get("code") == "MISSING_PREREQUISITE"]
    conflicts = tuple(conflict for conflict in result.conflicts if conflict.get("code") != "MISSING_PREREQUISITE")
    errors = list(result.errors)
    if unknown_live:
        errors.append({"code": "LIVE_PREREQUISITE_UNKNOWN", "message": "local-only analysis cannot establish whether referenced tables or columns exist", "items": unknown_live})
    coverage = {**result.coverage, "mode": "local-only", "live_state_checked": False, "repo_root": str(repo_root)}
    return PreflightReport(2 if errors else (1 if conflicts else 0), conflicts, tuple(errors), coverage)


def _live_report(migrations: Sequence[Migration], environment: str, repo_root: Path, schema: str | None = None) -> PreflightReport:
    from .db_targets import TargetResolutionError, batch_schema, flat_migrations_apply, resolve_target
    from .schema_catalog import CatalogError, capture_inventory, capture_snapshot

    values = os.environ
    try:
        requested = schema or values.get("PROJECT_SCHEMA") or None
        chosen = batch_schema([migration.schema for migration in migrations], requested, values)
        target = resolve_target(values, environment, "read", schema=chosen)
        assert_single_layout(repo_root, migrations, target.schema, flat_folders_apply=flat_migrations_apply(values))
        work_dir = repo_root / "scratch" / "migration-preflight"
        # Validate operation scope and dependencies before opening SQLcl.
        operations = analyze_batch(migrations, target.schema)
        inventory = capture_inventory(target, work_dir)
        keys = sorted({
            (operation.get("table"), "TABLE") for operation in operations if operation.get("table")
        } | {
            (operation.get("name"), operation.get("object_type")) for operation in operations if operation.get("name") and operation.get("object_type") in {"TABLE", "VIEW", "SEQUENCE", "INDEX"}
        })
        snapshot = capture_snapshot(target, inventory, keys, work_dir) if keys else SchemaSnapshot(inventory.identity, inventory.objects, {}, inventory.coverage, inventory.started_at, inventory.completed_at)
        checks = tuple(check for migration in migrations for check in migration.preconditions)
        checks_result = run_checks(target, checks, work_dir, phase="preconditions")
        return preflight(migrations, snapshot, checks_result)
    except MigrationManifestError as error:
        return PreflightReport(2, (), ({"code": "MIGRATION_LAYOUT", "message": str(error)},), {"complete": False, "mode": "live", "environment": environment, "limitations": [LIMITATION]})
    except (TargetResolutionError, CatalogError, OSError, RuntimeError, MigrationAnalysisError) as error:
        return PreflightReport(2, (), ({"code": "LIVE_PREFLIGHT_UNAVAILABLE", "message": str(error)},), {"complete": False, "mode": "live", "environment": environment, "limitations": [LIMITATION]})


def _render_preflight(report: PreflightReport, local: bool) -> str:
    mode = "Local selected-batch analysis only; no live database state was checked." if local else "Live selected-batch preflight against observed schema state."
    lines = [mode, report.limitation, f"Result: exit {report.exit_code}; {len(report.conflicts)} conflict(s), {len(report.errors)} incomplete/error condition(s)"]
    for conflict in report.conflicts:
        lines.append(f"CONFLICT [{conflict.get('code', 'UNKNOWN')}]: {conflict.get('name') or conflict.get('message') or conflict}")
    for error in report.errors:
        lines.append(f"INCOMPLETE [{error.get('code', 'UNKNOWN')}]: {error.get('message') or error.get('reason') or error}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
