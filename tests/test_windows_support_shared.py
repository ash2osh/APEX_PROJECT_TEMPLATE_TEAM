"""Shared Windows regression tests: PowerShell output completion and the optional DBA alias.

Copied from the 26.2 test_windows_support.py so that 26.1 and 26.2 carry the same
checks. The first class needs PowerShell 7 (pwsh) on POSIX; the interrupt-warning class
needs Windows PowerShell 5.1 on Windows; the DBA alias class needs bash.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import fake_sqlcl


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(_test_subprocess_safety.working_pwsh() and os.name != "nt", "PowerShell async-output double uses the POSIX wait path")
class SqlclOutputCompletionTests(unittest.TestCase):
    def test_wait_finishes_pending_output_after_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = Path(temporary) / "wait.ps1"
            driver.write_text('''param($Helper)
class PendingOutput {
  [bool] $OutputComplete = $false
  [bool] WaitForExit([int] $Milliseconds) { return $true }
  [void] WaitForExit() { $this.OutputComplete = $true }
}
. $Helper
$process = [PendingOutput]::new()
Wait-SqlclProcess $process
if (-not $process.OutputComplete) { Write-Error 'output processing is still pending'; exit 1 }
''', encoding="utf-8")
            result = subprocess.run(["pwsh", "-NoProfile", "-File", str(driver),
                                     str(ROOT / "scripts/invoke_sqlcl.ps1")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# Windows PowerShell 5.1 hard-wraps Write-Warning text at the console width when its output is
# redirected, which split a path a user would copy. The driver starts SQLcl through Invoke-Sqlcl,
# then ends the wait with an exception, as Ctrl-C does, so the interrupted-diagnostics warning runs.
# The transcript path is a directory, so writing the transcript fails inside that catch block.
INTERRUPT_WARNING_DRIVER = '''param([string] $Helper, [string] $WorkDirectory, [string] $StdInFile, [string] $Transcript)
. $Helper
function Wait-SqlclProcess {
  param($Process)
  throw "simulated interruption"
}
try {
  $null = Invoke-Sqlcl -Arguments @("-V") -WorkingDirectory $WorkDirectory -StdInFile $StdInFile -TranscriptFile $Transcript
} catch {
  # Expected: the interruption propagates once the warning has been written.
}
exit 0
'''


@unittest.skipUnless(os.name == "nt" and _test_subprocess_safety.working_windows_powershell(),
                     "needs Windows PowerShell 5.1 on Windows")
class SqlclInterruptWarningTests(unittest.TestCase):
    def test_interrupted_diagnostics_warning_keeps_the_transcript_path_on_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "diagnostics-directory-with-a-name-long-enough-to-pass-the-console-width"
            transcript.mkdir()
            work = root / "work"
            work.mkdir()
            stdin_file = work / ".sqlcl-stdin"
            stdin_file.write_text("", encoding="utf-8")
            driver = root / "interrupt.ps1"
            driver.write_text(INTERRUPT_WARNING_DRIVER, encoding="utf-8")
            # Invoke-Sqlcl starts `sql` by name, so a fake one goes first on PATH and the real SQLcl is never started.
            fake_bin = fake_sqlcl.install(root / "bin", "exit 0\n")
            result = subprocess.run(
                [_test_subprocess_safety.working_windows_powershell(), "-NoProfile", "-File", str(driver),
                 str(ROOT / "scripts" / "invoke_sqlcl.ps1"), str(work), str(stdin_file), str(transcript)],
                capture_output=True,
                text=True,
                check=False,
                env=fake_sqlcl.environment(fake_bin),
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(f"WARNING: SQLcl diagnostics could not be captured after interruption: {transcript}",
                      output.splitlines(), output)


def write_script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@unittest.skipIf(os.name == "nt", "simulates a Windows shell on a POSIX one")
class OptionalDbaAliasTests(unittest.TestCase):
    """The optional *_DBA_SQLCL_CONNECTION aliases are accepted by load_env.sh and load_env.ps1."""

    def environment(self, directory: Path, uname: str) -> dict[str, str]:
        tools = directory / "tools"
        tools.mkdir()
        write_script(tools / "uname", 'echo "$FAKE_UNAME"\n')
        write_script(tools / "cygpath", 'while [ "$#" -gt 1 ]; do shift; done\nprintf "C:%s\\n" "$1"\n')
        write_script(tools / "python3", 'for a in "$@"; do printf "%s\\n" "$a"; done\n')
        write_script(
            tools / "sql",
            'for a in "$@"; do printf "%s\\n" "$a"; done > "$FAKE_RECORD"\nprintf "%s\\n%s\\n" "$SQLPATH" "$ORACLE_PATH" >> "$FAKE_RECORD"\n',
        )
        return dict(
            os.environ,
            PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
            FAKE_UNAME=uname,
            FAKE_RECORD=str(directory / "record.txt"),
        )

    def test_powershell_env_loader_allows_and_clears_optional_dba_aliases(self) -> None:
        text = (ROOT / "scripts" / "load_env.ps1").read_text(encoding="utf-8")
        for key in ("DEV_DBA_SQLCL_CONNECTION", "STAGING_DBA_SQLCL_CONNECTION", "PROD_DBA_SQLCL_CONNECTION"):
            with self.subTest(key=key):
                self.assertGreaterEqual(text.count(key), 3)

    def test_load_env_accepts_an_optional_dba_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = self.environment(directory, "Linux")
            env_file = directory / ".env"
            env_file.write_text(
                (ROOT / ".env.example").read_text(encoding="utf-8")
                + "\nSTAGING_DBA_SQLCL_CONNECTION=stage-readonly\n",
                encoding="utf-8",
                newline="\n",
            )
            result = subprocess.run(
                ["bash", "-c", 'source "$1/scripts/load_env.sh" "$2" && printf "%s\\n" "$STAGING_DBA_SQLCL_CONNECTION"',
                 "bash", str(ROOT), str(env_file)],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "stage-readonly")
