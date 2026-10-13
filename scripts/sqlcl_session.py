#!/usr/bin/env python3
"""Run a prepared SQLcl driver through the repository's isolated launcher."""

from __future__ import annotations

import math
import os
import re
import signal
import shutil
import subprocess
import time
import selectors
import threading
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


class SqlclTimeout(SqlclError):
    """SQLcl exceeded its configured limit during a named workflow phase."""

    def __init__(
        self,
        run_dir: Path,
        *,
        phase: str,
        timeout_seconds: float,
        elapsed_seconds: float,
        output: str = "",
    ) -> None:
        self.phase = phase
        self.timeout_seconds = timeout_seconds
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"SQLcl {phase} session timed out; cut off after {timeout_seconds:g} s "
            f"(elapsed {elapsed_seconds:.2f} s)",
            run_dir,
            output,
        )


class SqlclCancelled(SqlclError):
    """The caller cancelled SQLcl and its isolated process tree was stopped."""


class _BridgeOutputLimitExceeded(Exception):
    def __init__(self, output: str) -> None:
        self.output = output


class _BridgeCancelled(Exception):
    def __init__(self, output: str) -> None:
        self.output = output


def _run_bridge_bounded_posix(
    command: list[str], *, cwd: Path, env: Mapping[str, str], timeout_seconds: float,
    output_limit_bytes: int, cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess:
    """Stream at most the requested bytes and kill the isolated SQLcl process tree on overflow."""
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.close()
    captured = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    previous_handlers: dict[int, object] = {}

    def stop_tree() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def terminate(signum, frame) -> None:
        stop_tree()
        previous = previous_handlers[signum]
        if callable(previous):
            previous(signum, frame)
        else:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    try:
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
        while True:
            if cancel_event is not None and cancel_event.is_set():
                stop_tree()
                process.wait()
                remainder = process.stdout.read()
                if remainder:
                    remaining_capacity = output_limit_bytes - len(captured)
                    captured.extend(remainder[:remaining_capacity])
                raise _BridgeCancelled(bytes(captured).decode("utf-8", errors="replace"))
            if process.poll() is not None and not selector.get_map():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_tree()
                process.wait()
                partial = bytes(captured).decode("utf-8", errors="replace")
                raise subprocess.TimeoutExpired(command, timeout_seconds, output=partial)
            if not selector.get_map():
                time.sleep(min(remaining, 0.01))
                continue
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                remaining_capacity = output_limit_bytes - len(captured)
                chunk = os.read(key.fileobj.fileno(), max(1, min(65536, remaining_capacity + 1)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                accepted = min(len(chunk), remaining_capacity)
                captured.extend(chunk[:accepted])
                if len(chunk) > accepted:
                    stop_tree()
                    process.wait()
                    raise _BridgeOutputLimitExceeded(bytes(captured).decode("utf-8", errors="replace"))
        if time.monotonic() >= deadline:
            stop_tree()
            process.wait()
            partial = bytes(captured).decode("utf-8", errors="replace")
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=partial)
        returncode = process.wait()
    except BaseException:
        stop_tree()
        process.wait()
        raise
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        selector.close()
        process.stdout.close()
    output = bytes(captured).decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, output, None)


def _run_bridge(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    output_limit_bytes: int | None = None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess:
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
        try:
            return windows_job.run(
                command,
                input_text="",
                timeout=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
                cancel_event=cancel_event,
                **options,
            )
        except windows_job.OutputLimitExceeded as error:
            raise _BridgeOutputLimitExceeded(error.output) from None
        except windows_job.Cancelled as error:
            raise _BridgeCancelled(error.output) from None
    if output_limit_bytes is not None:
        return _run_bridge_bounded_posix(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
            cancel_event=cancel_event,
        )
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
            pending: str | None = ""
            deadline = time.monotonic() + timeout_seconds
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    stop_tree()
                    output, _ = process.communicate()
                    raise _BridgeCancelled(output)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                try:
                    output, _ = process.communicate(pending, timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    pending = None
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
    phase: str | None = None,
    timeout_seconds: float | None = None,
    output_limit_bytes: int | None = None,
    cancel_event: threading.Event | None = None,
) -> SqlclResult:
    """Run SQLcl by argv in a private CWD; preserve output for every attempt."""
    run_dir = Path(run_dir)
    if ALIAS_RE.fullmatch(target.connection) is None:
        raise SqlclError("configured SQLcl alias contains unsupported characters", run_dir)
    if phase not in {None, "apply", "preconditions", "postconditions", "inventory"}:
        raise SqlclError("SQLcl phase must be apply, preconditions, postconditions, or inventory", run_dir)
    if output_limit_bytes is not None and (
        isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int) or output_limit_bytes <= 0
    ):
        raise SqlclError("SQLcl output limit must be a positive integer byte count", run_dir)
    if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
        raise SqlclError("SQLcl cancellation must be a threading event", run_dir)

    child_environment = dict(os.environ if environment is None else environment)
    timeout_key = "MIGRATION_APPLY_TIMEOUT_SECONDS" if phase == "apply" else "MIGRATION_CHECK_TIMEOUT_SECONDS"
    configured_timeout = child_environment.get(timeout_key) if phase is not None else None
    timeout_label = timeout_key if phase is not None else "SQLcl timeout"
    raw_timeout: object = timeout_seconds if timeout_seconds is not None else configured_timeout
    if raw_timeout is None:
        effective_timeout = 300.0
    else:
        try:
            effective_timeout = float(raw_timeout)
        except (TypeError, ValueError) as error:
            raise SqlclError(f"{timeout_label} must be a positive finite number of seconds", run_dir) from error
    if not math.isfinite(effective_timeout) or effective_timeout <= 0:
        raise SqlclError(f"{timeout_label} must be a positive finite number of seconds", run_dir)
    session_phase = phase or "sqlcl"

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
    started = time.monotonic()
    timeout_elapsed: float | None = None
    cancelled = False
    try:
        completed = _run_bridge(
            # as_posix: the bridge compares the driver path with the run directory
            # as strings, which only works when both use "/" (Git Bash accepts
            # C:/dir/file); on Linux and macOS it is the same as str().
            [bash_command(), bridge.as_posix(), resolved_run.as_posix(), target.connection, resolved_driver.as_posix()],
            cwd=resolved_run,
            env=child_environment,
            timeout_seconds=effective_timeout,
            output_limit_bytes=output_limit_bytes,
            cancel_event=cancel_event,
        )
        output = completed.stdout or ""
        reason = None
        if completed.returncode != 0:
            reason = f"SQLcl exited with status {completed.returncode}"
        elif SQLCL_ERROR_RE.search(output):
            reason = "SQLcl reported a database or client error"
        result = SqlclResult(completed.returncode, output, resolved_run)
    except _BridgeOutputLimitExceeded as error:
        output = error.output
        reason = "SQLcl output exceeded the configured byte limit"
        result = None
    except subprocess.TimeoutExpired as error:
        timeout_elapsed = time.monotonic() - started
        partial = error.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        output = str(partial)
        reason = "SQLcl timed out"
        result = None
    except _BridgeCancelled as error:
        output = error.output
        reason = "SQLcl session cancelled"
        cancelled = True
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

    if timeout_elapsed is not None:
        raise SqlclTimeout(
            resolved_run,
            phase=session_phase,
            timeout_seconds=effective_timeout,
            elapsed_seconds=timeout_elapsed,
            output=output,
        )
    if cancelled:
        raise SqlclCancelled(reason or "SQLcl session cancelled", resolved_run, output)
    if reason is not None:
        raise SqlclError(reason, resolved_run, output)
    assert result is not None
    return result
