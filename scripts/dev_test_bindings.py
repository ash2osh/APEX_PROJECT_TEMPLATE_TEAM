"""Typed bindings, render context, and object token expansion for DEV test scenarios.

Public API:
    BindValue = int | str
    DevTestBindingError(ValueError)
    RenderContext(owner, run_id, objects, values)
    validate_bindings(values) -> dict[str, BindValue]
    render_object_tokens(sql, context) -> str

Constraints:
    - Binds use the explicit DEVTEST_ namespace (uppercase ASCII).
    - Bind values are exact signed JSON-safe integers (-(2**53 - 1) to 2**53 - 1,
      not bool) or UTF-8 strings up to 4000 bytes.
    - RenderContext owner is validated uppercase; generated fixture names are
      within a 30-byte Oracle identifier budget.
    - Object tokens (${object.<binding>}) are expanded only outside SQL literals,
      q-quotes, and comments.
    - Ampersand substitution and arbitrary text expansions are refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

BindValue = int | str

# Signed JSON-safe integer range: -(2**53 - 1) to 2**53 - 1
MIN_SAFE_INTEGER = -(2**53 - 1)
MAX_SAFE_INTEGER = 2**53 - 1
MAX_STRING_BYTES = 4000

# Identifiers and binding formats
IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
OBJECT_BINDING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
SELECTOR_BINDING_RE = re.compile(r"^DEVTEST_[A-Z][A-Z0-9_]{0,119}\Z", re.ASCII)


class DevTestBindingError(ValueError):
    """Raised when binding names, values, context, or token expansions are invalid."""


def validate_bindings(values: Mapping[str, BindValue]) -> dict[str, BindValue]:
    """Validate and return a normalized dict of typed scenario bindings."""
    if not isinstance(values, Mapping):
        raise DevTestBindingError("values must be a mapping of binding names to int | str")

    validated: dict[str, BindValue] = {}
    for raw_name, val in values.items():
        if not isinstance(raw_name, str):
            raise DevTestBindingError(f"binding name {raw_name!r} must be a string")

        name = raw_name[1:] if raw_name.startswith(":") else raw_name
        if name in validated:
            raise DevTestBindingError(f"duplicate binding name {name}")

        if not SELECTOR_BINDING_RE.match(name):
            raise DevTestBindingError(
                f"binding name {raw_name!r} is invalid; must start with DEVTEST_ "
                "and contain only uppercase ASCII letters, digits, and underscores"
            )

        if type(val) is bool:
            raise DevTestBindingError(f"boolean binding is not allowed for {name}")
        elif type(val) is int:
            if not (MIN_SAFE_INTEGER <= val <= MAX_SAFE_INTEGER):
                raise DevTestBindingError(
                    f"integer binding for {name} exceeds signed JSON-safe range "
                    f"[{MIN_SAFE_INTEGER}, {MAX_SAFE_INTEGER}]"
                )
            validated[name] = val
        elif isinstance(val, str):
            try:
                encoded = val.encode("utf-8")
            except UnicodeEncodeError as err:
                raise DevTestBindingError(f"string binding for {name} contains invalid unicode: {err}") from None
            if len(encoded) > MAX_STRING_BYTES:
                raise DevTestBindingError(
                    f"string binding for {name} exceeds {MAX_STRING_BYTES} UTF-8 bytes ({len(encoded)} bytes)"
                )
            validated[name] = val
        else:
            raise DevTestBindingError(
                f"binding value of type {type(val).__name__} for {name} is unsupported; "
                "must be an integer or string"
            )

    return validated


@dataclass(frozen=True)
class RenderContext:
    """Frozen execution context for rendering scenario SQL and binding variables."""

    owner: str
    run_id: str
    objects: Mapping[str, str]
    values: Mapping[str, BindValue]

    def __init__(
        self,
        owner: str,
        run_id: str,
        objects: Mapping[str, str],
        values: Mapping[str, BindValue],
    ) -> None:
        if not isinstance(owner, str) or not owner.isupper() or not IDENTIFIER_RE.match(owner):
            raise DevTestBindingError(
                f"owner {owner!r} must be an uppercase Oracle identifier no longer than 128 characters"
            )
        try:
            owner_bytes = owner.encode("utf-8")
        except UnicodeEncodeError as err:
            raise DevTestBindingError(f"owner contains invalid unicode: {err}") from None
        if len(owner_bytes) > 128:
            raise DevTestBindingError("owner exceeds 128 bytes")

        if not isinstance(run_id, str) or not run_id.strip():
            raise DevTestBindingError("run_id must be a nonempty string")
        try:
            run_id_bytes = run_id.encode("utf-8")
        except UnicodeEncodeError as err:
            raise DevTestBindingError(f"run_id contains invalid unicode: {err}") from None
        if len(run_id_bytes) > MAX_STRING_BYTES:
            raise DevTestBindingError(
                f"run_id exceeds {MAX_STRING_BYTES} UTF-8 bytes ({len(run_id_bytes)} bytes)"
            )

        if not isinstance(objects, Mapping):
            raise DevTestBindingError("objects must be a mapping of binding names to generated object identifiers")

        validated_objects: dict[str, str] = {}
        for binding, obj_name in objects.items():
            if not isinstance(binding, str) or not OBJECT_BINDING_RE.match(binding):
                raise DevTestBindingError(
                    f"object binding name {binding!r} is invalid; must match ^[a-z][a-z0-9_]{{0,63}}$"
                )
            if not isinstance(obj_name, str) or not IDENTIFIER_RE.match(obj_name):
                raise DevTestBindingError(
                    f"generated object identifier {obj_name!r} must be an uppercase Oracle identifier"
                )
            if len(obj_name.encode("utf-8")) > 30:
                raise DevTestBindingError(
                    f"generated object identifier {obj_name!r} exceeds 30-byte Oracle budget "
                    f"({len(obj_name.encode('utf-8'))} bytes)"
                )
            validated_objects[binding] = obj_name

        validated_values = validate_bindings(values)
        if "DEVTEST_RUN_ID" in validated_values:
            if validated_values["DEVTEST_RUN_ID"] != run_id:
                raise DevTestBindingError(
                    f"reserved binding DEVTEST_RUN_ID ({validated_values['DEVTEST_RUN_ID']!r}) "
                    f"cannot override run_id ({run_id!r})"
                )
        else:
            validated_values["DEVTEST_RUN_ID"] = run_id

        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "objects", MappingProxyType(validated_objects))
        object.__setattr__(self, "values", MappingProxyType(validated_values))


def _read_q_quoted_literal(source: str, start: int, national: bool) -> tuple[int, str]:
    prefix_length = 2 if national else 1
    quote_index = start + prefix_length
    if quote_index + 1 >= len(source) or source[quote_index] != "'":
        raise DevTestBindingError("invalid q-quoted literal")
    delimiter = source[quote_index + 1]
    if delimiter.isspace() or ord(delimiter) < 32 or delimiter == "'":
        raise DevTestBindingError("invalid q-quote delimiter")

    closing_char = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(delimiter, delimiter)

    cursor = quote_index + 2
    while cursor < len(source):
        char = source[cursor]
        if char == closing_char:
            if cursor + 1 < len(source) and source[cursor + 1] == "'":
                return cursor + 2, source[quote_index + 2 : cursor]
        cursor += 1

    raise DevTestBindingError("unterminated q-quoted literal")


def _read_quoted_literal(source: str, start: int, quote: str, prefix_length: int = 0) -> tuple[int, str]:
    index = start + prefix_length + 1
    value_parts: list[str] = []
    while index < len(source):
        char = source[index]
        if char == quote:
            if index + 1 < len(source) and source[index + 1] == quote:
                value_parts.append(quote)
                index += 2
                continue
            return index + 1, "".join(value_parts)
        value_parts.append(char)
        index += 1
    raise DevTestBindingError(f"unterminated quoted literal with delimiter {quote!r}")


def render_object_tokens(sql: str, context: RenderContext) -> str:
    """Expand object tokens (${object.<binding>}) in SQL using RenderContext."""
    if not isinstance(sql, str):
        raise DevTestBindingError("sql must be a string")
    if not isinstance(context, RenderContext):
        raise DevTestBindingError("context must be a RenderContext instance")

    output_chunks: list[str] = []
    index = 0
    sql_len = len(sql)

    while index < sql_len:
        # 1. Line comment
        if sql.startswith("--", index):
            end = index + 2
            while end < sql_len and sql[end] not in "\r\n":
                end += 1
            comment_content = sql[index:end]
            if "${" in comment_content:
                raise DevTestBindingError("object tokens are not allowed inside line comments")
            output_chunks.append(comment_content)
            index = end
            continue

        # 2. Block comment
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise DevTestBindingError("unterminated block comment")
            comment_content = sql[index : end + 2]
            if "${" in comment_content:
                raise DevTestBindingError("object tokens are not allowed inside block comments")
            output_chunks.append(comment_content)
            index = end + 2
            continue

        # 3. National / standard q-quoted string
        char = sql[index]
        if (
            (char in "nN" and index + 2 < sql_len and sql[index + 1] in "qQ" and sql[index + 2] == "'")
            or (char in "qQ" and index + 1 < sql_len and sql[index + 1] == "'")
        ):
            national = char in "nN"
            end_pos, value = _read_q_quoted_literal(sql, index, national)
            if "${" in value:
                raise DevTestBindingError("object tokens are not allowed inside q-quoted strings")
            output_chunks.append(sql[index:end_pos])
            index = end_pos
            continue

        # 4. National / standard single-quoted string
        if (
            (char in "nN" and index + 1 < sql_len and sql[index + 1] == "'")
            or char == "'"
        ):
            prefix_len = 1 if char in "nN" else 0
            end_pos, value = _read_quoted_literal(sql, index, "'", prefix_length=prefix_len)
            if "${" in value:
                raise DevTestBindingError("object tokens are not allowed inside string literals")
            output_chunks.append(sql[index:end_pos])
            index = end_pos
            continue

        # 5. Double-quoted identifier
        if char == '"':
            end_pos, value = _read_quoted_literal(sql, index, '"')
            if "${" in value:
                raise DevTestBindingError("object tokens are not allowed inside quoted identifiers")
            output_chunks.append(sql[index:end_pos])
            index = end_pos
            continue

        # 6. SQLcl ampersand substitution
        if char == "&":
            raise DevTestBindingError("SQLcl ampersand substitution is not supported in scenario SQL")

        # 7. Object token ${...}
        if sql.startswith("${", index):
            end_pos = sql.find("}", index + 2)
            if end_pos < 0:
                raise DevTestBindingError("unterminated object token in SQL")
            token_content = sql[index + 2 : end_pos]
            if not token_content.startswith("object."):
                raise DevTestBindingError(
                    f"unsupported token ${{{token_content}}}; only ${{object.<binding>}} is supported"
                )
            binding_name = token_content[7:]
            if not OBJECT_BINDING_RE.match(binding_name):
                raise DevTestBindingError(
                    f"invalid object binding name {binding_name!r} in token ${{{token_content}}}"
                )
            if binding_name not in context.objects:
                raise DevTestBindingError(
                    f"unresolved object token ${{{token_content}}}; not found in context.objects"
                )
            generated_name = context.objects[binding_name]
            output_chunks.append(f'"{context.owner}"."{generated_name}"')
            index = end_pos + 1
            continue

        # 8. Regular character
        output_chunks.append(char)
        index += 1

    return "".join(output_chunks)
