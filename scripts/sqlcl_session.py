#!/usr/bin/env python3
"""Run a prepared SQLcl driver through the repository's isolated launcher."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from . import windows_job


def safe_rmtree(
    path: Path | str,
    *,
    retries: int = 25,
    delay: float = 0.1,
    is_windows: bool | None = None,
) -> None:
    """Remove a directory tree reliably, retrying on Windows while file locks release."""
    target = Path(path)
    if not target.exists():
        return
    if not (is_windows if is_windows is not None else (os.name == "nt")):
        shutil.rmtree(target, ignore_errors=True)
        return
    for _ in range(retries):
        try:
            shutil.rmtree(target)
            return
        except OSError:
            time.sleep(delay)
    shutil.rmtree(target, ignore_errors=True)


ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
# `Error starting at line` and `Error report -` head every error SQLcl prints. A
# client error such as "Unknown Command" is not a SQL error, so WHENEVER SQLERROR
# does not stop the script and SQLcl still exits 0; only the report shows it.
SQLCL_ERROR_RE = re.compile(r"(?m)^(?:(?:ORA-\d{5}|SP2-\d{4}|SQLcl Error):|Error starting at line\b|Error report -)")


_WSL_LAUNCHER_RE = re.compile(r"(?i)[\\/](System32|SysWOW64|Sysnative|WindowsApps)[\\/]")


def bash_command() -> str:
    """Return the Bash that runs the SQLcl bridge.

    A bare "bash" is fine on Linux and macOS. On Windows, CreateProcess searches
    the system directory before PATH, so it would start the WSL launcher in
    System32, which cannot run a Windows script path. Use TEAM_BASH when it names
    a file, else the first bash.exe on PATH that is not the launcher, else the Git
    for Windows copy next to git.exe, the same order team.ps1 uses.
    """
    if os.name != "nt":
        return "bash"
    override = os.environ.get("TEAM_BASH")
    if override and os.path.isfile(override):
        return override
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory.strip('"'), "bash.exe")
        if directory and os.path.isfile(candidate) and _WSL_LAUNCHER_RE.search(candidate) is None:
            return candidate
    git = shutil.which("git")
    if git:
        base = os.path.dirname(git)
        for relative in (("..", "bin"), ("..", "..", "bin"), ("..", "usr", "bin"), ("..", "..", "usr", "bin")):
            candidate = os.path.normpath(os.path.join(base, *relative, "bash.exe"))
            if os.path.isfile(candidate):
                return candidate
    return "bash"


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


def _run_bridge(command: list[str], *, cwd: Path, env: Mapping[str, str], timeout_seconds: float) -> subprocess.CompletedProcess:
    """Run the SQLcl bridge with its output (stderr included) as text and nothing on stdin."""
    options = {
        "cwd": cwd,
        "env": env,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        # Ctrl-C must end the wait and the SQLcl under the bridge; see windows_job.
        return windows_job.run(command, input_text="", timeout=timeout_seconds, **options)
    # Killing only the Bash bridge leaves its SQLcl/JVM descendants running.
    # Give this invocation its own process group so timeout/interrupt cleanup
    # cannot terminate the caller or another developer's SQLcl session.
    with subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=True, **options) as process:
        previous_handlers = {}

        def stop_tree():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def terminate(signum, frame):
            stop_tree()
            previous = previous_handlers[signum]
            if callable(previous):
                previous(signum, frame)
            else:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        try:
            # An isolated SQLcl no longer receives the caller's terminal-group
            # TERM/HUP. Forward termination while preserving the caller's exit
            # status or existing migration interrupt handler. Only the main
            # thread can register signal handlers.
            for signum in (signal.SIGTERM, signal.SIGHUP):
                previous = signal.getsignal(signum)
                if previous == signal.SIG_IGN:
                    continue
                previous_handlers[signum] = previous
                try:
                    signal.signal(signum, terminate)
                except ValueError:
                    del previous_handlers[signum]
                    break
            output, _ = process.communicate("", timeout=timeout_seconds)
        except BaseException as error:
            stop_tree()
            output, _ = process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                error.stdout = output
            raise
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
    return subprocess.CompletedProcess(command, process.returncode, output, None)


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
        completed = _run_bridge(
            # as_posix: the bridge compares the driver path with the run directory
            # as strings, which only works when both use "/" (Git Bash accepts
            # C:/dir/file); on Linux and macOS it is the same as str().
            [bash_command(), bridge.as_posix(), resolved_run.as_posix(), target.connection, resolved_driver.as_posix()],
            cwd=resolved_run,
            env=child_environment,
            timeout_seconds=timeout_seconds,
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
