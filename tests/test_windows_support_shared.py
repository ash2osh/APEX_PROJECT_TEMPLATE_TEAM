"""Shared Windows regression tests: PowerShell output completion and the optional DBA alias.

Copied from the 26.2 test_windows_support.py so that 26.1 and 26.2 carry the same
checks. The classes need PowerShell 7 (pwsh) for the first class; the second needs bash.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


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
