import re
import sys
import unittest
from pathlib import Path
import tempfile

from scripts.db_targets import Target
from scripts.schema_catalog import _catalog_driver


ROOT = Path(__file__).resolve().parents[1]
SQL_DRIVERS = (
    "backup_db.sql",
    "check_builder_drift.sql",
    "doctor.sql",
    "export_apps.sql",
    "lookup_app_schema.sql",
    "migrate.sql",
    "publish_app.sql",
    "schema_catalog.sql",
)


class SqlDriverContractTests(unittest.TestCase):
    def test_sql_errors_use_stable_failure_status_and_rollback(self) -> None:
        for name in SQL_DRIVERS:
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", contents)
                self.assertIn("WHENEVER OSERROR EXIT FAILURE ROLLBACK", contents)
                self.assertNotIn("SQL.SQLCODE", contents)

    def test_read_only_drivers_exit_success_with_rollback(self) -> None:
        for name in ("check_builder_drift.sql", "doctor.sql", "export_apps.sql", "lookup_app_schema.sql", "backup_db.sql"):
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertRegex(contents, r"(?im)^EXIT SUCCESS ROLLBACK\s*$")

    def test_generated_catalog_driver_exits_success_with_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            contents = driver.read_text(encoding="utf-8")
        self.assertRegex(contents, r"(?im)^EXIT SUCCESS ROLLBACK\s*$")
        self.assertNotIn("COMMIT", contents)
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = APP_DEV", contents)

    def test_generated_catalog_driver_terminates_alter_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            lines = driver.read_text(encoding="utf-8").splitlines()

        alter_session = next(line for line in lines if line.startswith("ALTER SESSION SET CURRENT_SCHEMA ="))
        self.assertTrue(alter_session.endswith(";"))

    def test_generated_catalog_driver_silences_sqlcl_before_first_include(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            lines = driver.read_text(encoding="utf-8").splitlines()

        first_include = next(index for index, line in enumerate(lines) if line.startswith("@@schema_catalog.sql"))
        self.assertIn("SET VERIFY OFF", lines[:first_include])
        self.assertIn("SET FEEDBACK OFF", lines[:first_include])

    def test_generated_catalog_driver_disables_ddl_insert_before_first_include(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            lines = driver.read_text(encoding="utf-8").splitlines()

        first_include = next(index for index, line in enumerate(lines) if line.startswith("@@schema_catalog.sql"))
        self.assertIn("SET DDL INSERT OFF", lines[:first_include])

    def test_identity_and_drift_drivers_emit_verification_sentinels(self) -> None:
        self.assertIn("APEX_DOCTOR_VERIFIED:&&expected_user", (ROOT / "scripts/doctor.sql").read_text(encoding="utf-8"))
        self.assertIn("APEX_DRIFT_QUERY_VERIFIED", (ROOT / "scripts/check_builder_drift.sql").read_text(encoding="utf-8"))

    def test_publish_driver_imports_an_explicit_source_and_descriptor(self) -> None:
        contents = (ROOT / "scripts/publish_app.sql").read_text(encoding="utf-8")
        self.assertIn('apex import -input "&&application_source" -deployment "&&deployment_file"', contents)
        self.assertNotIn("apex import -input .", contents)

    def test_publish_driver_rechecks_the_approved_live_state_before_import(self) -> None:
        contents = (ROOT / "scripts/publish_app.sql").read_text(encoding="utf-8")
        self.assertIn("DEFINE expected_live_state = '&7'", contents)
        recheck = contents.index("-20016")
        self.assertLess(recheck, contents.index("apex import -input"))
        self.assertIn("REGEXP_REPLACE(v_version, '[' || c_python_whitespace || ']+$')", contents)
        # Built in PL/SQL (32767-byte strings), not in SQL, so a long
        # multibyte version still fits once hex-encoded.
        block = contents[contents.index("c_python_whitespace CONSTANT") - 400 : recheck]
        self.assertIn("v_observed VARCHAR2(32767);", block)
        self.assertNotIn("RAWTOHEX(UTL_I18N.STRING_TO_RAW(REGEXP_REPLACE(MAX(", contents)
        # The SQL trims exactly the characters Python's str.rstrip() trims.
        listed = set(re.findall(r"\\([0-9A-F]{4})", contents[contents.index("c_python_whitespace CONSTANT"):contents.index("BEGIN", contents.index("c_python_whitespace CONSTANT"))]))
        python = {f"{code:04X}" for code in range(sys.maxunicode + 1) if chr(code).isspace()}
        self.assertEqual(listed, python)

    def test_revision_queries_distinguish_imported_app_from_absent_app(self) -> None:
        # APEX leaves last_updated_on NULL on import, so NVL(MAX(...)) alone
        # would report an installed app as NOT_FOUND.
        for name, queries in (("check_builder_drift.sql", 1), ("export_apps.sql", 2)):
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertEqual(contents.count("WHEN COUNT(*) = 0 THEN 'NOT_FOUND'"), queries)
                self.assertEqual(contents.count("WHEN MAX(last_updated_on) IS NULL THEN 'NO_TIMESTAMP'"), queries)
                self.assertEqual(contents.count("|| '|' || MAX(version)"), queries)
                self.assertIn("SET LINESIZE 32767", contents)


if __name__ == "__main__":
    unittest.main()
