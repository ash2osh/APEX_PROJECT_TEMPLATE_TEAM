import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TeamCliTests(unittest.TestCase):
    def test_team_help_lists_primary_commands(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("doctor", "export", "publish", "check-conflicts", "migrate", "backup-db", "deploy"):
            self.assertIn(command, result.stdout)

    def test_check_conflicts_command_does_not_require_database_configuration(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "check-conflicts"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No cross-developer conflicts", result.stdout)

    def test_publish_command_routes_non_dev_targets_to_deploy(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "publish", "100", "--env", "prod"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("use deploy for staging or production", result.stderr)

    def test_doctor_uses_read_only_identity_sqlcl_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (root / "scripts" / "verify_db_access.sql").write_text(
                "PROMPT identity checked\n", encoding="utf-8"
            )
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"))
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sql_log = root / "sql-args.txt"
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n",
                encoding="utf-8",
            )
            fake_sql.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SQL_LOG"] = str(sql_log)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")

            result = subprocess.run(
                ["bash", str(scripts / "team.sh"), "doctor"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("docker-demo", args)
            self.assertIn(f"@{scripts / 'doctor.sql'}", args)
            doctor_sql = (scripts / "doctor.sql").read_text(encoding="utf-8")
            self.assertIn("WHENEVER SQLERROR EXIT SQL.SQLCODE", doctor_sql)
            self.assertIn("@@verify_db_access.sql", doctor_sql)

    def make_deploy_checkout(self, root: Path, configure_profile: bool = True) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "deploy.sh",
            "publish_app.sh",
            "publish_app.sql",
            "load_env.sh",
        ):
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, scripts / name)
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        if configure_profile:
            env_text += "\nPROD_SQLCL_CONNECTION=prod-db\nPROD_EXPECTED_USER=PROD_DEPLOYER\n"
        (root / ".env").write_text(env_text, encoding="utf-8")
        app = root / "apps" / "DEMO" / "100"
        deployments = app / "deployments"
        deployments.mkdir(parents=True)
        (app / "application.apx").write_text("app SAMPLE ()\n", encoding="utf-8")
        (deployments / "prod.json").write_text(
            '{"workspace":{"name":"PROD_WORKSPACE"},"app":{"id":100,'
            '"databaseSession":{"parsingSchema":"PROD_APP"}}}\n',
            encoding="utf-8",
        )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        sql_log = root / "sql-called"
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        return scripts / "deploy.sh", sql_log

    def run_deploy(self, script: Path, sql_log: Path, *args: str, input_text: str = ""):
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        return subprocess.run(
            ["bash", str(script), "100", "--env", "prod", *args],
            cwd=script.parents[1],
            env=environment,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_manual_deploy_prints_explicit_descriptor_without_running_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--manual")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DBA runbook", result.stdout)
            self.assertIn("PROD_WORKSPACE", result.stdout)
            self.assertIn("PROD_APP", result.stdout)
            self.assertIn("deployments/prod.json", result.stdout)
            self.assertFalse(sql_log.exists(), "manual mode must not invoke SQLcl")

    def test_manual_deploy_works_without_a_local_production_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary), configure_profile=False)

            result = self.run_deploy(script, sql_log, "--manual")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("YOUR_PROD_SQLCL_CONNECTION", result.stdout)
            self.assertIn("YOUR_PROD_EXPECTED_USER", result.stdout)
            self.assertFalse(sql_log.exists(), "manual mode must not invoke SQLcl")

    def test_unconfirmed_deploy_does_not_invoke_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, input_text="n\n")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Deploying to PROD. Proceed? [y/N]", result.stdout)
            self.assertFalse(sql_log.exists(), "a declined deployment must not invoke SQLcl")

    def test_confirmed_deploy_uses_the_production_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, input_text="y\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-name", args)
            self.assertIn("prod-db", args)
            self.assertIn("deployments/prod.json", args)


if __name__ == "__main__":
    unittest.main()
