"""Strict parsing for the shared TEAM_ASSERT SQL output protocol."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from .sqlcl import run_sqlcl


class AssertionVerificationError(RuntimeError):
    """Raised when verification output cannot prove every assertion passed."""


_ASSERTION_RE = re.compile(r"^TEAM_ASSERT\|([^|]+)\|(PASS|FAIL)$")


def parse_team_assertions(stdout: str) -> tuple[str, ...]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("TEAM_ASSERT|"):
            continue
        match = _ASSERTION_RE.fullmatch(line)
        if match is None:
            raise AssertionVerificationError(f"malformed TEAM_ASSERT row: {line}")
        name, status = match.groups()
        if name in seen:
            raise AssertionVerificationError(f"duplicate assertion: {name}")
        seen.add(name)
        rows.append((name, status))
    if not rows:
        raise AssertionVerificationError("verification returned no TEAM_ASSERT rows")
    failures = sorted(name for name, status in rows if status != "PASS")
    if failures:
        raise AssertionVerificationError(
            "failed assertions: " + ", ".join(failures)
        )
    return tuple(name for name, _ in rows)


def run_verification_member(
    profile: Any,
    path: str | Path,
    work: str | Path,
    *,
    runner: Callable[..., Any] = run_sqlcl,
) -> tuple[str, ...]:
    member = Path(path)
    if member.is_symlink() or not member.is_file():
        raise AssertionVerificationError(
            f"verification member is not a regular file: {member}"
        )
    if not member.read_bytes().strip():
        return ()
    result = runner(profile, "read", member, Path(work))
    return parse_team_assertions(getattr(result, "stdout", ""))
