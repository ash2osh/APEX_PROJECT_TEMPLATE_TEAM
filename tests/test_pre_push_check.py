import contextlib
import io
import os
import shutil
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pre_push_check
from scripts.pre_push_check import (
    HOOK, changed_powershell_scripts, changed_python_files, changed_shell_scripts, changed_test_files, existing, powershell_parse_command,
)


class PrePushCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("scripts/a.py", "scripts/run.sh", "scripts/sub/b.py", "tools/t.py", "tests/test_a.py", "tests/helper.py", "docs/x.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_changed_test_files_keeps_only_top_level_test_modules_once(self):
        paths = [
            "tests/test_a.py", "tests/test_a.py", "tests/test_b.py", "tests/helper.py", "tests/known_flaky.txt",
            "tests/fixtures/test_c.py", "scripts/test_d.py", "docs/x.md",
        ]
        self.assertEqual(changed_test_files(paths), ["tests/test_a.py", "tests/test_b.py"])

    def test_changed_python_files_keeps_existing_code_files_in_the_code_folders(self):
        paths = ["scripts/a.py", "scripts/a.py", "scripts/sub/b.py", "tools/t.py", "tests/helper.py", "docs/x.py", "scripts/gone.py", "scripts/run.sh"]
        self.assertEqual(changed_python_files(paths, self.root), ["scripts/a.py", "scripts/sub/b.py", "tests/helper.py", "tools/t.py"])

    def test_changed_shell_scripts_keeps_existing_top_level_scripts(self):
        paths = ["scripts/run.sh", "scripts/missing.sh", "scripts/sub/x.sh", "docs/run.sh", "scripts/a.py"]
        self.assertEqual(changed_shell_scripts(paths, self.root), ["scripts/run.sh"])

    def test_existing_skips_folders_a_downstream_project_does_not_have(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scripts").mkdir()
            (root / "tools").mkdir()
            self.assertEqual(existing(("scripts", "tools", "tests"), root), ["scripts", "tools"])

    @unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "Git and POSIX sh are required")
    def test_hook_does_not_pass_repository_environment_to_fixture_tests(self):
        repo = self.root / "hook-repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "tests").mkdir()
        shutil.copy2(Path(__file__).resolve().parents[1] / "scripts/pre_push_check.py", repo / "scripts")

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-q", "-b", "main")
        git("config", "user.name", "fixture")
        git("config", "user.email", "fixture@example.com")
        git("add", "scripts")
        git("commit", "-q", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        (repo / "tests/test_environment.py").write_text(
            "import os\nimport unittest\n\n\n"
            "class EnvironmentTests(unittest.TestCase):\n"
            "    def test_git_fixture_is_isolated(self):\n"
            "        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_PREFIX'):\n"
            "            self.assertFalse(name in os.environ, name)\n",
            encoding="utf-8",
        )
        git("add", "tests")
        git("commit", "-q", "-m", "add test")
        hook = self.root / "pre-push"
        hook.write_text(HOOK, encoding="utf-8", newline="\n")
        environment = dict(os.environ, GIT_DIR=str(repo / ".git"), GIT_WORK_TREE=str(repo),
                           GIT_INDEX_FILE=str(repo / ".git/index"), GIT_PREFIX="")
        result = subprocess.run(["sh", str(hook)], cwd=repo, env=environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)


    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is not available")
    def test_hook_skips_with_a_message_in_a_checkout_without_the_script(self):
        hook = self.root / "pre-push"
        hook.write_text(HOOK, encoding="utf-8", newline="\n")
        empty = self.root / "old-checkout"
        empty.mkdir()
        result = subprocess.run(["sh", str(hook)], cwd=empty, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check skipped", result.stderr)
        self.assertIn("exec python3 scripts/pre_push_check.py", HOOK)


class ToolStepTests(unittest.TestCase):
    """Failed steps are named, and ShellCheck and the PowerShell parse run only when their tool is present."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("scripts/a.py", "scripts/run.sh", "scripts/b.ps1"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        self.calls = []

    def run_check(self, args, paths, tools, step=None):
        """Run main() on a fake repository with fake tools; return (exit code, printed output)."""
        def fake_step(label, command):
            self.calls.append((label, list(command)))
            return True

        output = io.StringIO()
        with patch.object(pre_push_check, "ROOT", self.root), \
                patch.object(pre_push_check, "changed_paths", return_value=paths), \
                patch.object(pre_push_check, "step", step or fake_step), \
                patch.object(pre_push_check.shutil, "which", new=lambda name: tools.get(name)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = pre_push_check.main(args)
        return code, output.getvalue()

    def test_changed_powershell_scripts_keeps_existing_top_level_scripts(self):
        (self.root / "scripts/sub").mkdir()
        (self.root / "scripts/sub/x.ps1").write_text("", encoding="utf-8")
        paths = ["scripts/b.ps1", "scripts/b.ps1", "scripts/sub/x.ps1", "scripts/gone.ps1", "docs/x.ps1", "scripts/run.sh"]
        self.assertEqual(changed_powershell_scripts(paths, self.root), ["scripts/b.ps1"])

    def test_powershell_parse_command_quotes_each_path_and_exits_one_on_an_error(self):
        command = powershell_parse_command("/usr/bin/pwsh", ["scripts/a'b.ps1", "scripts/c.ps1"])
        self.assertEqual(command[:3], ["/usr/bin/pwsh", "-NoProfile", "-Command"])
        script = command[3]
        self.assertIn("'scripts/a''b.ps1', 'scripts/c.ps1'", script)
        self.assertIn("ParseFile", script)
        self.assertIn("exit 1", script)

    def test_shellcheck_and_the_powershell_parse_run_on_changed_scripts_when_their_tools_exist(self):
        tools = {"bash": "/fake/bash", "shellcheck": "/fake/shellcheck", "pwsh": "/fake/pwsh"}
        code, output = self.run_check([], ["scripts/run.sh", "scripts/b.ps1"], tools)
        self.assertEqual(code, 0)
        self.assertIn(("ShellCheck (error severity)", ["/fake/shellcheck", "--severity=error", "scripts/run.sh"]), self.calls)
        parse = [command for label, command in self.calls if label == "PowerShell parse"]
        self.assertEqual(len(parse), 1)
        self.assertEqual(parse[0][:3], ["/fake/pwsh", "-NoProfile", "-Command"])
        self.assertIn("'scripts/b.ps1'", parse[0][3])
        self.assertIn("all checks passed", output)

    def test_a_missing_shellcheck_or_pwsh_prints_one_note_each_and_does_not_fail(self):
        tools = {"bash": "/fake/bash"}
        code, output = self.run_check([], ["scripts/run.sh", "scripts/b.ps1"], tools)
        self.assertEqual(code, 0)
        self.assertIn("note: shellcheck is not on PATH", output)
        self.assertIn("note: pwsh is not on PATH", output)
        self.assertEqual([label for label, _ in self.calls], ["Shell syntax scripts/run.sh"])

    def test_all_mode_checks_every_shell_and_powershell_script(self):
        tools = {"bash": "/fake/bash", "ruff": "/fake/ruff", "shellcheck": "/fake/shellcheck", "pwsh": "/fake/pwsh"}
        code, _ = self.run_check(["--all"], [], tools)
        self.assertEqual(code, 0)
        shellcheck = [command for label, command in self.calls if label.startswith("ShellCheck")]
        self.assertEqual(shellcheck, [["/fake/shellcheck", "--severity=error", "scripts/run.sh"]])
        parse = [command for label, command in self.calls if label == "PowerShell parse"]
        self.assertIn("'scripts/b.ps1'", parse[0][3])

    def test_every_failed_step_is_named_on_the_last_line(self):
        tools = {"bash": "/fake/bash", "ruff": "/fake/ruff", "shellcheck": "/fake/shellcheck"}

        def failing_step(label, command):
            return False

        code, output = self.run_check([], ["scripts/a.py", "scripts/run.sh"], tools, step=failing_step)
        lines = output.splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "CHECKS FAILED: Python syntax; Ruff; Shell syntax scripts/run.sh; ShellCheck (error severity)")
        self.assertIn("fix them before pushing", lines[-2])

    def test_a_real_syntax_error_names_its_step_on_the_last_line(self):
        (self.root / "scripts/bad.py").write_text("def broken(:\n", encoding="utf-8")
        output = io.StringIO()
        with patch.object(pre_push_check, "ROOT", self.root), \
                patch.object(pre_push_check, "changed_paths", return_value=["scripts/bad.py"]), \
                contextlib.redirect_stdout(output):
            code = pre_push_check.main([])
        last = output.getvalue().splitlines()[-1]
        self.assertEqual(code, 1)
        self.assertTrue(last.startswith("CHECKS FAILED: "), last)
        self.assertIn("Python syntax", last)


if __name__ == "__main__":
    unittest.main()
