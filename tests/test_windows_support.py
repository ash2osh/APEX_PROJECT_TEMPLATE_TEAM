"""Regression tests for the Windows (Windows PowerShell 5.1, PowerShell 7, Git Bash) fixes.

Most of these run everywhere: they pin behaviour that must not change on Linux
and macOS. The ones that need a real Windows are skipped elsewhere.
"""

import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)

from scripts import sqlcl_session


ROOT = Path(__file__).resolve().parents[1]


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

    def run_team(self, engine: str, checkout: Path, folder: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(checkout / "scripts" / "team.ps1"),
             "check-conflicts", folder, "--local"],
            capture_output=True,
            text=True,
            cwd=checkout,
            check=False,
        )

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


if __name__ == "__main__":
    unittest.main()
