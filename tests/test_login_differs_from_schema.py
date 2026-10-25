import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts import migrate
from scripts.db_targets import Target, resolve_target
from scripts.dev_test_target import DevTestTargetError, inspect_suite
from scripts.local_config import ConfigError, read_project_env
from scripts.migration_checks import _render_check_driver
from scripts.new_migration import owner_check
from scripts.schema_catalog import CatalogError, parse_inventory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
try:
    from test_schema_catalog import fixture_payload, framed
finally:
    sys.path.pop(0)

BASE = {
    "CODE_SQLCL_CONNECTION": "dev-db",
    "CODE_EXPECTED_USER": "APP_DEV",
    "CODE_SCHEMA": "APP_DEV",
    "DB_ENVIRONMENT": "development",
    "STAGING_SQLCL_CONNECTION": "stage-db",
    "STAGING_EXPECTED_USER": "APP_LOGIN",
    "STAGING_SCHEMA": "APP_STAGE",
}


class LoginDiffersFromSchemaTests(unittest.TestCase):
    def test_doc_exists_and_is_linked_from_readme_and_limitations(self) -> None:
        doc_path = ROOT / "docs" / "login-acts-as-schema.md"
        self.assertTrue(doc_path.is_file(), "the login/schema mode guide is missing")

        links = (
            (ROOT / "README.md", "docs/login-acts-as-schema.md"),
            (ROOT / "docs" / "known-limitations.md", "login-acts-as-schema.md"),
        )
        for path, link in links:
            with self.subTest(path=path.name):
                self.assertIn(link, path.read_text(encoding="utf-8"))

    def test_doc_names_the_banner_and_every_doctor_line(self) -> None:
        contents = (ROOT / "docs" / "login-acts-as-schema.md").read_text(encoding="utf-8")
        self.assertIn(
            "Acting as schema APP_STAGE through ALTER SESSION SET CURRENT_SCHEMA; login is APP_LOGIN.",
            contents,
        )

        for privilege in (
            "SELECT ANY DICTIONARY",
            "CREATE ANY PROCEDURE",
            "ALTER ANY PROCEDURE",
            "CREATE ANY TABLE",
            "ALTER ANY TABLE",
            "CREATE ANY INDEX",
            "CREATE ANY TRIGGER",
            "CREATE ANY SEQUENCE",
            "CREATE ANY VIEW",
            "CREATE ANY SYNONYM",
            "GRANT ANY OBJECT PRIVILEGE",
        ):
            with self.subTest(privilege=privilege):
                self.assertIn(f"SESSION_PRIVILEGE:{privilege}=", contents)
        self.assertIn("SESSION_ROLE:APEX_ADMINISTRATOR_ROLE=", contents)
        self.assertIn("SESSION_MODE:ACTS_AS_SCHEMA APP_STAGE LOGIN APP_LOGIN", contents)

    def test_doc_tells_check_authors_to_use_current_schema(self) -> None:
        contents = (ROOT / "docs" / "login-acts-as-schema.md").read_text(encoding="utf-8")
        writing_checks = contents.split("## Writing checks", 1)[1].split("\n## ", 1)[0]

        self.assertIn("CURRENT_SCHEMA", writing_checks)
        self.assertIn("SESSION_USER", writing_checks)

    def test_doctor_runs_privilege_report_only_when_login_differs(self) -> None:
        contents = (ROOT / "scripts" / "doctor.sql").read_text(encoding="utf-8")

        self.assertRegex(
            contents,
            r"(?is)COLUMN\s+doctor_session_privilege_script\s+NEW_VALUE\s+doctor_session_privilege_script\s+NOPRINT\s+"
            r"SELECT\s+CASE\s+WHEN\s+UPPER\('&&expected_user'\)\s*<>\s*UPPER\('&&target_schema'\)\s+"
            r"THEN\s+'doctor_session_privileges\.sql'\s+ELSE\s+'doctor_no_report\.sql'\s+END\s+"
            r"AS\s+doctor_session_privilege_script\s+FROM\s+DUAL\s*;\s*"
            r"@@&&doctor_session_privilege_script",
        )
        no_report = (ROOT / "scripts" / "doctor_no_report.sql").read_text(encoding="utf-8")
        self.assertRegex(no_report, r"(?s)\A\s*--[^\r\n]*(?:\r?\n)?\s*\Z")

    def test_privilege_report_is_read_only_and_uses_session_views(self) -> None:
        contents = (ROOT / "scripts" / "doctor_session_privileges.sql").read_text(encoding="utf-8")
        uncommented = re.sub(r"/\*.*?\*/|--[^\r\n]*", " ", contents, flags=re.DOTALL)
        without_literals = re.sub(r"'(?:''|[^'])*'", "''", uncommented)

        self.assertIn("SESSION_PRIVS", contents.upper())
        self.assertIn("SESSION_ROLES", contents.upper())
        self.assertNotRegex(contents, r"(?i)\bDBA_[A-Z0-9_$#]+\b")
        self.assertNotRegex(
            without_literals,
            r"(?i)\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|RENAME)\b",
        )

    def test_privilege_report_lists_the_pinned_names(self) -> None:
        contents = (ROOT / "scripts" / "doctor_session_privileges.sql").read_text(encoding="utf-8")
        for privilege in (
            "SELECT ANY DICTIONARY",
            "CREATE ANY PROCEDURE",
            "ALTER ANY PROCEDURE",
            "CREATE ANY TABLE",
            "ALTER ANY TABLE",
            "CREATE ANY INDEX",
            "CREATE ANY TRIGGER",
            "CREATE ANY SEQUENCE",
            "CREATE ANY VIEW",
            "CREATE ANY SYNONYM",
            "GRANT ANY OBJECT PRIVILEGE",
            "APEX_ADMINISTRATOR_ROLE",
        ):
            with self.subTest(name=privilege):
                self.assertIn(privilege, contents)
        for marker in (
            "SESSION_PRIVILEGE:",
            "SESSION_ROLE:APEX_ADMINISTRATOR_ROLE=",
            "SESSION_MODE:ACTS_AS_SCHEMA ",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, contents)

    def test_target_keeps_login_and_schema_apart(self) -> None:
        target = resolve_target(BASE, "staging", "migration")

        self.assertEqual((target.expected_user, target.schema), ("APP_LOGIN", "APP_STAGE"))

    def test_acts_as_other_schema_property(self) -> None:
        acting_target = Target("staging", "stage-db", "APP_LOGIN", "APP_STAGE", "staging")
        owner_target = Target("dev", "dev-db", "APP_DEV", "APP_DEV", "development")

        self.assertTrue(acting_target.acts_as_other_schema)
        self.assertFalse(owner_target.acts_as_other_schema)

    def test_migrate_summary_prints_banner_only_when_login_differs(self) -> None:
        acting_target = Target("staging", "stage-db", "APP_LOGIN", "APP_STAGE", "staging")
        owner_target = Target("dev", "dev-db", "APP_DEV", "APP_DEV", "development")
        banner = "Acting as schema APP_STAGE through ALTER SESSION SET CURRENT_SCHEMA; login is APP_LOGIN."

        with redirect_stdout(io.StringIO()) as acting_stdout:
            migrate._print_target_summary(acting_target, {}, [])
        acting_lines = acting_stdout.getvalue().splitlines()

        with redirect_stdout(io.StringIO()) as owner_stdout:
            migrate._print_target_summary(owner_target, {}, [])
        owner_output = owner_stdout.getvalue()

        self.assertEqual(acting_lines[1], banner)
        self.assertEqual(acting_stdout.getvalue().count(banner), 1)
        self.assertNotIn(banner, owner_output)

    def test_publish_prompt_names_the_login_when_it_differs(self) -> None:
        sys.path.insert(0, str(ROOT / "tests"))
        try:
            from fake_sqlcl import BASH
            from test_publish_cli import PublishAppCliTests
        finally:
            sys.path.pop(0)

        banner = "Acting as schema APP_STAGE through ALTER SESSION SET CURRENT_SCHEMA; login is APP_LOGIN."
        for expected_user in ("APP_LOGIN", "APP_STAGE"):
            with self.subTest(expected_user=expected_user), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                harness = PublishAppCliTests()
                runner, _, _, environment = harness.make_publish_fixture(root)
                env_path = root / ".env"
                env_path.write_text(
                    env_path.read_text(encoding="utf-8")
                    + f"\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER={expected_user}\n",
                    encoding="utf-8",
                )
                environment["PROJECT_ENV_FILE"] = str(env_path)
                app_dir = Path(environment["FAKE_SOURCE_DIR"])
                staging_descriptor = app_dir / "deployments" / "staging.json"
                staging_descriptor.write_text(
                    json.dumps({
                        "workspace": {"name": "APP_STAGE"},
                        "app": {"id": 100, "databaseSession": {"parsingSchema": "APP_STAGE"}},
                    }),
                    encoding="utf-8",
                )

                result = subprocess.run(
                    [BASH, str(runner), "100", "--env", "staging"],
                    cwd=root,
                    env=environment,
                    input=b"y\n",
                    capture_output=True,
                    check=False,
                )
                output = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")

                self.assertEqual(result.returncode, 0, output)
                self.assertIn("Deploying to STAGING. Proceed? [y/N]: ", output)
                if expected_user == "APP_LOGIN":
                    self.assertEqual(output.count(banner), 1)
                    self.assertLess(output.index(banner), output.index("Deploying to STAGING. Proceed? [y/N]: "))
                else:
                    self.assertNotIn(banner, output)

    def test_multi_schema_lists_stay_position_aligned(self) -> None:
        values = {
            **BASE,
            "STAGING_SQLCL_CONNECTION": "stage-a,stage-b",
            "STAGING_EXPECTED_USER": "APP_LOGIN,APP_LOGIN",
            "STAGING_SCHEMA": "APP_STAGE,APP_DEV",
        }

        target = resolve_target(values, "staging", "migration", schema="APP_DEV")

        self.assertEqual((target.connection, target.expected_user, target.schema), ("stage-b", "APP_LOGIN", "APP_DEV"))

    def test_migration_guard_checks_login_before_switching_schema(self) -> None:
        contents = (ROOT / "scripts" / "verify_migration_access.sql").read_text(encoding="utf-8")

        session_user_check = contents.index("IF l_session_user != c_expected_user")
        schema_switch = contents.index("ALTER SESSION SET CURRENT_SCHEMA =")
        current_schema_check = contents.index("IF SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') != c_target_schema")
        self.assertLess(session_user_check, schema_switch)
        self.assertLess(schema_switch, current_schema_check)

    def test_publish_guard_compares_both_identities(self) -> None:
        contents = (ROOT / "scripts" / "publish_identity_guard.sql").read_text(encoding="utf-8")

        self.assertIn("require_equal('SESSION_USER'", contents)
        self.assertIn("require_equal('CURRENT_SCHEMA'", contents)

    def test_generated_owner_check_reads_current_schema_not_session_user(self) -> None:
        sql = owner_check("APP_STAGE")["sql"]

        self.assertIn("CURRENT_SCHEMA", sql)
        self.assertNotIn("SESSION_USER", sql)

    def test_check_driver_switches_schema_and_reports_both_identities(self) -> None:
        target = Target("staging", "stage-db", "APP_LOGIN", "APP_STAGE", "staging")
        driver = _render_check_driver(target, [], "preconditions")

        schema_switch = driver.index("ALTER SESSION SET CURRENT_SCHEMA = APP_STAGE;")
        declaration = driver.index("DECLARE")
        self.assertLess(schema_switch, declaration)
        self.assertIn("l_result.put('session_user'", driver)
        self.assertIn("l_result.put('current_schema'", driver)

    def test_catalog_accepts_metadata_privilege_path_for_non_owner(self) -> None:
        target = Target("staging", "stage-db", "APP_LOGIN", "APP_STAGE", "staging")

        permitted = fixture_payload("owner-inventory.json")
        permitted["identity"].update({"session_user": "APP_LOGIN", "current_schema": "APP_STAGE"})
        for item in permitted["objects"]:
            item["owner"] = "APP_STAGE"
        permitted["coverage"].update({
            "path": "METADATA_PRIVILEGE",
            "privileges": ["SELECT ANY DICTIONARY"],
            "metadataReadable": True,
        })
        inventory = parse_inventory(framed(permitted), target)
        self.assertTrue(inventory.coverage["ownerComplete"])

        denied = fixture_payload("owner-inventory.json")
        denied["identity"].update({"session_user": "APP_LOGIN", "current_schema": "APP_STAGE"})
        for item in denied["objects"]:
            item["owner"] = "APP_STAGE"
        denied["coverage"].update({"path": "METADATA_PRIVILEGE", "privileges": [], "metadataReadable": True})
        with self.assertRaisesRegex(CatalogError, "enabled metadata-reader privileges were not validated"):
            parse_inventory(framed(denied), target)

    def test_owner_only_commands_name_the_reason(self) -> None:
        dev_values = {
            "CODE_SQLCL_CONNECTION": "dev-db",
            "CODE_EXPECTED_USER": "APP_LOGIN",
            "CODE_SCHEMA": "APP_STAGE",
            "DB_ENVIRONMENT": "development",
        }
        with self.assertRaises(DevTestTargetError) as dev_error:
            inspect_suite(dev_values, "APP_STAGE", None, None)
        self.assertIn("session_user", str(dev_error.exception))
        self.assertIn("selected_schema", str(dev_error.exception))

        env_text = "\n".join((
            "PROJECT_NAME=APP_LOGIN",
            "DEVELOPER_NAME=APP_DEV",
            "DB_ENVIRONMENT=development",
            "APEX_APP_ID=100",
            "TABLES_SCHEMA=APP_DEV",
            "TABLES_PREFIXES=*",
            "TABLES_SQLCL_CONNECTION=dev-db",
            "TABLES_EXPECTED_USER=APP_DEV",
            "CODE_SCHEMA=APP_DEV",
            "CODE_PREFIXES=*",
            "CODE_SQLCL_CONNECTION=dev-db",
            "CODE_EXPECTED_USER=APP_DEV",
            "APEX_PARSING_SCHEMA=APP_DEV",
            "APEX_SQLCL_CONNECTION=dev-db",
            "APEX_EXPECTED_USER=APP_DEV",
            "ORDS_SCHEMA=APP_STAGE",
            "ORDS_SQLCL_CONNECTION=stage-db",
            "ORDS_EXPECTED_USER=APP_LOGIN",
            "",
        ))
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / ".env"
            env_path.write_text(env_text, encoding="utf-8")
            with self.assertRaises(ConfigError) as config_error:
                read_project_env(env_path)
        self.assertIn("needs the schema owner", str(config_error.exception))


if __name__ == "__main__":
    unittest.main()
