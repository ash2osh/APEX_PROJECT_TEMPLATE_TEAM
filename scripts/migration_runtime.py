#!/usr/bin/env python3
"""Runtime prerequisites for guarded migration reporting."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
import tempfile

from .sqlcl_session import bash_command
from .sqlcl_version import CompatibilityError, parse_sqlcl_version, require_sqlcl_version


_REPO_ROOT = Path(__file__).resolve().parents[1]
_VERSION_BRIDGE = (
    'REPO_ROOT=$1; source "$1/scripts/sqlcl_safe.sh"; '
    'invoke_sqlcl_safe "$2" -V < "$2/.sqlcl-stdin"'
)


class MigrationRuntimeError(RuntimeError):
    """The local SQLcl runtime is unavailable or below the migration floor."""


def require_migration_sqlcl_version(*, runner: Callable = subprocess.run) -> None:
    """Require the SQLcl floor without opening a database connection."""
    try:
        with tempfile.TemporaryDirectory(prefix="migration-sqlcl-version-") as temporary:
            work_dir = Path(temporary)
            (work_dir / ".sqlcl-stdin").write_text("", encoding="utf-8")
            command = [
                bash_command(),
                "-c",
                _VERSION_BRIDGE,
                "migration-runtime",
                _REPO_ROOT.as_posix(),
                work_dir.as_posix(),
            ]
            result = runner(command, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise MigrationRuntimeError(
            "could not run the SQLcl version check; SQLcl 26.3.0.0 or newer is required"
        ) from None
    if result.returncode != 0:
        raise MigrationRuntimeError(
            "SQLcl version check failed; SQLcl 26.3.0.0 or newer is required"
        )
    output = result.stdout if isinstance(result.stdout, str) else ""
    try:
        require_sqlcl_version(parse_sqlcl_version(output))
    except CompatibilityError:
        raise MigrationRuntimeError(
            "SQLcl 26.3.0.0 or newer is required for live migration preflight"
        ) from None


__all__ = ["MigrationRuntimeError", "require_migration_sqlcl_version"]
