#!/usr/bin/env python3
"""Stamp a DEV publish tag into the application version before import.

APEX does not record a Builder timestamp for an import, so the version text is
the revision an import carries: `V2 Powered By xxx [ASHARIF-2026-09-26r001]`.
The counter counts up with each publish and restarts at r001 on a later date.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path


# APEX writes an omitted version as its default.
DEFAULT_VERSION = "Release 1.0"
DEVELOPER_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
VERSION_LINE = re.compile(r"^    version: (.*)$", re.MULTILINE)
NAME_LINE = re.compile(r"^    name: .*$", re.MULTILINE)
PUBLISH_TAG = re.compile(r"\s*\[([A-Z][A-Z0-9_]*)-(\d{4}-\d{2}-\d{2})r(\d{3,})\]$")
# apex_applications.version is VARCHAR2(255).
MAX_VERSION_BYTES = 255


def parse_version(value: str) -> str:
    """Read an APEXlang version value, quoted or bare."""
    if not (len(value) >= 2 and value.startswith('"') and value.endswith('"')):
        return value
    text = []
    index = 1
    while index < len(value) - 1:
        char = value[index]
        if char == "\\":
            escaped = value[index + 1] if index + 1 < len(value) - 1 else ""
            if escaped not in ('"', "\\"):
                raise ValueError(f"unsupported escape in APEXlang version: {value}")
            text.append(escaped)
            index += 2
            continue
        if char == '"':
            raise ValueError(f"unescaped quote in APEXlang version: {value}")
        text.append(char)
        index += 1
    return "".join(text)


def quote_version(text: str) -> str:
    # The tag's brackets make APEXlang quote the value; it escapes only
    # backslash and double quote (verified by re-export on APEX 26.1).
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def next_version(current: str, developer: str, publish_date: str) -> str:
    """Append the next publish tag, never repeating one already used.

    Publish only proceeds when the live version equals the local baseline, so
    each tag builds on the previous one. Counting up per date (whoever
    published) and never moving the date back keeps every tag unique; a
    repeated tag would hide the imports in between from the drift guard.
    """
    base = current
    tag_date = publish_date
    counter = 1
    tag = PUBLISH_TAG.search(current)
    if tag is not None:
        base = current[: tag.start()]
        previous_date = tag.group(2)
        if previous_date >= publish_date:
            tag_date = previous_date
            counter = int(tag.group(3)) + 1
    base = base.rstrip()
    stamp = f"[{developer}-{tag_date}r{counter:03d}]"
    return f"{base} {stamp}" if base else stamp


def stamp_application(path: Path, developer: str, publish_date: str) -> str:
    source = path.read_text(encoding="utf-8")
    versions = list(VERSION_LINE.finditer(source))
    if len(versions) > 1:
        raise ValueError(f"{path} has more than one application version line")
    if versions:
        match = versions[0]
        current = parse_version(match.group(1))
    else:
        match = NAME_LINE.search(source)
        if match is None:
            raise ValueError(f"{path} has no application name line to place the version after")
        current = DEFAULT_VERSION

    updated = next_version(current, developer, publish_date)
    if any(ord(char) < 32 for char in updated):
        raise ValueError("application version must not contain control characters")
    if len(updated.encode("utf-8")) > MAX_VERSION_BYTES:
        raise ValueError(f"application version exceeds {MAX_VERSION_BYTES} bytes: {updated}")

    line = f"    version: {quote_version(updated)}"
    if versions:
        source = source[: match.start()] + line + source[match.end():]
    else:
        source = source[: match.end()] + "\n" + line + source[match.end():]
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(source)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application_apx", type=Path)
    parser.add_argument("developer")
    parser.add_argument("--date", default=None, help="publish date as YYYY-MM-DD (defaults to today)")
    args = parser.parse_args(argv)

    if not DEVELOPER_NAME.fullmatch(args.developer):
        parser.error("developer must be an uppercase name of letters, digits, or underscores (at most 30)")
    publish_date = args.date or date.today().isoformat()
    try:
        date.fromisoformat(publish_date)
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")

    try:
        updated = stamp_application(args.application_apx, args.developer, publish_date)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"publish error: could not stamp the application version: {exc}", file=sys.stderr)
        return 1
    print(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
