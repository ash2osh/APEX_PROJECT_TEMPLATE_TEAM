from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import team


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
                        capture_output=True,
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
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launchers_accept_env_argument_after_subcommand(self):
        env_file = self.repo / ".env.example"
        result = subprocess.run(
            [str(self.repo / "scripts" / "team.sh"), "doctor", "--env", str(env_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status": "valid"', result.stdout)


class OfflineEnvForwardingTests(unittest.TestCase):
    def test_global_env_flag_reaches_an_env_aware_offline_handler(self):
        captured: list[list[str]] = []

        def spy(argv=None):
            captured.append(list(argv or []))
            return 0

        with patch("teamlib.release_adapter.main", spy):
            team.main([
                "--env", "profiles/test.env", "apply-release", "release.tar",
                "--plan", "plan.json", "--history", "history.json", "--target", "t.json",
            ])
        self.assertEqual(len(captured), 1)
        self.assertIn("--env", captured[0])
        self.assertEqual(captured[0][captured[0].index("--env") + 1], "profiles/test.env")

    def test_explicit_env_after_the_subcommand_is_not_duplicated(self):
        captured: list[list[str]] = []

        def spy(argv=None):
            captured.append(list(argv or []))
            return 0

        with patch("teamlib.release_adapter.main", spy):
            team.main([
                "--env", "profiles/global.env", "apply-release", "release.tar",
                "--plan", "plan.json", "--history", "history.json", "--target", "t.json",
                "--env", "profiles/explicit.env",
            ])
        self.assertEqual(captured[0].count("--env"), 1)
        self.assertEqual(captured[0][captured[0].index("--env") + 1], "profiles/explicit.env")

    def test_global_env_is_not_forwarded_to_handlers_that_reject_it(self):
        captured: list[list[str]] = []

        def spy(argv=None):
            captured.append(list(argv or []))
            return 0

        with patch("teamlib.ci.main", spy):
            team.main(["--env", "profiles/test.env", "ci-doctor", "--contract", "ci/runner-contract.json"])
        self.assertNotIn("--env", captured[0])


class RepositoryRootTests(unittest.TestCase):
    def test_repo_root_is_stable_from_any_subdirectory(self):
        root = Path(__file__).resolve().parents[2]
        original = Path.cwd()
        try:
            os.chdir(root / "docs")
            self.assertEqual(team._repo_root().resolve(), root)
            os.chdir(root)
            self.assertEqual(team._repo_root().resolve(), root)
        finally:
            os.chdir(original)

    def test_repo_root_falls_back_to_the_script_parent_outside_a_repository(self):
        root = Path(__file__).resolve().parents[2]
        original = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="team-nonrepo-") as directory:
            try:
                os.chdir(directory)
                self.assertEqual(team._repo_root().resolve(), root)
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
