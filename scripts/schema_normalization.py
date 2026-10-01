#!/usr/bin/env python3
"""Conservative logical-v1 normalization for supported Oracle definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .schema_catalog import ObjectDefinition


NORMALIZATION_VERSION = "logical-v1"
LOGICAL_OWNER = "__LOGICAL_OWNER__"
IDENTITY_SEQUENCE = "__IDENTITY_SEQUENCE__"
GENERATED_CONSTRAINT = "__GENERATED_CONSTRAINT__"
GENERATED_INDEX = "__GENERATED_INDEX__"

SUPPORTED_TYPES = {
    "TABLE", "VIEW", "SEQUENCE", "PACKAGE", "PACKAGE BODY", "PROCEDURE",
    "FUNCTION", "TRIGGER", "SYNONYM", "INDEX", "TYPE", "TYPE BODY", "CONSTRAINT",
}

EXCLUDED_PROPERTIES = {
    "avg_row_len", "blocks", "buffer_pool", "cache_size_runtime", "compression", "data_object_id",
    "empty_blocks", "freelist_groups", "freelists", "global_stats", "initial_extent",
    "initrans", "last_analyzed", "last_ddl_time", "last_number", "max_extents",
    "maxtrans", "min_extents", "next_extent", "num_rows", "object_id", "object_timestamp",
    "pct_increase", "pctfree", "pctused", "sample_size", "segment_created", "storage",
    "storage_clause", "tablespace", "tablespace_name", "timestamp", "user_stats",
    "restart_position", "logging", "nologging",
}

SQL_EXPRESSION_PROPERTIES = {
    "default", "default_expression", "expression", "search_condition", "view_text", "when_clause",
}

PHYSICAL_OPTIONS_WITH_VALUE = {
    "INITRANS", "MAXTRANS", "PCTFREE", "PCTUSED", "FREELISTS", "PCTINCREASE",
}
PHYSICAL_SINGLE_OPTIONS = {"LOGGING", "NOLOGGING", "COMPRESS", "NOCOMPRESS"}
_NUMBER_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", re.ASCII)


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


class _LexError(ValueError):
    pass


def _word_start(char: str) -> bool:
    return char.isalpha() or char in "_$#" or ord(char) >= 128


def _word_continue(char: str) -> bool:
    return char.isalnum() or char in "_$#" or ord(char) >= 128


def _quoted_end(sql: str, offset: int, quote: str) -> int:
    index = offset + 1
    while index < len(sql):
        if sql[index] == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    raise _LexError("unterminated SQL literal or quoted identifier")


def _tokens(sql: str) -> list[_Token]:
    result: list[_Token] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            if end < 0:
                end = length
            comment = sql[index:end]
            if comment.startswith("--+"):
                result.append(_Token("HINT", comment))
            index = end
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise _LexError("unterminated SQL block comment")
            end += 2
            comment = sql[index:end]
            if comment.startswith("/*+"):
                result.append(_Token("HINT", comment))
            index = end
            continue
        prefix_length = 0
        if sql[index : index + 3].lower() == "nq'":
            prefix_length = 3
        elif sql[index : index + 2].lower() == "q'":
            prefix_length = 2
        if prefix_length:
            opening_index = index + prefix_length
            if opening_index >= length:
                raise _LexError("incomplete alternative-quoted SQL literal")
            opening = sql[opening_index]
            closing = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opening, opening)
            terminator = closing + "'"
            end = sql.find(terminator, opening_index + 1)
            if end < 0:
                raise _LexError("unterminated alternative-quoted SQL literal")
            end += len(terminator)
            result.append(_Token("LITERAL", sql[index:end]))
            index = end
            continue
        if char in "nN" and index + 1 < length and sql[index + 1] == "'":
            end = _quoted_end(sql, index + 1, "'")
            result.append(_Token("LITERAL", sql[index:end]))
            index = end
            continue
        if char == "'":
            end = _quoted_end(sql, index, "'")
            result.append(_Token("LITERAL", sql[index:end]))
            index = end
            continue
        if char == '"':
            end = _quoted_end(sql, index, '"')
            result.append(_Token("QIDENT", sql[index + 1 : end - 1].replace('""', '"')))
            index = end
            continue
        if _word_start(char):
            end = index + 1
            while end < length and _word_continue(sql[end]):
                end += 1
            result.append(_Token("WORD", sql[index:end].upper()))
            index = end
            continue
        number = _NUMBER_RE.match(sql, index)
        if number:
            result.append(_Token("NUMBER", number.group(0)))
            index = number.end()
            continue
        operator = next((candidate for candidate in (":=", "=>", "<=", ">=", "<>", "!=", "||", "**", "^=") if sql.startswith(candidate, index)), None)
        if operator:
            result.append(_Token("SYMBOL", operator))
            index += len(operator)
            continue
        result.append(_Token("SYMBOL", char))
        index += 1
    return result


def _owner_token(token: _Token, target_owner: str) -> bool:
    if token.kind == "WORD":
        return token.value.upper() == target_owner.upper()
    if token.kind == "QIDENT":
        return token.value == target_owner
    return False


def _map_owner_tokens(tokens: list[_Token], target_owner: str) -> list[_Token]:
    mapped = []
    for index, token in enumerate(tokens):
        if index + 1 < len(tokens) and tokens[index + 1].kind == "SYMBOL" and tokens[index + 1].value == "." and _owner_token(token, target_owner):
            mapped.append(_Token("OWNER", LOGICAL_OWNER))
        else:
            mapped.append(token)
    return mapped


def _remove_physical_options(tokens: list[_Token]) -> list[_Token]:
    result: list[_Token] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        value = token.value if token.kind == "WORD" else ""
        if value == "TABLESPACE" and index + 1 < len(tokens):
            index += 2
            continue
        if value == "STORAGE" and index + 1 < len(tokens) and tokens[index + 1].value == "(":
            depth = 0
            index += 1
            while index < len(tokens):
                current = tokens[index]
                if current.kind == "SYMBOL" and current.value == "(":
                    depth += 1
                elif current.kind == "SYMBOL" and current.value == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            if depth != 0:
                raise _LexError("unbalanced STORAGE clause")
            continue
        if value == "SEGMENT" and index + 2 < len(tokens) and tokens[index + 1].value == "CREATION":
            index += 3
            continue
        if value == "FREELIST" and index + 2 < len(tokens) and tokens[index + 1].value == "GROUPS":
            index += 3
            continue
        if value in PHYSICAL_OPTIONS_WITH_VALUE:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if value in PHYSICAL_SINGLE_OPTIONS:
            index += 1
            if value == "COMPRESS" and index < len(tokens) and tokens[index].kind == "NUMBER":
                index += 1
            continue
        result.append(token)
        index += 1
    return result


def _canonical_ddl(definition: ObjectDefinition, target_owner: str, *, generated_name: str | None = None) -> tuple[str, ...]:
    tokens = _tokens(definition.raw_ddl)
    owner_mapped = _map_owner_tokens(tokens, target_owner)

    if generated_name:
        for index, token in enumerate(owner_mapped):
            if token.kind not in {"WORD", "QIDENT"}:
                continue
            name_matches = token.value == generated_name if token.kind == "QIDENT" else token.value == generated_name.upper()
            if not name_matches:
                continue
            if definition.key.object_type == "CONSTRAINT" and index > 0 and owner_mapped[index - 1].value == "CONSTRAINT":
                owner_mapped[index] = _Token("GENERATED", GENERATED_CONSTRAINT)
            elif definition.key.object_type == "INDEX" and any(
                previous.kind == "WORD" and previous.value == "INDEX"
                for previous in owner_mapped[max(0, index - 4) : index]
            ):
                owner_mapped[index] = _Token("GENERATED", GENERATED_INDEX)

    if definition.key.object_type in {"TABLE", "INDEX"}:
        owner_mapped = _remove_physical_options(owner_mapped)
    if definition.key.object_type == "SEQUENCE":
        filtered = []
        index = 0
        while index < len(owner_mapped):
            if index + 2 < len(owner_mapped) and owner_mapped[index].value == "START" and owner_mapped[index + 1].value == "WITH":
                index += 4 if index + 3 < len(owner_mapped) and owner_mapped[index + 2].value == ":" else 3
            else:
                filtered.append(owner_mapped[index])
                index += 1
        owner_mapped = filtered
    if owner_mapped and owner_mapped[-1].kind == "SYMBOL" and owner_mapped[-1].value == ";":
        owner_mapped.pop()
    return tuple(f"{token.kind}:{token.value}" for token in owner_mapped)


def _normalize_identity_options(value: str) -> tuple[str, ...] | str:
    try:
        tokens = _tokens(value)
        filtered: list[_Token] = []
        index = 0
        while index < len(tokens):
            if index + 2 < len(tokens) and tokens[index].value == "START" and tokens[index + 1].value == "WITH":
                index += 4 if index + 3 < len(tokens) and tokens[index + 2].value == ":" else 3
            else:
                filtered.append(tokens[index])
                index += 1
        return tuple(f"{token.kind}:{token.value}" for token in filtered)
    except _LexError:
        return value


def _normalize_attributes(
    value: Any,
    target_owner: str,
    *,
    parent_key: str = "",
    problems: list[str],
) -> Any:
    if isinstance(value, dict):
        normalized = {}
        for original_key, child in value.items():
            key = str(original_key).casefold()
            if key in EXCLUDED_PROPERTIES:
                continue
            if parent_key == "identity_columns" and key == "sequence_name":
                normalized[key] = IDENTITY_SEQUENCE
                continue
            if key in {"owner", "table_owner", "schema", "sequence_owner", "index_owner"} and isinstance(child, str):
                normalized[key] = LOGICAL_OWNER if child.upper() == target_owner.upper() else child
                continue
            if key in SQL_EXPRESSION_PROPERTIES and isinstance(child, str):
                try:
                    normalized[key] = tuple(f"{token.kind}:{token.value}" for token in _map_owner_tokens(_tokens(child), target_owner))
                except _LexError:
                    normalized[key] = {"complete": False, "raw": child}
                    problems.append(f"catalog expression property could not be tokenized: {key}")
                continue
            if key == "identity_options" and isinstance(child, str):
                normalized[key] = _normalize_identity_options(child)
                if isinstance(normalized[key], str):
                    problems.append("identity options could not be tokenized")
                continue
            normalized[key] = _normalize_attributes(child, target_owner, parent_key=key, problems=problems)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, list):
        return [_normalize_attributes(child, target_owner, parent_key=parent_key, problems=problems) for child in value]
    if isinstance(value, tuple):
        return [_normalize_attributes(child, target_owner, parent_key=parent_key, problems=problems) for child in value]
    return value


def normalize_definition(definition: ObjectDefinition, target_owner: str) -> dict:
    """Return logical-v1 data; raw DDL remains on the original definition."""
    reasons = []
    object_type = definition.key.object_type.upper()
    if object_type not in SUPPORTED_TYPES:
        reasons.append(f"unsupported object type: {object_type}")
    if not isinstance(target_owner, str) or not target_owner:
        reasons.append("target owner mapping is missing")
    elif definition.key.owner.upper() != target_owner.upper():
        reasons.append("target owner mapping does not match the definition owner")
    if not isinstance(definition.raw_ddl, str) or not definition.raw_ddl:
        reasons.append("full DDL is missing")

    attributes = definition.attributes if isinstance(definition.attributes, dict) else {}
    if not isinstance(definition.attributes, dict):
        reasons.append("structured catalog attributes are malformed")

    generated_name = None
    normalized_name = definition.key.name
    if object_type == "CONSTRAINT" and attributes.get("generated") == "GENERATED NAME" and attributes.get("table_name"):
        generated_name = definition.key.name
        normalized_name = GENERATED_CONSTRAINT
    elif object_type == "INDEX" and str(attributes.get("generated", "")).upper() == "Y" and attributes.get("table_name"):
        generated_name = definition.key.name
        normalized_name = GENERATED_INDEX

    try:
        ddl_tokens = _canonical_ddl(definition, target_owner, generated_name=generated_name)
    except _LexError as error:
        ddl_tokens = ()
        reasons.append(str(error))

    if object_type == "TABLE" and not isinstance(attributes.get("columns"), list):
        reasons.append("table column metadata is incomplete")
    attribute_problems: list[str] = []
    normalized_attributes = _normalize_attributes(attributes, target_owner, problems=attribute_problems)
    reasons.extend(attribute_problems)
    dependents = sorted(
        (LOGICAL_OWNER if key.owner.upper() == target_owner.upper() else key.owner, key.name, key.object_type.upper())
        for key in definition.dependents
    )
    return {
        "format_version": NORMALIZATION_VERSION,
        "complete": not reasons,
        "coverage": {"reason": "; ".join(reasons)} if reasons else {},
        "owner": LOGICAL_OWNER,
        "name": normalized_name,
        "object_type": object_type,
        "valid": definition.valid,
        "attributes": normalized_attributes,
        "ddl_tokens": ddl_tokens,
        "dependents": dependents,
    }


def normalization_coverage() -> dict:
    return {
        "version": NORMALIZATION_VERSION,
        "supported_types": sorted(SUPPORTED_TYPES),
        "properties": [
            "object validity", "table and column structure/order", "defaults and identity properties",
            "constraint/index/trigger definitions and state", "stable sequence settings", "package/procedure/function source",
        ],
        "exclusions": sorted(EXCLUDED_PROPERTIES),
        "exclusion_notes": [
            "storage, segment, tablespace, logging, compression, and physical allocation settings",
            "object IDs, data object IDs, DDL timestamps, and optimizer statistics",
            "sequence runtime LAST_NUMBER/restart position, including DDL START WITH values",
            "table and column comments",
            "security grants and application data",
        ],
        "ddl_transforms": sorted(PHYSICAL_OPTIONS_WITH_VALUE | PHYSICAL_SINGLE_OPTIONS | {"STORAGE", "TABLESPACE", "SEGMENT CREATION"}),
        "limitations": [
            "owner mapping applies only to qualified tokens for the explicitly mapped owner",
            "ambiguous generated dependent names remain distinct",
            "security grants and application data are outside logical-v1",
            "unsupported Oracle object forms or unavailable catalog properties are incomplete",
        ],
    }
