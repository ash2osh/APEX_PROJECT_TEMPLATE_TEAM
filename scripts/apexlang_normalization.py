"""Normalize the APEXlang ordering and whitespace changes seen on export."""

from __future__ import annotations

import re
from dataclasses import dataclass


DECLARATION = re.compile(r"^(?P<indent>[ \t]*)(?P<kind>textMessage|column|displayColumn)(?:[ \t]+(?P<identifier>[^\s(]+))?[ \t]+\([ \t]*$")
PROPERTY = {
    "column": re.compile(r"^[ \t]*name[ \t]*:[ \t]*(?P<value>[^\s]+)"),
    "displayColumn": re.compile(r"^[ \t]*column[ \t]*:[ \t]*(?P<value>[^\s]+)"),
    "textMessage": re.compile(r"^[ \t]*language[ \t]*:[ \t]*(?P<value>[^\s]+)"),
}


class NormalizationError(ValueError):
    """The source has ambiguous string boundaries and cannot be normalized safely."""


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    parent_depth: int
    indent: str
    kind: str
    key: tuple[str, ...]


def _literal_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return half-open spans for quoted and multiline values in APEXlang."""
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            line_start = text.rfind("\n", 0, index) + 1
            if not text[line_start:index].strip(" \t"):
                newline = text.find("\n", index)
                index = len(text) if newline < 0 else newline + 1
                continue
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue

        if text.startswith("```", index):
            end = index + 3
            while end < len(text):
                if text.startswith("\\```", end):
                    end += 4
                elif text.startswith("```", end):
                    end += 3
                    spans.append((index, end))
                    index = end
                    break
                else:
                    end += 1
            else:
                raise NormalizationError("unterminated APEXlang multiline string")
            continue

        if text[index] in {'"', "`"}:
            quote = text[index]
            end = index + 1
            while end < len(text):
                character = text[end]
                if character == "\\":
                    if end + 1 >= len(text) or text[end + 1] == "\n":
                        raise NormalizationError("unterminated escape in APEXlang quoted value")
                    end += 2
                    continue
                if character == quote:
                    spans.append((index, end + 1))
                    index = end + 1
                    break
                if character == "\n":
                    raise NormalizationError("unterminated APEXlang quoted value")
                end += 1
            else:
                raise NormalizationError("unterminated APEXlang quoted value")
            continue

        index += 1
    return tuple(spans)


def _mask_literals(text: str, spans: tuple[tuple[int, int], ...]) -> str:
    """Mask literal code points while retaining positions and physical LFs."""
    characters = list(text)
    for start, end in spans:
        for index in range(start, end):
            if characters[index] != "\n":
                characters[index] = " "
    return "".join(characters)


def _property_value_suffix(line: str, offset: int, literal_mask: list[bool]) -> str | None:
    """Return a property's first value character, skipping its identifier and separator."""
    for index, character in enumerate(line):
        if character != ":" or literal_mask[offset + index]:
            continue
        identifier = line[:index].strip(" \t")
        if not identifier:
            continue
        value_start = index + 1
        while value_start < len(line) and line[value_start] in " \t":
            value_start += 1
        return line[value_start:]
    return None


def _without_outer_formatting(text: str) -> str:
    spans = _literal_spans(text)
    literal_mask = [False] * len(text)
    for start, end in spans:
        literal_mask[start:end] = [True] * (end - start)
    masked = _mask_literals(text, spans)

    result: list[tuple[str, bool]] = []
    offset = 0
    in_array = False
    for line in text.split("\n"):
        line_length = len(line)
        masked_line = masked[offset:offset + len(line)]
        was_in_array = in_array
        unquoted_payload = False
        if in_array:
            content = line[:-1] if line.endswith("\r") and not literal_mask[offset + line_length - 1] else line
            if content.strip(" \t") == "]":
                in_array = False
            else:
                unquoted_payload = bool(content.strip(" \t"))
        elif not masked_line.lstrip(" \t").startswith("//"):
            value = _property_value_suffix(line, offset, literal_mask)
            if value is not None:
                if value.strip(" \t") == "[":
                    in_array = True
                elif value and not value.startswith(('"', "```", "{")):
                    unquoted_payload = True
        is_comment = not was_in_array and masked_line.lstrip(" \t").startswith("//")
        if line.endswith("\r") and not literal_mask[offset + line_length - 1]:
            line = line[:-1]
        overlaps_literal = any(literal_mask[offset:offset + len(line)])
        if not is_comment:
            end = len(line)
            if not unquoted_payload:
                while end and line[end - 1] in " \t":
                    if literal_mask[offset + end - 1]:
                        break
                    end -= 1
            result.append((line[:end], overlaps_literal))
        offset += line_length + 1

    while result and not result[-1][0].strip(" \t\r") and not result[-1][1]:
        del result[-1]

    return "\n".join(line for line, _ in result) + ("\n" if result else "")


def _parenthesis_delta(line: str) -> int:
    delta = 0
    index = 0
    while index < len(line):
        character = line[index]
        if character == "/" and index + 1 < len(line) and line[index + 1] == "/":
            break
        if character == "(":
            delta += 1
        elif character == ")":
            delta -= 1
        index += 1
    return delta


def _property_value(kind: str, identifier: str, lines: list[str]) -> str:
    pattern = PROPERTY.get(kind)
    if pattern is None:
        return identifier
    for line in lines:
        match = pattern.match(line)
        if match:
            value = match.group("value").strip("`\"'")
            if value:
                return value
    return identifier


def _blocks(lines: list[str], masked_lines: list[str]) -> list[Block]:
    result = []
    depth = 0
    index = 0
    while index < len(lines):
        match = DECLARATION.match(lines[index])
        candidate = match is not None and DECLARATION.match(masked_lines[index]) is not None
        parent_depth = depth
        delta = _parenthesis_delta(masked_lines[index])
        if candidate and delta > 0:
            current_depth = depth + delta
            end = index
            while end + 1 < len(lines) and current_depth > parent_depth:
                end += 1
                line_delta = _parenthesis_delta(masked_lines[end])
                current_depth += line_delta
            if current_depth == parent_depth:
                kind = match.group("kind")
                identifier = (match.group("identifier") or "").strip("`\"'")
                body = lines[index + 1:end]
                primary = identifier if kind == "textMessage" else _property_value(kind, identifier, body)
                language = _property_value("textMessage", "", body) if kind == "textMessage" else ""
                key = (primary, language) if kind == "textMessage" else (primary,)
                result.append(Block(index, end, parent_depth, match.group("indent"), kind, key))
                depth = current_depth
                index = end + 1
                continue
        depth += delta
        index += 1
    return result


def _sortable_groups(lines: list[str], blocks: list[Block]) -> list[list[Block]]:
    groups = []
    current = []
    for block in blocks:
        if not current:
            current = [block]
            continue
        previous = current[-1]
        gap = lines[previous.end + 1:block.start]
        adjacent = (
            block.parent_depth == previous.parent_depth
            and block.indent == previous.indent
            and block.kind == previous.kind
            and all(not line.strip() for line in gap)
        )
        if adjacent:
            current.append(block)
        else:
            if len(current) > 1:
                groups.append(current)
            current = [block]
    if len(current) > 1:
        groups.append(current)
    return groups


def _sort_blocks(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    spans = _literal_spans(text)
    masked_lines = _mask_literals(text, spans).split("\n")
    blocks = _blocks(lines, masked_lines)
    for group in reversed(_sortable_groups(lines, blocks)):
        gaps = [lines[left.end + 1:right.start] for left, right in zip(group, group[1:], strict=False)]
        sorted_blocks = sorted(group, key=lambda block: block.key)
        replacement = []
        for index, block in enumerate(sorted_blocks):
            replacement.extend(lines[block.start:block.end + 1])
            if index < len(gaps):
                replacement.extend(gaps[index])
        lines[group[0].start:group[-1].end + 1] = replacement
    return lines


def normalize_apexlang(content: bytes) -> bytes:
    """Normalize only outer serialization formatting, preserving literal values."""
    text = _without_outer_formatting(content.decode("utf-8"))
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    lines = _sort_blocks(lines)
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")
