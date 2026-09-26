import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "publish_app.sh"


class PublishAppCliTests(unittest.TestCase):
    def run_publish(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(PUBLISH), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_publish_fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "publish_app.sh",
            "publish_app.sql",
            "load_env.sh",
            "check_db_target.sh",
        ):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        shutil.copy2(ROOT / ".env.example", root / ".env")

        app = root / "apps" / "DEMO" / "100"
        deployments = app / "deployments"
        deployments.mkdir(parents=True)
        (app / "application.apx").write_text("application {}\n", encoding="utf-8")
        (deployments / "dev.json").write_text(
            json.dumps(
                {
                    "workspace": {"name": "DEV_WORKSPACE"},
                    "app": {"id": 100, "databaseSession": {"parsingSchema": "DEMO"}},
                }
            ),
            encoding="utf-8",
        )

        fake_bin = root / "bin"
        fake_bin.mkdir()
        sql_log = root / "sql-args.txt"
        sql_cwd = root / "sql-cwd.txt"
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n"
            "pwd > \"$FAKE_SQL_CWD\"\n"
            "case \"${FAKE_SQL_MODE:-success}\" in\n"
            "  sp2) printf '%s\\n' 'SP2-0640: Not connected' ;;\n"
            "  no-sentinel) : ;;\n"
            "  *) printf '%s\\n' 'APEX_IMPORT_VERIFIED:100' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["FAKE_SQL_CWD"] = str(sql_cwd)
        return scripts / "publish_app.sh", sql_log, sql_cwd, environment

    def test_help_describes_numeric_id_and_environment_options(self) -> None:
        result = self.run_publish("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<app_id>", result.stdout)
        self.assertIn("--env", result.stdout)
        self.assertIn("--force", result.stdout)

    def test_rejects_non_numeric_application_id_before_loading_configuration(self) -> None:
        result = self.run_publish("my-app")
        self.assertEqual(result.returncode, 2)
        self.assertIn("positive numeric application id", result.stderr)

    def test_deployment_templates_are_explicit_and_use_numeric_app_ids(self) -> None:
        expected = {
            "dev": ("DEV_WORKSPACE", "DEV_APP"),
            "staging": ("STAGE_WORKSPACE", "STAGE_APP"),
            "prod": ("PROD_WORKSPACE", "PROD_APP"),
        }
        for environment, (workspace, schema) in expected.items():
            with self.subTest(environment=environment):
                path = ROOT / "apps" / "templates" / "deployments" / f"{environment}.json"
                self.assertTrue(path.is_file(), f"missing deployment template: {path}")
                deployment = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(deployment["workspace"]["name"], workspace)
                self.assertEqual(deployment["app"]["id"], 100)
                self.assertEqual(
                    deployment["app"]["databaseSession"]["parsingSchema"], schema
                )

    def test_sqlcl_import_uses_the_explicit_deployment_descriptor(self) -> None:
        import_script = ROOT / "scripts" / "publish_app.sql"
        self.assertTrue(import_script.is_file(), f"missing import script: {import_script}")
        self.assertIn(
            "apex import -input . -deployment &&deployment_file",
            import_script.read_text(encoding="utf-8"),
        )
        self.assertIn("APEX_IMPORT_VERIFIED:&&expected_app_id", import_script.read_text(encoding="utf-8"))

    def test_force_publish_passes_numeric_id_and_environment_descriptor_to_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, sql_cwd, environment = self.make_publish_fixture(root)
            result = subprocess.run(
                ["bash", str(runner), "100", "--force"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-name", args)
            self.assertIn("docker-demo", args)
            self.assertIn(f"@{runner.parent / 'publish_app.sql'}", args)
            self.assertIn("deployments/dev.json", args)
            self.assertEqual(sql_cwd.read_text(encoding="utf-8").strip(), str(root / "apps/DEMO/100"))

    def test_client_error_or_missing_verification_never_reports_published(self) -> None:
        for mode in ("sp2", "no-sentinel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment = self.make_publish_fixture(root)
                environment["FAKE_SQL_MODE"] = mode
                result = subprocess.run(
                    ["bash", str(runner), "100", "--force"],
                    cwd=root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Published APEX App", result.stdout + result.stderr)

    def test_powershell_publish_requires_clean_client_output_and_sentinel(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")

        for mode, expected_success in (("sp2", False), ("no-sentinel", False), ("success", True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scripts = root / "scripts"
                scripts.mkdir()
                for name in (
                    "publish_app.ps1",
                    "publish_app.sql",
                    "load_env.ps1",
                    "invoke_sqlcl.ps1",
                    "check_db_target.ps1",
                ):
                    shutil.copy2(ROOT / "scripts" / name, scripts / name)
                shutil.copy2(ROOT / ".env.example", root / ".env")
                app = root / "apps" / "DEMO" / "100"
                deployments = app / "deployments"
                deployments.mkdir(parents=True)
                (app / "application.apx").write_text("application {}\n", encoding="utf-8")
                (deployments / "dev.json").write_text(
                    json.dumps({
                        "workspace": {"name": "DEV_WORKSPACE"},
                        "app": {"id": 100, "databaseSession": {"parsingSchema": "DEMO"}},
                    }),
                    encoding="utf-8",
                )
                fake_bin = root / "bin"
                fake_bin.mkdir()
                fake_sql = fake_bin / "sql"
                fake_sql.write_text(
                    "#!/usr/bin/env bash\n"
                    "case \"$FAKE_SQL_MODE\" in\n"
                    "  sp2) printf '%s\\n' 'SP2-0640: Not connected' ;;\n"
                    "  no-sentinel) : ;;\n"
                    "  *) printf '%s\\n' 'APEX_IMPORT_VERIFIED:100' ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                fake_sql.chmod(0o755)
                environment = os.environ.copy()
                environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                environment["FAKE_SQL_MODE"] = mode

                result = subprocess.run(
                    [pwsh, "-NoProfile", "-File", str(scripts / "publish_app.ps1"), "100", "--force"],
                    cwd=root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                if expected_success:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Published APEX App 100", result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("Published APEX App 100", result.stdout + result.stderr)

    def test_staging_publish_skips_dev_builder_drift_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("publish_app.sh", "publish_app.sql", "load_env.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)

            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            env_text += "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\n"
            (root / ".env").write_text(env_text, encoding="utf-8")
            app = root / "apps" / "DEMO" / "100"
            deployments = app / "deployments"
            deployments.mkdir(parents=True)
            (app / "application.apx").write_text("application {}\n", encoding="utf-8")
            (deployments / "staging.json").write_text(
                json.dumps(
                    {
                        "workspace": {"name": "STAGE_WORKSPACE"},
                        "app": {"id": 100, "databaseSession": {"parsingSchema": "STAGE_APP"}},
                    }
                ),
                encoding="utf-8",
            )

            guard_marker = root / "drift-guard-called"
            drift_guard = scripts / "check_builder_drift.py"
            drift_guard.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text('called')\n"
                "raise SystemExit(77)\n",
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sql_log = root / "sql-args.txt"
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n",
                encoding="utf-8",
            )
            fake_sql.write_text(
                fake_sql.read_text(encoding="utf-8")
                + "printf '%s\\n' 'APEX_IMPORT_VERIFIED:100'\n",
                encoding="utf-8",
            )
            fake_sql.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SQL_LOG"] = str(sql_log)

            result = subprocess.run(
                ["bash", str(scripts / "publish_app.sh"), "100", "--env", "staging"],
                cwd=root,
                env=environment,
                input="y\n",
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(guard_marker.exists(), "staging promotions must not compare Builder edit timestamps")
            self.assertIn("deployments/staging.json", sql_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
