"""Definition comparison and exact write-session guards for dedicated DEV views."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from . import feature_ddl_forms, migration_checks
from .feature_manifest import FeatureInput
from .feature_release import _sha256, _sql_literal
from .validate_migration import statement_spans


_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:E[+-]?[0-9]+)?[FD]?", re.IGNORECASE | re.ASCII)
_COMPOUND_OPERATOR = re.compile(r"<=|>=|<>|!=|\^=|~=|\|\||=>|:=|\*\*|\.\.")
_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)


def _identifier(token):
    if token.kind not in {"WORD", "QIDENT"}:
        raise ValueError("view column aliases must be identifiers")
    return token.value


def _tokens(text):
    tokens = migration_checks._tokens(text)
    if tokens and tokens[-1].kind == "SYMBOL" and tokens[-1].value == ";":
        tokens.pop()
    result = []
    consumed = 0
    for token in tokens:
        if token.start < consumed:
            if token.end > consumed:
                raise ValueError("ambiguous view query token boundary")
            continue
        start = token.start
        number = _NUMBER.match(text, start) if token.kind == "SYMBOL" else None
        operator = _COMPOUND_OPERATOR.match(text, start) if token.kind == "SYMBOL" else None
        if number or operator:
            match = number or operator
            consumed = match.end()
            result.append(migration_checks._Token("NUMBER" if number else "OPERATOR", match[0].upper(), start, consumed))
        else:
            result.append(token)
    return result


def _query_tokens(text, tokens):
    if not tokens:
        raise ValueError("view query text is missing")
    # Offsets preserve literal bytes and optimizer hint text discarded by the analyzer.
    result = []
    end = tokens[0].start
    for token in tokens:
        gap = text[end:token.start]
        for comment in _COMMENT.findall(gap):
            if comment.startswith(("/*+", "--+")):
                result.append(("hint", comment))
        if token.kind in {"WORD", "QIDENT"}:
            kind = "quoted" if token.kind == "QIDENT" else "word"
            value = token.value
        elif token.kind == "NUMBER":
            kind, value = token.kind, token.value
        else:
            kind, value = token.kind, text[token.start:token.end]
        result.append((kind, value))
        end = token.end
    return tuple(result)


def _split_query_options(tokens):
    check = None
    if len(tokens) >= 5 and [token.value for token in tokens[-5:-1]] == ["WITH", "CHECK", "OPTION", "CONSTRAINT"]:
        check = _identifier(tokens[-1])
        tokens = tokens[:-2]
    tail = [token.value for token in tokens[-3:] if token.kind == "WORD"]
    if tail in (["WITH", "CHECK", "OPTION"], ["WITH", "READ", "ONLY"]):
        return tokens[:-3], " ".join(tail[1:]), check
    return tokens, None, None


def _projection_aliases(query):
    level = 0
    select_started = False
    groups = [[]]
    for token in query:
        if token.kind == "SYMBOL" and token.value == "(":
            level += 1
        elif token.kind == "SYMBOL" and token.value == ")":
            level -= 1
        if level == 0 and token.kind == "WORD":
            if token.value == "FROM" and select_started:
                break
            if token.value == "SELECT" and not select_started:
                select_started = True
                continue
        if select_started:
            if level == 0 and token.kind == "SYMBOL" and token.value == ",":
                groups.append([])
            else:
                groups[-1].append(token)
    if groups and groups[0] and groups[0][0].kind == "WORD" and groups[0][0].value in {"DISTINCT", "UNIQUE", "ALL"}:
        groups[0] = groups[0][1:]
    aliases = []
    for group in groups:
        if not group:
            return None
        if len(group) > 2 and group[-2].kind == "WORD" and group[-2].value == "AS":
            aliases.append(_identifier(group[-1]))
        elif len(group) >= 2 and group[-1].kind in {"WORD", "QIDENT"} and (
            group[-2].value == ")" or group[-2].kind in {"LITERAL", "NUMBER"}
            or (len(group) == 2 and group[0].kind in {"WORD", "QIDENT"})
        ):
            aliases.append(_identifier(group[-1]))
        elif all(
            token.kind in {"WORD", "QIDENT"} if index % 2 == 0 else token.value == "."
            for index, token in enumerate(group)
        ) and len(group) % 2 == 1:
            aliases.append(_identifier(group[-1]))
        else:
            # Complex expressions, unlabelled projections and star expansion
            # need an explicit column list to establish the intended interface.
            return None
    return tuple(aliases)


def _has_top_level_wildcard(query):
    depth = 0
    projection = False
    previous = None
    for kind, value in query:
        if kind == "hint":
            continue
        if depth == 0 and kind == "word":
            if value == "SELECT":
                projection = True
            elif value in {"FROM", "UNION", "INTERSECT", "MINUS", "EXCEPT"}:
                projection = False
        if kind == "SYMBOL":
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
            elif depth == 0 and projection and value == "*" and previous in {
                ("word", "SELECT"), ("word", "DISTINCT"), ("word", "ALL"), ("word", "UNIQUE"), ("SYMBOL", ","), ("SYMBOL", "."),
            }:
                return True
        previous = (kind, value)
    return False


def definition(ddl, schema):
    tokens = _tokens(ddl)
    if [token.value for token in tokens[:3]] != ["CREATE", "OR", "REPLACE"]:
        raise ValueError("a DEV view requires CREATE OR REPLACE VIEW")
    index = 3
    modifiers = set()
    while index < len(tokens) and tokens[index].kind == "WORD" and tokens[index].value in {
        "FORCE", "NO", "NOFORCE", "EDITIONABLE", "NONEDITIONABLE", "EDITIONING",
    }:
        modifiers.add(tokens[index].value)
        index += 1
    if not migration_checks._word(tokens, index, "VIEW"):
        raise ValueError("a DEV view requires one conventional or editioning VIEW definition")
    owner, name, index = migration_checks._parse_name(tokens, index + 1, schema)
    if owner != schema:
        raise ValueError(f"view owner {owner!r} differs from selected schema {schema}")
    aliases = None
    if index < len(tokens) and tokens[index].value == "(":
        closing = feature_ddl_forms._find_matching_paren(tokens, index)
        if closing is None:
            raise ValueError("unclosed view alias list")
        raw = tokens[index + 1:closing]
        if not raw or len(raw) % 2 == 0 or any(token.value != "," for token in raw[1::2]):
            raise ValueError("view alias list must contain only ordered column identifiers")
        aliases = tuple(_identifier(token) for token in raw[::2])
        index = closing + 1
    collation = None
    if migration_checks._word(tokens, index, "DEFAULT") and migration_checks._word(tokens, index + 1, "COLLATION"):
        if index + 2 >= len(tokens):
            raise ValueError("missing DEFAULT COLLATION name")
        collation = _identifier(tokens[index + 2])
        index += 3
    bequeath = "DEFINER"
    if migration_checks._word(tokens, index, "BEQUEATH") and index + 1 < len(tokens):
        bequeath = tokens[index + 1].value
        if bequeath not in ("CURRENT_USER", "DEFINER"):
            raise ValueError("view BEQUEATH clause is unsupported")
        index += 2
    if not migration_checks._word(tokens, index, "AS"):
        raise ValueError("view header attributes could not be proven")
    query, access, check = _split_query_options(tokens[index + 1:])
    return {
        "name": name, "aliases": aliases or _projection_aliases(query),
        "editioning": "EDITIONING" in modifiers, "editionable": "NONEDITIONABLE" not in modifiers,
        "bequeath": bequeath, "collation": collation, "access": access, "check": check,
        "query": _query_tokens(ddl, query),
    }


def selected_view(relative, raw, unit):
    if len(PurePosixPath(relative).parts) != 5 or PurePosixPath(relative).parts[3] != "views":
        raise ValueError("select a dedicated views/<name>.sql file for DEV view apply")
    text = raw.decode("utf-8")
    spans = statement_spans(text)
    if len(spans) != 1:
        raise ValueError(f"expected one CREATE OR REPLACE VIEW in {relative}")
    start, end = spans[0]
    ddl = text[start:end]
    parsed = definition(ddl, unit[0])
    modeled = feature_ddl_forms._view(ddl, unit[0])
    if parsed["name"] != unit[2] or not modeled or not modeled["replace"]:
        raise ValueError(f"unsupported dedicated view definition or dependencies in {relative}")
    if _has_top_level_wildcard(parsed["query"]):
        raise ValueError(f"list the projected columns explicitly so Oracle's stored query can be verified in {relative}")
    if parsed["aliases"] is None:
        raise ValueError(f"use an explicit view column list to verify all output aliases in {relative}")
    return FeatureInput(relative, raw, _sha256(raw), unit, "ddl")


def matches(item, observed):
    actual = definition(observed["ddl"], item.unit[0])
    text = observed["text"]
    query, access, check = _split_query_options(_tokens(text))
    if actual["query"] != _query_tokens(text, query) or (access is not None and access != actual["access"]) or (check is not None and check != actual["check"]):
        raise ValueError(f"complete view catalog text and DDL disagree for {item.unit[2]}")
    if actual["name"] != item.unit[2] or actual["aliases"] is None:
        raise ValueError(f"complete output aliases are unavailable for view {item.unit[2]}")
    expected = definition(item.source.decode("utf-8"), item.unit[0])
    if expected["collation"] is None and actual["collation"] is not None:
        raise ValueError(f"declare DEFAULT COLLATION explicitly to verify view {item.unit[2]}'s collation")
    return observed["valid"] is True and actual == expected


def _append_clob(variable, text):
    return [
        f"  DBMS_LOB.WRITEAPPEND({variable}, LENGTH2(UTL_I18N.RAW_TO_CHAR(HEXTORAW('{chunk}'), 'AL32UTF8')), "
        f"UTL_I18N.RAW_TO_CHAR(HEXTORAW('{chunk}'), 'AL32UTF8'));"
        for chunk in migration_checks._hex_chunks(text)
    ]


def view_driver(item):
    text = item.source.decode("utf-8")
    start, end = statement_spans(text)[0]
    ddl = text[start:end].strip().removesuffix(";")
    return ("\n".join([
        "DECLARE", "  l_ddl CLOB;", "BEGIN", "  DBMS_LOB.CREATETEMPORARY(l_ddl, TRUE);",
        *_append_clob("l_ddl", ddl), "  EXECUTE IMMEDIATE l_ddl;",
        "  DBMS_LOB.FREETEMPORARY(l_ddl);", "END;", "/", "",
    ]).encode("utf-8"), "clob")


def before_guards(checkpoint, observed):
    """Compare complete catalog DDL again in the actual writer, before any payload."""
    lines = []
    for item in checkpoint.inputs:
        owner, kind, name = item.unit
        if kind != "VIEW":
            continue
        actual = observed["sourceUnits"].get(f"VIEW:{name}")
        where = f"owner = {_sql_literal(owner)} AND object_name = {_sql_literal(name)} AND object_type = 'VIEW'"
        refusal = f"RAISE_APPLICATION_ERROR(-20984, {_sql_literal('FEATURE_VIEW_GUARD_REFUSED: ' + name)});"
        lines.extend(("DECLARE", "  l_count PLS_INTEGER;", "  l_actual CLOB;", "  l_expected CLOB;", "BEGIN"))
        if actual is None:
            lines.extend((f"  SELECT COUNT(*) INTO l_count FROM all_objects WHERE {where};", f"  IF l_count != 0 THEN {refusal} END IF;"))
        else:
            status = "'VALID'" if actual["valid"] else "'INVALID'"
            lines.extend((
                f"  SELECT COUNT(*) INTO l_count FROM all_objects WHERE {where} AND status = {status};",
                f"  IF l_count != 1 THEN {refusal} END IF;",
                "  DBMS_LOB.CREATETEMPORARY(l_expected, TRUE);",
                *_append_clob("l_expected", actual["ddl"]),
                "  EXECUTE IMMEDIATE 'ALTER SESSION ENABLE COMMIT IN PROCEDURE';",
                f"  l_actual := DBMS_METADATA.GET_DDL('VIEW', {_sql_literal(name)}, {_sql_literal(owner)});",
                "  EXECUTE IMMEDIATE 'ALTER SESSION DISABLE COMMIT IN PROCEDURE';",
                f"  IF l_actual IS NULL OR NVL(DBMS_LOB.COMPARE(l_actual, l_expected), -1) != 0 THEN {refusal} END IF;",
                "  DBMS_LOB.FREETEMPORARY(l_expected);",
            ))
        lines.extend(("END;", "/"))
    return lines
