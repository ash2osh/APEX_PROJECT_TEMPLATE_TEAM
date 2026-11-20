"""Offline checks for scripts/windows_check.ps1, the Windows PowerShell 5.1 readiness script.

Only the script's own behaviour is tested here, with pwsh standing in for the engine. The real
`tests` and `pre-push` steps never run here, since they would run the whole suite again: the `tests`
step runs only against a fake root holding two small unittest modules. The tests also do not read
the developer's .env: each run removes PROJECT_ENV_FILE from the child environment, and the loader
probe names its own file.
"""

from __future__ import annotations

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows_check.ps1"
PWSH = _test_subprocess_safety.working_pwsh()
RUN_TIMEOUT_SECONDS = 120


def blank(chunk: str) -> str:
    """Replace every character except newlines with a space, so line numbers still match."""
    return "".join("\n" if character == "\n" else " " for character in chunk)


def strip_comments_and_strings(text: str) -> str:
    """Blank out comments, here-strings and quoted strings with a simple scanner (a heuristic)."""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        following = text[index + 1] if index + 1 < length else ""
        if character == "<" and following == "#":
            end = text.find("#>", index + 2)
            end = length if end < 0 else end + 2
            out.append(blank(text[index:end]))
            index = end
        elif character == "#":
            end = text.find("\n", index)
            end = length if end < 0 else end
            out.append(blank(text[index:end]))
            index = end
        elif character == "@" and following in ("'", '"'):
            # A here-string ends at a line that starts with the closing quote and @.
            terminator = "\n" + following + "@"
            end = text.find(terminator, index + 2)
            end = length if end < 0 else end + len(terminator)
            out.append(blank(text[index:end]))
            index = end
        elif character in ("'", '"'):
            quote = character
            end = index + 1
            while end < length:
                if quote == '"' and text[end] == "`":
                    end += 2
                    continue
                if text[end] == quote:
                    if end + 1 < length and text[end + 1] == quote:
                        end += 2
                        continue
                    break
                end += 1
            end = min(end + 1, length)
            out.append(blank(text[index:end]))
            index = end
        else:
            out.append(character)
            index += 1
    return "".join(out)


def run_windows_check(script: Path, *arguments: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PROJECT_ENV_FILE", None)
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(script), *arguments],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )


class WindowsCheckScriptTextTests(unittest.TestCase):
    """Checks that need no PowerShell at all."""

    def test_script_is_ascii_lf_and_declares_windows_powershell_51(self) -> None:
        data = SCRIPT.read_bytes()
        offending = [offset for offset, byte in enumerate(data) if byte > 127]
        self.assertEqual([], offending[:5], f"non-ASCII byte at offset {offending[:1]}")
        self.assertNotIn(b"\r", data, "the script must use LF line endings, not CRLF")
        first_line = data.split(b"\n", 1)[0]
        self.assertEqual(b"#Requires -Version 5.1", first_line)

    def test_script_uses_no_powershell_7_only_operators(self) -> None:
        text = SCRIPT.read_text(encoding="ascii")
        stripped = strip_comments_and_strings(text)
        original_lines = text.splitlines()
        stripped_lines = stripped.splitlines()
        forbidden = [
            ("??", "null-coalescing operator"),
            ("?.", "null-conditional member access"),
            ("&&", "pipeline chain operator"),
            ("||", "pipeline chain operator"),
        ]
        problems: list[str] = []
        for number, line in enumerate(stripped_lines, start=1):
            for token, meaning in forbidden:
                if token in line:
                    problems.append(f"line {number} ({meaning} {token!r}): {original_lines[number - 1].strip()}")
            if re.search(r"\s\?\s", line) or re.search(r"\?[^\n]*\s:\s", line):
                problems.append(f"line {number} (ternary-looking '? :'): {original_lines[number - 1].strip()}")
        self.assertEqual([], problems, "PowerShell 7-only syntax found:\n" + "\n".join(problems))


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class WindowsCheckScriptRunTests(unittest.TestCase):
    """Runs the script under pwsh, against the real repository and against throwaway fake roots."""

    def make_fake_root(self, directory: Path) -> Path:
        scripts = directory / "scripts"
        scripts.mkdir()
        shutil.copy2(SCRIPT, scripts / "windows_check.ps1")
        for name in ("load_env.ps1", "template_release.ps1"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        return scripts / "windows_check.ps1"

    def test_fast_steps_pass_on_the_real_repository(self) -> None:
        result = run_windows_check(
            SCRIPT,
            "-AllowPowerShellCore",
            "-RepoRoot",
            str(ROOT),
            "-Only",
            "ascii,parse,env-loader",
        )
        output = result.stdout
        self.assertEqual(0, result.returncode, output + result.stderr)
        self.assertIn("RESULT ascii PASS", output)
        self.assertIn("RESULT parse PASS", output)
        self.assertIn("RESULT env-loader PASS", output)

    def test_non_ascii_script_without_bom_fails_the_ascii_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self.make_fake_root(Path(temporary))
            body = "# caf\u00e9\nWrite-Output 'ok'\n".encode("utf-8")
            bad = script.parent / "accented.ps1"
            bad.write_bytes(body)
            result = run_windows_check(script, "-AllowPowerShellCore", "-RepoRoot", temporary, "-Only", "ascii")
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT ascii FAIL", result.stdout)

            bad.write_bytes(b"\xef\xbb\xbf" + body)
            result = run_windows_check(script, "-AllowPowerShellCore", "-RepoRoot", temporary, "-Only", "ascii")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT ascii PASS", result.stdout)

    def test_unterminated_if_fails_the_parse_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self.make_fake_root(Path(temporary))
            (script.parent / "broken.ps1").write_bytes(b"if (\n")
            result = run_windows_check(script, "-AllowPowerShellCore", "-RepoRoot", temporary, "-Only", "parse")
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT parse FAIL", result.stdout)
            self.assertIn("broken.ps1", result.stdout)

    def test_step_selection_errors_exit_2(self) -> None:
        both = run_windows_check(SCRIPT, "-AllowPowerShellCore", "-Only", "environment", "-Skip", "ascii")
        self.assertEqual(2, both.returncode, both.stdout + both.stderr)
        unknown = run_windows_check(SCRIPT, "-AllowPowerShellCore", "-Only", "no-such-step")
        self.assertEqual(2, unknown.returncode, unknown.stdout + unknown.stderr)

    def test_environment_step_passes_only_with_core_allowed(self) -> None:
        allowed = run_windows_check(SCRIPT, "-AllowPowerShellCore", "-Only", "environment")
        self.assertEqual(0, allowed.returncode, allowed.stdout + allowed.stderr)
        self.assertIn("RESULT environment PASS", allowed.stdout)

        refused = run_windows_check(SCRIPT, "-Only", "environment")
        if _test_subprocess_safety.selected_powershell_engine() == "powershell":
            # The refusal is for PowerShell 6 or newer; Windows PowerShell 5.1 passes without the switch.
            self.assertEqual(0, refused.returncode, refused.stdout + refused.stderr)
            self.assertIn("RESULT environment PASS", refused.stdout)
            return
        self.assertEqual(1, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("RESULT environment FAIL", refused.stdout)
        self.assertIn("run this script with powershell.exe", refused.stdout)


STUB_TEAM = (
    "$command = [string] $args[0]\n"
    "if ($command -eq 'targets') { Write-Output 'SCHEMA1'; exit 0 }\n"
    "if ($command -eq 'verify-local') {\n"
    "  $envFile = [Environment]::GetEnvironmentVariable('PROJECT_ENV_FILE')\n"
    "  foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {\n"
    "    if ($line -match '^APEX_APP_ID=') { Write-Output ('STUB ' + $line) }\n"
    "  }\n"
    "  $verifyPath = Join-Path $PSScriptRoot 'verify.txt'\n"
    "  if (Test-Path -LiteralPath $verifyPath -PathType Leaf) {\n"
    "    foreach ($line in [System.IO.File]::ReadAllLines($verifyPath)) { Write-Output $line }\n"
    "  }\n"
    "  $codeText = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'verify.code')).Trim()\n"
    "  exit ([int] $codeText)\n"
    "}\n"
    "exit 2\n"
)


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class TeamOfflineStubTests(unittest.TestCase):
    """team-offline against a stub team.ps1 in a temporary fake root; the real verify-local never runs."""

    def make_root(self, directory: Path, verify_lines: list[str], code: int, apps: tuple[str, ...] = ()) -> Path:
        scripts = directory / "scripts"
        scripts.mkdir()
        shutil.copy2(SCRIPT, scripts / "windows_check.ps1")
        for name in ("load_env.ps1", "template_release.ps1", "resolve_python.ps1"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (scripts / "team.ps1").write_bytes(STUB_TEAM.encode("ascii"))
        (scripts / "verify.txt").write_bytes(("".join(line + "\n" for line in verify_lines)).encode("ascii"))
        (scripts / "verify.code").write_bytes(str(code).encode("ascii"))
        for app in apps:
            (directory / "apps" / app).mkdir(parents=True)
        return scripts / "windows_check.ps1"

    def run_team_offline(self, script: Path, root: Path) -> subprocess.CompletedProcess[str]:
        return run_windows_check(script, "-AllowPowerShellCore", "-RepoRoot", str(root), "-Only", "team-offline", cwd=root)

    def test_bare_template_excuses_only_the_missing_application(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = self.make_root(root, ["[FAIL] configured_app_100: x"], 2)
            result = self.run_team_offline(script, root)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT team-offline PASS", result.stdout)
            self.assertIn(
                "bare template: no application under apps/, so configured_app_100 is expected to fail",
                result.stdout,
            )
            self.assertIn("APEX_APP_ID=100", result.stdout)

    def test_bare_template_still_fails_on_other_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = self.make_root(root, ["[FAIL] configured_app_100: x", "[FAIL] template_lock: x"], 2)
            result = self.run_team_offline(script, root)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT team-offline FAIL", result.stdout)

    def test_lowest_numeric_application_is_checked_and_non_numeric_folders_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = self.make_root(root, [], 0, apps=("SCHEMA1/200", "SCHEMA1/150", "templates/deployments"))
            result = self.run_team_offline(script, root)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT team-offline PASS", result.stdout)
            self.assertIn("APEX_APP_ID=150", result.stdout)

    def test_missing_directory_of_a_real_application_is_not_excused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = self.make_root(root, ["[FAIL] configured_app_150: x"], 2, apps=("SCHEMA1/150",))
            result = self.run_team_offline(script, root)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT team-offline FAIL", result.stdout)


# Fake unittest modules for the tests step. The noisy one prints summary look-alikes to stdout. A pipe makes
# stdout block-buffered, so that text is flushed at exit, after unittest's own summary on stderr.
NOISY_PASSING_MODULE = r'''import unittest


class NoisyPassTests(unittest.TestCase):
    def test_passes_but_prints_look_alikes(self):
        print(r"OK   C:\fake\path: ok")
        print("FAILED-looking noise")
        self.assertTrue(True)
'''

FAILING_MODULE = r'''import unittest


class BrokenTests(unittest.TestCase):
    def test_fails(self):
        self.fail("boom")

    def test_errors(self):
        raise RuntimeError("x")
'''

HAVE_PYTHON = bool(shutil.which("python") or shutil.which("python3"))


@unittest.skipUnless(PWSH, "PowerShell engine is not installed")
@unittest.skipUnless(HAVE_PYTHON, "python or python3 is not on PATH")
class TestsStepTests(unittest.TestCase):
    """The tests step against a fake root with a small unittest package, failing or passing."""

    def make_root(self, directory: Path, modules: dict[str, str]) -> Path:
        scripts = directory / "scripts"
        scripts.mkdir()
        shutil.copy2(SCRIPT, scripts / "windows_check.ps1")
        shutil.copy2(ROOT / "scripts" / "resolve_python.ps1", scripts / "resolve_python.ps1")
        tests = directory / "tests"
        tests.mkdir()
        for name, text in modules.items():
            (tests / name).write_bytes(text.encode("ascii"))
        return scripts / "windows_check.ps1"

    def run_tests_step(self, script: Path, root: Path) -> subprocess.CompletedProcess[str]:
        return run_windows_check(script, "-AllowPowerShellCore", "-RepoRoot", str(root), "-Only", "tests", cwd=root)

    @staticmethod
    def result_line(output: str) -> str:
        matches = [line for line in output.splitlines() if line.startswith("RESULT tests ")]
        return matches[0] if matches else ""

    def test_failing_run_names_the_failing_tests_and_keeps_the_real_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = self.make_root(root, {"test_noisy.py": NOISY_PASSING_MODULE, "test_broken.py": FAILING_MODULE})
            result = self.run_tests_step(script, root)
            output = result.stdout
            self.assertEqual(1, result.returncode, output + result.stderr)
            self.assertIn("RESULT tests FAIL", output)
            detail = self.result_line(output)
            self.assertIn("FAILED (failures=1, errors=1)", detail)
            self.assertNotIn("OK   C:\\fake", detail)
            self.assertIn("2 failing test(s)", detail)

            lines = output.splitlines()
            self.assertIn("  failing tests (2):", lines)
            heading = lines.index("  failing tests (2):")
            headers = [line.strip() for line in lines[heading + 1 : heading + 3]]
            self.assertTrue(any(header.startswith("FAIL: ") for header in headers), headers)
            self.assertTrue(any(header.startswith("ERROR: ") for header in headers), headers)
            self.assertLess(heading, lines.index("  last 60 lines of output:"))

    def test_passing_run_reports_unittests_summary_not_the_printed_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = self.make_root(root, {"test_noisy.py": NOISY_PASSING_MODULE})
            result = self.run_tests_step(script, root)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("RESULT tests PASS", result.stdout)
            detail = self.result_line(result.stdout)
            self.assertIn("Ran 1 test in", detail)
            self.assertIn("; OK", detail)
            self.assertNotIn("OK   C:\\fake", detail)


if __name__ == "__main__":
    unittest.main()
