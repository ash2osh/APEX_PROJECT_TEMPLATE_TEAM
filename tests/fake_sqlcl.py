"""Install a fake `sql` that every caller of SQLcl can start, on POSIX and on Windows.

A test writes the fake as a Bash script. On POSIX that script runs as `sql`
through its shebang, and so does it under Git Bash on Windows. Windows itself
cannot start an extensionless file (CreateProcess, Start-Process, Python's
subprocess), so a `sql.exe` beside it runs the same script with Git Bash. It is a
real executable (pip's bundled distlib script launcher around a three-line
Python forwarder), which matters: Start-Process, Python subprocess and Bash all
start it, and the command line, stdin and exit code reach the script unchanged.
A `sql.cmd` shim is not equivalent, because cmd.exe would interpret `&`, `^` and
`%` in the arguments; it is written only when the launcher cannot be built.

Import this module instead of `_no_real_sqlcl`: it installs that guard too.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)

from scripts import sqlcl_session

# Git Bash on Windows, plain `bash` elsewhere: a bare "bash" is the WSL launcher on Windows.
BASH = sqlcl_session.bash_command()

# Bash that undoes the forwarder's "__AT__" marking and runs the fake with the original arguments.
_AT_PROLOGUE = (
    'script=$1; shift; restored=(); '
    'for a in "$@"; do case "$a" in __AT__*) a="@${a#__AT__}";; esac; restored+=("$a"); done; '
    'exec "$script" "${restored[@]}"'
)


def native(path: Path | str) -> str:
    """The form in which SQLcl receives a path argument: C:/dir/file on Windows, str(path) elsewhere."""
    return Path(path).as_posix() if os.name == "nt" else str(path)


def install(directory: Path, script: str) -> Path:
    """Write the fake `sql` into directory, creating it, and return the directory.

    script is the body of a Bash script; a shebang line is added unless it has one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "sql"
    fake.write_text(script if script.startswith("#!") else "#!/usr/bin/env bash\n" + script, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    add_launcher(directory)
    return directory


def add_launcher(directory: Path) -> None:
    """Make the Bash script directory/sql startable by Windows; nothing happens elsewhere.

    For a test that writes its own script (install() calls this itself).
    """
    if os.name != "nt":
        return
    if not _build_windows_launcher(directory, directory / "sql"):
        shim = f'@echo off\r\n"{sqlcl_session.bash_command()}" "%~dp0sql" %*\r\nexit /b %ERRORLEVEL%\r\n'
        (directory / "sql.cmd").write_text(shim, encoding="ascii", newline="")


def _build_windows_launcher(directory: Path, fake: Path) -> bool:
    """Write directory\\sql.exe, which runs `fake` with Git Bash; False when it cannot be built."""
    try:
        from pip._vendor.distlib.scripts import ScriptMaker
    except ImportError:
        return False
    # distlib wraps only a script that starts with a shebang line.
    #
    # Git Bash's runtime (msys-2.0.dll) expands an argument that starts with "@" and names an
    # existing file as a RESPONSE FILE when bash.exe is started by a Windows process, so
    # `sql ... @C:/work/scripts/doctor.sql` would reach the fake as the words of doctor.sql.
    # Real SQLcl is a native program and gets the argument as is. The forwarder therefore hides
    # the leading "@" and a Bash prologue restores it before it execs the script (MSYS to MSYS
    # starts do not expand).
    forwarder = (
        "#!python\nimport subprocess, sys\n"
        f"BASH = {sqlcl_session.bash_command()!r}\n"
        f"SCRIPT = {fake.as_posix()!r}\n"
        f"PROLOGUE = {_AT_PROLOGUE!r}\n"
        "arguments = ['__AT__' + a[1:] if a.startswith('@') else a for a in sys.argv[1:]]\n"
        "sys.exit(subprocess.call([BASH, '-c', PROLOGUE, 'sql', SCRIPT, *arguments]))\n"
    )
    with tempfile.TemporaryDirectory() as source:
        (Path(source) / "sql.py").write_text(forwarder, encoding="utf-8", newline="\n")
        maker = ScriptMaker(source, str(directory), add_launchers=True)
        maker.clobber = True
        maker.variants = {""}
        maker.set_mode = False
        maker.make("sql.py")
    return (directory / "sql.exe").is_file()


def environment(directory: Path, base: Mapping[str, str] | None = None, **values: str) -> dict[str, str]:
    """Return base (default: the current environment) with directory first on PATH."""
    result = dict(os.environ if base is None else base)
    result["PATH"] = f"{directory}{os.pathsep}{result.get('PATH', '')}"
    result.update(values)
    return result
