#!/usr/bin/env python3
"""Compare a feature source file with the DEV source of the same stored unit."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


_CREATE = re.compile(r"(?is)CREATE\s+OR\s+REPLACE\s+(?:(?:NON)?EDITIONABLE\s+)?(?=PACKAGE)")
_HEADER = re.compile(r"(?is)\bPACKAGE(?:\s+(BODY))?\s+(?:\"?\w+\"?\.)?\"?(\w+)\"?")
_ALTER_SESSION = re.compile(r"(?im)^\s*ALTER\s+SESSION[^\n]*\n")

EQUAL = "EQUAL"
BEHIND = "BEHIND DEV"
AHEAD = "AHEAD OF DEV"
DIVERGED = "DIVERGED"


@dataclass(frozen=True)
class Comparison:
    """Code lines only in the feature file and only on DEV, after normalisation."""

    only_feature: tuple[str, ...]
    only_live: tuple[str, ...]

    @property
    def state(self) -> str:
        if not self.only_feature and not self.only_live:
            return EQUAL
        if not self.only_feature:
            return BEHIND
        if not self.only_live:
            return AHEAD
        return DIVERGED


def normalized_lines(text: str) -> list[str]:
    """Code lines of a package source: session settings, the CREATE header, comments, blanks, whitespace and case ignored."""
    text = _ALTER_SESSION.sub("", text.replace("\r", ""))
    create = _CREATE.search(text)
    if create is not None:
        text = text[create.end():]
    text = _HEADER.sub(lambda match: "PACKAGE " + ("BODY " if match.group(1) else "") + match.group(2), text, count=1)
    lines: list[str] = []
    for line in text.split("\n"):
        normalized = re.sub(r"\s+", " ", line.strip()).upper()
        if not normalized or normalized.startswith(("--", "/*", "*")) or normalized == "/":
            continue
        lines.append(normalized)
    return lines


def compare(feature_text: str, live_text: str) -> Comparison:
    feature_lines = normalized_lines(feature_text)
    live_lines = normalized_lines(live_text)
    only_feature: list[str] = []
    only_live: list[str] = []
    matcher = difflib.SequenceMatcher(None, feature_lines, live_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            only_feature.extend(feature_lines[i1:i2])
            only_live.extend(live_lines[j1:j2])
    return Comparison(tuple(only_feature), tuple(only_live))


@dataclass(frozen=True)
class Drift:
    """The state of a feature file against DEV when the DEV source at the last capture is known."""

    state: str
    missing: tuple[str, ...]
    reintroduced: tuple[str, ...]


def with_baseline(feature_text: str, live_text: str, baseline_text: str) -> Drift:
    """Three-way comparison: the file, DEV now, and DEV as it was when the file was captured.

    An edit to existing code is the file's own work, not a loss, as long as DEV has not changed since the capture.
    DEV changes made since then are lost by a release when the file lacks the lines DEV added (`missing`) or still
    holds the lines DEV removed (`reintroduced`).
    """
    since_capture = compare(baseline_text, live_text)
    file_lines = set(normalized_lines(feature_text))
    missing = tuple(line for line in since_capture.only_live if line not in file_lines)
    reintroduced = tuple(line for line in since_capture.only_feature if line in file_lines)
    if missing or reintroduced:
        own_edits = compare(baseline_text, feature_text).state != EQUAL
        return Drift(DIVERGED if own_edits else BEHIND, missing, reintroduced)
    return Drift(EQUAL if compare(feature_text, live_text).state == EQUAL else AHEAD, (), ())
