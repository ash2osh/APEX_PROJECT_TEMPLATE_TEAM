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
    CONTRACT_TESTS, HOOK, changed_powershell_scripts, changed_python_files, changed_shell_scripts, changed_test_files, existing, find_bash,
    find_powershell, is_template_checkout, powershell_parse_command, test_modules_to_run, untracked_step,
)


class PrePushCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("scripts/a.py", "scripts/run.sh", "scripts/sub/b.py", "tools/t.py", "tests/test_a.py", "tests/test_b.py", "tests/helper.py", "docs/x.py"):
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
        self.assertEqual(changed_test_files(paths, self.root), ["tests/test_a.py", "tests/test_b.py"])

    def test_a_deleted_test_module_is_not_run(self):
        paths = ["tests/test_a.py", "tests/test_gone.py", "tests/test_gone.py"]
        self.assertEqual(changed_test_files(paths, self.root), ["tests/test_a.py"])

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
        # The copied loaders read the release from the template manifest (Phase 2 release switch).
        shutil.copy2(Path(__file__).resolve().parents[1] / "template-manifest.json", repo / "template-manifest.json")

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


class ChangedPathsTests(unittest.TestCase):
    """changed_paths() compares with the upstream branch or origin/main; with neither, it checks every tracked file."""

    GIT_ENVIRONMENT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = {name: value for name, value in os.environ.items() if name not in self.GIT_ENVIRONMENT}
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True)

    def commit_file(self, name, message):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
        self.git("add", name)
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-q", "-m", message)

    def init_repository(self):
        self.git("init", "-q", "-b", "main")
        self.commit_file("scripts/committed.py", "first commit")

    def paths_for_this_repository(self):
        with patch.object(pre_push_check, "ROOT", self.root):
            return pre_push_check.changed_paths()

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_first_push_without_a_comparison_base_checks_every_committed_file(self):
        self.init_repository()
        self.commit_file("tests/test_committed.py", "second commit")
        self.assertEqual(self.paths_for_this_repository(), ["scripts/committed.py", "tests/test_committed.py"])

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_without_a_comparison_base_uncommitted_and_untracked_files_are_still_included(self):
        self.init_repository()
        (self.root / "scripts" / "edited.py").write_text("", encoding="utf-8")
        self.git("add", "scripts/edited.py")
        (self.root / "tools").mkdir()
        (self.root / "tools" / "new.py").write_text("", encoding="utf-8")
        self.assertIn("tools/new.py", self.paths_for_this_repository())
        self.assertIn("scripts/committed.py", self.paths_for_this_repository())

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_with_origin_main_only_commits_after_it_are_compared(self):
        self.init_repository()
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.commit_file("tests/test_new.py", "after origin main")
        self.assertEqual(self.paths_for_this_repository(), ["tests/test_new.py"])

    def test_main_prints_the_fallback_note_only_without_a_comparison_base(self):
        with patch.object(pre_push_check, "ROOT", self.root), \
                patch.object(pre_push_check, "changed_paths", return_value=[]), \
                patch.object(pre_push_check, "comparison_base", return_value=None):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                pre_push_check.main([])
        self.assertIn("note: no upstream branch or origin/main to compare with; checking every tracked file", output.getvalue())

        output = io.StringIO()
        with patch.object(pre_push_check, "ROOT", self.root), \
                patch.object(pre_push_check, "changed_paths", return_value=[]), \
                patch.object(pre_push_check, "comparison_base", return_value="origin/main"), \
                contextlib.redirect_stdout(output):
            pre_push_check.main([])
        self.assertNotIn("note: no upstream branch", output.getvalue())


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

    def run_check(self, args, paths, tools, step=None, bash_finder=None):
        """Run main() on a fake repository with fake tools; return (exit code, printed output).

        find_bash is faked from tools["bash"], so the result does not depend on this machine's PATH (on Windows,
        find_bash never asks which() for bash). bash_finder replaces that fake when a test needs the real lookup.
        """
        def fake_step(label, command):
            self.calls.append((label, list(command)))
            return True

        output = io.StringIO()
        with patch.object(pre_push_check, "ROOT", self.root), \
                patch.object(pre_push_check, "changed_paths", return_value=paths), \
                patch.object(pre_push_check, "step", step or fake_step), \
                patch.object(pre_push_check, "find_bash", new=bash_finder or (lambda: tools.get("bash"))), \
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
        # On Windows a missing pwsh also looks for Windows PowerShell; the fake tools dict has none, so that note is printed.
        pwsh_note = "note: neither pwsh nor Windows PowerShell is on PATH" if os.name == "nt" else "note: pwsh is not on PATH"
        self.assertIn(pwsh_note, output)
        self.assertEqual([label for label, _ in self.calls], ["Shell syntax scripts/run.sh"])

    def test_without_git_bash_on_windows_the_shell_check_is_skipped_with_one_note_and_the_run_passes(self):
        real_find_bash = pre_push_check.find_bash

        def no_bash_on_windows():
            return real_find_bash(which=lambda name: None, environ={"PATH": ""}, is_windows=True)

        code, output = self.run_check([], ["scripts/run.sh"], {}, bash_finder=no_bash_on_windows)
        self.assertEqual(code, 0)
        self.assertEqual(output.count("note: Git Bash (bash) is not on PATH, so the shell syntax check was not run"), 1)
        self.assertEqual([label for label, _ in self.calls], [])

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

    def test_a_core_stamp_runs_the_shared_file_check(self):
        (self.root / ".core-stamp.json").write_text("{}", encoding="utf-8")
        (self.root / "scripts" / "sync_core.py").write_text("", encoding="utf-8")
        code, _ = self.run_check([], [], {})
        self.assertEqual(code, 0)
        checks = [command for label, command in self.calls if label.startswith("Shared core files")]
        self.assertEqual(checks, [[pre_push_check.sys.executable, str(self.root / "scripts" / "sync_core.py"), "check", str(self.root)]])

    def test_without_a_core_stamp_the_shared_file_check_does_not_run(self):
        (self.root / "scripts" / "sync_core.py").write_text("", encoding="utf-8")
        code, output = self.run_check([], [], {})
        self.assertEqual(code, 0)
        self.assertFalse(any(label.startswith("Shared core files") for label, _ in self.calls))
        self.assertIn("nothing to check", output)

    def test_a_deleted_test_file_is_not_run_as_a_step(self):
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "test_kept.py").write_text("", encoding="utf-8")
        code, _ = self.run_check([], ["tests/test_gone.py", "tests/test_kept.py"], {"ruff": "/fake/ruff"})
        self.assertEqual(code, 0)
        labels = [label for label, _ in self.calls]
        self.assertIn("Changed test tests/test_kept.py", labels)
        self.assertFalse(any("test_gone" in label for label in labels))

    def test_the_core_stamp_step_is_skipped_when_a_template_lock_exists(self):
        (self.root / ".core-stamp.json").write_text("{}", encoding="utf-8")
        (self.root / "scripts" / "sync_core.py").write_text("", encoding="utf-8")
        (self.root / ".template-lock.json").write_text("{}", encoding="utf-8")
        code, _ = self.run_check([], [], {})
        self.assertEqual(code, 0)
        self.assertFalse(any(label.startswith("Shared core files") for label, _ in self.calls))

        (self.root / ".template-lock.json").unlink()
        self.calls.clear()
        code, _ = self.run_check([], [], {})
        self.assertEqual(code, 0)
        self.assertTrue(any(label.startswith("Shared core files") for label, _ in self.calls))

    def test_contract_modules_run_with_the_changed_ones_once_and_missing_ones_are_skipped(self):
        for name in ("tests/test_a.py", *CONTRACT_TESTS[:2]):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text("", encoding="utf-8")
        paths = ["tests/test_a.py", "tests/test_template_manifest.py"]
        self.assertEqual(test_modules_to_run(paths, self.root), [
            "tests/test_a.py", "tests/test_documentation_contract.py", "tests/test_template_manifest.py",
        ])
        self.assertEqual(test_modules_to_run([], self.root), ["tests/test_documentation_contract.py", "tests/test_template_manifest.py"])

    def test_a_downstream_project_without_contract_modules_runs_only_changed_tests(self):
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "test_b.py").write_text("", encoding="utf-8")
        self.assertEqual(test_modules_to_run(["tests/test_b.py"], self.root), ["tests/test_b.py"])
        self.assertEqual(test_modules_to_run([], self.root), [])

    def test_contract_modules_run_as_their_own_labelled_steps(self):
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests" / "test_template_manifest.py").write_text("", encoding="utf-8")
        (self.root / "tests" / "test_a.py").write_text("", encoding="utf-8")
        code, _ = self.run_check([], ["tests/test_a.py"], {"ruff": "/fake/ruff"})
        self.assertEqual(code, 0)
        labels = [label for label, _ in self.calls]
        self.assertIn("Changed test tests/test_a.py", labels)
        self.assertIn("Contract test tests/test_template_manifest.py", labels)

    def test_a_template_checkout_is_one_with_a_stamp_and_no_template_lock(self):
        self.assertFalse(is_template_checkout(self.root))
        (self.root / ".core-stamp.json").write_text("{}", encoding="utf-8")
        self.assertTrue(is_template_checkout(self.root))
        (self.root / ".template-lock.json").write_text("{}", encoding="utf-8")
        self.assertFalse(is_template_checkout(self.root))

    def test_untracked_files_fail_the_step_and_name_each_path(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(untracked_step(["scripts/new.py", "docs/new.md"]))
        text = output.getvalue()
        self.assertIn("untracked file scripts/new.py: git add it or ignore it; CI and the manifest/ownership tests see only tracked files", text)
        self.assertIn("untracked file docs/new.md:", text)
        self.assertTrue(text.splitlines()[-1].endswith("FAILED"))

    def test_no_untracked_files_pass_the_step_silently(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(untracked_step([]))
        self.assertNotIn("untracked file", output.getvalue())

    def test_a_template_checkout_refuses_untracked_files_before_the_tests(self):
        (self.root / ".core-stamp.json").write_text("{}", encoding="utf-8")
        with patch.object(pre_push_check, "git_lines", return_value=["scripts/new.py"]) as git:
            code, output = self.run_check([], ["tests/test_a.py"], {"ruff": "/fake/ruff"})
        self.assertEqual(code, 1)
        git.assert_any_call("ls-files", "--others", "--exclude-standard", "--", "scripts", "tests", "tools", "docs")
        self.assertIn("untracked file scripts/new.py:", output)
        self.assertIn("CHECKS FAILED: Untracked files", output)
        self.assertNotIn("Changed test tests/test_a.py", [label for label, _ in self.calls])

    def test_a_downstream_project_does_not_check_untracked_files(self):
        (self.root / ".core-stamp.json").write_text("{}", encoding="utf-8")
        (self.root / ".template-lock.json").write_text("{}", encoding="utf-8")
        with patch.object(pre_push_check, "git_lines", return_value=["scripts/new.py"]):
            code, output = self.run_check([], [], {})
        self.assertEqual(code, 0)
        self.assertNotIn("untracked file", output)

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


class BashAndPowerShellLookupTests(unittest.TestCase):
    """find_bash and find_powershell never return the WSL launcher; the Windows branches are forced with is_windows."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_file(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return str(path)

    def test_windows_find_bash_skips_the_wsl_launcher_and_takes_the_next_bash_on_path(self):
        self.make_file("System32", "bash.exe")
        git_bin_bash = self.make_file("Git", "bin", "bash.exe")
        path = os.pathsep.join([str(self.root / "System32"), f'"{self.root / "Git" / "bin"}"'])
        self.assertEqual(find_bash(which=lambda name: None, environ={"PATH": path}, is_windows=True), git_bin_bash)

    def test_windows_find_bash_returns_none_when_only_the_wsl_launcher_is_on_path(self):
        self.make_file("System32", "bash.exe")
        found = find_bash(which=lambda name: None, environ={"PATH": str(self.root / "System32")}, is_windows=True)
        self.assertIsNone(found)

    def test_windows_find_bash_prefers_an_existing_team_bash(self):
        team_bash = self.make_file("tools", "bash.exe")
        self.make_file("Git", "bin", "bash.exe")
        environ = {"TEAM_BASH": team_bash, "PATH": str(self.root / "Git" / "bin")}
        self.assertEqual(find_bash(which=lambda name: None, environ=environ, is_windows=True), team_bash)

    def test_windows_find_bash_ignores_a_team_bash_that_is_missing(self):
        git_bin_bash = self.make_file("Git", "bin", "bash.exe")
        environ = {"TEAM_BASH": str(self.root / "missing" / "bash.exe"), "PATH": str(self.root / "Git" / "bin")}
        self.assertEqual(find_bash(which=lambda name: None, environ=environ, is_windows=True), git_bin_bash)

    def test_windows_find_bash_falls_back_to_the_git_for_windows_copy_beside_git(self):
        usr_bin_bash = self.make_file("usr", "bin", "bash.exe")
        git = str(self.root / "cmd" / "git.exe")
        found = find_bash(which=lambda name: git if name == "git" else None, environ={"PATH": ""}, is_windows=True)
        self.assertEqual(found, usr_bin_bash)

    def test_find_bash_off_windows_is_the_bash_on_path(self):
        self.assertEqual(find_bash(which=lambda name: "/usr/bin/bash" if name == "bash" else None, is_windows=False), "/usr/bin/bash")
        self.assertIsNone(find_bash(which=lambda name: None, is_windows=False))

    def test_find_powershell_prefers_pwsh(self):
        tools = {"pwsh": "/fake/pwsh", "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}
        self.assertEqual(find_powershell(which=tools.get, is_windows=True), ("/fake/pwsh", "PowerShell parse"))

    def test_find_powershell_uses_windows_powershell_when_pwsh_is_missing_on_windows(self):
        windows_powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        found = find_powershell(which={"powershell": windows_powershell}.get, is_windows=True)
        self.assertEqual(found, (windows_powershell, "PowerShell parse (Windows PowerShell)"))

    def test_find_powershell_does_not_use_windows_powershell_off_windows_or_when_absent(self):
        self.assertIsNone(find_powershell(which={"powershell": "/fake/powershell"}.get, is_windows=False))
        self.assertIsNone(find_powershell(which=lambda name: None, is_windows=True))


if __name__ == "__main__":
    unittest.main()
