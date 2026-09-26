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
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> \"$FAKE_SQL_LOG\"\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        return scripts / "migrate.sh", migrations, sql_log

    def run_migrate(self, script: Path, migration: Path, sql_log: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{sql_log.parent / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
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
            self.assertTrue(any("migrate.sql" in arg for arg in args))
            self.assertTrue(any("../migrations/alice/20260926_create_orders.sql" in arg for arg in args))


if __name__ == "__main__":
    unittest.main()
