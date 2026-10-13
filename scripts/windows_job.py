"""Run a command on Windows so that Ctrl-C interrupts the wait and ends its process tree.

Console events do not behave like POSIX foreground-group signals. Git Bash can leave
Ctrl-C disabled for a native child, and waiting on that child can delay Python's
KeyboardInterrupt. ``run`` installs a console handler that wakes its polling loop,
then terminates the command's job object, including SQLcl and any descendants. The
job is also closed on process exit so its children cannot outlive the caller.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Sequence
from typing import Any

# How long one wait for the command lasts before Python looks for a Ctrl-C.
POLL_SECONDS = 0.2
# How long to wait for the processes of a terminated job to be gone, so that the
# directories they ran in can be removed.
GONE_SECONDS = 10


class OutputLimitExceeded(Exception):
    """A bounded Windows child produced more output bytes than the caller allowed."""

    def __init__(self, output: str) -> None:
        self.output = output
        super().__init__("process output exceeded its configured byte limit")


class Cancelled(Exception):
    """The caller cancelled the process tree."""

    def __init__(self, output: str) -> None:
        self.output = output
        super().__init__("process tree cancelled")

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

    class _ConsoleControlEvent:
        """Turn console Ctrl-C/Break into a cancellation event for the polling loop."""

        def __init__(self) -> None:
            self.event = threading.Event()
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32.SetConsoleCtrlHandler.argtypes = (wintypes.LPVOID, wintypes.BOOL)
            self._kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def handle_control_event(event_type: int) -> bool:
                if event_type in (0, 1):  # CTRL_C_EVENT or CTRL_BREAK_EVENT
                    self.event.set()
                    return True
                return False

            self._callback = callback_type(handle_control_event)
            self._callback_pointer = ctypes.cast(self._callback, wintypes.LPVOID)
            # Git Bash can mark Ctrl-C as ignored for itself and its children.
            # Reset that inherited state, then install a handler that wakes the
            # polling loop so it can terminate the SQLcl job and finish cleanup.
            self._kernel32.SetConsoleCtrlHandler(None, False)
            self._installed = bool(self._kernel32.SetConsoleCtrlHandler(self._callback_pointer, True))

        def close(self) -> None:
            if self._installed:
                self._kernel32.SetConsoleCtrlHandler(self._callback_pointer, False)
                self._installed = False


def run(
    command: Sequence[str],
    *,
    input_text: str = "",
    timeout: float | None = None,
    output_limit_bytes: int | None = None,
    cancel_event: threading.Event | None = None,
    **popen_options: Any,
) -> subprocess.CompletedProcess:
    """subprocess.run for Windows: Ctrl-C and the timeout end the command and everything it started.

    Only the Windows path is implemented; callers keep subprocess.run elsewhere.
    The command's stdin receives input_text and is then closed. When the job object
    cannot be made, the process tree is ended with taskkill instead.
    """
    if os.name != "nt":
        raise OSError("windows_job.run is for Windows")
    control_event = _ConsoleControlEvent()
    try:
        return _run_with_control_event(
            command,
            input_text=input_text,
            timeout=timeout,
            output_limit_bytes=output_limit_bytes,
            cancel_event=cancel_event,
            console_cancel_event=control_event.event,
            **popen_options,
        )
    finally:
        control_event.close()


def _run_with_control_event(
    command: Sequence[str],
    *,
    input_text: str,
    timeout: float | None,
    output_limit_bytes: int | None,
    cancel_event: threading.Event | None,
    console_cancel_event: threading.Event,
    **popen_options: Any,
) -> subprocess.CompletedProcess:
    if output_limit_bytes is not None:
        return _run_bounded(
            command,
            input_text=input_text,
            timeout=timeout,
            output_limit_bytes=output_limit_bytes,
            cancel_event=cancel_event,
            console_cancel_event=console_cancel_event,
            **popen_options,
        )
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
            if console_cancel_event.is_set():
                raise KeyboardInterrupt
            if cancel_event is not None and cancel_event.is_set():
                partial = _end(process, job)
                raise Cancelled(partial or "")
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


def _run_bounded(
    command: Sequence[str],
    *,
    input_text: str,
    timeout: float | None,
    output_limit_bytes: int,
    cancel_event: threading.Event | None = None,
    console_cancel_event: threading.Event | None = None,
    **popen_options: Any,
) -> subprocess.CompletedProcess:
    """Capture a fixed byte budget and terminate the Windows job as soon as it overflows."""
    deadline = None if timeout is None else time.monotonic() + timeout
    text_mode = bool(popen_options.pop("text", False))
    encoding = popen_options.pop("encoding", None) or "utf-8"
    errors = popen_options.pop("errors", None) or "replace"
    popen_options.pop("stdout", None)
    popen_options.pop("stderr", None)
    try:
        job: _Job | None = _Job()
    except OSError:
        job = None
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=False, **popen_options)
    try:
        if job is not None:
            try:
                job.add(process)
            except OSError:
                job.close()
                job = None
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(input_text.encode(encoding, errors))
        process.stdin.close()
        captured = bytearray()
        exceeded = threading.Event()
        reader_error: list[BaseException] = []

        def read_output() -> None:
            try:
                while True:
                    remaining = output_limit_bytes - len(captured)
                    chunk = os.read(process.stdout.fileno(), max(1, min(8192, remaining + 1)))
                    if not chunk:
                        return
                    captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()
                        return
            except BaseException as error:
                reader_error.append(error)

        reader = threading.Thread(target=read_output, name="sqlcl-output-reader", daemon=True)
        reader.start()
        while True:
            if console_cancel_event is not None and console_cancel_event.is_set():
                raise KeyboardInterrupt
            if cancel_event is not None and cancel_event.is_set():
                _terminate_tree(process, job)
                reader.join(GONE_SECONDS)
                output = bytes(captured).decode(encoding, errors)
                raise Cancelled(output)
            if exceeded.is_set():
                _terminate_tree(process, job)
                reader.join(GONE_SECONDS)
                output = bytes(captured).decode(encoding, errors)
                raise OutputLimitExceeded(output)
            if reader_error and not reader.is_alive():
                _terminate_tree(process, job)
                reader.join(GONE_SECONDS)
                raise OSError("failed while reading child output") from reader_error[0]
            if process.poll() is not None and not reader.is_alive():
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                _terminate_tree(process, job)
                reader.join(GONE_SECONDS)
                output = bytes(captured).decode(encoding, errors)
                raise subprocess.TimeoutExpired(command, timeout, output=output)
            time.sleep(POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining))
        process.wait()
        reader.join()
        if exceeded.is_set():
            _terminate_tree(process, job)
            output = bytes(captured).decode(encoding, errors)
            raise OutputLimitExceeded(output)
        if reader_error:
            raise OSError("failed while reading child output") from reader_error[0]
        if deadline is not None and time.monotonic() >= deadline:
            output = bytes(captured).decode(encoding, errors)
            raise subprocess.TimeoutExpired(command, timeout, output=output)
        output = bytes(captured).decode(encoding, errors) if text_mode else bytes(captured)
        return subprocess.CompletedProcess(command, process.returncode, output, None)
    except BaseException:
        if process.poll() is None:
            _terminate_tree(process, job)
        raise
    finally:
        if job is not None:
            job.close()
        if process.stdout is not None:
            process.stdout.close()


def _terminate_tree(process: subprocess.Popen, job: Any) -> None:
    if job is not None:
        job.terminate()
        job.wait_until_empty(GONE_SECONDS)
    else:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    try:
        process.wait(timeout=GONE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


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
