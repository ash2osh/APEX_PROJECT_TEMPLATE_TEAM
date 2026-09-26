import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_DRIVERS = (
    "backup_db.sql",
    "check_builder_drift.sql",
    "doctor.sql",
    "export_apps.sql",
    "migrate.sql",
    "publish_app.sql",
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
        for name in ("check_builder_drift.sql", "doctor.sql", "export_apps.sql", "backup_db.sql"):
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertRegex(contents, r"(?im)^EXIT SUCCESS ROLLBACK\s*$")

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
