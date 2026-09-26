import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MigrateCliTests(unittest.TestCase):
    def make_checkout(self, root: Path) -> tuple[Path, Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "migrate.sh",
            "migrate.sql",
            "validate_migration.py",
            "check_conflicts.py",
            "load_env.sh",
            "check_db_target.sh",
            "verify_db_access.sql",
        ):
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, scripts / name)
        shutil.copy2(ROOT / ".env.example", root / ".env")
        migrations = root / "migrations"
        migrations.mkdir()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        sql_log = root / "sql-called"
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" >> \"$FAKE_SQL_LOG\"\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in @*) cat \"${arg#@}\" > \"$FAKE_DRIVER_LOG\" ;; esac\n"
            "done\n"
            "printf '%s\\n' MIGRATION_SCRIPT_COMPLETED\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        return scripts / "migrate.sh", migrations, sql_log

    def run_migrate(self, script: Path, migration: Path, sql_log: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{sql_log.parent / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["FAKE_DRIVER_LOG"] = str(sql_log.with_suffix(".driver.sql"))
        return subprocess.run(
            ["bash", str(script), str(migration.relative_to(script.parents[1]))],
            cwd=script.parents[1],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_conflict_checker_blocks_before_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, migrations, sql_log = self.make_checkout(Path(temporary))
            alice = migrations / "alice"
            bob = migrations / "bob"
            alice.mkdir()
            bob.mkdir()
            first = alice / "one.sql"
            second = bob / "two.sql"
            first.write_text("CREATE TABLE ORDERS (ID NUMBER);\n", encoding="utf-8")
            second.write_text("CREATE TABLE ORDERS (ID NUMBER);\n", encoding="utf-8")

            result = self.run_migrate(script, first, sql_log)

            self.assertEqual(result.returncode, 1)
            self.assertIn("TABLE ORDERS", result.stdout)
            self.assertFalse(sql_log.exists(), "SQLcl must not run after a conflict")

    def test_clean_migration_uses_configured_sqlcl_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, migrations, sql_log = self.make_checkout(Path(temporary))
            alice = migrations / "alice"
            alice.mkdir()
            migration = alice / "20260926_create_orders.sql"
            migration.write_text("CREATE TABLE ORDERS (ID NUMBER);\n", encoding="utf-8")

            result = self.run_migrate(script, migration, sql_log)

            self.assertEqual(result.returncode, 0, result.stderr)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-name", args)
            self.assertIn("docker-demo", args)
            driver = sql_log.with_suffix(".driver.sql").read_text(encoding="utf-8")
            self.assertIn("@@../../scripts/migrate.sql DEMO development DEMO", driver)
            self.assertIn("SET DEFINE OFF", driver)
            self.assertIn("@@../../migrations/alice/20260926_create_orders.sql", driver)
            self.assertLess(driver.index("SET DEFINE OFF"), driver.index("@@../../migrations/"))

    def test_migration_driver_selects_configured_target_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, migrations, sql_log = self.make_checkout(Path(temporary))
            env_path = script.parents[1] / ".env"
            env_text = env_path.read_text(encoding="utf-8")
            env_text = env_text.replace("CODE_SCHEMA=DEMO", "CODE_SCHEMA=APP_CODE")
            env_text = env_text.replace("CODE_EXPECTED_USER=DEMO", "CODE_EXPECTED_USER=MIGRATION_USER")
            env_path.write_text(env_text, encoding="utf-8")
            developer = migrations / "alice"
            developer.mkdir()
            migration = developer / "20260926_create_orders.sql"
            migration.write_text("CREATE TABLE ORDERS (ID NUMBER);\n", encoding="utf-8")

            result = self.run_migrate(script, migration, sql_log)

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = sql_log.with_suffix(".driver.sql").read_text(encoding="utf-8")
            self.assertIn("@@../../scripts/migrate.sql APP_CODE development MIGRATION_USER", generated)
            driver = (script.parent / "migrate.sql").read_text(encoding="utf-8")
            set_schema = "ALTER SESSION SET CURRENT_SCHEMA = &&target_schema"
            self.assertIn(set_schema, driver)
            self.assertLess(driver.index("@@verify_db_access.sql"), driver.index(set_schema))
            self.assertNotIn("@@&&migration_file", driver)

    def test_migration_failure_rolls_back_and_success_commits(self) -> None:
        driver = (ROOT / "scripts" / "migrate.sql").read_text(encoding="utf-8")
        self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", driver)
        self.assertIn("WHENEVER OSERROR EXIT FAILURE ROLLBACK", driver)
        with tempfile.TemporaryDirectory() as temporary:
            script, migrations, sql_log = self.make_checkout(Path(temporary))
            developer = migrations / "alice"
            developer.mkdir()
            migration = developer / "20260926_ampersand.sql"
            migration.write_text(
                "CREATE TABLE RESEARCH_DEVELOPMENT (NAME VARCHAR2(40) DEFAULT 'Research & Development');\n",
                encoding="utf-8",
            )

            result = self.run_migrate(script, migration, sql_log)

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = sql_log.with_suffix(".driver.sql").read_text(encoding="utf-8")
            self.assertIn("EXIT SUCCESS COMMIT", generated)
            self.assertLess(generated.index("SET DEFINE OFF"), generated.index("ampersand.sql"))

    def test_sqlcl_client_directives_are_rejected_before_sqlcl(self) -> None:
        for body in (
            "SET DEFINE ON\n",
            "CREATE TABLE ORDERS (ID NUMBER);\nPROMPT after DML\n",
            "WHENEVER SQLERROR CONTINUE\n",
            "HOST echo unexpected\n",
            "@@UPDATE.sql\nSELECT 1 FROM dual;\n",
            "/* outer /* inner */\nPROMPT client directive\n/* close */ -- */\nSELECT 1 FROM dual;\n",
            "BEGIN\nNULL;\nEND;\n.\nPROMPT client directive\n/\n",
            'CREATE JAVA SOURCE NAMED "Test" AS\npublic class Test {}\n;\nPROMPT client directive\n/\n',
            'CREATE JAVA SOURCE NAMED "Test" AS\npublic class Test {\n/*\n;\n*/\n}\nPROMPT client directive\n/\n',
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temporary:
                script, migrations, sql_log = self.make_checkout(Path(temporary))
                developer = migrations / "alice"
                developer.mkdir()
                migration = developer / "unsafe.sql"
                migration.write_text(body, encoding="utf-8")

                result = self.run_migrate(script, migration, sql_log)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)
                self.assertFalse(sql_log.exists(), "SQLcl must not run for client directives")


if __name__ == "__main__":
    unittest.main()
