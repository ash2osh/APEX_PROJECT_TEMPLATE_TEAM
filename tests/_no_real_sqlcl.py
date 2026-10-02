"""Keep the unit tests away from a real SQLcl and therefore a real database.

Every test that exercises a script puts a fake `sql` first on PATH. If a fake
cannot run (Windows cannot execute the extensionless shell scripts the tests
write) the lookup falls through to whatever SQLcl the developer has installed,
and the fixtures' default connection name `docker-demo` is often a real saved
connection. Importing this module makes that fall-through fail loudly instead:

* POSIX: a `sql` that prints an explanation and exits 99 is placed at the very
  end of the search order that tests see, behind every fake but ahead of a real
  SQLcl.
* Windows: the same, plus a `sql.exe` (a copy of the system whoami.exe, which
  rejects SQLcl's arguments and exits 1), because CreateProcess, Start-Process
  and Python's subprocess start `sql.exe` while Git Bash starts the Bash `sql`.
  The directory that holds the real SQLcl stays on PATH: it may hold other tools
  the tests need (scoop or Chocolatey shims), and shadowing is enough.

Test modules that run scripts import it (`import _no_real_sqlcl`); `unittest
discover -s tests` puts this directory on sys.path.
"""

from __future__ import annotations

import atexit
import os
import shlex
import shutil
import tempfile

import _windows_lf  # noqa: F401  (Path.write_text writes LF on Windows too)

_MESSAGE = "a unit test reached a real SQLcl; the test's fake sql did not run first"
_DONE = False


def _holds_sqlcl(directory: str) -> bool:
    return any(os.path.isfile(os.path.join(directory, name)) for name in ("sql.exe", "sql.cmd", "sql.bat"))


def install() -> None:
    global _DONE
    if _DONE:
        return
    _DONE = True
    entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    guard_directory = tempfile.mkdtemp(prefix="no-real-sqlcl-")
    atexit.register(shutil.rmtree, guard_directory, True)
    guard = os.path.join(guard_directory, "sql")
    with open(guard, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"#!/bin/sh\necho {shlex.quote(_MESSAGE)} >&2\nexit 99\n")
    os.chmod(guard, 0o755)
    if os.name == "nt":
        whoami = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
        if os.path.isfile(whoami):
            shutil.copy2(whoami, os.path.join(guard_directory, "sql.exe"))
        else:
            # No stand-in executable: fall back to hiding the directories that hold SQLcl.
            entries = [entry for entry in entries if not _holds_sqlcl(entry.strip('"'))]
    os.environ["PATH"] = os.pathsep.join([guard_directory, *entries])


install()
