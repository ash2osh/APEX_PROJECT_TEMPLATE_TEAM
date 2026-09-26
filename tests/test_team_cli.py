import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TeamCliTests(unittest.TestCase):
    def run_bash_env_loader(self, environment: Path) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            loader = scripts / "load_env.sh"
            shutil.copy2(ROOT / "scripts" / "load_env.sh", loader)
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -e; source "$1" "$2"; printf "%s|%s|%s|%s\\n" "$CODE_SCHEMA" "$CODE_EXPECTED_USER" "$CODE_PREFIXES" "$APEX_PARSING_SCHEMA"',
                    "bash",
                    str(loader),
                    str(environment),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_team_help_lists_primary_commands(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in (
            "doctor",
            "export",
            "publish",
            "check-conflicts",
            "migrate",
            "backup-db",
            "deploy",
            "upgrade-template",
        ):
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

    def test_bash_environment_loader_accepts_dollar_and_hash_oracle_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            content = (ROOT / ".env.example").read_text(encoding="utf-8")
            content = content.replace("CODE_SCHEMA=DEMO", "CODE_SCHEMA=DEMO$")
            content = content.replace("CODE_EXPECTED_USER=DEMO", "CODE_EXPECTED_USER=DEMO#")
            content = content.replace("CODE_PREFIXES=*", "CODE_PREFIXES=AB$,XY#")
            content = content.replace("APEX_PARSING_SCHEMA=DEMO", "APEX_PARSING_SCHEMA=APP$")
            environment.write_text(content, encoding="utf-8")

            result = self.run_bash_env_loader(environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "DEMO$|DEMO#|AB$,XY#|APP$\n")

    def test_bash_environment_loader_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            example = (ROOT / ".env.example").read_bytes()
            environment.write_bytes(b"\xef\xbb\xbf" + example)

            result = self.run_bash_env_loader(environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "DEMO|DEMO|*|DEMO\n")

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
            self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", doctor_sql)
            self.assertIn("@@verify_db_access.sql", doctor_sql)

    def make_deploy_checkout(self, root: Path, configure_profile: bool = True) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "deploy.sh",
            "publish_app.sh",
            "publish_app.sql",
            "load_env.sh",
            "export_apps.sql",
            "verify_db_access.sql",
            "normalize_apx.sh",
            "record_export_state.py",
            "verify_publish_state.py",
            "validate_app_source.py",
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
        (app / ".apex").mkdir()
        (app / ".apex" / "apexlang.json").write_text('{"version":1}\n', encoding="utf-8")
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
            "#!/usr/bin/env bash\n"
            "mode=other; export_schema=DEMO\n"
            "for arg in \"$@\"; do case \"$arg\" in *@*publish_app.sql) mode=import ;; *@*export_apps.sql) mode=export ;; esac; done\n"
            "if [[ $mode == export ]]; then found_script=0; for arg in \"$@\"; do if [[ $found_script == 1 ]]; then export_schema=$arg; break; fi; case \"$arg\" in *@*export_apps.sql) found_script=1 ;; esac; done; fi\n"
            "if [[ $mode == import ]]; then printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"; printf '%s\\n' 'Import successful.' 'APEX_IMPORT_VERIFIED:100'; fi\n"
            "if [[ $mode == export ]]; then exported=\"$PWD/apps/$export_schema/100\"; mkdir -p \"$exported/.apex\"; cp \"$FAKE_SOURCE_DIR/application.apx\" \"$exported/application.apx\"; cp \"$FAKE_SOURCE_DIR/.apex/apexlang.json\" \"$exported/.apex/apexlang.json\"; printf '%s\\n' '2026-09-26T09:30:00|2026-09-26T09:30:02|Release 1.0' > \"$PWD/.apex-export-before.txt\"; cp \"$PWD/.apex-export-before.txt\" \"$PWD/.apex-export-after.txt\"; fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        return scripts / "deploy.sh", sql_log

    def run_deploy(self, script: Path, sql_log: Path, *args: str, input_text: str = ""):
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["FAKE_SOURCE_DIR"] = (script.parents[1] / "apps/DEMO/100").as_posix()
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
            sql_lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("sql ")]
            self.assertEqual(len(sql_lines), 2, result.stdout)
            self.assertEqual(
                shlex.split(sql_lines[0]),
                [
                    "sql",
                    "-S",
                    "-noupdates",
                    "-name",
                    "prod-db",
                    f"@{script.parents[1] / 'scripts' / 'publish_app.sql'}",
                    "PROD_APP",
                    "production",
                    "PROD_DEPLOYER",
                    "deployments/prod.json",
                    "100",
                ],
            )
            self.assertEqual(
                shlex.split(sql_lines[1]),
                [
                    "sql",
                    "-S",
                    "-noupdates",
                    "-name",
                    "prod-db",
                    f"@{script.parents[1] / 'scripts' / 'export_apps.sql'}",
                    "PROD_APP",
                    "100",
                    "production",
                    "PROD_DEPLOYER",
                ],
            )
            self.assertIn("verify_publish_state.py", result.stdout)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", result.stdout)
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

    def make_upgrade_fixture(self, root: Path) -> tuple[Path, Path]:
        template = root / "template"
        project = root / "project"
        for repo in (template, project):
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (template / "template-manifest.json").write_text(
            '{"schemaVersion": 1, "upstream": "x", "templateOwned": ["scripts/**", "template-manifest.json"],'
            ' "projectOwned": [], "templateOnly": []}',
            encoding="utf-8",
        )
        (template / "scripts").mkdir()
        (template / "scripts" / "hello.sh").write_text("echo template\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(template), "commit", "-q", "-m", "v1"], check=True)
        (project / "scripts").mkdir()
        for name in ("team.sh", "team.ps1", "upgrade_template.py", "load_env.sh", "load_env.ps1"):
            shutil.copy2(ROOT / "scripts" / name, project / "scripts" / name)
        # .env is local configuration; ignoring it keeps the tree clean for the engine.
        (project / ".gitignore").write_text(".env\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "project"], check=True)
        return template, project

    def test_upgrade_template_command_runs_the_engine_for_this_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                ["bash", str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertTrue((project / ".template-lock.json").is_file())

    def test_upgrade_template_warns_when_env_misses_a_new_required_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))
            (project / ".env").write_text("PROJECT_NAME=x\n", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(".env needs attention after the upgrade", result.stderr)

    def test_powershell_upgrade_template_command_runs_the_engine(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                [
                    pwsh,
                    "-NoProfile",
                    "-File",
                    str(project / "scripts" / "team.ps1"),
                    "upgrade-template",
                    "--source",
                    str(template),
                    "--dry-run",
                ],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertFalse((project / ".template-lock.json").exists())


if __name__ == "__main__":
    unittest.main()
