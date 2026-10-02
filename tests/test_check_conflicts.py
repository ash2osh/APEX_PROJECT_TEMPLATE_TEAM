import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_conflicts.sh"


class MigrationPreflightCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def add_folder(self, name, sql):
        folder = self.root / "migrations" / name
        folder.mkdir()
        (folder / "001-change.sql").write_text(sql, encoding="utf-8", newline="\n")
        (folder / "checks.json").write_text(json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}) + "\n", encoding="utf-8")
        return folder

    def run_checker(self, *arguments):
        return subprocess.run(
            [BASH, str(CHECKER), "--repo-root", str(self.root), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={"PATH": __import__("os").environ["PATH"], "HOME": __import__("os").environ.get("HOME", "")},
        )

    def test_local_mode_explicitly_reports_only_selected_scope_and_needs_no_env(self):
        self.add_folder("2026-09-27_create-orders-r001", "CREATE TABLE ORDERS (ID NUMBER);\n")

        result = self.run_checker("migrations/2026-09-27_create-orders-r001", "--local")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local selected-batch analysis only", result.stdout)
        self.assertIn("Other repositories' pending migrations are not visible", result.stdout)

    def test_no_folder_or_missing_mode_is_usage_error_and_never_claims_cross_developer_scan(self):
        for arguments in ((), ("--local",)):
            with self.subTest(arguments=arguments):
                result = self.run_checker(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)
                self.assertNotIn("No cross-developer conflicts", result.stdout)

    def test_shared_oracle_namespace_conflict_is_found_within_selected_batch(self):
        self.add_folder("2026-09-27_create-orders-r001", "CREATE TABLE ORDERS (ID NUMBER);\n")
        self.add_folder("2026-09-28_create-sequence-r001", "CREATE SEQUENCE ORDERS;\n")

        result = self.run_checker("migrations/2026-09-27_create-orders-r001", "migrations/2026-09-28_create-sequence-r001", "--local")

        self.assertEqual(result.returncode, 1)
        self.assertIn("BATCH_NAMESPACE_COLLISION", result.stdout)

    def test_historical_unselected_folders_are_not_treated_as_object_reservations(self):
        self.add_folder("2026-09-27_old-create-r001", "CREATE TABLE CUSTOMERS (ID NUMBER);\n")
        self.add_folder("2026-09-28_selected-create-r001", "CREATE TABLE CUSTOMERS (ID NUMBER, NAME VARCHAR2(20));\n")

        result = self.run_checker("migrations/2026-09-28_selected-create-r001", "--local")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_file_argument_is_rejected_with_conversion_guidance(self):
        result = self.run_checker("migrations/2026-09-26_create-orders-r001/001-create-orders.sql", "--local")
        self.assertEqual(result.returncode, 2)
        self.assertIn("legacy file paths are not supported", result.stderr)

    def test_a_folder_argument_may_end_with_the_slash_that_shell_completion_adds(self):
        self.add_folder("2026-09-27_create-orders-r001", "CREATE TABLE ORDERS (ID NUMBER);\n")

        result = self.run_checker("migrations/2026-09-27_create-orders-r001/", "--local")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Local selected-batch analysis only", result.stdout)

    def test_the_same_folder_with_and_without_the_slash_is_still_selected_once(self):
        self.add_folder("2026-09-27_create-orders-r001", "CREATE TABLE ORDERS (ID NUMBER);\n")

        result = self.run_checker("migrations/2026-09-27_create-orders-r001", "migrations/2026-09-27_create-orders-r001/", "--local")

        self.assertEqual(result.returncode, 2)
        self.assertIn("may be selected only once", result.stderr)

    def test_json_mode_reports_scope_and_coverage(self):
        self.add_folder("2026-09-27_create-orders-r001", "CREATE TABLE ORDERS (ID NUMBER);\n")
        result = self.run_checker("migrations/2026-09-27_create-orders-r001", "--local", "--format", "json")
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["coverage"]["mode"], "local-only")
        self.assertFalse(report["coverage"]["live_state_checked"])


if __name__ == "__main__":
    unittest.main()
