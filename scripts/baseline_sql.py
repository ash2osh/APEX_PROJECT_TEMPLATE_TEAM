"""SQL quoting and check helpers shared by the baseline build modules, moved out of scripts/baseline_build.py.

These are leaves: scripts/baseline_reference_data.py and scripts/baseline_bundles.py import them directly, and
scripts/baseline_build.py re-exports them. The one name that stays in scripts/baseline.py, BaselineError, is imported
inside the function that raises it: baseline imports the build modules at its own end, so a module-level import here
would fail whenever one of the build modules is imported before baseline.
"""

from __future__ import annotations

import hashlib
import re

from .migration_manifest import validate_check_query


def _sql_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or any(character in value for character in "\r\n\x00"):
        from . import baseline

        raise baseline.BaselineError("catalog returned an unsafe empty identifier")
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_Q_DELIMITERS = (
    ("~", "~"), ("!", "!"), ("^", "^"), ("#", "#"), ("$", "$"), ("%", "%"),
    ("&", "&"), ("*", "*"), ("-", "-"), ("+", "+"), ("=", "="), ("?", "?"),
    (":", ":"), (";", ";"), ("@", "@"), ("_", "_"), (",", ","), (".", "."),
    ("/", "/"), ("[", "]"), ("{", "}"), ("(", ")"), ("<", ">"),
)


def _q_literal(value: str) -> str:
    for opening, closing in _Q_DELIMITERS:
        if closing + "'" not in value:
            return f"q'{opening}{value}{closing}'"
    return _sql_literal(value)


def _check(check_id: str, sql: str) -> dict:
    validate_check_query(sql)
    return {"id": check_id, "sql": sql, "expected": 1}


def _safe_check_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "object"
    return slug[:39].rstrip("-") + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def sql_tokens(sql: str) -> list[tuple[str, str]]:
    """Tokenize SQL while retaining exact bytes, including comments and Oracle q quotes."""
    from . import baseline

    tokens: list[tuple[str, str]] = []
    i = 0
    while i < len(sql):
        start = i
        char = sql[i]
        if char.isspace():
            i += 1
            while i < len(sql) and sql[i].isspace():
                i += 1
            kind = "space"
        elif sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end + 1
            kind = "comment"
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise baseline.BaselineError("unclosed block comment in SQL statement")
            i = end + 2
            kind = "comment"
        elif sql[i:i + 2].lower() == "q'" or sql[i:i + 3].lower() == "nq'":
            prefix = 2 if sql[i:i + 2].lower() == "q'" else 3
            if i + prefix >= len(sql) or sql[i + prefix].isspace():
                raise baseline.BaselineError("invalid q-quote delimiter in SQL statement")
            opening = sql[i + prefix]
            closing = {"[": "]", "{": "}", "<": ">", "(": ")"}.get(opening, opening)
            end = sql.find(closing + "'", i + prefix + 1)
            if end < 0:
                raise baseline.BaselineError("unclosed q-quote literal in SQL statement")
            i = end + 2
            kind = "literal"
        elif char in "'\"":
            i += 1
            while i < len(sql):
                if sql[i] == char:
                    if i + 1 < len(sql) and sql[i + 1] == char:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                raise baseline.BaselineError("unclosed quoted text in SQL statement")
            kind = "identifier" if char == '"' else "literal"
        elif char.isalnum() or char in "_$#":
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in "_$#"):
                i += 1
            kind = "word"
        else:
            i += 1
            kind = "symbol"
        tokens.append((kind, sql[start:i]))
    return tokens


def retarget_sql_identifiers(sql: str, source_schema: str, target_schema: str) -> str:
    """Map actual source-owner tokens, retaining literal and comment text verbatim.

    A view may use the source schema's name as a table alias. Refuse ambiguous
    qualified expressions rather than silently rewriting an alias as an owner.
    """
    from . import baseline

    if not isinstance(sql, str) or not source_schema or not target_schema:
        raise baseline.BaselineError("SQL and schema names must be non-empty strings")
    if source_schema == target_schema:
        return sql
    tokens = sql_tokens(sql)
    significant = [i for i, (kind, _) in enumerate(tokens) if kind not in {"space", "comment"}]
    words = [tokens[i][1].upper() for i in significant[:8] if tokens[i][0] == "word"]
    is_view = bool(words and words[0] == "CREATE" and "VIEW" in words)
    def matches(kind: str, text: str) -> bool:
        return (kind == "identifier" and text == _sql_identifier(source_schema)) or (
            kind == "word" and text.upper() == source_schema.upper()
        )

    clause_at: dict[int, str] = {}
    if is_view:
        clause_stack: list[str] = []
        current_clause = ""
        clause_keywords = {
            "SELECT", "FROM", "WHERE", "GROUP", "HAVING", "ORDER",
            "CONNECT", "START", "WITH", "SET", "VALUES",
            "UNION", "INTERSECT", "MINUS",
        }
        for index in significant:
            _kind, text = tokens[index]
            upper = text.upper()
            if text == "(":
                clause_stack.append(current_clause)
                current_clause = ""
            elif text == ")":
                if clause_stack:
                    current_clause = clause_stack.pop()
            elif upper in clause_keywords:
                current_clause = upper
            elif upper == "JOIN":
                current_clause = "JOIN"
            elif upper == "ON":
                current_clause = "ON"
            elif text == "," and current_clause == "ON":
                current_clause = "FROM"
            clause_at[index] = current_clause

        for pos, index in enumerate(significant):
            kind, text = tokens[index]
            if not matches(kind, text):
                continue
            previous = tokens[significant[pos - 1]][1] if pos else ""
            following = tokens[significant[pos + 1]][1] if pos + 1 < len(significant) else ""
            if previous != "." and following != ".":
                raise baseline.BaselineError("ambiguous source-schema name in view; review the migration")

    result = list(tokens)
    for pos, index in enumerate(significant):
        kind, text = tokens[index]
        if not matches(kind, text) or pos + 1 >= len(significant):
            continue
        previous = tokens[significant[pos - 1]][1] if pos else ""
        following = tokens[significant[pos + 1]][1]
        if following != "." or previous in {".", ":"}:
            continue
        if is_view:
            is_three_part = (
                pos + 3 < len(significant)
                and tokens[significant[pos + 2]][0] in {"identifier", "word"}
                and tokens[significant[pos + 3]][1] == "."
            )
            in_table_pos = previous.upper() in {"VIEW", "FROM", "JOIN"} or (
                previous == "," and clause_at.get(index) == "FROM"
            )
            if not (is_three_part or in_table_pos):
                raise baseline.BaselineError("ambiguous source-schema qualifier in view; review the migration")
        result[index] = (kind, _sql_identifier(target_schema))
    return "".join(text for _, text in result)
