#!/usr/bin/env python3
"""Fail closed on unsupported APEX releases and SQLcl versions."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

APEX_RELEASE = "26.2"
MINIMUM_SQLCL = "26.3.0.0"


class CompatibilityError(ValueError):
    """Release evidence is missing, malformed, or incompatible."""


def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value, re.ASCII) is None:
        raise CompatibilityError("version must be a dotted numeric release")
    return tuple(int(part) for part in value.split("."))


def release_line(version: str) -> str:
    parts = _version(version)
    return f"{parts[0]}.{parts[1]}"


def require_release(version: str, expected: str = APEX_RELEASE) -> None:
    if release_line(version) != expected:
        raise CompatibilityError(f"APEX {expected} is required; found {version}; use the matching template branch")


def require_sqlcl_version(version: str, minimum: str = MINIMUM_SQLCL) -> None:
    actual, floor = _version(version), _version(minimum)
    length = max(len(actual), len(floor))
    if actual + (0,) * (length - len(actual)) < floor + (0,) * (length - len(floor)):
        raise CompatibilityError(f"SQLcl {minimum} or newer is required for APEX operations; found {version}")


def parse_sqlcl_version(output: str) -> str:
    output = re.sub(r"\x1b\[[0-9;]*m", "", output)
    versions = re.findall(r"(?m)^\s*SQLcl:\s*Release\s+([0-9]+(?:\.[0-9]+)+)(?=\s|$)", output)
    if len(versions) != 1:
        raise CompatibilityError("SQLcl release is unavailable or ambiguous; check sql -V")
    return versions[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("sqlcl-version")
    version.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        require_sqlcl_version(parse_sqlcl_version(args.output.read_text(encoding="utf-8")))
    except (CompatibilityError, OSError, UnicodeError) as error:
        print(f"APEX compatibility error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
