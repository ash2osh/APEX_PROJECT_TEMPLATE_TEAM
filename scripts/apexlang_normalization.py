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
BACKTICK = "`"


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    parent_depth: int
    indent: str
    kind: str
    key: tuple[str, ...]


def _toggle_backticks(line: str, in_backtick: bool) -> bool:
    escaped = False
    for character in line:
        if character == BACKTICK and not escaped:
            in_backtick = not in_backtick
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return in_backtick


def _without_comment_lines(lines: list[str]) -> list[str]:
    result = []
    in_backtick = False
    for line in lines:
        if not in_backtick and line.lstrip().startswith("//"):
            continue
        result.append(line)
        in_backtick = _toggle_backticks(line, in_backtick)
    return result


def _parenthesis_delta(line: str, in_backtick: bool) -> tuple[int, bool]:
    delta = 0
    escaped = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == BACKTICK and not escaped:
            in_backtick = not in_backtick
        elif not in_backtick:
            if character == "/" and index + 1 < len(line) and line[index + 1] == "/":
                break
            if character == "(":
                delta += 1
            elif character == ")":
                delta -= 1
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
        index += 1
    return delta, in_backtick


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


def _blocks(lines: list[str]) -> list[Block]:
    result = []
    depth = 0
    in_backtick = False
    index = 0
    while index < len(lines):
        match = DECLARATION.match(lines[index])
        candidate = match is not None and not in_backtick
        parent_depth = depth
        delta, next_backtick = _parenthesis_delta(lines[index], in_backtick)
        if candidate and delta > 0:
            current_depth = depth + delta
            end = index
            state = next_backtick
            while end + 1 < len(lines) and current_depth > parent_depth:
                end += 1
                line_delta, state = _parenthesis_delta(lines[end], state)
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
                in_backtick = state
                index = end + 1
                continue
        depth += delta
        in_backtick = next_backtick
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
    blocks = _blocks(lines)
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
    """Return normalized UTF-8 APEXlang bytes, preserving meaningful content."""
    text = content.decode("utf-8")
    lines = text.splitlines()
    lines = _without_comment_lines(lines)
    lines = [line.rstrip() for line in lines]
    lines = _sort_blocks(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")
