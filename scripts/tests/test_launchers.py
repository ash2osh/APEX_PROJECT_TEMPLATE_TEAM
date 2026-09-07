from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[2]

    def test_bash_launchers_delegate_and_work_outside_repo(self):
        with tempfile.TemporaryDirectory(prefix="team-launcher-") as directory:
            cwd = Path(directory)
            commands = (
                ("scripts/team.sh", ("new-migration", "--help")),
                ("scripts/build_release.sh", ("--help",)),
                ("scripts/apply_release.sh", ("--help",)),
            )
            for launcher, arguments in commands:
                with self.subTest(launcher=launcher):
                    result = subprocess.run(
                        [str(self.repo / launcher), *arguments],
                        cwd=cwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_wrappers_have_no_inline_workflow_logic(self):
        for path in sorted((self.repo / "scripts").glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            if path.name in {"team.sh", "apply_release.sh"}:
                continue
            with self.subTest(path=path.name):
                self.assertIn("exec", text)
                self.assertNotIn("sql ", text)

    def test_power_shell_entry_points_are_present_and_array_forwarding_is_used(self):
        paths = sorted((self.repo / "scripts").glob("*.ps1"))
        self.assertTrue(paths)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("@args", text)
                self.assertNotIn("Invoke-Expression", text)

    def test_native_power_shell_help_when_available(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is not installed on this host")
        result = subprocess.run(
            [powershell, "-NoProfile", "-File", str(self.repo / "scripts" / "team.ps1"), "new-migration", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
