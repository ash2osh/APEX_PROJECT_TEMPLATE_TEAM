"""Install a fake `sql` that every caller of SQLcl can start, on POSIX and on Windows.

A test writes the fake as a Bash script. On POSIX that script runs as `sql`
through its shebang, and so does it under Git Bash on Windows. Windows itself
cannot start an extensionless file (CreateProcess, Start-Process, Python's
subprocess), so a `sql.cmd` shim beside it runs the same script with Git Bash.
A Python caller finds that shim with shutil.which, which applies PATHEXT.

Import this module instead of `_no_real_sqlcl`: it installs that guard too.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)

from scripts import sqlcl_session


def install(directory: Path, script: str) -> Path:
    """Write the fake `sql` into directory, creating it, and return the directory.

    script is the body of a Bash script; a shebang line is added unless it has one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "sql"
    fake.write_text(script if script.startswith("#!") else "#!/usr/bin/env bash\n" + script, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    if os.name == "nt":
        shim = f'@echo off\r\n"{sqlcl_session.bash_command()}" "%~dp0sql" %*\r\nexit /b %ERRORLEVEL%\r\n'
        (directory / "sql.cmd").write_text(shim, encoding="ascii", newline="")
    return directory


def environment(directory: Path, base: Mapping[str, str] | None = None, **values: str) -> dict[str, str]:
    """Return base (default: the current environment) with directory first on PATH."""
    result = dict(os.environ if base is None else base)
    result["PATH"] = f"{directory}{os.pathsep}{result.get('PATH', '')}"
    result.update(values)
    return result
