#!/usr/bin/env python3
"""Statement forms a feature checkpoint models beyond the structural analyzer.

The structural analyzer in ``migration_checks`` classifies a statement as
supported only when it can name its exact effect. Legacy migrations tolerate the
rest once the folder carries reviewed checks. A feature checkpoint has no such
exemption: the freeze gate (``feature_manifest``) and the structural preflight
(``feature_preflight``) must both understand every statement, because the
capsule's coverage claim and its generated before/after checks depend on it.

This module is the single place that recognises the extra forms, so the two
gates cannot disagree about a statement. A form is recognised only when

* it names objects of the checkpoint's own schema (a reference to another
  schema is refused with a message, never modelled),
* the whole statement is consumed by the grammar below (an unknown clause makes
  the statement unrecognised, so it stays refused), and
* the preflight can generate a catalog check that proves the effect.

Forms: ``COMMENT ON TABLE|COLUMN``, ``CREATE [OR REPLACE] [PUBLIC] SYNONYM`` and
``ALTER TABLE ... ADD [CONSTRAINT n] PRIMARY KEY|UNIQUE|CHECK|FOREIGN KEY``, plus
the direct object grant that was already modelled. Destructive or stateful
statements (DROP, RENAME, ENABLE/DISABLE, MODIFY) are deliberately not here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .migration_checks import MigrationAnalysisError, _Token, _parse_name, _tokens, _value, _word

IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z][A-Za-z0-9_$#]*)'
SYNONYM_RE = re.compile(
    rf"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(PUBLIC)\s+)?SYNONYM\s+"
    rf"({IDENT})(?:\s*\.\s*({IDENT}))?\s+FOR\s+({IDENT})(?:\s*\.\s*({IDENT}))?"
    rf"(?:\s*@\s*({IDENT}))?\s*\Z",
    re.I | re.ASCII,
)
GRANT_RE = re.compile(
    rf"\s*GRANT\s+(.+?)\s+ON\s+({IDENT})\s*\.\s*({IDENT})\s+TO\s+"
    rf"({IDENT}(?:\s*,\s*{IDENT})*)(?:\s+WITH\s+GRANT\s+OPTION)?\s*\Z",
    re.I | re.ASCII,
)
OBJECT_GRANT_RE = re.compile(
    rf"GRANT\s+(?:ALL(?:\s+PRIVILEGES)?|[A-Za-z][A-Za-z0-9_$#]*(?:\s*,\s*[A-Za-z][A-Za-z0-9_$#]*)*)"
    rf"\s+ON\s+({IDENT})\s*\.\s*({IDENT})\s+TO\s+"
    rf"{IDENT}(?:\s*,\s*{IDENT})*"
    rf"(?:\s+WITH\s+GRANT\s+OPTION)?\Z",
    re.I | re.ASCII,
)
MAX_COMMENT_CHARS = 4000
CONSTRAINT_TYPES = {"PRIMARY": "P", "UNIQUE": "U", "CHECK": "C", "FOREIGN": "R"}


class FeatureDdlError(ValueError):
    """A recognised statement names something outside the checkpoint's scope."""


def unquote_identifier(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value.upper()


def static_grant_object(statement: str, schema: str) -> tuple[str, str] | None:
    match = OBJECT_GRANT_RE.fullmatch(statement.strip())
    if match is None:
        return None
    owner, name = (unquote_identifier(match.group(index)) for index in (1, 2))
    if owner != schema:
        raise FeatureDdlError(f"object grant owner {owner!r} does not match feature checkpoint schema {schema}")
    return owner, name


def model_statement(statement: str, operation: Mapping, schema: str) -> dict | None:
    """Return the modelled operation for a statement the analyzer left open, or None.

    ``operation`` is the analyzer's verdict for the statement. The result keeps
    its location fields (``file``, ``statement_index``) and adds ``supported``.
    """
    text = statement.strip()
    kind = operation.get("kind")
    modelled: dict | None = None
    if kind == "REVIEWED_OPERATION" and text[:6].upper() == "GRANT ":
        modelled = _grant(text, schema)
    elif kind in {"OPAQUE", "UNKNOWN"}:
        upper = text.upper()
        if upper.startswith("CREATE"):
            modelled = _synonym(text, schema)
        elif upper.startswith("COMMENT"):
            modelled = _comment(text, schema)
        elif upper.startswith("ALTER"):
            modelled = _constraint(text, schema)
    if modelled is None:
        return None
    return {**operation, **modelled, "supported": True}


def operation_key(operation: Mapping) -> tuple[str, str]:
    """The (owner, name) pair a modelled operation reports in the capsule's object keys."""
    owner, name = str(operation["owner"]), operation.get("name")
    if operation.get("kind") == "ADD_CONSTRAINT":
        name = operation.get("constraint") or operation.get("table")
    return owner, str(name)


def _grant(text: str, schema: str) -> dict | None:
    grant = GRANT_RE.fullmatch(text)
    static = static_grant_object(text, schema)
    if grant is None or static is None:
        return None
    return {
        "kind": "GRANT_OBJECT", "owner": static[0], "name": static[1],
        "privileges": grant.group(1).strip().upper(),
        "grantees": tuple(unquote_identifier(value.strip()) for value in grant.group(4).split(",")),
    }


def _synonym(text: str, schema: str) -> dict | None:
    synonym = SYNONYM_RE.fullmatch(text)
    if synonym is None:
        return None
    public = synonym.group(1) is not None
    first, second = synonym.group(2), synonym.group(3)
    synonym_owner = "PUBLIC" if public else (unquote_identifier(first) if second else schema)
    synonym_name = unquote_identifier(second if second else first)
    target_first, target_second, db_link = synonym.group(4), synonym.group(5), synonym.group(6)
    target_owner = unquote_identifier(target_second and target_first or schema)
    target_name = unquote_identifier(target_second or target_first)
    if db_link:
        raise FeatureDdlError("synonym dependency through a database link is outside selected structural coverage")
    if synonym_owner not in {schema, "PUBLIC"}:
        raise FeatureDdlError("synonym owner is outside the selected schema scope")
    if target_owner != schema:
        raise FeatureDdlError("external synonym target access is not provable from selected structural coverage")
    return {
        "kind": "CREATE_SYNONYM", "owner": synonym_owner, "name": synonym_name,
        "target_owner": target_owner, "target_name": target_name,
    }


def _qualified(tokens: Sequence[_Token], index: int) -> tuple[list[str], int]:
    """Read ``a[.b[.c]]`` as identifier parts; return them with the next index."""
    parts: list[str] = []
    while index < len(tokens) and tokens[index].kind in {"WORD", "QIDENT"}:
        parts.append(_value(tokens[index]))
        index += 1
        if index + 1 < len(tokens) and tokens[index].kind == "SYMBOL" and tokens[index].value == ".":
            index += 1
            continue
        break
    return parts, index


def _comment(text: str, schema: str) -> dict | None:
    try:
        tokens = _tokens(text)
    except MigrationAnalysisError:
        return None
    if not (_word(tokens, 0, "COMMENT") and _word(tokens, 1, "ON")):
        return None
    if _word(tokens, 2, "TABLE"):
        object_type, wanted = "TABLE", {1, 2}
    elif _word(tokens, 2, "COLUMN"):
        object_type, wanted = "COLUMN", {2, 3}
    else:
        return None
    parts, index = _qualified(tokens, 3)
    if len(parts) not in wanted or not _word(tokens, index, "IS"):
        return None
    rest = tokens[index + 1 :]
    if rest and rest[-1].kind == "SYMBOL" and rest[-1].value == ";":
        rest = rest[:-1]
    if len(rest) != 1 or rest[0].kind != "LITERAL":
        return None
    raw = text[rest[0].start : rest[0].end]
    if not (raw.startswith("'") and raw.endswith("'") and len(raw) >= 2):
        return None  # N'...' and q'[...]' literals are not modelled
    comment = raw[1:-1].replace("''", "'")
    if len(comment) > MAX_COMMENT_CHARS:
        raise FeatureDdlError(f"comment is longer than {MAX_COMMENT_CHARS} characters")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in comment):
        raise FeatureDdlError("comment contains a control character")
    owner = parts[0] if len(parts) == (2 if object_type == "TABLE" else 3) else schema
    if owner != schema:
        raise FeatureDdlError(f"COMMENT ON owner {owner!r} does not match feature checkpoint schema {schema}")
    if object_type == "TABLE":
        name, column = parts[-1], None
    else:
        name, column = parts[-2], parts[-1]
    return {
        "kind": "COMMENT_ON", "owner": schema, "object_type": object_type,
        "name": name, "column": column, "comment": comment,
    }


def _identifier_list(tokens: Sequence[_Token], index: int) -> tuple[tuple[str, ...], int] | None:
    if index >= len(tokens) or tokens[index].value != "(" or tokens[index].kind != "SYMBOL":
        return None
    names: list[str] = []
    index += 1
    while True:
        if index >= len(tokens) or tokens[index].kind not in {"WORD", "QIDENT"}:
            return None
        names.append(_value(tokens[index]))
        index += 1
        if index < len(tokens) and tokens[index].kind == "SYMBOL" and tokens[index].value == ",":
            index += 1
            continue
        break
    if index >= len(tokens) or tokens[index].value != ")" or tokens[index].kind != "SYMBOL":
        return None
    if len(set(names)) != len(names):
        return None
    return tuple(names), index + 1


def _constraint(text: str, schema: str) -> dict | None:
    try:
        tokens = _tokens(text)
    except MigrationAnalysisError:
        return None
    if tokens and tokens[-1].kind == "SYMBOL" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not (_word(tokens, 0, "ALTER") and _word(tokens, 1, "TABLE")):
        return None
    try:
        owner, table, index = _parse_name(tokens, 2, schema)
    except MigrationAnalysisError:
        return None
    if not _word(tokens, index, "ADD"):
        return None
    index += 1
    constraint = None
    if _word(tokens, index, "CONSTRAINT"):
        if index + 1 >= len(tokens) or tokens[index + 1].kind not in {"WORD", "QIDENT"}:
            return None
        constraint = _value(tokens[index + 1])
        index += 2
    if index >= len(tokens) or tokens[index].kind != "WORD" or tokens[index].value not in CONSTRAINT_TYPES:
        return None
    constraint_type = CONSTRAINT_TYPES[tokens[index].value]
    word = tokens[index].value
    index += 1
    if word in {"PRIMARY", "FOREIGN"}:
        if not _word(tokens, index, "KEY"):
            return None
        index += 1

    columns: tuple[str, ...] = ()
    condition: str | None = None
    if constraint_type == "C":
        if index >= len(tokens) or tokens[index].value != "(":
            return None
        depth, end = 0, None
        for position in range(index, len(tokens)):
            if tokens[position].kind == "SYMBOL" and tokens[position].value == "(":
                depth += 1
            elif tokens[position].kind == "SYMBOL" and tokens[position].value == ")":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        if end is None or end == index + 1:
            return None
        condition = text[tokens[index].end : tokens[end].start].strip()
        index = end + 1
        if constraint is None:
            return None  # an unnamed CHECK has no catalog identity to prove; ask for a name
    else:
        parsed = _identifier_list(tokens, index)
        if parsed is None:
            return None
        columns, index = parsed

    ref_table: str | None = None
    ref_columns: tuple[str, ...] | None = None
    delete_rule = "NO ACTION"
    if constraint_type == "R":
        if not _word(tokens, index, "REFERENCES"):
            return None
        try:
            ref_owner, ref_table, index = _parse_name(tokens, index + 1, schema)
        except MigrationAnalysisError:
            return None
        if ref_owner != schema:
            raise FeatureDdlError(
                f"foreign key reference to {ref_owner}.{ref_table} is outside feature checkpoint schema {schema}"
            )
        if index < len(tokens) and tokens[index].value == "(":
            parsed = _identifier_list(tokens, index)
            if parsed is None or len(parsed[0]) != len(columns):
                return None
            ref_columns, index = parsed
        if _word(tokens, index, "ON"):
            if not _word(tokens, index + 1, "DELETE"):
                return None
            if _word(tokens, index + 2, "CASCADE"):
                delete_rule, index = "CASCADE", index + 3
            elif _word(tokens, index + 2, "SET") and _word(tokens, index + 3, "NULL"):
                delete_rule, index = "SET NULL", index + 4
            else:
                return None

    status, validated = "ENABLED", None
    seen_state: set[str] = set()
    while index < len(tokens):
        option = tokens[index].value if tokens[index].kind == "WORD" else None
        if option not in {"ENABLE", "DISABLE", "VALIDATE", "NOVALIDATE"}:
            return None
        group = "status" if option in {"ENABLE", "DISABLE"} else "validation"
        if group in seen_state:
            return None
        seen_state.add(group)
        if group == "status":
            status = "ENABLED" if option == "ENABLE" else "DISABLED"
        else:
            validated = "VALIDATED" if option == "VALIDATE" else "NOT VALIDATED"
        index += 1
    if status == "ENABLED" and validated is None:
        validated = "VALIDATED"
    if owner != schema:
        raise FeatureDdlError(f"ALTER TABLE owner {owner!r} does not match feature checkpoint schema {schema}")
    return {
        "kind": "ADD_CONSTRAINT", "owner": schema, "name": table, "table": table,
        "constraint": constraint, "constraint_type": constraint_type, "columns": columns,
        "ref_table": ref_table, "ref_columns": ref_columns, "delete_rule": delete_rule,
        "status": status, "validated": validated, "condition": condition,
    }
