import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


class ExportCliTests(unittest.TestCase):
    def make_checkout(self, root: Path, powershell: bool) -> tuple[Path, dict[str, bytes]]:
        scripts = root / "scripts"
        scripts.mkdir()
        if powershell:
            names = (
                "export_apps.ps1",
                "export_apps.sql",
                "load_env.ps1",
                "check_db_target.ps1",
                "invoke_sqlcl.ps1",
                "normalize_apx.ps1",
                "replace_mirror.ps1",
            )
        else:
            names = (
                "export_apps.sh",
                "export_apps.sql",
                "load_env.sh",
                "check_db_target.sh",
                "normalize_apx.sh",
                "replace_mirror.sh",
            )
        names += ("record_export_state.py", "preserve_deployments.py")
        for name in names:
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, scripts / name)

        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        env_text = env_text.replace("APEX_APP_ID=100,200", "APEX_APP_ID=100")
        (root / ".env").write_text(env_text, encoding="utf-8")
        app = root / "apps" / "DEMO" / "100"
        deployments = app / "deployments"
        deployments.mkdir(parents=True)
        expected = {
            "dev.json": b'{ "workspace" : { "name" : "DEV_KEEP" } }\n',
            "staging.json": b'{ "workspace" : { "name" : "STAGE_KEEP" } }\n',
            "prod.json": b'{ "workspace" : { "name" : "PROD_KEEP" } }\n',
        }
        for name, contents in expected.items():
            (deployments / name).write_bytes(contents)

        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Export Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "export@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "apps/DEMO/100/deployments"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed descriptors"], check=True)

        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "mkdir -p apps/DEMO/exported/.apex apps/DEMO/exported/deployments\n"
            "printf 'new app source\\n' > apps/DEMO/exported/application.apx\n"
            "printf '{\\\"mmdVersion\\\":1}\\n' > apps/DEMO/exported/.apex/apexlang.json\n"
            "printf '{\\\"default\\\":true}\\n' > apps/DEMO/exported/deployments/default.json\n"
            "printf '%s\\n' \"$FAKE_DB_BEFORE\" > .apex-export-before.txt\n"
            "printf '%s\\n' \"$FAKE_DB_AFTER\" > .apex-export-after.txt\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        script = scripts / ("export_apps.ps1" if powershell else "export_apps.sh")
        return script, expected

    def run_export(
        self,
        script: Path,
        powershell: bool,
        before: str = "2026-09-26T08:00:00|2026-09-26T09:00:00",
        after: str = "2026-09-26T08:00:00|2026-09-26T09:00:02",
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        environment["FAKE_DB_BEFORE"] = before
        environment["FAKE_DB_AFTER"] = after
        command = (
            [PWSH, "-NoProfile", "-File", str(script), "-AppId", "100"]
            if powershell
            else ["bash", str(script), "100"]
        )
        return subprocess.run(
            command,
            cwd=script.parents[1],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_descriptors_preserved(self, script: Path, expected: dict[str, bytes]) -> None:
        app = script.parents[1] / "apps" / "DEMO" / "100"
        for name, contents in expected.items():
            with self.subTest(descriptor=name):
                self.assertEqual((app / "deployments" / name).read_bytes(), contents)
        self.assertEqual(
            (app / "application.apx").read_text(encoding="utf-8"),
            "new app source\n",
        )

    def test_bash_export_preserves_committed_environment_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, expected = self.make_checkout(Path(temporary), powershell=False)
            result = self.run_export(script, powershell=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_descriptors_preserved(script, expected)

    @unittest.skipUnless(PWSH, "PowerShell Core is not installed")
    def test_powershell_export_preserves_committed_environment_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, expected = self.make_checkout(Path(temporary), powershell=True)
            result = self.run_export(script, powershell=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_descriptors_preserved(script, expected)

    def test_bash_export_refuses_builder_changes_during_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, expected = self.make_checkout(Path(temporary), powershell=False)
            result = self.run_export(
                script,
                powershell=False,
                after="2026-09-26T08:00:01|2026-09-26T09:00:02",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed in Builder while export was running", result.stderr)
            app = script.parents[1] / "apps" / "DEMO" / "100"
            self.assertFalse((app / "application.apx").exists())
            for name, contents in expected.items():
                self.assertEqual((app / "deployments" / name).read_bytes(), contents)

    @unittest.skipUnless(PWSH, "PowerShell Core is not installed")
    def test_powershell_export_refuses_builder_changes_during_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, expected = self.make_checkout(Path(temporary), powershell=True)
            result = self.run_export(
                script,
                powershell=True,
                after="2026-09-26T08:00:01|2026-09-26T09:00:02",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed in Builder while export was running", result.stderr)
            app = script.parents[1] / "apps" / "DEMO" / "100"
            self.assertFalse((app / "application.apx").exists())
            for name, contents in expected.items():
                self.assertEqual((app / "deployments" / name).read_bytes(), contents)

    def test_export_sql_captures_state_before_and_after_apex_export(self) -> None:
        script = (ROOT / "scripts" / "export_apps.sql").read_text(encoding="utf-8")
        before = script.index("SPOOL .apex-export-before.txt")
        export = script.index("apex export -applicationid")
        after = script.index("SPOOL .apex-export-after.txt")
        self.assertLess(before, export)
        self.assertLess(export, after)
        self.assertEqual(script.count("MAX(last_updated_on)"), 2)
        self.assertEqual(script.count("TO_CHAR(SYSDATE"), 2)


if __name__ == "__main__":
    unittest.main()
