import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


class BackupDbCliTests(unittest.TestCase):
    def make_checkout(self, root: Path, powershell: bool) -> Path:
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        if powershell:
            names = (
                "backup_db.ps1",
                "backup_db.sql",
                "load_env.ps1",
                "check_db_target.ps1",
                "invoke_sqlcl.ps1",
                "replace_mirror.ps1",
            )
        else:
            names = (
                "backup_db.sh",
                "backup_db.sql",
                "load_env.sh",
                "check_db_target.sh",
                "replace_mirror.sh",
            )
        for name in names:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)

        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        env_text = env_text.replace("TABLES_SCHEMA=DEMO", "TABLES_SCHEMA=DEMO$")
        env_text = env_text.replace("CODE_SCHEMA=DEMO", "CODE_SCHEMA=DEMO$")
        (root / ".env").write_text(env_text, encoding="utf-8")

        (root / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Backup Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "backup@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)

        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "schema=\"$6\"\n"
            "scope=\"$7\"\n"
            "spool_schema=\"${11:-}\"\n"
            "if [[ -z \"$spool_schema\" || \"$spool_schema\" == *'$'* ]]; then\n"
            "  printf '%s\\n' 'SP2-0332: Cannot create spool file'\n"
            "  exit 0\n"
            "fi\n"
            "case \"$scope\" in\n"
            "  tables) scope_dir=tables; object=FAKE_TABLE; object_type=TABLE ;;\n"
            "  code) scope_dir=views; object=FAKE_VIEW; object_type=VIEW ;;\n"
            "  *) exit 4 ;;\n"
            "esac\n"
            "mkdir -p \"database/$spool_schema/$scope_dir\"\n"
            "printf 'CREATE FAKE OBJECT;\\n' > \"database/$spool_schema/$scope_dir/$object.sql\"\n"
            "printf '%s=1\\n' \"$object_type\" > \"database/$spool_schema/manifest-$scope.txt\"\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        return scripts / ("backup_db.ps1" if powershell else "backup_db.sh")

    def run_backup(self, script: Path, powershell: bool) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        command = (
            [PWSH, "-NoProfile", "-File", str(script)]
            if powershell
            else ["bash", str(script)]
        )
        return subprocess.run(
            command,
            cwd=script.parents[1],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_dollar_schema_mirror_installed(self, script: Path, output: str) -> None:
        mirror = script.parents[1] / "database" / "DEMO$"
        self.assertTrue(mirror.is_dir(), output)
        self.assertEqual((mirror / "manifest-tables.txt").read_text(encoding="utf-8"), "TABLE=1\n")
        self.assertEqual((mirror / "manifest-code.txt").read_text(encoding="utf-8"), "VIEW=1\n")
        self.assertTrue((mirror / "tables" / "FAKE_TABLE.sql").is_file())
        self.assertTrue((mirror / "views" / "FAKE_VIEW.sql").is_file())
        database_entries = [path.name for path in (script.parents[1] / "database").iterdir()]
        self.assertEqual(database_entries, ["DEMO$"])

    def test_bash_backup_spools_safely_then_installs_original_dollar_schema_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self.make_checkout(Path(temporary), powershell=False)

            result = self.run_backup(script, powershell=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_dollar_schema_mirror_installed(script, result.stdout + result.stderr)

    @unittest.skipUnless(PWSH, "PowerShell Core is not installed")
    def test_powershell_backup_spools_safely_then_installs_original_dollar_schema_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = self.make_checkout(Path(temporary), powershell=True)

            result = self.run_backup(script, powershell=True)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_dollar_schema_mirror_installed(script, result.stdout + result.stderr)

    def test_sql_driver_uses_wrapper_supplied_safe_schema_directory(self) -> None:
        script = (ROOT / "scripts" / "backup_db.sql").read_text(encoding="utf-8")
        self.assertIn("DEFINE spool_schema = '&6'", script)
        self.assertIn("SPOOL database/&&spool_schema/", script)
        self.assertNotIn("SPOOL database/&&target_schema/", script)


if __name__ == "__main__":
    unittest.main()
