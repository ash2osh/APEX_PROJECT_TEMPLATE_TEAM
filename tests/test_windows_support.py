"""Regression tests for the Windows (Windows PowerShell 5.1, PowerShell 7, Git Bash) fixes.

Most of these run everywhere: they pin behaviour that must not change on Linux
and macOS. The ones that need a real Windows are skipped elsewhere.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)

from scripts import sqlcl_session


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("pwsh") and os.name != "nt", "PowerShell async-output double uses the POSIX wait path")
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


class BashCommandTests(unittest.TestCase):
    """scripts/sqlcl_session.py must start Git Bash, not the WSL launcher, on Windows."""

    def test_posix_uses_the_plain_name(self) -> None:
        if os.name == "nt":
            self.skipTest("posix only")
        self.assertEqual(sqlcl_session.bash_command(), "bash")

    def run_as_windows(self, path: str, team_bash: str | None = None) -> str:
        environment = {"PATH": path}
        if team_bash is not None:
            environment["TEAM_BASH"] = team_bash
        # Replace the module's `os`, not os.name itself: that is global, and on
        # Python 3.10 pathlib refuses to build a Path while os.name says "nt".
        windows_os = types.SimpleNamespace(name="nt", environ=os.environ, path=os.path, pathsep=os.pathsep)
        with patch.object(sqlcl_session, "os", windows_os), patch.dict(os.environ, environment, clear=True):
            return sqlcl_session.bash_command()

    def test_windows_skips_the_wsl_launcher_directories_and_takes_git_bash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            system32 = Path(temporary) / "Windows" / "System32"
            git_bin = Path(temporary) / "Git" / "bin"
            for directory in (system32, git_bin):
                directory.mkdir(parents=True)
                (directory / "bash.exe").write_bytes(b"")
            result = self.run_as_windows(os.pathsep.join([str(system32), str(git_bin)]))
        self.assertEqual(Path(result), git_bin / "bash.exe")

    def test_windows_also_skips_windowsapps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apps = Path(temporary) / "Microsoft" / "WindowsApps"
            apps.mkdir(parents=True)
            (apps / "bash.exe").write_bytes(b"")
            with patch.object(sqlcl_session.shutil, "which", return_value=None):
                result = self.run_as_windows(str(apps))
        self.assertEqual(result, "bash")

    def test_team_bash_wins_when_it_names_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            chosen = Path(temporary) / "mybash.exe"
            chosen.write_bytes(b"")
            result = self.run_as_windows("", team_bash=str(chosen))
        self.assertEqual(Path(result), chosen)

    def test_team_bash_naming_a_missing_file_is_ignored(self) -> None:
        with patch.object(sqlcl_session.shutil, "which", return_value=None):
            result = self.run_as_windows("", team_bash=str(ROOT / "no-such-bash.exe"))
        self.assertEqual(result, "bash")

    def test_windows_falls_back_to_git_next_to_git_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            git_root = Path(temporary) / "Git"
            (git_root / "cmd").mkdir(parents=True)
            (git_root / "bin").mkdir()
            (git_root / "cmd" / "git.exe").write_bytes(b"")
            (git_root / "bin" / "bash.exe").write_bytes(b"")
            with patch.object(sqlcl_session.shutil, "which", return_value=str(git_root / "cmd" / "git.exe")):
                result = self.run_as_windows("")
        self.assertEqual(Path(result), git_root / "bin" / "bash.exe")


class TeamPowerShellVerifyCommandTests(unittest.TestCase):
    """The PowerShell entry point advertises and dispatches read-only verification."""

    def engines(self):
        engines = []
        for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
            engine = shutil.which(name)
            if engine and "/snap/bin/" not in Path(engine).as_posix():
                engines.append(engine)
        return engines

    def test_help_describes_verify_options(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("verify <folder>", result.stdout)
                self.assertIn("--jobs N", result.stdout)

    def test_verify_without_folders_reports_its_usage(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "verify"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage: scripts/team.ps1 verify", result.stderr)


class TeamPowerShellRolloutCommandTests(unittest.TestCase):
    """The PowerShell entry point exposes the same rollout contract as Bash."""

    def engines(self):
        return [
            engine
            for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh")
            if (engine := shutil.which(name)) and "/snap/bin/" not in Path(engine).as_posix()
        ]

    def test_help_documents_preflight_inventory_retry_setting(self):
        text = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        self.assertIn("MIGRATION_PREFLIGHT_INVENTORY_RETRIES", text)
        self.assertIn("default 3", text)

    def test_help_describes_rollout_options(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("rollout <manifest.json>", result.stdout)
                self.assertIn("--from-step N", result.stdout)
                self.assertIn("--dry-run", result.stdout)
                self.assertIn("--report <file>", result.stdout)
                self.assertIn('ConvertTo-MigrationFolderArgument $reportValue', (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8"))

    def test_rollout_without_manifest_reports_its_usage(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "rollout"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage: scripts/team.ps1 rollout", result.stderr)


class TeamPowerShellMigrationRehearsalTests(unittest.TestCase):
    """The PowerShell wrapper advertises and forwards rehearsal reports."""

    def engines(self):
        return [
            engine
            for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh")
            if (engine := shutil.which(name)) and "/snap/bin/" not in Path(engine).as_posix()
        ]

    def test_help_describes_migration_rehearsal(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("migrate <folder>", result.stdout)
                self.assertIn("--rehearse", result.stdout)
                self.assertIn("--report <file>", result.stdout)

    def test_migrate_without_folders_reports_rehearsal_usage(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "migrate"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage: scripts/team.ps1 migrate", result.stderr)
                self.assertIn("--rehearse", result.stderr)

    def test_wrapper_converts_a_report_path_for_git_bash(self):
        wrapper = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        self.assertIn('$migrateArguments[$migrateIndex] -ceq "--report"', wrapper)
        self.assertIn('ConvertTo-MigrationFolderArgument ([string] $migrateArguments[$migrateIndex])', wrapper)
        self.assertIn('"--report=" + (ConvertTo-MigrationFolderArgument $reportValue)', wrapper)


class TeamPowerShellMigrationRevisionTests(unittest.TestCase):
    """The PowerShell entry point exposes and converts arguments for revision handling."""

    def engines(self):
        return [
            engine
            for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh")
            if (engine := shutil.which(name)) and "/snap/bin/" not in Path(engine).as_posix()
        ]

    def test_help_describes_revision_and_check_options(self):
        wrapper = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        self.assertIn("revise <folder> [--reason TEXT]", wrapper)
        self.assertIn("revise --check <folder>", wrapper)

    def test_revision_without_arguments_reports_usage(self):
        engines = self.engines()
        if not engines:
            self.skipTest("PowerShell is not installed outside the sandbox-blocked snap launcher")
        for engine in engines:
            with self.subTest(engine=Path(engine).name):
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "team.ps1"), "revise"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage: scripts/team.ps1 revise", result.stderr)

    def test_wrapper_converts_folder_argument_for_git_bash(self):
        wrapper = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        self.assertIn('$revisionArguments[1] = ConvertTo-MigrationFolderArgument', wrapper)
        self.assertIn('$revisionArguments[0] = ConvertTo-MigrationFolderArgument', wrapper)
        self.assertIn('Invoke-TeamBash -ScriptName "team.sh"', wrapper)


class SqlclNativePathTests(unittest.TestCase):
    def test_native_path_is_the_identity_off_windows(self) -> None:
        if os.name == "nt":
            self.skipTest("Git Bash converts the path on Windows")
        result = subprocess.run(
            ["bash", "-c", 'source "$1/scripts/sqlcl_safe.sh"; sqlcl_native_path "$2"', "bash", str(ROOT), "/tmp/some dir/[1]/x.sql"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/tmp/some dir/[1]/x.sql\n")

    def test_invoke_sqlcl_safe_passes_arguments_unchanged_off_windows(self) -> None:
        if os.name == "nt":
            self.skipTest("arguments are converted on Windows")
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            work = Path(temporary) / "work"
            work.mkdir()
            record = Path(temporary) / "args.txt"
            fake = bin_dir / "sql"
            fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "$FAKE_RECORD"\n', encoding="utf-8")
            fake.chmod(0o755)
            environment = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_RECORD=str(record))
            result = subprocess.run(
                ["bash", "-c", 'source "$1/scripts/sqlcl_safe.sh"; invoke_sqlcl_safe "$2" -S "@/tmp/x/doctor.sql" /tmp/out plain', "bash", str(ROOT), str(work)],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(record.read_text(encoding="utf-8").splitlines(), ["-S", "@/tmp/x/doctor.sql", "/tmp/out", "plain"])


class BackupPrefixTokenTests(unittest.TestCase):
    """SQLcl's Windows launcher expands an unquoted * argument, so % means 'every object' too."""

    def test_every_all_objects_test_accepts_the_percent_token(self) -> None:
        sql = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertNotRegex(sql, r"'&&object_prefixes' = '\*'")
        self.assertGreaterEqual(len(re.findall(r"'&&object_prefixes' IN \('\*', '%'\)", sql)), 10)

    def test_the_windows_wrappers_send_percent_for_star(self) -> None:
        self.assertIn('"%"', (ROOT / "scripts" / "backup_db.ps1").read_text(encoding="utf-8"))
        self.assertIn('prefixes="%"', (ROOT / "scripts" / "backup_db.sh").read_text(encoding="utf-8"))


def write_script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@unittest.skipIf(os.name == "nt", "simulates a Windows shell on a POSIX one")
class WindowsShellGateTests(unittest.TestCase):
    """The Bash helpers convert paths only when `uname -s` names a Windows shell.

    A fake `uname` and `cygpath` (which maps /x to C:/x) stand in for Git Bash.
    """

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

    def run_python3(self, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", 'source "$1" "$2" && shift 2 && python3 "$@"', "bash",
             str(ROOT / "scripts" / "load_env.sh"), str(ROOT / ".env.example"), *arguments],
            capture_output=True, text=True, env=environment, check=False,
        )

    def run_sqlcl(self, environment: dict[str, str], work: Path, *arguments: str) -> list[str]:
        result = subprocess.run(
            ["bash", "-c", 'source "$1/scripts/sqlcl_safe.sh"; shift; invoke_sqlcl_safe "$@"', "bash", str(ROOT), str(work), *arguments],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(environment["FAKE_RECORD"]).read_text(encoding="utf-8").splitlines()

    def test_load_env_does_not_shadow_python3_off_windows(self) -> None:
        for uname in ("Linux", "Darwin", "FreeBSD"):
            with self.subTest(uname=uname), tempfile.TemporaryDirectory() as temporary:
                environment = self.environment(Path(temporary), uname)
                result = subprocess.run(
                    ["bash", "-c", 'source "$1" "$2" && type -t python3', "bash",
                     str(ROOT / "scripts" / "load_env.sh"), str(ROOT / ".env.example")],
                    capture_output=True, text=True, env=environment, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "file")

    def test_load_env_accepts_preflight_inventory_retry_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = self.environment(directory, "Linux")
            env_file = directory / ".env"
            env_file.write_text(
                (ROOT / ".env.example").read_text(encoding="utf-8")
                + "\nMIGRATION_PREFLIGHT_INVENTORY_RETRIES=4\n",
                encoding="utf-8",
                newline="\n",
            )
            result = subprocess.run(
                ["bash", "-c", 'source "$1" "$2" && printf "%s\\n" "$MIGRATION_PREFLIGHT_INVENTORY_RETRIES"',
                 "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_file)],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4")

    def test_powershell_env_loader_allows_and_clears_preflight_retry_setting(self) -> None:
        text = (ROOT / "scripts" / "load_env.ps1").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("MIGRATION_PREFLIGHT_INVENTORY_RETRIES"), 2)

    def test_load_env_python3_converts_existing_paths_on_a_windows_shell(self) -> None:
        for uname in ("MINGW64_NT-10.0-26100", "MSYS_NT-10.0-26100", "CYGWIN_NT-10.0"):
            with self.subTest(uname=uname), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment = self.environment(directory, uname)
                existing = directory / "app.sql"
                existing.write_text("", encoding="utf-8")
                result = self.run_python3(
                    environment, str(existing), "-m", "scripts.tool", str(directory / "new-output.txt"), "/no/such/dir/out", "relative"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [f"C:{existing}", "-m", "scripts.tool", f"C:{directory}/new-output.txt", "/no/such/dir/out", "relative"],
                )

    def test_sqlcl_launcher_passes_arguments_unchanged_whatever_the_platform_off_windows(self) -> None:
        for uname in ("Linux", "Darwin", "FreeBSD"):
            with self.subTest(uname=uname), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment = self.environment(directory, uname)
                work = directory / "work"
                work.mkdir()
                lines = self.run_sqlcl(environment, work, "-S", "@/tmp/x/doctor.sql", str(directory / "out.txt"), "plain")
                self.assertEqual(lines[:4], ["-S", "@/tmp/x/doctor.sql", str(directory / "out.txt"), "plain"])
                self.assertEqual(lines[4:], [f"{work}/.sqlcl-path"] * 2)

    def test_sqlcl_launcher_passes_native_paths_on_a_windows_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = self.environment(directory, "MINGW64_NT-10.0-26100")
            work = directory / "work"
            work.mkdir()
            lines = self.run_sqlcl(environment, work, "-S", "@/tmp/x/doctor.sql", str(directory / "out.txt"), "plain")
            self.assertEqual(lines[:4], ["-S", "@C:/tmp/x/doctor.sql", f"C:{directory}/out.txt", "plain"])
            self.assertEqual(lines[4:], [f"C:{work}/.sqlcl-path"] * 2)


class GitAttributesTests(unittest.TestCase):
    """A clone made with core.autocrlf=true must hold the same bytes as one made elsewhere."""

    def test_every_tracked_text_file_is_forced_to_lf(self) -> None:
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("needs a Git checkout")
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--eol", "-z"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        loose = []
        for entry in filter(None, listing.split("\0")):
            info, _, name = entry.partition("\t")
            if info.split()[0] in ("i/lf", "i/crlf", "i/mixed") and "eol=lf" not in info:
                loose.append(name)
        self.assertEqual(loose, [], "text files that .gitattributes does not force to LF")


@unittest.skipUnless(os.name == "nt", "needs Windows PowerShell and Git Bash")
class TeamPowerShellFolderArgumentTests(unittest.TestCase):
    """team.ps1 turns the Windows spellings of a migration folder into migrations/<folder>."""

    def make_checkout(self, temporary: str) -> Path:
        checkout = Path(temporary) / "checkout"
        shutil.copytree(ROOT / "scripts", checkout / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        folder = checkout / "migrations" / "2026-10-02_win-r001"
        folder.mkdir(parents=True)
        (folder / "001-create.sql").write_bytes(b"CREATE TABLE T (ID NUMBER);\n")
        (folder / "checks.json").write_bytes(
            b'{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ok","sql":"SELECT 1 FROM dual","expected":1}]}\n'
        )
        return checkout

    def run_team(self, engine: str, checkout: Path, folder: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(checkout / "scripts" / "team.ps1"),
             "check-conflicts", folder, "--local"],
            capture_output=True,
            text=True,
            cwd=checkout,
            env=environment,
            check=False,
        )

    def python_exe_only_path(self) -> str:
        """A PATH whose only Python is python.exe, as after a python.org or winget install.

        There is no python3.exe and no py launcher, so the Bash helpers' literal
        `python3` cannot be found unless team.ps1 provides one.
        """
        home = Path(sys.executable).resolve().parent
        if not (home / "python.exe").is_file() or (home / "python3.exe").exists():
            self.skipTest("the running Python is not a python.exe-only install")
        system = os.environ["SystemRoot"]
        git = shutil.which("git")
        entries = [str(Path(system) / "System32"), system, str(Path(system) / "System32" / "WindowsPowerShell" / "v1.0"), str(home)]
        if git:
            entries.append(str(Path(git).parent))
        return os.pathsep.join(entries)

    def test_a_python_exe_without_python3_exe_is_enough_for_the_bash_helpers(self) -> None:
        engines = [name for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        if not engines:
            self.skipTest("no PowerShell found")
        environment = {**os.environ, "PATH": self.python_exe_only_path()}
        engines = [shutil.which(name) for name in engines]
        with tempfile.TemporaryDirectory() as temporary:
            checkout = self.make_checkout(temporary)
            for engine in engines:
                with self.subTest(engine=Path(engine).name):
                    result = self.run_team(engine, checkout, "migrations/2026-10-02_win-r001", environment)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Result: exit 0", result.stdout)
                    # The one-run shim folder is gone afterwards.
                    self.assertEqual(sorted(path.name for path in (checkout / "scratch").glob("team-python-*")), [])

    def test_more_spellings_of_the_folder_and_what_is_refused(self) -> None:
        engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        if not engines:
            self.skipTest("no PowerShell found")
        refusal = "use a repository-relative migrations/<dated-folder> path"
        with tempfile.TemporaryDirectory() as temporary:
            checkout = self.make_checkout(temporary)
            inside = checkout / "migrations" / "2026-10-02_win-r001"
            drive, rest = os.path.splitdrive(str(inside))
            accepted = {
                "absolute with forward slashes": inside.as_posix(),
                "absolute with a lower-case drive letter": drive.lower() + rest,
                "absolute with an upper-case drive letter": drive.upper() + rest,
                "relative with .\\ and a trailing slash": ".\\migrations\\2026-10-02_win-r001\\",
                "relative with forward slashes and a trailing slash": "migrations/2026-10-02_win-r001/",
            }
            refused = {
                "outside the checkout": str(Path(temporary) / "elsewhere" / "migrations" / "2026-10-02_win-r001"),
                "a UNC path to the same checkout": "\\\\localhost\\" + drive[0] + "$" + rest,
                "parent directory": "..\\migrations\\2026-10-02_win-r001",
                "the migrations folder itself with another folder": "migrations\\..\\migrations\\2026-10-02_win-r001",
            }
            for engine in engines:
                for label, form in accepted.items():
                    with self.subTest(engine=Path(engine).name, accepted=label):
                        result = self.run_team(engine, checkout, form)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("Result: exit 0", result.stdout)
                for label, form in refused.items():
                    with self.subTest(engine=Path(engine).name, refused=label):
                        result = self.run_team(engine, checkout, form)
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertIn(refusal, result.stderr)
                        self.assertNotIn("Exception", result.stderr)

    def test_a_double_quote_in_a_folder_argument_is_refused_in_one_line(self) -> None:
        # Windows PowerShell 5.1 let .NET's Path.IsPathRooted throw on it and printed an exception block.
        engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        if not engines:
            self.skipTest("no PowerShell found")
        with tempfile.TemporaryDirectory() as temporary:
            checkout = self.make_checkout(temporary)
            for engine in engines:
                with self.subTest(engine=Path(engine).name):
                    result = self.run_team(engine, checkout, 'migrations/double"quote')
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("preflight error:", result.stderr)
                    self.assertNotIn("Exception", result.stderr)
                    self.assertNotIn("Illegal characters", result.stderr)

    def test_a_single_quote_in_an_argument_for_bash_is_refused_not_mangled(self) -> None:
        # Git Bash's launcher reads ' in the command line as a quote: the argument lost it and
        # was joined with the next one ("migrations/singlequote --local"), which then failed
        # with a message about a missing --local.
        engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        if not engines:
            self.skipTest("no PowerShell found")
        with tempfile.TemporaryDirectory() as temporary:
            checkout = self.make_checkout(temporary)
            for engine in engines:
                with self.subTest(engine=Path(engine).name):
                    result = self.run_team(engine, checkout, "migrations/single'quote")
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("team error:", result.stderr)
                    self.assertIn("single quote", result.stderr)
                    self.assertNotIn("select exactly one of", result.stderr)

    def test_no_shim_is_written_when_git_bash_already_runs_python3(self) -> None:
        # The probe passes `python3 -c '...'` to bash -c. Windows PowerShell 5.1 passes embedded
        # double quotes unescaped, which used to make the probe fail and write a needless shim.
        bash = sqlcl_session.bash_command()
        probe = subprocess.run([bash, "-c", "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'"], capture_output=True, check=False)
        if probe.returncode != 0:
            self.skipTest("this Git Bash has no working python3")
        engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        with tempfile.TemporaryDirectory() as temporary:
            script = (
                f". '{ROOT / 'scripts' / 'resolve_python.ps1'}'; "
                f"$shim = Get-TeamPythonShim -BashPath '{bash}' -ScratchRoot '{temporary}'; "
                "if ($null -eq $shim) { 'none' } else { 'shim' }"
            )
            for engine in engines:
                with self.subTest(engine=Path(engine).name):
                    result = subprocess.run([engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "none")
                    self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_a_failing_python3_stub_is_skipped_for_a_working_python(self) -> None:
        # The Microsoft Store alias python3.exe fails when Python came from elsewhere.
        home = Path(sys.executable).resolve().parent
        whoami = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
        if not (home / "python.exe").is_file() or not whoami.is_file():
            self.skipTest("needs python.exe and whoami.exe")
        with tempfile.TemporaryDirectory() as temporary:
            stub_directory = Path(temporary)
            shutil.copy2(whoami, stub_directory / "python3.exe")  # exits 1 for -c ...
            environment = {**os.environ, "PATH": os.pathsep.join([str(stub_directory), str(home)])}
            script = f". '{ROOT / 'scripts' / 'resolve_python.ps1'}'; $python = Resolve-TeamPython; $python.Path"
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, env=environment, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), (home / "python.exe").resolve())

    def test_windows_spellings_of_the_folder_are_accepted(self) -> None:
        engines = [name for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]
        if not engines:
            self.skipTest("no PowerShell found")
        with tempfile.TemporaryDirectory() as temporary:
            checkout = self.make_checkout(temporary)
            forms = (
                r"migrations\2026-10-02_win-r001",
                r".\migrations\2026-10-02_win-r001" + "\\",
                "migrations/2026-10-02_win-r001",
                str(checkout / "migrations" / "2026-10-02_win-r001"),
            )
            for engine in engines:
                for form in forms:
                    with self.subTest(engine=engine, form=form):
                        result = self.run_team(engine, checkout, form)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("Result: exit 0", result.stdout)


@unittest.skipUnless(shutil.which("pwsh"), "needs PowerShell 7")
class ResolvePythonTests(unittest.TestCase):
    """scripts/resolve_python.ps1 on every platform: a working Python 3.10+ is found and run."""

    def test_the_resolver_returns_a_python_that_runs(self) -> None:
        script = (
            f". '{ROOT / 'scripts' / 'resolve_python.ps1'}'; $python = Resolve-TeamPython; "
            "& $python.Path @($python.Prefix) -c 'import sys; print(sys.version_info >= (3, 10))'"
        )
        result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-Command", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")


class SqlclLauncherLayoutTests(unittest.TestCase):
    """The four SQLcl launcher layouts on PATH and their resolution across callers."""

    def test_holds_sqlcl_detects_all_four_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            d1 = base / "d1"
            d1.mkdir()
            (d1 / "sql.exe").touch()
            (d1 / "sql.cmd").touch()
            self.assertTrue(_no_real_sqlcl._holds_sqlcl(str(d1)))

            d2 = base / "d2"
            d2.mkdir()
            (d2 / "sql.exe").touch()
            (d2 / "sql.bat").touch()
            self.assertTrue(_no_real_sqlcl._holds_sqlcl(str(d2)))

            d3_earlier = base / "d3_earlier"
            d3_earlier.mkdir()
            (d3_earlier / "sql.cmd").touch()
            d3_later = base / "d3_later"
            d3_later.mkdir()
            (d3_later / "sql.exe").touch()
            self.assertTrue(_no_real_sqlcl._holds_sqlcl(str(d3_earlier)))
            self.assertTrue(_no_real_sqlcl._holds_sqlcl(str(d3_later)))

            d4 = base / "d4"
            d4.mkdir()
            (d4 / "sql").touch()
            (d4 / "sql.exe").touch()
            self.assertTrue(_no_real_sqlcl._holds_sqlcl(str(d4)))

    @unittest.skipUnless(os.name == "nt", "Windows only: tests launcher resolution across Windows callers")
    def test_all_callers_resolve_working_launcher_across_layouts(self) -> None:
        try:
            from pip._vendor.distlib.scripts import ScriptMaker
        except ImportError:
            self.skipTest("needs distlib ScriptMaker to create native launchers")

        bash = sqlcl_session.bash_command()
        engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)

            def make_exe(d: Path, tag: str) -> None:
                d.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / "sql.py"
                    src.write_text(f'#!python\nprint("OUT:{tag}")\n', encoding="utf-8")
                    maker = ScriptMaker(tmp, str(d), add_launchers=True)
                    maker.clobber = True
                    maker.variants = {""}
                    maker.set_mode = False
                    maker.make("sql.py")

            def make_cmd(d: Path, tag: str) -> None:
                d.mkdir(parents=True, exist_ok=True)
                (d / "sql.cmd").write_text(f"@echo off\r\necho OUT:{tag}\r\n", encoding="ascii")

            def make_bat(d: Path, tag: str) -> None:
                d.mkdir(parents=True, exist_ok=True)
                (d / "sql.bat").write_text(f"@echo off\r\necho OUT:{tag}\r\n", encoding="ascii")

            def make_sh(d: Path, tag: str) -> None:
                d.mkdir(parents=True, exist_ok=True)
                f = d / "sql"
                f.write_text(f'#!/usr/bin/env bash\necho "OUT:{tag}"\n', encoding="utf-8", newline="\n")
                f.chmod(0o755)

            layouts = [
                ("(i) sql.exe + sql.cmd", [("d1", [lambda p: make_exe(p, "exe"), lambda p: make_cmd(p, "cmd")])]),
                ("(ii) sql.exe + sql.bat", [("d1", [lambda p: make_exe(p, "exe"), lambda p: make_bat(p, "bat")])]),
                ("(iii) sql.cmd earlier, sql.exe later", [
                    ("d1", [lambda p: make_cmd(p, "cmd")]),
                    ("d2", [lambda p: make_exe(p, "exe")]),
                ]),
                ("(iv) sql + sql.exe (real)", [("d1", [lambda p: make_sh(p, "sh"), lambda p: make_exe(p, "exe")])]),
            ]

            system_path = os.environ.get("PATH", "")

            for label, specs in layouts:
                with self.subTest(layout=label):
                    created = []
                    safe_label = "".join(c if c.isalnum() else "_" for c in label)
                    sub = base / safe_label
                    for dname, makers in specs:
                        dp = sub / dname
                        dp.mkdir(parents=True, exist_ok=True)
                        for m in makers:
                            m(dp)
                        created.append(dp)

                    test_path = os.pathsep.join(str(d) for d in created) + os.pathsep + system_path
                    env = {**os.environ, "PATH": test_path}

                    # Python shutil.which
                    prog = shutil.which("sql", path=test_path)
                    self.assertIsNotNone(prog, f"Python found no launcher for {label}")
                    py_run = subprocess.run([prog], env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(py_run.returncode, 0, f"Python failed to run {prog}: {py_run.stderr}")
                    self.assertIn("OUT:", py_run.stdout)

                    # PowerShell Start-Process
                    for engine in engines:
                        ps_script = (
                            "$p = Start-Process -FilePath 'sql' -NoNewWindow -PassThru -Wait "
                            "-RedirectStandardOutput out.txt -RedirectStandardError err.txt; exit $p.ExitCode"
                        )
                        with tempfile.TemporaryDirectory() as td:
                            ps_run = subprocess.run(
                                [engine, "-NoProfile", "-Command", ps_script],
                                cwd=td,
                                env=env,
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            self.assertEqual(ps_run.returncode, 0, f"{Path(engine).name} failed: {ps_run.stderr}")
                            out = (Path(td) / "out.txt").read_text(encoding="utf-8")
                            self.assertIn("OUT:", out)

                    # Git Bash command sql
                    bash_run = subprocess.run(
                        [bash, "-c", "command sql"], env=env, capture_output=True, text=True, check=False
                    )
                    self.assertEqual(bash_run.returncode, 0, f"Git Bash failed: {bash_run.stderr}")
                    self.assertIn("OUT:", bash_run.stdout)


class SafeRmtreeTests(unittest.TestCase):
    """safe_rmtree must reliably remove directories and tolerate transient errors."""

    def test_removes_directory_and_contents(self) -> None:
        temp_dir = tempfile.mkdtemp()
        sub = Path(temp_dir) / "subdir"
        sub.mkdir()
        (sub / "file.txt").write_text("content", encoding="utf-8")
        sqlcl_session.safe_rmtree(temp_dir)
        self.assertFalse(os.path.exists(temp_dir))

    def test_ignores_nonexistent_path(self) -> None:
        nonexistent = Path(tempfile.gettempdir()) / "nonexistent_dir_for_test_12345"
        sqlcl_session.safe_rmtree(nonexistent)

    def test_retries_on_transient_oserror(self) -> None:
        calls = 0

        def flaky_rmtree(target: Path | str, *args, **kwargs) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise PermissionError("Access is denied")
            os.rmdir(target)

        temp_dir = tempfile.mkdtemp()
        with patch.object(sqlcl_session.shutil, "rmtree", side_effect=flaky_rmtree):
            sqlcl_session.safe_rmtree(temp_dir, delay=0.01, is_windows=True)
        self.assertGreaterEqual(calls, 3)
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir)


_ORPHAN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
. '{invoke}'
# A process whose parent has ended keeps that parent's id as its ParentProcessId, as the
# children of every ended launcher do. Windows hands the id to a new process later on.
$launcher = Start-Process cmd.exe -ArgumentList '/c', 'start "" /b ping -n 120 127.0.0.1 >nul' -WindowStyle Hidden -PassThru
$null = $launcher.Handle
$launcher.WaitForExit()
$orphan = $null
for ($i = 0; $i -lt 100 -and $null -eq $orphan; $i++) {{
  $orphan = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.Id) AND Name = 'PING.EXE'" | Select-Object -First 1
  if ($null -eq $orphan) {{ Start-Sleep -Milliseconds 100 }}
}}
if ($null -eq $orphan) {{ throw 'the orphan never started' }}
$orphanProcess = [System.Diagnostics.Process]::GetProcessById([int] $orphan.ProcessId)
# Stands for a SQLcl launcher that was given the same id afterwards and has just ended.
$reused = [pscustomobject] @{{ Id = $launcher.Id; StartTime = [datetime]::Now }}
$reused | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ param($Milliseconds) $true }}
$watch = [System.Diagnostics.Stopwatch]::StartNew()
try {{
  {command} $reused
}} finally {{
  "ELAPSED_MS=$([int] $watch.Elapsed.TotalMilliseconds)"
  "ORPHAN_ALIVE=$(-not $orphanProcess.HasExited)"
  if (-not $orphanProcess.HasExited) {{ $orphanProcess.Kill() }}
}}
"""

_GRANDCHILD_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
. '{invoke}'
# A launcher whose child (cmd) has a child of its own (ping); the launcher has ended.
$launcher = Start-Process cmd.exe -ArgumentList '/c', 'cmd /c ping -n 120 127.0.0.1 >nul' -WindowStyle Hidden -PassThru
$null = $launcher.Handle
$ping = $null
for ($i = 0; $i -lt 100 -and $null -eq $ping; $i++) {{
  $inner = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.Id) AND Name = 'cmd.exe'" | Select-Object -First 1
  if ($null -ne $inner) {{ $ping = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($inner.ProcessId) AND Name = 'PING.EXE'" | Select-Object -First 1 }}
  if ($null -eq $ping) {{ Start-Sleep -Milliseconds 100 }}
}}
if ($null -eq $ping) {{ throw 'the grandchild never started' }}
$pingProcess = [System.Diagnostics.Process]::GetProcessById([int] $ping.ProcessId)
$innerProcess = [System.Diagnostics.Process]::GetProcessById([int] $inner.ProcessId)
$view = [pscustomobject] @{{ Id = $launcher.Id; StartTime = $launcher.StartTime }}
$view | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ param($Milliseconds) $true }}
$watch = [System.Diagnostics.Stopwatch]::StartNew()
try {{
  {command} $view
}} finally {{
  "ELAPSED_MS=$([int] $watch.Elapsed.TotalMilliseconds)"
  Start-Sleep -Milliseconds 300
  "CHILD_ALIVE=$(-not $innerProcess.HasExited)"
  "GRANDCHILD_ALIVE=$(-not $pingProcess.HasExited)"
  foreach ($left in @($pingProcess, $innerProcess, $launcher)) {{ if (-not $left.HasExited) {{ $left.Kill() }} }}
}}
"""


@unittest.skipUnless(os.name == "nt", "Windows process ids and ParentProcessId; off Windows Invoke-Sqlcl ends the tree with Kill($true)")
class SqlclProcessTreeTests(unittest.TestCase):
    """invoke_sqlcl.ps1 ends what SQLcl started, and nothing else, after SQLcl ends or is interrupted."""

    def engines(self) -> list[str]:
        return [shutil.which(name) for name in ("powershell.exe", "pwsh.exe") if shutil.which(name)]

    def run_script(self, engine: str, template: str, command: str) -> dict[str, str]:
        script = template.format(invoke=str(ROOT / "scripts" / "invoke_sqlcl.ps1").replace("'", "''"), command=command)
        result = subprocess.run([engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                                capture_output=True, text=True, check=False, timeout=120)
        self.assertEqual(result.returncode, 0, f"{Path(engine).name}: {result.stdout}{result.stderr}")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def test_a_process_left_by_an_earlier_owner_of_the_launcher_id_is_not_waited_for_or_killed(self) -> None:
        # Windows reuses process ids and keeps a dead parent's id on its children: selecting
        # children by ParentProcessId alone adopted unrelated processes (explorer.exe among
        # them on a real machine), waited ten seconds for them and then killed their tree.
        for engine in self.engines():
            for command in ("Wait-SqlclProcess", "Stop-SqlclProcess"):
                with self.subTest(engine=Path(engine).name, command=command):
                    values = self.run_script(engine, _ORPHAN_SCRIPT, command)
                    self.assertEqual(values["ORPHAN_ALIVE"], "True", "a process SQLcl never started must not be killed")
                    self.assertLess(int(values["ELAPSED_MS"]), 5000, "nor waited for")

    def test_what_the_launcher_started_is_ended_at_the_deadline_with_its_own_children(self) -> None:
        engine = self.engines()[0]
        values = self.run_script(engine, _GRANDCHILD_SCRIPT, "Wait-SqlclProcess")
        self.assertEqual(values["CHILD_ALIVE"], "False")
        self.assertEqual(values["GRANDCHILD_ALIVE"], "False", "the child's own children end with it")
        self.assertGreaterEqual(int(values["ELAPSED_MS"]), 9000, "the children get their ten seconds first")
        self.assertLess(int(values["ELAPSED_MS"]), 20000)


if __name__ == "__main__":
    unittest.main()
