"""Database version support floor for the template tools.

The tools are tested on Oracle Database 23ai and newer (26ai reports itself as 23.x).
Older releases keep working, but the tools print one warning line to stderr.
"""

from __future__ import annotations

import sys

SUPPORTED_MAJOR = 23

_warned_versions: set[str] = set()


def parse_major(version: object) -> int | None:
    """Return the integer before the first dot of a database identity version, or None."""
    if not isinstance(version, str):
        return None
    head = version.strip().split(".", 1)[0]
    if not head or not head.isascii() or not head.isdigit():
        return None
    return int(head)


def support_warning(version: object) -> str | None:
    """Return the one-line warning for a release older than the floor, otherwise None."""
    major = parse_major(version)
    if major is None or major >= SUPPORTED_MAJOR:
        return None
    return (
        f"warning: Oracle Database {version.strip()} is older than 23ai, the supported floor. "
        "The template tools are tested on 23ai and newer; this run continues on a best-effort basis."
    )


def warn_once(version: object, stream=None) -> bool:
    """Print the support warning once per process for each distinct version string.

    Returns True when a line was printed. Nothing is written to stdout.
    """
    text = support_warning(version)
    if text is None or version in _warned_versions:
        return False
    _warned_versions.add(version)
    print(text, file=stream if stream is not None else sys.stderr)
    return True
