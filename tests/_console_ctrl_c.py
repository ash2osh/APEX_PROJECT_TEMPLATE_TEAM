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
