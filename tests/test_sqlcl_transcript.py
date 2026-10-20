"""SQLcl diagnostics survive an exception while waiting for a real child process."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import fake_sqlcl

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("pwsh"), "requires PowerShell Core")
class SqlclTranscriptTests(unittest.TestCase):
    def test_wait_exception_keeps_captured_output(self):
        self.run_launcher(interrupted=True)

    def test_completed_process_keeps_the_same_transcript(self):
        self.run_launcher(interrupted=False)

    def run_launcher(self, *, interrupted):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = fake_sqlcl.install(root / "bin", 'cat > /dev/null\nprintf "before interruption\\n"\nprintf "stderr evidence\\n" >&2\n')
            transcript = root / "transcript.log"
            helper = str(ROOT / "scripts/invoke_sqlcl.ps1").replace("'", "''")
            override = ("function Wait-SqlclProcess { param($Process) $Process.WaitForExit(); "
                        "throw 'simulated wait interruption' }\n") if interrupted else ""
            script = root / "run.ps1"
            script.write_text(
                "param($RunDirectory, $Transcript)\n$ErrorActionPreference = 'Stop'\n"
                + f". '{helper}'\n" + override
                + "try { $code = Invoke-Sqlcl -Arguments @('-S') -WorkingDirectory $RunDirectory "
                "-StdInFile (Join-Path $RunDirectory 'stdin') -TranscriptFile $Transcript; exit $code }\n"
                "catch { Write-Output $_.Exception.Message; exit 2 }\n", encoding="utf-8")
            result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(script), str(root), str(transcript)],
                                    cwd=root, env=fake_sqlcl.environment(binary), text=True, capture_output=True)
            self.assertEqual(result.returncode, 2 if interrupted else 0, result.stdout + result.stderr)
            self.assertTrue(transcript.is_file(), "waiting errors must preserve captured SQLcl diagnostics")
            content = transcript.read_text(encoding="utf-8")
            self.assertIn("before interruption", content)
            self.assertIn("stderr evidence", content)
