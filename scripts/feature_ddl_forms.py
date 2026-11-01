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

Forms: ``COMMENT ON TABLE|COLUMN``, ``CREATE [OR REPLACE] [PUBLIC] SYNONYM``,
``ALTER TABLE ... ADD [CONSTRAINT n] PRIMARY KEY|UNIQUE|CHECK|FOREIGN KEY``,
``CREATE [OR REPLACE] VIEW`` (including CTEs, inline views, and subqueries),
``ALTER TABLE ... MODIFY (column ...)``, function-based/expression
``CREATE [UNIQUE] INDEX``, plus direct object grants. Destructive or stateful
statements (DROP, RENAME, ENABLE/DISABLE, TRUNCATE) are deliberately not here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .migration_checks import (
    MigrationAnalysisError,
    _Token,
    _parse_name,
    _skip_create_modifiers,
    _tokens,
    _value,
    _word,
)

IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z][A-Za-z0-9_$#]*)'
SYNONYM_RE = re.compile(
    rf"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(PUBLIC)\s+)?SYNONYM\s+"
    rf"({IDENT})(?:\s*\.\s*({IDENT}))?\s+FOR\s+({IDENT})(?:\s*\.\s*({IDENT}))?"
    rf"(?:\s*@\s*({IDENT}))?\s*\Z",
    re.I | re.ASCII,
)
GRANT_RE = re.compile(
    rf"\s*GRANT\s+(.+?)\s+ON\s+(?:({IDENT})\s*\.\s*)?({IDENT})\s+TO\s+"
    rf"({IDENT}(?:\s*,\s*{IDENT})*)(?:\s+WITH\s+GRANT\s+OPTION)?\s*\Z",
    re.I | re.ASCII,
)
MAX_COMMENT_CHARS = 4000
CONSTRAINT_TYPES = {"PRIMARY": "P", "UNIQUE": "U", "CHECK": "C", "FOREIGN": "R"}
UNSUPPORTED_VIEW_WORDS = {
    "LATERAL", "JSON_TABLE", "XMLTABLE", "MODEL", "PIVOT", "UNPIVOT"
}
FORBIDDEN_VIEW_WORDS = {
    "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE",
    "DELETE", "UPDATE", "INSERT", "MERGE",
}


class FeatureDdlError(ValueError):
    """A recognised statement names something outside the checkpoint's scope."""


def unquote_identifier(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value.upper()


def static_grant_object(statement: str, schema: str) -> tuple[str, str] | None:
    grant = _grant(statement.strip(), schema)
    if grant is None:
        return None
    return str(grant["owner"]), str(grant["name"])


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
    elif kind == "REVIEWED_ALTER" and text[:6].upper() == "ALTER ":
        modelled = _modify(text, schema)
    elif kind in {"OPAQUE", "UNKNOWN"}:
        upper = text.upper()
        if upper.startswith("CREATE"):
            modelled = _synonym(text, schema) or _view(text, schema) or _expression_index(text, schema)
        elif upper.startswith("COMMENT"):
            modelled = _comment(text, schema)
        elif upper.startswith("ALTER"):
            modelled = _constraint(text, schema) or _modify(text, schema)
    if modelled is None:
        return None
    return {**operation, **modelled, "supported": True}


def operation_key(operation: Mapping) -> tuple[str, str]:
    """The (owner, name) pair a modelled operation reports in the capsule's object keys."""
    owner, name = str(operation["owner"]), operation.get("name")
    if operation.get("kind") == "ADD_CONSTRAINT":
        name = operation.get("constraint") or operation.get("table")
    return owner, str(name)


def _find_matching_paren(tokens: Sequence[_Token], start_index: int) -> int | None:
    if start_index >= len(tokens) or tokens[start_index].value != "(":
        return None
    depth = 0
    for idx in range(start_index, len(tokens)):
        tok = tokens[idx]
        if tok.kind == "SYMBOL":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")":
                depth -= 1
                if depth == 0:
                    return idx
    return None


def _read_integer(tokens: Sequence[_Token], index: int) -> tuple[int, int] | None:
    if index >= len(tokens) or tokens[index].kind != "SYMBOL" or not tokens[index].value.isdigit():
        return None
    digits: list[str] = []
    while index < len(tokens) and tokens[index].kind == "SYMBOL" and tokens[index].value.isdigit():
        digits.append(tokens[index].value)
        index += 1
    return int("".join(digits)), index


def _grant(text: str, schema: str) -> dict | None:
    try:
        tokens = _tokens(text)
    except MigrationAnalysisError:
        return None
    if tokens and tokens[-1].kind == "SYMBOL" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not _word(tokens, 0, "GRANT"):
        return None
    idx = 1
    priv_tokens: list[_Token] = []
    while idx < len(tokens) and not _word(tokens, idx, "ON"):
        priv_tokens.append(tokens[idx])
        idx += 1
    if not priv_tokens or not _word(tokens, idx, "ON"):
        return None
    idx += 1

    priv_str = "".join(t.value if t.kind in {"WORD", "SYMBOL"} else " " for t in priv_tokens).strip().upper()
    priv_parts = [p.strip() for p in priv_str.split(",") if p.strip()]
    if not priv_parts:
        return None

    try:
        owner, name, idx = _parse_name(tokens, idx, schema)
    except MigrationAnalysisError:
        return None

    if not _word(tokens, idx, "TO"):
        return None
    idx += 1

    grantees: list[str] = []
    while idx < len(tokens):
        if tokens[idx].kind not in {"WORD", "QIDENT"}:
            return None
        grantees.append(_value(tokens[idx]))
        idx += 1
        if idx < len(tokens) and tokens[idx].kind == "SYMBOL" and tokens[idx].value == ",":
            idx += 1
            continue
        break

    if not grantees:
        return None

    if idx < len(tokens) and _word(tokens, idx, "WITH"):
        if not (_word(tokens, idx + 1, "GRANT") and _word(tokens, idx + 2, "OPTION")):
            return None
        idx += 3

    if idx != len(tokens):
        return None

    if owner != schema:
        raise FeatureDdlError(f"object grant owner {owner!r} does not match feature checkpoint schema {schema}")

    return {
        "kind": "GRANT_OBJECT",
        "owner": schema,
        "name": name,
        "privileges": ", ".join(priv_parts),
        "grantees": tuple(grantees),
    }


def _synonym(text: str, schema: str) -> dict | None:
    try:
        tokens = _tokens(text)
    except MigrationAnalysisError:
        return None
    if tokens and tokens[-1].kind == "SYMBOL" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not _word(tokens, 0, "CREATE"):
        return None
    idx = 1
    if _word(tokens, idx, "OR") and _word(tokens, idx + 1, "REPLACE"):
        idx += 2
    public = False
    if _word(tokens, idx, "PUBLIC"):
        public = True
        idx += 1
    if not _word(tokens, idx, "SYNONYM"):
        return None
    idx += 1
    try:
        synonym_owner, synonym_name, idx = _parse_name(tokens, idx, "PUBLIC" if public else schema)
    except MigrationAnalysisError:
        return None
    if not _word(tokens, idx, "FOR"):
        return None
    idx += 1
    try:
        target_owner, target_name, idx = _parse_name(tokens, idx, schema)
    except MigrationAnalysisError:
        return None

    db_link = None
    if idx < len(tokens) and tokens[idx].kind == "SYMBOL" and tokens[idx].value == "@":
        db_link = True

    if db_link:
        raise FeatureDdlError("synonym dependency through a database link is outside selected structural coverage")
    if idx != len(tokens):
        return None

    if public:
        synonym_owner = "PUBLIC"
    if synonym_owner not in {schema, "PUBLIC"}:
        raise FeatureDdlError("synonym owner is outside the selected schema scope")
    if target_owner != schema:
        raise FeatureDdlError("external synonym target access is not provable from selected structural coverage")

    return {
        "kind": "CREATE_SYNONYM",
        "owner": synonym_owner,
        "name": synonym_name,
        "target_owner": target_owner,
        "target_name": target_name,
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


def _modify(text: str, schema: str) -> dict | None:
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
    if not _word(tokens, index, "MODIFY"):
        return None
    index += 1
    if index >= len(tokens):
        return None

    specs_tokens: list[list[_Token]] = []
    if tokens[index].value == "(" and tokens[index].kind == "SYMBOL":
        closing = _find_matching_paren(tokens, index)
        if closing is None or closing != len(tokens) - 1:
            return None
        inner = tokens[index + 1 : closing]
        if not inner:
            return None
        current: list[_Token] = []
        depth = 0
        for t in inner:
            if t.kind == "SYMBOL" and t.value == "(":
                depth += 1
            elif t.kind == "SYMBOL" and t.value == ")":
                depth -= 1
            elif t.kind == "SYMBOL" and t.value == "," and depth == 0:
                if not current:
                    return None
                specs_tokens.append(current)
                current = []
                continue
            current.append(t)
        if not current:
            return None
        specs_tokens.append(current)
    else:
        specs_tokens.append(list(tokens[index:]))

    columns = []
    for spec in specs_tokens:
        if not spec or spec[0].kind not in {"WORD", "QIDENT"}:
            return None
        col_name = _value(spec[0])
        rem = spec[1:]
        nullable = None
        if len(rem) >= 2 and _word(rem, -2, "NOT") and _word(rem, -1, "NULL"):
            nullable = False
            rem = rem[:-2]
        elif len(rem) >= 1 and _word(rem, -1, "NULL"):
            nullable = True
            rem = rem[:-1]

        data_type = None
        char_length = None
        char_used = None
        precision = None
        scale = None

        if not rem:
            if nullable is None:
                return None
        else:
            first_type = rem[0].value.upper() if rem[0].kind == "WORD" else None
            idx = 1
            if first_type == "VARCHAR2":
                if idx >= len(rem) or rem[idx].value != "(":
                    return None
                parsed_len = _read_integer(rem, idx + 1)
                if parsed_len is None:
                    return None
                char_length, idx = parsed_len
                if idx < len(rem) and rem[idx].kind == "WORD" and rem[idx].value.upper() in {"BYTE", "CHAR"}:
                    char_used = "B" if rem[idx].value.upper() == "BYTE" else "C"
                    idx += 1
                if idx >= len(rem) or rem[idx].value != ")" or idx + 1 != len(rem):
                    return None
                data_type = "VARCHAR2"
            elif first_type == "NUMBER":
                if idx == len(rem):
                    data_type = "NUMBER"
                elif idx < len(rem) and rem[idx].value == "(":
                    parsed_p = _read_integer(rem, idx + 1)
                    if parsed_p is None:
                        return None
                    precision, idx = parsed_p
                    if idx < len(rem) and rem[idx].value == ",":
                        parsed_s = _read_integer(rem, idx + 1)
                        if parsed_s is None:
                            return None
                        scale, idx = parsed_s
                    else:
                        scale = 0
                    if idx >= len(rem) or rem[idx].value != ")" or idx + 1 != len(rem):
                        return None
                    data_type = "NUMBER"
                else:
                    return None
            elif first_type == "DATE":
                if idx != len(rem):
                    return None
                data_type = "DATE"
            elif first_type == "TIMESTAMP":
                if idx == len(rem):
                    data_type = "TIMESTAMP"
                elif idx < len(rem) and rem[idx].value == "(":
                    parsed_p = _read_integer(rem, idx + 1)
                    if parsed_p is None:
                        return None
                    scale, idx = parsed_p
                    if idx >= len(rem) or rem[idx].value != ")" or idx + 1 != len(rem):
                        return None
                    data_type = "TIMESTAMP"
                else:
                    return None
            elif first_type == "CLOB":
                if idx != len(rem):
                    return None
                data_type = "CLOB"
            else:
                return None

        columns.append({
            "name": col_name,
            "data_type": data_type,
            "char_length": char_length,
            "char_used": char_used,
            "precision": precision,
            "scale": scale,
            "nullable": nullable,
        })

    if owner != schema:
        raise FeatureDdlError(f"ALTER TABLE owner {owner!r} does not match feature checkpoint schema {schema}")

    return {
        "kind": "ALTER_MODIFY_COLUMN",
        "object_type": "COLUMN",
        "owner": schema,
        "name": table,
        "table": table,
        "columns": tuple(col["name"] for col in columns),
        "column_specs": tuple(columns),
    }


def _expression_index(text: str, schema: str) -> dict | None:
    try:
        tokens = _tokens(text)
    except MigrationAnalysisError:
        return None
    if tokens and tokens[-1].kind == "SYMBOL" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not _word(tokens, 0, "CREATE"):
        return None
    idx = 1
    unique = False
    bitmap = False
    if _word(tokens, idx, "UNIQUE") or _word(tokens, idx, "BITMAP"):
        unique = _word(tokens, idx, "UNIQUE")
        bitmap = _word(tokens, idx, "BITMAP")
        idx += 1
    if not _word(tokens, idx, "INDEX"):
        return None
    idx += 1
    if _word(tokens, idx, "IF") and _word(tokens, idx + 1, "NOT") and _word(tokens, idx + 2, "EXISTS"):
        idx += 3
    try:
        owner, index_name, idx = _parse_name(tokens, idx, schema)
    except MigrationAnalysisError:
        return None
    if not _word(tokens, idx, "ON"):
        return None
    idx += 1
    try:
        tbl_owner, tbl_name, idx = _parse_name(tokens, idx, schema)
    except MigrationAnalysisError:
        return None
    if idx >= len(tokens) or tokens[idx].value != "(":
        return None
    closing = _find_matching_paren(tokens, idx)
    if closing is None or closing != len(tokens) - 1:
        return None

    inner = tokens[idx + 1 : closing]
    segments: list[list[_Token]] = []
    current: list[_Token] = []
    depth = 0
    for t in inner:
        if t.kind == "SYMBOL" and t.value == "(":
            depth += 1
        elif t.kind == "SYMBOL" and t.value == ")":
            depth -= 1
        elif t.kind == "SYMBOL" and t.value == "," and depth == 0:
            if not current:
                return None
            segments.append(current)
            current = []
            continue
        current.append(t)
    if not current:
        return None
    segments.append(current)

    simple_cols: list[str] = []
    has_expressions = False
    expr_count = 0
    for seg in segments:
        if len(seg) == 1 and seg[0].kind in {"WORD", "QIDENT"}:
            simple_cols.append(_value(seg[0]))
        else:
            has_expressions = True
            expr_count += 1

    if not has_expressions:
        return None

    if owner != schema:
        raise FeatureDdlError(f"CREATE INDEX owner {owner!r} does not match feature checkpoint schema {schema}")
    if tbl_owner != schema:
        raise FeatureDdlError(f"CREATE INDEX table owner {tbl_owner!r} does not match feature checkpoint schema {schema}")

    return {
        "kind": "CREATE_INDEX",
        "object_type": "INDEX",
        "owner": schema,
        "name": index_name,
        "key": ("INDEX", index_name),
        "namespace": "INDEX",
        "table": tbl_name,
        "unique": unique,
        "bitmap": bitmap,
        "supported": True,
        "columns": tuple(simple_cols),
        "total_columns": len(segments),
        "expression_count": expr_count,
        "has_expressions": True,
    }


def _extract_view_dependencies(query_toks: Sequence[_Token], default_schema: str) -> tuple[tuple[str, str], ...] | None:
    if not query_toks:
        return None
    for idx, tok in enumerate(query_toks):
        if tok.kind == "SYMBOL" and tok.value in {"@", ";"}:
            return None
        if "@" in tok.value:
            return None
        if tok.kind == "WORD":
            upper = tok.value.upper()
            if upper in UNSUPPORTED_VIEW_WORDS or upper in FORBIDDEN_VIEW_WORDS:
                return None
            if upper == "TABLE" and idx + 1 < len(query_toks) and query_toks[idx + 1].value == "(":
                return None
            if upper == "AS" and idx + 1 < len(query_toks) and _word(query_toks, idx + 1, "OF"):
                return None
            if upper == "VERSIONS" and idx + 1 < len(query_toks) and _word(query_toks, idx + 1, "BETWEEN"):
                return None
            if upper == "SAMPLE" and idx + 1 < len(query_toks) and query_toks[idx + 1].value == "(":
                return None

    last_tok = query_toks[-1]
    if last_tok.kind == "SYMBOL" and last_tok.value in {",", "+", "-", "*", "/", "<", ">", "=", "!"}:
        return None
    if last_tok.kind == "WORD" and last_tok.value.upper() in {
        "WHERE", "AND", "OR", "NOT", "JOIN", "ON", "USING", "FROM", "SELECT",
        "WITH", "AS", "IN", "LIKE", "BETWEEN", "IS", "ORDER", "GROUP", "HAVING", "BY",
    }:
        return None

    dependencies: list[tuple[str, str]] = []

    def scan_query(toks: Sequence[_Token], outer_ctes: set[str]) -> bool:
        ctes_in_scope = set(outer_ctes)
        idx = 0

        if _word(toks, idx, "WITH"):
            idx += 1
            if _word(toks, idx, "RECURSIVE"):
                idx += 1
            while idx < len(toks):
                if toks[idx].kind not in {"WORD", "QIDENT"}:
                    return False
                cte_name = _value(toks[idx]).upper()
                idx += 1
                if idx < len(toks) and toks[idx].value == "(":
                    closing = _find_matching_paren(toks, idx)
                    if closing is None:
                        return False
                    idx = closing + 1
                if not _word(toks, idx, "AS"):
                    return False
                idx += 1
                if idx >= len(toks) or toks[idx].value != "(":
                    return False
                closing = _find_matching_paren(toks, idx)
                if closing is None:
                    return False
                if not scan_query(toks[idx + 1 : closing], ctes_in_scope):
                    return False
                ctes_in_scope.add(cte_name)
                idx = closing + 1
                if idx < len(toks) and toks[idx].value == ",":
                    idx += 1
                    continue
                break

        p = idx
        while p < len(toks):
            tok = toks[p]
            if tok.kind == "SYMBOL" and tok.value == "(":
                closing = _find_matching_paren(toks, p)
                if closing is None:
                    return False
                sub_toks = toks[p + 1 : closing]
                first_word = next((t.value.upper() for t in sub_toks if t.kind == "WORD"), None)
                if first_word in {"SELECT", "WITH"}:
                    if not scan_query(sub_toks, ctes_in_scope):
                        return False
                else:
                    inner_p = 0
                    while inner_p < len(sub_toks):
                        if sub_toks[inner_p].kind == "SYMBOL" and sub_toks[inner_p].value == "(":
                            inner_close = _find_matching_paren(sub_toks, inner_p)
                            if inner_close is not None:
                                inner_sub = sub_toks[inner_p + 1 : inner_close]
                                fw = next((t.value.upper() for t in inner_sub if t.kind == "WORD"), None)
                                if fw in {"SELECT", "WITH"}:
                                    if not scan_query(inner_sub, ctes_in_scope):
                                        return False
                                inner_p = inner_close + 1
                                continue
                        inner_p += 1
                p = closing + 1
                continue

            if tok.kind == "WORD" and tok.value.upper() == "FROM":
                p += 1
                parsed_p = parse_table_references(toks, p, ctes_in_scope)
                if parsed_p is None:
                    return False
                p = parsed_p
                continue

            if tok.kind == "WORD" and tok.value.upper() == "JOIN":
                p += 1
                parsed_p = parse_single_table_reference(toks, p, ctes_in_scope)
                if parsed_p is None:
                    return False
                p = parsed_p
                continue

            p += 1
        return True

    def parse_single_table_reference(toks: Sequence[_Token], p: int, ctes_in_scope: set[str]) -> int | None:
        while p < len(toks) and toks[p].kind == "WORD" and toks[p].value.upper() in {
            "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL"
        }:
            p += 1
        if p >= len(toks):
            return None
        if toks[p].value == "(":
            closing = _find_matching_paren(toks, p)
            if closing is None:
                return None
            sub = toks[p + 1 : closing]
            if not scan_query(sub, ctes_in_scope):
                return None
            p = closing + 1
        else:
            try:
                owner, name, p = _parse_name(toks, p, default_schema)
            except MigrationAnalysisError:
                return None
            uname = name.upper()
            uowner = owner.upper()
            if uname not in ctes_in_scope:
                if uname != "DUAL" and uowner not in {"SYS", "SYSTEM"}:
                    dependencies.append((owner, name))
        if p < len(toks) and _word(toks, p, "PARTITION"):
            p += 1
            if p < len(toks) and toks[p].value == "(":
                closing = _find_matching_paren(toks, p)
                if closing is None:
                    return None
                p = closing + 1
        if p < len(toks) and _word(toks, p, "AS"):
            p += 1
        if p < len(toks) and toks[p].kind in {"WORD", "QIDENT"}:
            if toks[p].value.upper() not in {
                "ON", "USING", "WHERE", "GROUP", "HAVING", "ORDER",
                "UNION", "INTERSECT", "MINUS", "JOIN", "INNER", "LEFT",
                "RIGHT", "FULL", "CROSS", "NATURAL",
            }:
                p += 1
        if p < len(toks) and _word(toks, p, "USING"):
            p += 1
            if p < len(toks) and toks[p].value == "(":
                closing = _find_matching_paren(toks, p)
                if closing is None:
                    return None
                p = closing + 1
        elif p < len(toks) and _word(toks, p, "ON"):
            p += 1
            while p < len(toks):
                tok = toks[p]
                if tok.kind == "SYMBOL" and tok.value == "(":
                    closing = _find_matching_paren(toks, p)
                    if closing is None:
                        return None
                    sub = toks[p + 1 : closing]
                    fw = next((t.value.upper() for t in sub if t.kind == "WORD"), None)
                    if fw in {"SELECT", "WITH"}:
                        if not scan_query(sub, ctes_in_scope):
                            return None
                    p = closing + 1
                    continue
                if tok.kind == "SYMBOL" and tok.value == ",":
                    break
                if tok.kind == "WORD" and tok.value.upper() in {
                    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
                    "WHERE", "GROUP", "HAVING", "ORDER", "UNION", "INTERSECT", "MINUS",
                }:
                    break
                p += 1
        return p

    def parse_table_references(toks: Sequence[_Token], p: int, ctes_in_scope: set[str]) -> int | None:
        while p < len(toks):
            parsed_p = parse_single_table_reference(toks, p, ctes_in_scope)
            if parsed_p is None:
                return None
            p = parsed_p
            if p < len(toks) and toks[p].value == ",":
                p += 1
                continue
            break
        if p < len(toks) and toks[p].value != ")":
            if toks[p].kind != "WORD" or toks[p].value.upper() not in {
                "WHERE", "GROUP", "HAVING", "ORDER", "UNION", "INTERSECT", "MINUS",
                "CONNECT", "START", "FETCH", "OFFSET", "FOR", "WINDOW", "WITH",
                "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
            }:
                return None
        return p

    if not scan_query(query_toks, set()):
        return None
    return tuple(dict.fromkeys(dependencies))


def _view(text: str, schema: str) -> dict | None:
    try:
        tokens = _tokens(text)
    except MigrationAnalysisError:
        return None
    if tokens and tokens[-1].kind == "SYMBOL" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not _word(tokens, 0, "CREATE"):
        return None
    idx, replace = _skip_create_modifiers(tokens, 1)
    if not _word(tokens, idx, "VIEW"):
        return None
    idx += 1
    try:
        owner, name, idx = _parse_name(tokens, idx, schema)
    except MigrationAnalysisError:
        return None

    if idx < len(tokens) and tokens[idx].value == "(":
        closing = _find_matching_paren(tokens, idx)
        if closing is None:
            return None
        idx = closing + 1

    if not _word(tokens, idx, "AS"):
        return None
    idx += 1

    query_tokens = tokens[idx:]
    if not query_tokens:
        return None

    # Strip trailing WITH READ ONLY / WITH CHECK OPTION
    if (
        len(query_tokens) >= 3
        and _word(query_tokens, -3, "WITH")
        and _word(query_tokens, -2, "READ")
        and _word(query_tokens, -1, "ONLY")
    ):
        query_tokens = query_tokens[:-3]
    elif (
        len(query_tokens) >= 3
        and _word(query_tokens, -3, "WITH")
        and _word(query_tokens, -2, "CHECK")
        and _word(query_tokens, -1, "OPTION")
    ):
        query_tokens = query_tokens[:-3]

    deps = _extract_view_dependencies(query_tokens, schema)
    if deps is None:
        return None

    if owner != schema:
        raise FeatureDdlError(f"CREATE VIEW owner {owner!r} does not match feature checkpoint schema {schema}")

    return {
        "kind": "CREATE_VIEW",
        "object_type": "VIEW",
        "owner": schema,
        "name": name,
        "key": ("COMMON", name),
        "namespace": "COMMON",
        "replace": replace,
        "if_not_exists": False,
        "unique": False,
        "bitmap": False,
        "supported": True,
        "manual_checks_required": replace,
        "columns": (),
        "dependencies": deps,
    }
