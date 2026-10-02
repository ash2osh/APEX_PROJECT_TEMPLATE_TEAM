import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import fake_sqlcl


class FakeSqlclTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "the POSIX path runs the script through its shebang")
    def test_the_fake_runs_as_sql_when_its_directory_is_first_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = fake_sqlcl.install(Path(temporary) / "bin", 'printf "%s|" "$@"\necho "$FAKE_VALUE"\n')
            result = subprocess.run(
                ["sql", "-S", "x y"], capture_output=True, text=True, check=False,
                env=fake_sqlcl.environment(directory, FAKE_VALUE="ok"),
            )
        self.assertEqual(result.stdout, "-S|x y|ok\n")
        self.assertEqual(result.returncode, 0)

    def test_a_script_that_names_its_own_interpreter_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = fake_sqlcl.install(Path(temporary) / "bin", "#!/bin/sh\nexit 3\n")
            first_line = (directory / "sql").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "#!/bin/sh")

    def test_on_windows_a_cmd_shim_runs_the_same_script_with_git_bash(self) -> None:
        # Simulated: only the shim's text can be checked here, not that Windows runs it.
        windows = types.SimpleNamespace(name="nt", environ=os.environ, pathsep=os.pathsep, path=os.path)
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(fake_sqlcl, "os", windows), \
                patch.object(fake_sqlcl.sqlcl_session, "bash_command", return_value=git_bash):
            directory = fake_sqlcl.install(Path(temporary) / "bin", "echo hi\n")
            shim = (directory / "sql.cmd").read_bytes()
            script = (directory / "sql").read_bytes()
        self.assertEqual(shim, b'@echo off\r\n"C:\\Program Files\\Git\\bin\\bash.exe" "%~dp0sql" %*\r\nexit /b %ERRORLEVEL%\r\n')
        self.assertEqual(script, b"#!/usr/bin/env bash\necho hi\n")

    @unittest.skipIf(os.name == "nt", "a shim exists on Windows")
    def test_no_shim_is_written_off_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = fake_sqlcl.install(Path(temporary) / "bin", "echo hi\n")
            names = sorted(path.name for path in directory.iterdir())
        self.assertEqual(names, ["sql"])


if __name__ == "__main__":
    unittest.main()
