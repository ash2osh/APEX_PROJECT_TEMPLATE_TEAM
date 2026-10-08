import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import fake_sqlcl
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


def plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*\|?\s*", " ", text)


def script_command(script: Path, *arguments: str) -> list[str]:
    if script.suffix.casefold() == ".ps1":
        return ["pwsh", "-NoProfile", "-File", str(script), *arguments]
    return [BASH, str(script), *arguments]


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
    NAMES = ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "verify_db_access.sql", "sqlcl_safe.sh", "apex_compatibility.py")

    def test_apex_doctor_requires_explicit_workspace_username(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            script,environment=self.make_checkout(root,{'APEX_WORKSPACE_USERNAME=DEMO':'# username deliberately unset'})
            environment['APEX_WORKSPACE_USERNAME']='INHERITED'
            result=self.run_team(script,environment,'doctor')
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('APEX_WORKSPACE_USERNAME',plain(result.stderr))

    def test_workspace_validation_is_not_required_for_table_only_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            settings={**TWO_SCHEMAS,'APEX_PARSING_SCHEMA=DEMO':'APEX_PARSING_SCHEMA=ONE',
                'APEX_SQLCL_CONNECTION=docker-demo':'APEX_SQLCL_CONNECTION=conn-one',
                'APEX_EXPECTED_USER=DEMO':'APEX_EXPECTED_USER=ONE',
                'APEX_WORKSPACE_USERNAME=DEMO':'# username deliberately unset'}
            script,environment=self.make_checkout(root,settings)
            result=self.run_team(script,environment,'doctor','--schema','TWO')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

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
            # Drain stdin first. PowerShell feeds it through a pipe; a fake that exits before the
            # pipe is closed races Start-Process ("Broken pipe", or a transcript read too early).
            "cat > /dev/null\n"
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release %s Production\\n' \"${FAKE_SQLCL_VERSION:-26.3}\"; exit 0; fi\n"
            "printf '%s|%s|%s\\n' \"$4\" \"$6\" \"$8\" >> \"$FAKE_SQL_CALLS\"\n"
            "if [[ \"${FAKE_FAIL_CONNECTION:-}\" == \"$4\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "printf 'APEX_DOCTOR_VERIFIED:%s\\nAPEX_RELEASE_VERIFIED:26.2\\nAPEX_WORKSPACE_USERS_VERIFIED\\n' \"$8\"\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(calls)
        environment.pop("PROJECT_SCHEMA", None)
        script_name = "team.ps1" if "team.ps1" in self.NAMES else "team.sh"
        return scripts / script_name, environment

    def run_team(self, script: Path, environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            script_command(script, *arguments),
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
            self.assertIn("Doctor checks passed for the configured DEV connection.", plain(result.stdout))
            self.assertEqual(["docker-demo|DEMO|DEMO"], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines())

    def test_old_sqlcl_refuses_apex_doctor_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), {})
            environment["FAKE_SQLCL_VERSION"] = "26.2.2.0"
            result = self.run_team(script, environment, "doctor")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("SQLcl 26.3.0.0", plain(result.stderr))
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())

    def test_table_only_selection_does_not_require_apex_sqlcl_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replacements = {"APEX_PARSING_SCHEMA=DEMO": "APEX_PARSING_SCHEMA=OTHER",
                            "APEX_EXPECTED_USER=DEMO": "APEX_EXPECTED_USER=OTHER",
                            "APEX_SQLCL_CONNECTION=docker-demo": "APEX_SQLCL_CONNECTION=other-db"}
            script, environment = self.make_checkout(Path(temporary), replacements)
            environment["FAKE_SQLCL_VERSION"] = "26.2.2.0"
            result = self.run_team(script, environment, "doctor", "--schema", "DEMO")
            self.assertEqual(0, result.returncode, plain(result.stdout + result.stderr))
            self.assertEqual(["docker-demo|DEMO|DEMO"], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines())

    def test_multi_schema_doctor_checks_every_schema_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines()
            self.assertEqual(["conn-one|ONE|ONE", "conn-two|TWO|TWO"], sorted(calls))
            self.assertIn("Doctor checks passed for all 2 configured DEV schema connections.", plain(result.stdout))

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
            self.assertIn("ONE", plain(result.stderr))

    def test_unknown_or_malformed_schema_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            for value, expected in (("NOPE", "not configured"), ("two", "uppercase Oracle identifier")):
                with self.subTest(schema=value):
                    result = self.run_team(script, environment, "doctor", "--schema", value)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(expected, plain(result.stderr))
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())

    def test_schema_option_requires_a_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor", "--schema")
            self.assertEqual(2, result.returncode)
            self.assertIn("--schema requires a schema name", plain(result.stderr))

    def test_an_empty_schema_value_is_refused_before_any_sqlcl_call(self) -> None:
        # An empty name used to select nothing, which widened the command to every schema.
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            for arguments in (("--schema", ""), ("--schema=",)):
                with self.subTest(arguments=arguments):
                    result = self.run_team(script, environment, "doctor", *arguments)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("--schema requires a schema name", plain(result.stderr))
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())

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
                [BASH, str(scripts / "check_db_target.sh"), *arguments],
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
            # Drain stdin first. PowerShell feeds it through a pipe; a fake that exits before the
            # pipe is closed races Start-Process ("Broken pipe", or a transcript read too early).
            "cat > /dev/null\n"
            "set -euo pipefail\n"
            "connection=\"$4\"; schema=\"$6\"; scope=\"$7\"; spool_schema=\"${11}\"\n"
            "printf '%s|%s|%s\\n' \"$connection\" \"$schema\" \"$scope\" >> \"$FAKE_SQL_CALLS\"\n"
            "if [[ \"${FAKE_FAIL_SCHEMA:-}\" == \"$schema\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "# Mirrors the real driver's complete, alphabetically ordered type rows.\n"
            "if [[ \"$scope\" == tables ]]; then\n"
            "  mkdir -p \"database/$spool_schema/tables\"\n"
            "  printf 'CREATE TABLE T_%s;\\n' \"$schema\" > \"database/$spool_schema/tables/T_$schema.sql\"\n"
            "  printf 'TABLE=1\\n' > \"database/$spool_schema/manifest-tables.txt\"\n"
            "else\n"
            "  mkdir -p \"database/$spool_schema/views\" \"database/$spool_schema/synonyms\"\n"
            "  printf 'CREATE VIEW V_%s;\\n' \"$schema\" > \"database/$spool_schema/views/V_$schema.sql\"\n"
            "  printf 'CREATE SYNONYM S_%s;\\n' \"$schema\" > \"database/$spool_schema/synonyms/S_$schema.sql\"\n"
            "  if [[ \"${FAKE_SHORT_MANIFEST:-}\" == \"$schema\" ]]; then extra=2; else extra=1; fi\n"
            "  manifest_rows=(\"FUNCTION=0\" \"PACKAGE=0\" \"PACKAGE BODY=0\" \"PROCEDURE=0\" \"SYNONYM=$extra\" \"TRIGGER=0\" \"VIEW=1\")\n"
            "  if [[ \"${FAKE_OMIT_MANIFEST_SCHEMA:-}\" == \"$schema\" ]]; then\n"
            "    remaining_rows=()\n"
            "    for manifest_row in \"${manifest_rows[@]}\"; do\n"
            "      [[ \"${manifest_row%=*}\" == \"${FAKE_OMIT_MANIFEST_TYPE:-}\" ]] || remaining_rows+=(\"$manifest_row\")\n"
            "    done\n"
            "    manifest_rows=(\"${remaining_rows[@]}\")\n"
            "  fi\n"
            "  printf '%s\\n' \"${manifest_rows[@]}\" > \"database/$spool_schema/manifest-code.txt\"\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment.pop("PROJECT_SCHEMA", None)
        script_name = "backup_db.ps1" if "backup_db.ps1" in self.NAMES else "backup_db.sh"
        return scripts / script_name, environment

    def run_backup(self, script: Path, environment: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            script_command(script),
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
            self.assertIn("not configured", plain(result.stderr))
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
            self.assertIn("incomplete", plain(result.stderr))
            self.assertFalse((root / "database").exists())

    def test_missing_required_manifest_type_installs_nothing_for_any_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(
                script,
                environment,
                FAKE_OMIT_MANIFEST_SCHEMA="TWO",
                FAKE_OMIT_MANIFEST_TYPE="TRIGGER",
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "database backup manifest for TWO (code) is missing the TRIGGER row; the mirror was not replaced",
                plain(result.stderr),
            )
            self.assertFalse((root / "database").exists())

    def test_complete_manifest_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_backup(script, environment)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for schema in ("ONE", "TWO"):
                mirror = root / "database" / schema
                self.assertEqual("TABLE=1\n", (mirror / "manifest-tables.txt").read_text(encoding="utf-8"))
                self.assertEqual(
                    "FUNCTION=0\nPACKAGE=0\nPACKAGE BODY=0\nPROCEDURE=0\nSYNONYM=1\nTRIGGER=0\nVIEW=1\n",
                    (mirror / "manifest-code.txt").read_text(encoding="utf-8"),
                )

    def test_a_dirty_mirror_for_any_schema_is_refused_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            dirty = root / "database" / "TWO"
            dirty.mkdir(parents=True)
            (dirty / "local-edit.sql").write_text("-- edit\n", encoding="utf-8")
            result = self.run_backup(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("refusing to back up over dirty mirror: database/TWO", plain(result.stderr))
            self.assertEqual([], self.calls(environment))

class ExportCliTests(unittest.TestCase):
    NAMES = (
        "export_apps.sh", "export_apps.sql", "verify_deployment_state.sql", "apex_compatibility.py", "verify_apex_release.sql", "lookup_app_schema.sql", "load_env.sh", "check_db_target.sh",
        "sqlcl_safe.sh", "normalize_apx.sh", "replace_mirror.sh", "verify_db_access.sql",
        "record_export_state.py", "source_evidence.py", "preserve_deployments.py",
    )
    APP_SCHEMAS = "117:ONE,301:TWO,205:THREE"

    def make_checkout(self, root: Path, replacements: dict[str, str], app_ids: str = "117,301") -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text({**replacements, "APEX_APP_ID=100,200": f"APEX_APP_ID={app_ids}"}), encoding="utf-8")
        git_init(root)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        # lookup:  -S -noupdates -name <conn> @lookup_app_schema.sql <schema> <app> <env> <user>
        # export:  -S -noupdates -name <conn> @export_apps.sql      <schema> <app> <env> <user>
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            # Drain stdin first. PowerShell feeds it through a pipe; a fake that exits before the
            # pipe is closed races Start-Process ("Broken pipe", or a transcript read too early).
            "cat > /dev/null\n"
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release 26.3 Production\\n'; exit 0; fi\n"
            "set -euo pipefail\n"
            "connection=\"$4\"; script=\"$5\"; schema=\"$6\"; app=\"$7\"\n"
            "printf '%s|%s|%s|%s\\n' \"$(basename \"${script#@}\")\" \"$connection\" \"$schema\" \"$app\" >> \"$FAKE_SQL_CALLS\"\n"
            "case \"$script\" in\n"
            "  *lookup_app_schema.sql)\n"
            "    for pair in ${FAKE_APP_SCHEMAS//,/ }; do\n"
            "      if [[ \"${pair%%:*}\" == \"$app\" ]]; then printf 'APEX_APP_SCHEMA:%s:%s\\n' \"$app\" \"${pair##*:}\"; exit 0; fi\n"
            "    done\n"
            "    printf 'APEX_APP_SCHEMA:%s:NOT_FOUND\\n' \"$app\"\n"
            "    ;;\n"
            "  *export_apps.sql)\n"
            "    if [[ \"${FAKE_FAIL_APP:-}\" == \"$app\" ]]; then printf 'ORA-01017: invalid credentials\\n'; exit 1; fi\n"
            "    mkdir -p \"apps/$schema/exported/.apex\"\n"
            "    printf 'source of %s\\n' \"$app\" > \"apps/$schema/exported/application.apx\"\n"
            "    printf '{\"mmdVersion\":\"26.2.0+3479\"}\\n' > \"apps/$schema/exported/.apex/apexlang.json\"\n"
            "    printf '2026-09-26T08:00:00|2026-09-26T09:00:00|Release 1.0\\n' > .apex-export-before.txt\n"
            "    printf '2026-09-26T08:00:00|2026-09-26T09:00:02|Release 1.0\\n' > .apex-export-after.txt\n"
            "    ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment["FAKE_APP_SCHEMAS"] = self.APP_SCHEMAS
        environment.pop("PROJECT_SCHEMA", None)
        script_name = "export_apps.ps1" if "export_apps.ps1" in self.NAMES else "export_apps.sh"
        return scripts / script_name, environment

    def run_export(self, script: Path, environment: dict[str, str], *arguments: str, **extra: str) -> subprocess.CompletedProcess[str]:
        command_arguments = arguments
        if script.suffix.casefold() == ".ps1" and arguments:
            command_arguments = ("-AppId", arguments[0], *arguments[1:])
        return subprocess.run(
            script_command(script, *command_arguments),
            cwd=script.parents[1],
            env={**environment, **extra},
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self, environment: dict[str, str]) -> list[str]:
        path = Path(environment["FAKE_SQL_CALLS"])
        return path.read_text().splitlines() if path.exists() else []

    def test_each_app_is_exported_under_its_own_schema_with_its_own_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_export(script, environment)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual("source of 117\n", (root / "apps" / "ONE" / "117" / "application.apx").read_text())
            self.assertEqual("source of 301\n", (root / "apps" / "TWO" / "301" / "application.apx").read_text())
            exports = [call for call in self.calls(environment) if call.startswith("export_apps.sql")]
            self.assertEqual(["export_apps.sql|conn-one|ONE|117", "export_apps.sql|conn-two|TWO|301"], exports)
            lookups = [call for call in self.calls(environment) if call.startswith("lookup_app_schema.sql")]
            self.assertEqual(["lookup_app_schema.sql|conn-one|ONE|117", "lookup_app_schema.sql|conn-one|ONE|301"], lookups)

    def test_single_app_argument_exports_only_that_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_export(script, environment, "301")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue((root / "apps" / "TWO" / "301").is_dir())
            self.assertFalse((root / "apps" / "ONE").exists())

    def test_app_owned_by_an_unlisted_schema_is_refused_before_any_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS, app_ids="205")
            result = self.run_export(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("THREE", plain(result.stderr))
            self.assertIn("not listed in APEX_PARSING_SCHEMA", plain(result.stderr))
            self.assertFalse(any(call.startswith("export_apps.sql") for call in self.calls(environment)))
            self.assertFalse((root / "apps").exists())

    def test_unknown_app_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS, app_ids="999")
            result = self.run_export(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("999", plain(result.stderr))

    def test_schema_option_must_match_the_apps_parsing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_export(script, environment, "117", PROJECT_SCHEMA="TWO")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("117", plain(result.stderr))
            self.assertIn("ONE", plain(result.stderr))
            self.assertFalse(any(call.startswith("export_apps.sql") for call in self.calls(environment)))

    def test_a_failing_export_installs_no_application(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            result = self.run_export(script, environment, FAKE_FAIL_APP="301")
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / "apps").exists(), "an earlier app must not be installed after a later failure")

    def test_dirty_destination_is_refused_before_any_export_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, TWO_SCHEMAS)
            dirty = root / "apps" / "TWO" / "301"
            dirty.mkdir(parents=True)
            (dirty / "application.apx").write_text("local edit\n", encoding="utf-8")
            result = self.run_export(script, environment)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("refusing to export over dirty mirror: apps/TWO/301", plain(result.stderr))
            self.assertFalse(any(call.startswith("export_apps.sql") for call in self.calls(environment)))

    def test_single_schema_export_makes_no_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(root, {}, app_ids="100")
            result = self.run_export(script, environment, "100")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(["export_apps.sql|docker-demo|DEMO|100"], self.calls(environment))
            self.assertTrue((root / "apps" / "DEMO" / "100").is_dir())

    def test_production_looking_lookup_connection_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            env_path = root / ".env"
            env_path.write_text(
                env_path.read_text(encoding="utf-8").replace(
                    "APEX_SQLCL_CONNECTION=conn-one,conn-two",
                    "APEX_SQLCL_CONNECTION=conn-prod,conn-two",
                ),
                encoding="utf-8",
            )
            result = self.run_export(script, environment, "117")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("resembles production", plain(result.stderr))
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())

class PublishCliTests(unittest.TestCase):
    NAMES = (
        "publish_app.sh", "publish_app.sql", "deployment_descriptor.py", "application_lock.py", "application_lock.sql", "no_application_lock.sql", "db_targets.py", "sqlcl_session.py", "sqlcl_session.sh", "windows_job.py", "apex_compatibility.py", "verify_apex_release.sql", "load_env.sh", "check_db_target.sh", "export_apps.sql", "verify_deployment_state.sql",
        "lookup_app_schema.sql", "verify_db_access.sql", "normalize_apx.sh", "record_export_state.py", "source_evidence.py",
        "verify_publish_state.py", "validate_app_source.py", "stamp_publish_version.py",
        "check_builder_drift.py", "check_builder_drift.sql", "sqlcl_safe.sh",
    )

    def make_fixture(
        self, root: Path, folder_schema: str, descriptor_schema: str, extra_env: str = "", schemas: dict[str, str] = TWO_SCHEMAS,
    ) -> tuple[Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in self.NAMES:
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".env").write_text(env_text({**schemas, "APEX_APP_ID=100,200": "APEX_APP_ID=117"}) + extra_env, encoding="utf-8")
        app = root / "apps" / folder_schema / "117"
        (app / "deployments").mkdir(parents=True)
        (app / ".apex").mkdir()
        (app / "application.apx").write_text('app SAMPLE (\n    name: Sample\n    version: "Release 1.0"\n)\n', encoding="utf-8")
        (app / ".apex" / "apexlang.json").write_text('{"version":1}\n', encoding="utf-8")
        import json
        for environment_name in ("dev", "staging", "prod"):
            (app / "deployments" / f"{environment_name}.json").write_text(
                json.dumps({"workspace": {"name": "WS"}, "app": {"id": 117, "databaseSession": {"parsingSchema": descriptor_schema}}}),
                encoding="utf-8",
            )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            # Drain stdin first. PowerShell feeds it through a pipe; a fake that exits before the
            # pipe is closed races Start-Process ("Broken pipe", or a transcript read too early).
            "cat > /dev/null\n"
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release 26.3 Production\\n'; exit 0; fi\n"
            'if [[ -n "${FAKE_LOCK_HANDLER:-}" ]]; then python3 "$FAKE_LOCK_HANDLER" "$@"; lock_status=$?; [[ $lock_status == 3 ]] || exit "$lock_status"; fi\n'
            "connection=\"$4\"; script=\"$5\"\n"
            "printf '%s|%s\\n' \"$(basename \"${script#@}\")\" \"$connection\" >> \"$FAKE_SQL_CALLS\"\n"
            "case \"$script\" in\n"
            "  *lookup_app_schema.sql) printf '%s\\n' \"$8\" >> \"$FAKE_SQL_LOOKUP_ENVIRONMENTS\"; printf 'APEX_APP_SCHEMA:117:%s\\n' \"${FAKE_LIVE_SCHEMA:-NOT_FOUND}\" ;;\n"
            "  *check_builder_drift.sql) printf 'APEX_DRIFT_QUERY_VERIFIED\\n' ;;\n"
            "  *publish_app.sql) printf 'Import successful.\\nAPEX_IMPORT_VERIFIED:117\\n' ;;\n"
            "  *) : ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["PROJECT_ENV_FILE"] = str(root / ".env")
        environment["FAKE_LOCK_HANDLER"] = str(ROOT/"tests/fake_application_lock_sql.py")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        environment["FAKE_SQL_LOOKUP_ENVIRONMENTS"] = str(root / "lookup-environments.txt")
        environment.pop("PROJECT_SCHEMA", None)
        script_name = "publish_app.ps1" if "publish_app.ps1" in self.NAMES else "publish_app.sh"
        return scripts / script_name, environment

    def run_publish(self, script: Path, environment: dict[str, str], *arguments: str, input_text: str | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            script_command(script, "117", *arguments),
            cwd=script.parents[1], env={**environment, **extra}, input=input_text, text=True, capture_output=True, check=False,
        )

    def calls(self, environment: dict[str, str]) -> list[str]:
        path = Path(environment["FAKE_SQL_CALLS"])
        return path.read_text().splitlines() if path.exists() else []

    def test_describe_selects_the_entry_for_the_descriptors_schema(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\nSTAGING_SCHEMA=ONE,TWO\n"
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
            result = self.run_publish(script, environment, "--env", "staging", "--describe")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(["TWO", "stage-two", "STWO"], [lines[2], lines[3], lines[4]])

    def test_dev_describe_uses_the_apex_profile_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--describe")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(["TWO", "conn-two", "TWO"], [lines[2], lines[3], lines[4]])

    def test_folder_and_descriptor_schema_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "ONE", "TWO")
            result = self.run_publish(script, environment, "--describe")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is stored under apps/ONE", plain(result.stderr))
            self.assertEqual([], self.calls(environment))

    def test_one_schema_project_also_requires_folder_descriptor_and_profile_to_agree(self) -> None:
        # Only the multi-schema branch used to compare them: a one-schema project (DEMO)
        # imported a descriptor naming DEMO2 and changed the live app's parsing schema.
        cases = (
            ("DEMO", "DEMO2", "is stored under apps/DEMO but its descriptor parses as DEMO2"),
            ("DEMO2", "DEMO2", "schema DEMO2 is not listed in APEX_PARSING_SCHEMA"),
        )
        for folder_schema, descriptor_schema, expected in cases:
            with self.subTest(folder=folder_schema, descriptor=descriptor_schema), tempfile.TemporaryDirectory() as temporary:
                script, environment = self.make_fixture(Path(temporary), folder_schema, descriptor_schema, schemas={})
                result = self.run_publish(script, environment, "--force")
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(expected, plain(result.stderr))
                self.assertEqual([], self.calls(environment))

    def test_one_schema_project_publishes_its_own_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "DEMO", "DEMO", schemas={})
            result = self.run_publish(script, environment, "--describe")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            # Bash prints one field per line, PowerShell one tab-separated line.
            self.assertEqual("DEMO", result.stdout.replace("\t", "\n").splitlines()[2])

    def test_schema_option_must_match_the_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--describe", PROJECT_SCHEMA="ONE")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("does not match the application's parsing schema", plain(result.stderr))

    def test_live_parsing_schema_must_agree_before_the_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--force", FAKE_LIVE_SCHEMA="ONE")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is parsed by ONE", plain(result.stderr))
            self.assertFalse(any(call.startswith("publish_app.sql") for call in self.calls(environment)))

    def test_schema_missing_from_the_staging_list_is_refused(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one\nSTAGING_EXPECTED_USER=SONE\nSTAGING_SCHEMA=ONE\n"
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
            result = self.run_publish(script, environment, "--env", "staging")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is not listed in STAGING_SCHEMA", plain(result.stderr))
            self.assertEqual([], self.calls(environment))

    def test_multischema_staging_and_production_require_a_schema_list(self) -> None:
        for target in ("STAGING", "PROD"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                extra = f"\n{target}_SQLCL_CONNECTION=target-only\n{target}_EXPECTED_USER=TARGET_USER\n"
                script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
                result = self.run_publish(script, environment, "--env", "staging" if target == "STAGING" else "prod", "--describe")
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn(f"is not listed in {target}_SCHEMA", plain(result.stderr))
                self.assertEqual([], self.calls(environment))

    def test_live_schema_lookup_uses_the_selected_publish_environment(self) -> None:
        extra = (
            "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\nSTAGING_SCHEMA=ONE,TWO\n"
            "PROD_SQLCL_CONNECTION=prod-one,prod-two\nPROD_EXPECTED_USER=PONE,PTWO\nPROD_SCHEMA=ONE,TWO\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
            for app_environment, expected_lookup_environment in (("staging", "staging"), ("prod", "production")):
                with self.subTest(app_environment=app_environment):
                    lookup_path = Path(environment["FAKE_SQL_LOOKUP_ENVIRONMENTS"])
                    lookup_path.unlink(missing_ok=True)
                    result = self.run_publish(
                        script,
                        environment,
                        "--env",
                        app_environment,
                        "--force",
                        input_text="yes\n",
                        FAKE_LIVE_SCHEMA="ONE",
                    )
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    self.assertIn("is parsed by ONE", plain(result.stderr))
                    self.assertEqual([expected_lookup_environment], lookup_path.read_text(encoding="utf-8").splitlines())

    def test_schema_missing_from_the_apex_profile_is_refused(self) -> None:
        replacements = {**TWO_SCHEMAS, "APEX_PARSING_SCHEMA=ONE,TWO": "APEX_PARSING_SCHEMA=ONE,THREE", "APEX_EXPECTED_USER=ONE,TWO": "APEX_EXPECTED_USER=ONE,THREE"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_fixture(root, "TWO", "TWO")
            (root / ".env").write_text(env_text({**replacements, "APEX_APP_ID=100,200": "APEX_APP_ID=117"}), encoding="utf-8")
            result = self.run_publish(script, environment, "--force")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("is not listed in APEX_PARSING_SCHEMA", plain(result.stderr))

    def test_production_looking_dev_lookup_connection_is_refused_before_any_sqlcl_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script, environment = self.make_fixture(root, "ONE", "ONE")
            env_path = root / ".env"
            env_path.write_text(
                env_path.read_text(encoding="utf-8").replace(
                    "APEX_SQLCL_CONNECTION=conn-one,conn-two",
                    "APEX_SQLCL_CONNECTION=conn-prod,conn-two",
                ),
                encoding="utf-8",
            )
            result = self.run_publish(script, environment, "--force")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("resembles production", plain(result.stderr))
            self.assertFalse(Path(environment["FAKE_SQL_CALLS"]).exists())


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellDoctorCliTests(DoctorCliTests):
    NAMES = ("team.ps1", "load_env.ps1", "check_db_target.ps1", "invoke_sqlcl.ps1", "resolve_python.ps1", "doctor.sql", "verify_db_access.sql", "apex_compatibility.py")

    def test_schema_option_requires_a_value(self) -> None:
        # PowerShell reports this usage error with exit code 1; Bash uses 2.
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_checkout(Path(temporary), TWO_SCHEMAS)
            result = self.run_team(script, environment, "doctor", "--schema")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("--schema requires a schema name", plain(result.stderr))
            self.assertEqual([], Path(environment["FAKE_SQL_CALLS"]).read_text().splitlines() if Path(environment["FAKE_SQL_CALLS"]).exists() else [])


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellBackupCliTests(BackupCliTests):
    NAMES = ("backup_db.ps1", "backup_db.sql", "load_env.ps1", "check_db_target.ps1", "invoke_sqlcl.ps1", "resolve_python.ps1", "replace_mirror.ps1")

    def test_backup_profile_loop_does_not_shadow_automatic_profile_variable(self) -> None:
        source = (ROOT / "scripts" / "backup_db.ps1").read_text(encoding="utf-8")
        self.assertRegex(source, r"(?im)^foreach \(\$backupProfile in @\(")
        self.assertNotRegex(source, r"(?i)\$profile\b")


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellExportCliTests(ExportCliTests):
    NAMES = (
        "export_apps.ps1", "export_apps.sql", "verify_deployment_state.sql", "apex_compatibility.py", "verify_apex_release.sql", "lookup_app_schema.sql", "load_env.ps1", "check_db_target.ps1",
        "invoke_sqlcl.ps1", "resolve_python.ps1", "normalize_apx.ps1", "replace_mirror.ps1", "verify_db_access.sql",
        "record_export_state.py", "source_evidence.py", "preserve_deployments.py",
    )


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellPublishCliTests(PublishCliTests):
    NAMES = (
        "publish_app.ps1", "publish_app.sql", "deployment_descriptor.py", "apex_compatibility.py", "verify_apex_release.sql", "load_env.ps1", "check_db_target.ps1", "invoke_sqlcl.ps1", "resolve_python.ps1",
        "export_apps.sql", "verify_deployment_state.sql", "apex_compatibility.py", "verify_apex_release.sql", "lookup_app_schema.sql", "verify_db_access.sql", "normalize_apx.ps1",
        "record_export_state.py", "source_evidence.py", "verify_publish_state.py", "validate_app_source.py",
        "stamp_publish_version.py", "check_builder_drift.py", "check_builder_drift.sql",
    )

    def test_describe_selects_the_entry_for_the_descriptors_schema(self) -> None:
        # PowerShell emits --describe as one tab-delimited record; the Bash test
        # checks the same schema, connection, and user in a multi-line layout.
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\nSTAGING_SCHEMA=ONE,TWO\n"
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO", extra)
            result = self.run_publish(script, environment, "--env", "staging", "--describe")
            self.assertEqual(0, result.returncode, plain(result.stdout + result.stderr))
            fields = result.stdout.rstrip("\r\n").split("\t")
            self.assertEqual(["TWO", "stage-two", "STWO"], fields[2:5])

    def test_dev_describe_uses_the_apex_profile_entry(self) -> None:
        # See the staging equivalent above: assert the target tuple from the
        # PowerShell record rather than Bash's line-number-specific rendering.
        with tempfile.TemporaryDirectory() as temporary:
            script, environment = self.make_fixture(Path(temporary), "TWO", "TWO")
            result = self.run_publish(script, environment, "--describe")
            self.assertEqual(0, result.returncode, plain(result.stdout + result.stderr))
            fields = result.stdout.rstrip("\r\n").split("\t")
            self.assertEqual(["TWO", "conn-two", "TWO"], fields[2:5])


if __name__ == "__main__":
    unittest.main()
