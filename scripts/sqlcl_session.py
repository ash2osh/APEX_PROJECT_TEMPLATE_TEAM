#!/usr/bin/env python3
"""Run a prepared SQLcl driver through the repository's isolated launcher."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping


ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
# `Error starting at line` and `Error report -` head every error SQLcl prints. A
# client error such as "Unknown Command" is not a SQL error, so WHENEVER SQLERROR
# does not stop the script and SQLcl still exits 0; only the report shows it.
SQLCL_ERROR_RE = re.compile(r"(?m)^(?:(?:ORA-\d{5}|SP2-\d{4}|SQLcl Error):|Error starting at line\b|Error report -)")


@dataclass(frozen=True)
class SqlclResult:
    returncode: int
    output: str
    run_dir: Path


class SqlclError(RuntimeError):
    """SQLcl did not complete with an unambiguous successful result."""

    def __init__(self, message: str, run_dir: Path, output: str = "") -> None:
        self.run_dir = run_dir
        self.output = output
        super().__init__(f"{message}; diagnostics: {run_dir / 'sqlcl-output.log'}")


def run_sqlcl(
    target,
    driver_path: Path,
    run_dir: Path,
    *,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 300,
) -> SqlclResult:
    """Run SQLcl by argv in a private CWD; preserve output for every attempt."""
    if ALIAS_RE.fullmatch(target.connection) is None:
        raise SqlclError("configured SQLcl alias contains unsupported characters", run_dir)
    if timeout_seconds <= 0:
        raise SqlclError("SQLcl timeout must be positive", run_dir)

    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        raise SqlclError("SQLcl working directory cannot be a symlink", run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        run_dir.chmod(0o700)
    except OSError as error:
        raise SqlclError(f"cannot secure SQLcl working directory ({error})", run_dir) from error

    driver_path = Path(driver_path)
    try:
        resolved_run = run_dir.resolve(strict=True)
        resolved_driver = driver_path.resolve(strict=True)
        resolved_driver.relative_to(resolved_run)
    except (OSError, ValueError) as error:
        raise SqlclError("SQLcl driver must be a regular file inside its private run directory", run_dir) from error
    if driver_path.is_symlink() or not resolved_driver.is_file():
        raise SqlclError("SQLcl driver must be a regular file inside its private run directory", run_dir)

    bridge = Path(__file__).with_name("sqlcl_session.sh").resolve(strict=True)
    child_environment = dict(os.environ if environment is None else environment)
    try:
        completed = subprocess.run(
            ["bash", str(bridge), str(resolved_run), target.connection, str(resolved_driver)],
            cwd=resolved_run,
            env=child_environment,
            input="",
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        output = completed.stdout or ""
        reason = None
        if completed.returncode != 0:
            reason = f"SQLcl exited with status {completed.returncode}"
        elif SQLCL_ERROR_RE.search(output):
            reason = "SQLcl reported a database or client error"
        result = SqlclResult(completed.returncode, output, resolved_run)
    except subprocess.TimeoutExpired as error:
        partial = error.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        output = str(partial)
        reason = f"SQLcl timed out after {timeout_seconds:g} seconds"
        result = None
    except OSError as error:
        output = f"unable to start SQLcl: {error}\n"
        reason = "SQLcl could not be started"
        result = None

    log_path = resolved_run / "sqlcl-output.log"
    try:
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as log_file:
            log_file.write(output)
    except OSError as error:
        raise SqlclError(f"could not preserve SQLcl diagnostics ({error})", resolved_run, output) from error

    if reason is not None:
        raise SqlclError(reason, resolved_run, output)
    assert result is not None
    return result
