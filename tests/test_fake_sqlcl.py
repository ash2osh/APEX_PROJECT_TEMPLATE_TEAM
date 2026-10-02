"""tests/fake_sqlcl.py must give every caller of SQLcl the same fake, on POSIX and on Windows.

The fake records its arguments and stdin and exits with FAKE_EXIT. What these tests
pin is that nothing is lost or reinterpreted on the way: spaces, `@C:/...` scripts,
`%`, `&`, `^`, quotes, stdin and the exit status reach the script, whichever
launcher starts it (Python subprocess, Start-Process in Windows PowerShell 5.1 and
7, Git Bash).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import fake_sqlcl

from scripts import sqlcl_session

RECORDER = r"""
rec="${FAKE_RECORD:?}"
: > "$rec"
for a in "$@"; do printf '%s\n' "$a" >> "$rec"; done
printf 'stdin=%s\n' "$(cat)" >> "$rec"
exit "${FAKE_EXIT:-0}"
"""

TRICKY = ["-S", "-noupdates", "-name", "conn x", "@C:/Users/a b/[1]/x.sql", "100%", "a&b", "c^d", 'say "hi"', "PATH=%PATH%"]


class FakeSqlclTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = fake_sqlcl.install(Path(self.temporary.name) / "bin", RECORDER)
        self.record = Path(self.temporary.name) / "record.txt"
        self.environment = fake_sqlcl.environment(self.directory, FAKE_RECORD=str(self.record), FAKE_EXIT="7")

    def recorded(self) -> list[str]:
        return self.record.read_text(encoding="utf-8").splitlines()

    def test_python_subprocess_starts_the_fake_found_with_shutil_which(self) -> None:
        # CreateProcess resolves a bare name against the PARENT's PATH, not the env= given
        # to the child, so a Python caller looks the program up with shutil.which first,
        # as scripts/check_builder_drift.py does inside the process that runs the guard.
        program = shutil.which("sql", path=self.environment["PATH"])
        self.assertIsNotNone(program)
        result = subprocess.run([program, *TRICKY], input=b"hello", capture_output=True, env=self.environment, check=False)

        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.recorded(), [*TRICKY, "stdin=hello"])

    def test_git_bash_starts_the_script_itself(self) -> None:
        bash = sqlcl_session.bash_command()
        result = subprocess.run(
            [bash, "-c", 'sql "$@"; echo "status=$?"', "bash", *TRICKY],
            input=b"hi",
            capture_output=True,
            env=self.environment,
            check=False,
        )

        self.assertIn(b"status=7", result.stdout, result.stderr)
        self.assertEqual(self.recorded(), [*TRICKY, "stdin=hi"])

    def test_windows_gets_a_real_executable_not_a_cmd_shim(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows only")
        self.assertTrue((self.directory / "sql.exe").is_file())
        self.assertFalse((self.directory / "sql.cmd").exists())

    @unittest.skipUnless(os.name == "nt", "Start-Process is what Invoke-Sqlcl uses on Windows")
    def test_start_process_in_both_powershell_engines_passes_everything_through(self) -> None:
        # Start-Process joins ArgumentList without escaping quotes (invoke_sqlcl.ps1 quotes
        # only arguments with a space), so leave the double quote out of this probe.
        arguments = [argument for argument in TRICKY if '"' not in argument]
        argument_list = ",".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
        script = (
            f"$argv = @({argument_list}); "
            '$in = [System.IO.Path]::GetTempFileName(); [System.IO.File]::WriteAllText($in, "hello"); '
            "$quoted = @($argv | ForEach-Object { if ($_ -match '\\s') { '\"' + $_ + '\"' } else { $_ } }); "
            "$p = Start-Process -FilePath sql -ArgumentList $quoted -NoNewWindow -Wait -PassThru "
            "-RedirectStandardInput $in -RedirectStandardOutput ([System.IO.Path]::GetTempFileName()) "
            "-RedirectStandardError ([System.IO.Path]::GetTempFileName()); [System.IO.File]::Delete($in); exit $p.ExitCode"
        )
        engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        self.assertTrue(engines)
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                    capture_output=True,
                    text=True,
                    env=self.environment,
                    check=False,
                )
                self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
                self.assertEqual(self.recorded(), [*arguments, "stdin=hello"])


if __name__ == "__main__":
    unittest.main()
