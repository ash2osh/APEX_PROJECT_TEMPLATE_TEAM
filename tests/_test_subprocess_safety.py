"""Bound test subprocesses and keep them independent of the caller's stdin."""

from __future__ import annotations

from functools import lru_cache
import os
import shutil
import signal
import subprocess
import time


DEFAULT_TIMEOUT_SECONDS = 300
# Seconds between the child's readiness marker and the interrupt. See interrupt_process_group.
INTERRUPT_SETTLE_SECONDS = 0.1
_original_run = subprocess.run
_original_popen = subprocess.Popen


class _TestPopen(_original_popen):
    """Use EOF for unspecified child stdin and bound default waits."""

    def __init__(self, *args, **kwargs):
        # A child started with creationflags (the Windows console Ctrl-C helper) must keep the console's
        # own stdin: redirecting it changes how the console delivers the keypress.
        if kwargs.get("stdin") is None and not kwargs.get("creationflags"):
            kwargs["stdin"] = subprocess.DEVNULL
        super().__init__(*args, **kwargs)

    def communicate(self, input=None, timeout=None):
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_SECONDS
        return super().communicate(input=input, timeout=timeout)

    def wait(self, timeout=None):
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_SECONDS
        return super().wait(timeout=timeout)


def _safe_run(*popenargs, **kwargs):
    if kwargs.get("stdin") is None and kwargs.get("input") is None and not kwargs.get("creationflags"):
        kwargs["stdin"] = subprocess.DEVNULL
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = DEFAULT_TIMEOUT_SECONDS
    return _original_run(*popenargs, **kwargs)


subprocess.Popen = _TestPopen
subprocess.run = _safe_run


@lru_cache(maxsize=8)
def _launcher_works(executable: str) -> bool:
    try:
        result = _original_run(
            [executable, "-NoProfile", "-Command", "exit 0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@lru_cache(maxsize=1)
def working_pwsh() -> str | None:
    """Return PowerShell Core only when its launcher can actually start."""
    executable = shutil.which("pwsh")
    return executable if executable and _launcher_works(executable) else None


@lru_cache(maxsize=1)
def working_powershell() -> str | None:
    """Return either PowerShell launcher only when it can actually start."""
    for name in ("powershell", "pwsh"):
        executable = shutil.which(name)
        if executable and _launcher_works(executable):
            return executable
    return None


@lru_cache(maxsize=1)
def working_windows_powershell() -> str | None:
    """Return Windows PowerShell only when its launcher can actually start."""
    executable = shutil.which("powershell")
    return executable if executable and _launcher_works(executable) else None


def interrupt_process_group(process: subprocess.Popen, settle: float = INTERRUPT_SETTLE_SECONDS) -> None:
    """Send SIGINT to the process group of a POSIX test child started with start_new_session=True.

    Call it only after the child has printed or written its own readiness marker. The settle delay lets PowerShell's
    native-process wait start before the signal arrives: an interrupt that lands while pwsh is still starting a native
    process can abort pwsh instead of exercising the script's own cleanup. That is the suspected cause of a CI failure
    reported as "pwsh exit -6". Every interrupt test should use this helper so the delay is the same everywhere.
    """
    if os.name == "nt":
        raise RuntimeError("interrupt_process_group is POSIX only; Windows interrupt tests use the console Ctrl-C helper")
    if settle < 0:
        raise ValueError("settle must not be negative")
    time.sleep(settle)
    os.killpg(process.pid, signal.SIGINT)
