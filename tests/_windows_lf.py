"""Make Path.write_text write LF on Windows, as it does everywhere else.

Python's text mode turns "\n" into "\r\n" on Windows. The scripts under test refuse
CRLF in migration SQL, checks.json, descriptors, .env files and shell scripts, and
Bash cannot run a script with CRLF line endings, so a fixture written with a bare
`write_text("...\n")` is a different file on Windows. Importing this module (the test
guard `_no_real_sqlcl` does) makes write_text default to newline="\n" there.

Content that already holds "\r\n" is written as is, so a test that needs CRLF for a
refusal still gets it, and a caller that passes newline= keeps its choice.
"""

from __future__ import annotations

import os
import pathlib

_PATCHED = False


def install() -> None:
    global _PATCHED
    if _PATCHED or os.name != "nt":
        return
    _PATCHED = True
    original = pathlib.Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        return original(self, data, encoding=encoding, errors=errors, newline="\n" if newline is None else newline)

    pathlib.Path.write_text = write_text


install()
