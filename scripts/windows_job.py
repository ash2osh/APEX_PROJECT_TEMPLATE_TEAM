"""Run a command on Windows so that Ctrl-C interrupts the wait and ends everything the command started.

On Linux and macOS a Ctrl-C reaches the whole foreground process group: Python's wait
for its child ends at once, and the Bash and SQLcl processes below it stop as well.
Neither holds on Windows. A Ctrl-C keypress is CTRL_C_EVENT for every process on the
console, but Python waits for a child in a call that only returns when the child has
ended, so KeyboardInterrupt arrives after SQLcl has finished, and the Git Bash
processes that Python starts do not take the event, so SQLcl keeps running when
Python stops. run() therefore waits in short steps and puts the command in a job
object, which ends every process in it, whatever its parent, once it is terminated or
closed (also when this process is killed).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from typing import Any

# How long one wait for the command lasts before Python looks for a Ctrl-C.
POLL_SECONDS = 0.2
# How long to wait for the processes of a terminated job to be gone, so that the
# directories they ran in can be removed.
GONE_SECONDS = 10

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _PROCESS_SET_QUOTA_AND_TERMINATE = 0x0100 | 0x0001
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _Job:
        """A job object that ends all of its processes when it is terminated or closed."""

        def __init__(self) -> None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
            kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
            kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID)
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            self._kernel32 = kernel32
            self._handle = kernel32.CreateJobObjectW(None, None)
            if not self._handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

        def add(self, process: subprocess.Popen) -> None:
            kernel32 = self._kernel32
            process_handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA_AND_TERMINATE, False, process.pid)
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel32.AssignProcessToJobObject(self._handle, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel32.CloseHandle(process_handle)

        def terminate(self) -> None:
            self._kernel32.TerminateJobObject(self._handle, 1)

        def wait_until_empty(self, seconds: float) -> None:
            accounting = _Accounting()
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if self._kernel32.QueryInformationJobObject(
                    self._handle, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                ) and accounting.ActiveProcesses == 0:
                    return
                time.sleep(0.05)

        def close(self) -> None:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None


def run(
    command: Sequence[str], *, input_text: str = "", timeout: float | None = None, **popen_options: Any
) -> subprocess.CompletedProcess:
    """subprocess.run for Windows: Ctrl-C and the timeout end the command and everything it started.

    Only the Windows path is implemented; callers keep subprocess.run elsewhere.
    The command's stdin receives input_text and is then closed. When the job object
    cannot be made, the process tree is ended with taskkill instead.
    """
    if os.name != "nt":
        raise OSError("windows_job.run is for Windows")
    try:
        job: _Job | None = _Job()
    except OSError:
        job = None
    process = subprocess.Popen(command, stdin=subprocess.PIPE, **popen_options)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        if job is not None:
            try:
                job.add(process)
            except OSError:
                job.close()
                job = None
        pending: str | None = input_text
        while True:
            wait = POLL_SECONDS if deadline is None else min(POLL_SECONDS, deadline - time.monotonic())
            if wait <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                output, _ = process.communicate(pending, timeout=wait)
                break
            except subprocess.TimeoutExpired:
                pending = None
    except BaseException as error:
        partial = _end(process, job)
        if isinstance(error, subprocess.TimeoutExpired):
            error.stdout = partial
        raise
    finally:
        if job is not None:
            job.close()
    return subprocess.CompletedProcess(command, process.returncode, output, None)


def _end(process: subprocess.Popen, job: Any) -> Any:
    """End process and everything it started, wait until they are gone, and return the output so far."""
    if job is not None:
        job.terminate()
        job.wait_until_empty(GONE_SECONDS)
    else:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    try:
        return process.communicate(timeout=GONE_SECONDS)[0]
    except subprocess.TimeoutExpired:
        return None
