#!/usr/bin/env python3
"""Fail closed on unsupported APEX releases and SQLcl versions."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__:
    from .sqlcl_version import (
        CompatibilityError,
        MINIMUM_SQLCL as MINIMUM_SQLCL,
        _version,
        parse_sqlcl_version,
        require_sqlcl_version,
    )
else:  # Direct script invocation is used by the Bash and PowerShell adapters.
    from sqlcl_version import (
        CompatibilityError,
        MINIMUM_SQLCL as MINIMUM_SQLCL,
        _version,
        parse_sqlcl_version,
        require_sqlcl_version,
    )

APEX_RELEASE = "26.2"


def release_line(version: str) -> str:
    parts = _version(version)
    return f"{parts[0]}.{parts[1]}"


def require_release(version: str, expected: str = APEX_RELEASE) -> None:
    if release_line(version) != expected:
        raise CompatibilityError(f"APEX {expected} is required; found {version}; use the matching template branch")


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
