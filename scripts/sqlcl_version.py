#!/usr/bin/env python3
"""Release-neutral SQLcl version parsing and minimum-version checks."""

from __future__ import annotations

import re


MINIMUM_SQLCL = "26.3.0.0"


class CompatibilityError(ValueError):
    """Release evidence is missing, malformed, or incompatible."""


def _version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value, re.ASCII) is None:
        raise CompatibilityError("version must be a dotted numeric release")
    return tuple(int(part) for part in value.split("."))


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


__all__ = ["CompatibilityError", "MINIMUM_SQLCL", "parse_sqlcl_version", "require_sqlcl_version"]
