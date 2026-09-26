import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_conflicts.py"


class MigrationConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.migrations = Path(self.temporary.name) / "migrations"
        self.migrations.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_migration(self, developer: str, name: str, source: str) -> Path:
        directory = self.migrations / developer
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text(source, encoding="utf-8")
        return path

    def run_checker(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(CHECKER), "--migrations-dir", str(self.migrations)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_distinct_objects_from_different_developers_are_clean(self) -> None:
        self.add_migration("alice", "one.sql", "CREATE TABLE ORDERS (ID NUMBER);\n")
        self.add_migration("bob", "two.sql", "CREATE SEQUENCE ORDER_SEQ;\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No cross-developer conflicts", result.stdout)

    def test_same_table_name_conflicts_across_developers_and_schemas(self) -> None:
        first = self.add_migration("alice", "one.sql", "CREATE TABLE app.ORDERS (ID NUMBER);\n")
        second = self.add_migration("bob", "two.sql", "create table ORDERS (ID NUMBER);\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertIn("TABLE ORDERS", result.stdout)
        self.assertIn(str(first.relative_to(self.migrations)), result.stdout)
        self.assertIn(str(second.relative_to(self.migrations)), result.stdout)

    def test_duplicate_view_and_sequence_declarations_conflict(self) -> None:
        self.add_migration("alice", "one.sql", "CREATE OR REPLACE VIEW ACTIVE_ORDERS AS SELECT 1 X FROM DUAL;\nCREATE SEQUENCE ORDER_SEQ;\n")
        self.add_migration("bob", "two.sql", "CREATE OR REPLACE VIEW ACTIVE_ORDERS AS SELECT 2 X FROM DUAL;\nCREATE SEQUENCE ORDER_SEQ;\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertIn("VIEW ACTIVE_ORDERS", result.stdout)
        self.assertIn("SEQUENCE ORDER_SEQ", result.stdout)

    def test_duplicate_added_columns_conflict(self) -> None:
        self.add_migration("alice", "one.sql", "ALTER TABLE ORDERS ADD STATUS VARCHAR2(30);\n")
        self.add_migration("bob", "two.sql", "ALTER TABLE orders ADD (status VARCHAR2(50));\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertIn("COLUMN ORDERS.STATUS", result.stdout)

    def test_comments_and_string_literals_do_not_create_false_declarations(self) -> None:
        self.add_migration(
            "alice",
            "one.sql",
            "-- CREATE TABLE HIDDEN_TABLE (ID NUMBER);\n"
            "BEGIN\n  v_text := 'CREATE SEQUENCE HIDDEN_SEQ';\nEND;\n/\n",
        )
        self.add_migration("bob", "two.sql", "CREATE TABLE REAL_TABLE (ID NUMBER);\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No cross-developer conflicts", result.stdout)


if __name__ == "__main__":
    unittest.main()
