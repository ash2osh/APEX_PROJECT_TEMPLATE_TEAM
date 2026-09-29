import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")

TWO_SCHEMAS = {
    "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=ONE,TWO",
    "TABLES_SQLCL_CONNECTION=docker-demo": "TABLES_SQLCL_CONNECTION=conn-one,conn-two",
    "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=ONE,TWO",
    "CODE_SCHEMA=DEMO": "CODE_SCHEMA=ONE,TWO",
    "CODE_SQLCL_CONNECTION=docker-demo": "CODE_SQLCL_CONNECTION=conn-one,conn-two",
    "CODE_EXPECTED_USER=DEMO": "CODE_EXPECTED_USER=ONE,TWO",
    "APEX_PARSING_SCHEMA=DEMO": "APEX_PARSING_SCHEMA=ONE,TWO",
    "APEX_SQLCL_CONNECTION=docker-demo": "APEX_SQLCL_CONNECTION=conn-one,conn-two",
    "APEX_EXPECTED_USER=DEMO": "APEX_EXPECTED_USER=ONE,TWO",
}


def env_text(replacements: dict[str, str]) -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Multi Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "multi@example.test"], check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)


class DoctorCliTests(unittest.TestCase):
    NAMES = ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "verify_db_access.sql", "sqlcl_safe.sh")

    def make_checkout(self, root: Path, replacements: dict[str, str]) -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text(replacements), encoding="utf-8")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        calls = root / "sql-calls.txt"
        fake_sql = fake_bin / "sql"
        # Positional: -S -noupdates -name <conn> @<script> <schema> <env> <user>
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s|%s|%s\\n' \"$4\" \"$6\" \"$8\" >> \"$FAKE_SQL_CALLS\"\n"
            "if [[ \"${FAKE_FAIL_CONNECTION:-}\" == \"$4\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "printf 'APEX_DOCTOR_VERIFIED:%s\\n' \"$8\"\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(calls)
        environment.pop("PROJECT_SCHEMA", None)
        return scripts / "team.sh", environment

    def run_team(self, script: Path, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *arguments],
            cwd=script.parents[1],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_single_schema_doctor_checks_one_identity_and_prints_the_old_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), {})
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Doctor checks passed for the configured DEV connection.", result.stdout)
            self.assertEqual(["docker-demo|DEMO|DEMO"], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines())

    def test_multi_schema_doctor_checks_every_schema_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-one|ONE|ONE", "conn-two|TWO|TWO"], sorted(calls))
            self.assertIn("Doctor checks passed for all 2 configured DEV schema connections.", result.stdout)

    def test_schema_option_narrows_doctor_to_one_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor", "--schema", "TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["conn-two|TWO|TWO"], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines())

    def test_one_failing_schema_fails_doctor_after_checking_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            environment["FAKE_FAIL_CONNECTION"] = "conn-one"
            result = self.run_team(script, environment, "doctor")
            self.assertNotEqual(0, result.returncode)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-one|ONE|ONE", "conn-two|TWO|TWO"], sorted(calls))
            self.assertIn("ONE", result.stderr)

    def test_unknown_or_malformed_schema_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            for value, expected in (("NOPE", "not configured"), ("two", "uppercase Oracle identifier")):
                with self.subTest(schema=value):
                    result = self.run_team(script, environment, "doctor", "--schema", value)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(expected, result.stderr)
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())

    def test_schema_option_requires_a_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor", "--schema")
            self.assertEqual(2, result.returncode)
            self.assertIn("--schema requires a schema name", result.stderr)

    def test_split_profile_project_still_checks_each_distinct_identity(self) -> None:
        # Tables in one schema, code and APEX in another: no --schema needed.
        replacements = {
            "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=DATA",
            "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=DATA",
            "TABLES_SQLCL_CONNECTION=docker-demo": "TABLES_SQLCL_CONNECTION=conn-data",
        }
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), replacements)
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-data|DATA|DATA", "docker-demo|DEMO|DEMO"], sorted(calls))


class CheckDbTargetTests(unittest.TestCase):
    def run_check(self, replacements: dict[str, str], *arguments: str, project_schema: str | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("check_db_target.sh", "load_env.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (root / ".env").write_text(env_text(replacements), encoding="utf-8")
            environment = os.environ.copy()
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment.pop("PROJECT_SCHEMA", None)
            if project_schema:
                environment["PROJECT_SCHEMA"] = project_schema
            return subprocess.run(
                ["bash", str(scripts / "check_db_target.sh"), *arguments],
                env=environment, text=True, capture_output=True, check=False,
            )

    def test_single_schema_check_is_unchanged(self) -> None:
        self.assertEqual(0, self.run_check({}, "read", "apex").returncode)

    def test_multi_schema_without_a_schema_is_refused(self) -> None:
        result = self.run_check(TWO_SCHEMAS, "read", "apex")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--schema", result.stderr)

    def test_schema_argument_selects_the_profile_entry(self) -> None:
        self.assertEqual(0, self.run_check(TWO_SCHEMAS, "read", "code", "TWO").returncode)

    def test_a_profile_that_does_not_list_the_schema_is_refused(self) -> None:
        replacements = {
            **TWO_SCHEMAS,
            "TABLES_SCHEMA=ONE,TWO": "TABLES_SCHEMA=ONE",
            "TABLES_SQLCL_CONNECTION=conn-one,conn-two": "TABLES_SQLCL_CONNECTION=conn-one",
            "TABLES_EXPECTED_USER=ONE,TWO": "TABLES_EXPECTED_USER=ONE",
        }
        result = self.run_check(replacements, "read", "tables", "TWO")
        self.assertEqual(2, result.returncode)
        self.assertIn("the tables profile does not list schema TWO", result.stderr)

    def test_production_looking_connection_is_still_refused_per_entry(self) -> None:
        replacements = {**TWO_SCHEMAS, "APEX_SQLCL_CONNECTION=conn-one,conn-two": "APEX_SQLCL_CONNECTION=conn-one,conn-prod"}
        result = self.run_check(replacements, "read", "apex", "TWO")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("resembles production", result.stderr)


class BackupCliTests(unittest.TestCase):
    NAMES = (
        "backup_db.sh", "backup_db.sql", "load_env.sh", "check_db_target.sh", "sqlcl_safe.sh", "replace_mirror.sh",
    )

    def make_checkout(self, root: Path, replacements: dict[str, str]) -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text(replacements), encoding="utf-8")
        git_init(root)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        # Positional: -S -noupdates -name <conn> @<script> <schema> <scope> <env> <user> <prefixes> <spool_schema>
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "connection=\"$4\"; schema=\"$6\"; scope=\"$7\"; spool_schema=\"${11}\"\n"
            "printf '%s|%s|%s\\n' \"$connection\" \"$schema\" \"$scope\" >> \"$FAKE_SQL_CALLS\"\n"
            "if [[ \"${FAKE_FAIL_SCHEMA:-}\" == \"$schema\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "if [[ \"$scope\" == tables ]]; then\n"
            "  mkdir -p \"database/$spool_schema/tables\"\n"
            "  printf 'CREATE TABLE T_%s;\\n' \"$schema\" > \"database/$spool_schema/tables/T_$schema.sql\"\n"
            "  printf 'TABLE=1\\n' > \"database/$spool_schema/manifest-tables.txt\"\n"
            "else\n"
            "  mkdir -p \"database/$spool_schema/views\" \"database/$spool_schema/synonyms\"\n"
            "  printf 'CREATE VIEW V_%s;\\n' \"$schema\" > \"database/$spool_schema/views/V_$schema.sql\"\n"
            "  printf 'CREATE SYNONYM S_%s;\\n' \"$schema\" > \"database/$spool_schema/synonyms/S_$schema.sql\"\n"
            "  if [[ \"${FAKE_SHORT_MANIFEST:-}\" == \"$schema\" ]]; then extra=2; else extra=1; fi\n"
            "  printf 'VIEW=1\\nSYNONYM=%s\\n' \"$extra\" > \"database/$spool_schema/manifest-code.txt\"\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment.pop("PROJECT_SCHEMA", None)
        return scripts / "backup_db.sh", environment

    def run_backup(self, script: Path, environment: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script)],
            cwd=script.parents[1],
            env={**environment, **extra},
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self, environment: dict[str, str]) -> list[str]:
        path = Path(environment["FAKE_SQL_CALLS"])
        return sorted(path.read_text().splitlines()) if path.exists() else []

    def test_every_listed_schema_is_mirrored_with_tables_code_and_synonyms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(
                ["conn-one|ONE|code", "conn-one|ONE|tables", "conn-two|TWO|code", "conn-two|TWO|tables"],
                self.calls(environment),
            )
            for schema in ("ONE", "TWO"):
                mirror = root / "database" / schema
                self.assertTrue((mirror / "tables" / f"T_{schema}.sql").is_file())
                self.assertTrue((mirror / "views" / f"V_{schema}.sql").is_file())
                self.assertTrue((mirror / "synonyms" / f"S_{schema}.sql").is_file())
            self.assertEqual(["ONE", "TWO"], sorted(path.name for path in (root / "database").iterdir()))

    def test_schema_option_mirrors_only_that_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment, PROJECT_SCHEMA="TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["conn-two|TWO|code", "conn-two|TWO|tables"], self.calls(environment))
            self.assertEqual(["TWO"], sorted(path.name for path in (root / "database").iterdir()))

    def test_a_profile_that_does_not_list_the_schema_skips_that_scope(self) -> None:
        replacements = {
            **TWO_SCHEMAS,
            "TABLES_SCHEMA=ONE,TWO": "TABLES_SCHEMA=ONE",
            "TABLES_SQLCL_CONNECTION=conn-one,conn-two": "TABLES_SQLCL_CONNECTION=conn-one",
            "TABLES_EXPECTED_USER=ONE,TWO": "TABLES_EXPECTED_USER=ONE",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, replacements)
            result = self.run_backup(script, environment, PROJECT_SCHEMA="TWO")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["conn-two|TWO|code"], self.calls(environment))
            self.assertFalse((root / "database" / "TWO" / "tables").exists())

    def test_an_unlisted_schema_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_backup(script, environment, PROJECT_SCHEMA="NOPE")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("not configured", result.stderr)
            self.assertEqual([], self.calls(environment))

    def test_a_failing_schema_installs_nothing_for_any_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment, FAKE_FAIL_SCHEMA="TWO")
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / "database").exists(), "no mirror may be installed after a failure")

    def test_a_short_manifest_installs_nothing_for_any_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment, FAKE_SHORT_MANIFEST="TWO")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("incomplete", result.stderr)
            self.assertFalse((root / "database").exists())

    def test_a_dirty_mirror_for_any_schema_is_refused_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            dirty = root / "database" / "TWO"
            dirty.mkdir(parents=True)
            (dirty / "local-edit.sql").write_text("-- edit\n", encoding="utf-8")
            result = self.run_backup(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("refusing to back up over dirty mirror: database/TWO", result.stderr)
            self.assertEqual([], self.calls(environment))

if __name__ == "__main__":
    unittest.main()
