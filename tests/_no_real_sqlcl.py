"""Keep the unit tests away from a real SQLcl and therefore a real database.

Every test that exercises a script puts a fake `sql` first on PATH. If a fake
cannot run (Windows cannot execute the extensionless shell scripts the tests
write) the lookup falls through to whatever SQLcl the developer has installed,
and the fixtures' default connection name `docker-demo` is often a real saved
connection. Importing this module makes that fall-through fail loudly instead:

* POSIX: a `sql` that prints an explanation and exits 99 is placed at the very
  end of the search order that tests see, behind every fake but ahead of a real
  SQLcl.
* Windows: directories holding sql.exe, sql.cmd or sql.bat are removed from PATH.

Test modules that run scripts import it (`import _no_real_sqlcl`); `unittest
discover -s tests` puts this directory on sys.path.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

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
    if os.name == "nt":
        kept = [entry for entry in entries if not _holds_sqlcl(entry.strip('"'))]
        os.environ["PATH"] = os.pathsep.join(kept)
        return
    guard_directory = tempfile.mkdtemp(prefix="no-real-sqlcl-")
    atexit.register(shutil.rmtree, guard_directory, True)
    guard = os.path.join(guard_directory, "sql")
    with open(guard, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"#!/bin/sh\necho '{_MESSAGE}' >&2\nexit 99\n")
    os.chmod(guard, 0o755)
    os.environ["PATH"] = os.pathsep.join([guard_directory, *entries])


install()
