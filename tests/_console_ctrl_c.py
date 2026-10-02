"""Start a command in a console of its own and press Ctrl-C there (Windows only).

A Ctrl-C keypress is not a signal sent to one process: the console hands CTRL_C_EVENT
to every process attached to it. The command gets a hidden console of its own, with
stdin, stdout and stderr on that console as in a terminal (redirecting any of them
changes how Git Bash starts native programs, and which ones can see the keypress). A
short-lived helper attaches to the console, generates the same event, waits for the
command to end and reads what the console shows, so the test process, and the console
it runs in, never see the event.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Runs in a process of its own: attaching to another console means leaving the current one.
_SENDER = """
import ctypes, sys
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CreateFileW.restype = wintypes.HANDLE


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class BUFFER_INFO(ctypes.Structure):
    _fields_ = [("Size", COORD), ("Cursor", COORD), ("Attributes", wintypes.WORD), ("Window", SMALL_RECT), ("Maximum", COORD)]


kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.GetConsoleScreenBufferInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
kernel32.ReadConsoleOutputCharacterW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, COORD, ctypes.c_void_p)
pid, wait_ms = int(sys.argv[1]), int(float(sys.argv[2]) * 1000)
kernel32.FreeConsole()
if not kernel32.AttachConsole(pid):
    sys.exit(3)
kernel32.SetConsoleCtrlHandler(None, True)
process = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
if not kernel32.GenerateConsoleCtrlEvent(0, 0):
    sys.exit(4)
kernel32.WaitForSingleObject(process, wait_ms)
# The console outlives the command while this process is attached, so what the command printed is still there.
screen = kernel32.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
info = BUFFER_INFO()
kernel32.GetConsoleScreenBufferInfo(screen, ctypes.byref(info))
lines = []
for row in range(info.Cursor.Y + 1):
    line = ctypes.create_unicode_buffer(info.Size.X)
    count = wintypes.DWORD()
    kernel32.ReadConsoleOutputCharacterW(screen, line, info.Size.X, COORD(0, row), ctypes.byref(count))
    lines.append(line.value.rstrip())
sys.stdout.buffer.write("\\n".join(lines).rstrip().encode("utf-8"))
"""

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def start_in_new_console(command: list[str], **popen_arguments: Any) -> subprocess.Popen:
    """Popen for command with a hidden console of its own, its standard handles on that console."""
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup, **popen_arguments)


def press_ctrl_c(process: subprocess.Popen, wait_seconds: float = 20) -> str:
    """Send Ctrl-C to the console that process (the first process started on it) owns.

    Waits up to wait_seconds for process to end and returns the text the console shows by then.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SENDER, str(process.pid), str(wait_seconds)],
        capture_output=True, check=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def write_python_command(directory: Path, name: str, source: str) -> None:
    """Write directory\\<name>.exe, a Windows launcher that runs the Python script source as a command.

    Unlike the Bash fakes of fake_sqlcl it is one native program that Ctrl-C ends, as it ends SQLcl.
    """
    from pip._vendor.distlib.scripts import ScriptMaker

    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as sources:
        (Path(sources) / f"{name}.py").write_text("#!python\n" + source, encoding="utf-8", newline="\n")
        maker = ScriptMaker(sources, str(directory), add_launchers=True)
        maker.clobber = True
        maker.variants = {""}
        maker.set_mode = False
        maker.make(f"{name}.py")


def interrupt(
    command: list[str],
    root: Path,
    environment: dict[str, str],
    started: Path,
    sql_pid: Path | None = None,
    wait_seconds: float = 20,
) -> tuple[subprocess.Popen, str, bool]:
    """Run command in a console of its own and press Ctrl-C there once the file started exists.

    Returns the ended process, what the console shows, and whether the process whose Windows
    id the fake wrote to sql_pid still ran after the command had ended. No standard handle is
    redirected: the keypress reaches the whole chain only while every process shares the
    console. Raises AssertionError when the fake never started or the command outlives Ctrl-C.
    """
    process = start_in_new_console(command, cwd=root, env=environment)
    sql_survived = False
    try:
        deadline = time.monotonic() + 60
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not started.exists():
            raise AssertionError("the command never reached the step that waits")
        screen = press_ctrl_c(process, wait_seconds)
        if process.poll() is None:
            raise AssertionError(f"the command was still running {wait_seconds:g} seconds after Ctrl-C; the console shows {screen!r}")
        if sql_pid is not None:
            # Ending a process is asynchronous, so give the last one a moment.
            deadline = time.monotonic() + 10
            while process_is_running(int(sql_pid.read_text())) and time.monotonic() < deadline:
                time.sleep(0.1)
            sql_survived = process_is_running(int(sql_pid.read_text()))
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait()
        if sql_pid is not None and sql_pid.exists():
            subprocess.run(["taskkill", "/PID", sql_pid.read_text().strip(), "/T", "/F"], capture_output=True, check=False)
    return process, screen, sql_survived


def process_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
