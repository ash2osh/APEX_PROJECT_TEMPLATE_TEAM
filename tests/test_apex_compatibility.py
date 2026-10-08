import unittest
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fake_sqlcl import BASH, install

from scripts.apex_compatibility import CompatibilityError, parse_sqlcl_version, release_line, require_release, require_sqlcl_version


class CompatibilityTests(unittest.TestCase):
    def test_release_line_accepts_numeric_26_2_only(self):
        self.assertEqual(release_line("26.2.0"), "26.2")
        require_release("26.2.0", "26.2")
        for version in ("26.1.0", "26.3.0"):
            with self.subTest(version=version), self.assertRaises(CompatibilityError):
                require_release(version, "26.2")

    def test_malformed_release_fails_closed(self):
        for version in ("", "26", "26.2preview", "26.2\n26.1", "26.-2", " 26.2 "):
            with self.subTest(version=version), self.assertRaises(CompatibilityError):
                release_line(version)

    def test_sqlcl_floor_is_numeric_and_zero_padded(self):
        for version in ("26.3", "26.3.0.0", "26.10.0.0", "27.0"):
            require_sqlcl_version(version, "26.3.0.0")
        for version in ("26.2.2.0", "26.2.10", "25.10", "unknown", ""):
            with self.subTest(version=version), self.assertRaises(CompatibilityError):
                require_sqlcl_version(version, "26.3.0.0")

    def test_sqlcl_banner_must_unambiguously_report_release(self):
        self.assertEqual(parse_sqlcl_version("SQLcl: Release 26.3 Production\nBuild: 26.3.0.260.1620\n"), "26.3")
        self.assertEqual(parse_sqlcl_version("SQLcl: Release 26.3.0.0 Production\n"), "26.3.0.0")
        for output in ("Java 26.3", "SQLcl unavailable", "SQLcl: Release 26.3preview",
                       "SQLcl: Release 26.3 Production\nSQLcl: Release 26.2 Production"):
            with self.subTest(output=output), self.assertRaises(CompatibilityError):
                parse_sqlcl_version(output)


class LauncherCompatibilityTests(unittest.TestCase):
    def test_bash_and_powershell_refuse_old_unknown_and_failed_sqlcl(self):
        root = Path(__file__).resolve().parents[1]
        cases = (("SQLcl: Release 26.3 Production", 0, True),
                 ("SQLcl: Release 26.2.2.0 Production", 0, False),
                 ("unavailable", 0, False), ("SQLcl: Release 26.3 Production", 1, False))
        for banner, status, accepted in cases:
            with self.subTest(banner=banner, status=status), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                # PowerShell feeds redirected stdin through a pipe; drain it before
                # exiting so this fast fake cannot race Start-Process's pipe close.
                bin_dir = install(directory / "bin", f"cat > /dev/null\nprintf '%s\\n' '{banner}'\nexit {status}\n")
                environment = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
                work = directory / "work"
                result = subprocess.run([BASH, "-c", 'REPO_ROOT=$1; source "$1/scripts/sqlcl_safe.sh"; sqlcl_require_apex_version "$2"',
                                         "bash", str(root), str(work)], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if shutil.which("pwsh"):
                    driver = directory / "check.ps1"
                    driver.write_text('param($Helper,$Work)\n. $Helper\ntry { Assert-SqlclApexVersion -WorkDirectory $Work } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 2 }\n')
                    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(driver),
                                             str(root / "scripts/invoke_sqlcl.ps1"), str(work)],
                                            env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
