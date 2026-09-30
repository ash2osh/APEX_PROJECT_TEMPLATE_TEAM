import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts.db_targets import Target
from scripts.migration_manifest import QueryCheck, load_batch
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaSnapshot
from scripts.migration_checks import CheckReport, analyze_batch, compiled_units, preflight, run_checks


class MigrationChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def add_folder(self, folder_name, sql, *, preconditions=(), postconditions=None):
        folder = self.root / "migrations" / folder_name
        folder.mkdir()
        if isinstance(sql, str):
            sql = {"001-change.sql": sql}
        for filename, content in sql.items():
            (folder / filename).write_text(content, encoding="utf-8", newline="\n")
        checks = {
            "schemaVersion": 1,
            "preconditions": list(preconditions),
            "postconditions": list(postconditions or [{"id": "verified", "sql": "SELECT 1 FROM dual", "expected": 1}]),
        }
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8")
        return folder

    def test_compiled_units_names_only_what_the_migration_compiles(self):
        self.add_folder("2026-09-30_units-r001", {
            "001-units.sql": (
                "-- CREATE PACKAGE in_a_comment AS\n"
                "CREATE OR REPLACE EDITIONABLE PACKAGE other.zz_pkg AS\n  PROCEDURE p;\nEND;\n/\n"
                "create or replace package body \"Zz_Pkg\" as procedure p is begin null; end; end;\n/\n"
                "CREATE OR REPLACE TRIGGER zz_trg BEFORE INSERT ON t FOR EACH ROW BEGIN NULL; END;\n/\n"
                "CREATE OR REPLACE FORCE VIEW zz_v AS SELECT 1 x FROM dual;\n"
                "ALTER PACKAGE zz_old COMPILE;\n"
                "ALTER TYPE zz_t COMPILE BODY;\n"
                "ALTER PACKAGE zz_spec COMPILE DEBUG SPECIFICATION;\n"
                "CREATE OR REPLACE AND COMPILE JAVA SOURCE NAMED \"ZzJava\" AS\npublic class ZzJava {}\n/\n"
                "CREATE OR REPLACE MLE MODULE zz_mle LANGUAGE JAVASCRIPT AS\nexport function f() { return 1; }\n/\n"
                "ALTER TRIGGER zz_trg ENABLE;\n"
                "ALTER TABLE t ADD (c NUMBER);\n"
                "CREATE TABLE zz_x (id NUMBER);\n"
            ),
        })
        migration = self.migration_batch("2026-09-30_units-r001")[0]
        self.assertEqual(
            compiled_units(migration, "APP"),
            (
                ("OTHER", "PACKAGE", "ZZ_PKG"),
                ("APP", "PACKAGE BODY", "Zz_Pkg"),
                ("APP", "TRIGGER", "ZZ_TRG"),
                ("APP", "VIEW", "ZZ_V"),
                ("APP", "PACKAGE", "ZZ_OLD"),
                ("APP", "PACKAGE BODY", "ZZ_OLD"),
                ("APP", "TYPE BODY", "ZZ_T"),
                ("APP", "PACKAGE", "ZZ_SPEC"),
                ("APP", "JAVA SOURCE", "ZzJava"),
                ("APP", "MLE MODULE", "ZZ_MLE"),
            ),
        )

    def test_table_only_migration_compiles_no_units(self):
        self.add_folder("2026-09-30_table-r001", "CREATE TABLE zz_x (id NUMBER);\n")
        self.assertEqual(compiled_units(self.migration_batch("2026-09-30_table-r001")[0], "APP"), ())

    def migration_batch(self, *folders):
        return load_batch(self.root, [f"migrations/{folder}" for folder in folders])

    def snapshot(self, objects=(), definitions=()):
        owner = "APP"
        inventory = {}
        for name, object_type, extra in objects:
            row = {"owner": owner, "name": name, "type": object_type, "status": "VALID"}
            row.update(extra or {})
            inventory[ObjectKey(owner, name, object_type)] = row
        return SchemaSnapshot(
            {"session_user": "MIGRATOR", "current_schema": owner, "db_unique_name": "DEVDB", "container_id": "1", "container_name": "APP_PDB", "edition": "ORA$BASE", "service_name": "dev"},
            inventory,
            {definition.key: definition for definition in definitions},
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS", "ALL_TABLES", "ALL_TAB_COLUMNS", "ALL_VIEWS", "ALL_SEQUENCES"]},
            "2026-09-28T10:00:00Z",
            "2026-09-28T10:00:02Z",
        )

    @staticmethod
    def passing_checks():
        return CheckReport(passed=True, complete=True, results=(), errors=(), coverage={"complete": True})

    def test_table_and_sequence_share_namespace_even_in_one_selected_batch(self):
        self.add_folder("2026-09-27_create-table-r001", "CREATE TABLE ORDERS (ID NUMBER);\n")
        self.add_folder("2026-09-28_create-sequence-r001", "CREATE SEQUENCE ORDERS;\n")

        report = preflight(self.migration_batch("2026-09-27_create-table-r001", "2026-09-28_create-sequence-r001"), self.snapshot(), self.passing_checks())

        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(item["code"] == "BATCH_NAMESPACE_COLLISION" for item in report.conflicts))

    def test_quoted_identifiers_preserve_case_and_owner_is_resolved_from_target(self):
        folder = self.add_folder("2026-09-27_create-quoted-r001", 'CREATE TABLE "lowerName" (ID NUMBER);\n')
        migration = load_batch(self.root, [f"migrations/{folder.name}"])[0]

        operations = analyze_batch((migration,), "APP")

        self.assertEqual(operations[0]["name"], "lowerName")
        self.assertEqual(operations[0]["owner"], "APP")
        self.assertEqual(operations[0]["key"], ("COMMON", "lowerName"))

    def test_qualified_target_owner_is_canonical_and_other_schema_is_incomplete(self):
        self.add_folder("2026-09-27_create-qualified-r001", "CREATE TABLE APP.CUSTOMERS (ID NUMBER);\n")
        migration = self.migration_batch("2026-09-27_create-qualified-r001")
        self.assertEqual(analyze_batch(migration, "APP")[0]["owner"], "APP")

        self.add_folder("2026-09-28_create-external-r001", "CREATE TABLE OTHER_SCHEMA.CUSTOMERS (ID NUMBER);\n")
        external = self.migration_batch("2026-09-28_create-external-r001")
        report = preflight(external, self.snapshot(), self.passing_checks())
        self.assertEqual(report.exit_code, 2)
        self.assertTrue(any(item["code"] == "UNSUPPORTED_OPERATION" for item in report.errors))

    def test_ordered_create_table_then_index_view_and_add_column_is_supported(self):
        self.add_folder("2026-09-27_create-orders-r001", {
            "001-create-table.sql": "CREATE TABLE ORDERS (ID NUMBER);\n",
            "002-create-index.sql": "CREATE INDEX ORDERS_I ON ORDERS(ID);\n",
            "003-create-view.sql": "CREATE VIEW ORDERS_V AS SELECT ID FROM ORDERS;\n",
            "004-add-status.sql": "ALTER TABLE ORDERS ADD STATUS VARCHAR2(20);\n",
        })
        migrations = self.migration_batch("2026-09-27_create-orders-r001")

        operations = analyze_batch(migrations, "APP")
        report = preflight(migrations, self.snapshot(), self.passing_checks())

        self.assertEqual([item["kind"] for item in operations], ["CREATE_TABLE", "CREATE_INDEX", "CREATE_VIEW", "ALTER_ADD_COLUMN"])
        self.assertEqual(report.exit_code, 0, report.to_dict())

    def test_existing_live_namespace_object_blocks_create_and_if_not_exists_does_not_pass(self):
        self.add_folder("2026-09-27_create-customers-r001", "CREATE TABLE IF NOT EXISTS CUSTOMERS (ID NUMBER);\n")
        migrations = self.migration_batch("2026-09-27_create-customers-r001")
        existing = self.snapshot((("CUSTOMERS", "SEQUENCE", {}),))

        report = preflight(migrations, existing, self.passing_checks())

        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(item["code"] == "LIVE_NAMESPACE_OCCUPIED" for item in report.conflicts))

    def test_duplicate_add_column_is_rejected_but_staged_add_is_not_checked_against_initial_state(self):
        self.add_folder("2026-09-27_create-orders-r001", {
            "001-create-table.sql": "CREATE TABLE ORDERS (ID NUMBER);\n",
            "002-add-status.sql": "ALTER TABLE ORDERS ADD STATUS VARCHAR2(20);\n",
            "003-add-status-again.sql": "ALTER TABLE ORDERS ADD STATUS VARCHAR2(30);\n",
        })
        migration = self.migration_batch("2026-09-27_create-orders-r001")

        report = preflight(migration, self.snapshot(), self.passing_checks())

        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(item["code"] == "COLUMN_ALREADY_EXISTS" for item in report.conflicts))

    def test_alter_add_column_detects_existing_column_from_live_table_definition(self):
        self.add_folder("2026-09-27_add-status-r001", "ALTER TABLE ORDERS ADD STATUS VARCHAR2(20);\n")
        migration = self.migration_batch("2026-09-27_add-status-r001")
        orders = ObjectDefinition(ObjectKey("APP", "ORDERS", "TABLE"), {"columns": [{"name": "ID", "column_id": 1}, {"name": "STATUS", "column_id": 2}]}, "CREATE TABLE APP.ORDERS (ID NUMBER, STATUS VARCHAR2(20))", (), True)

        report = preflight(migration, self.snapshot((("ORDERS", "TABLE", {}),), (orders,)), self.passing_checks())

        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any(item["code"] == "COLUMN_ALREADY_EXISTS" for item in report.conflicts))

    def test_unsupported_operations_require_explicit_precondition_and_coverage_is_reported(self):
        self.add_folder("2026-09-27_data-change-r001", "UPDATE CUSTOMERS SET ACTIVE = 'Y';\n")
        migration = self.migration_batch("2026-09-27_data-change-r001")

        report = preflight(migration, self.snapshot(), self.passing_checks())

        self.assertEqual(report.exit_code, 2)
        self.assertTrue(any(item["code"] == "UNSUPPORTED_OPERATION" for item in report.errors))

    def test_opaque_operation_with_reviewed_checks_is_disclosed_as_manual_coverage(self):
        precondition = {"id": "row-state", "sql": "SELECT 1 FROM dual", "expected": 1}
        self.add_folder("2026-09-27_data-change-r001", "UPDATE CUSTOMERS SET ACTIVE = 'Y';\n", preconditions=(precondition,))
        migration = self.migration_batch("2026-09-27_data-change-r001")

        report = preflight(migration, self.snapshot(), self.passing_checks())

        self.assertEqual(report.exit_code, 0, report.to_dict())
        self.assertEqual(report.coverage["opaque_operations"], 1)
        self.assertTrue(report.coverage["manual_review_required"])

    def test_independent_pending_creates_can_both_pass_empty_live_catalog(self):
        self.add_folder("2026-09-27_create-customers-r001", "CREATE TABLE CUSTOMERS (ID NUMBER);\n")
        migration = self.migration_batch("2026-09-27_create-customers-r001")

        first = preflight(migration, self.snapshot(), self.passing_checks())
        second = preflight(migration, self.snapshot(), self.passing_checks())

        self.assertEqual((first.exit_code, second.exit_code), (0, 0))
        self.assertIn("Other repositories' pending migrations are not visible", first.limitation)

    def test_check_results_require_exactly_one_numeric_one(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        outcomes = (
            {"id": "one", "row_count": 1, "column_count": 1, "value": 0, "numeric": True},
            {"id": "one", "row_count": 0, "column_count": 1, "value": None, "numeric": True},
            {"id": "one", "row_count": 2, "column_count": 1, "value": 1, "numeric": True},
            {"id": "one", "row_count": 1, "column_count": 2, "value": 1, "numeric": True},
            {"id": "one", "row_count": 1, "column_count": 1, "value": None, "numeric": True},
            {"id": "one", "row_count": 1, "column_count": 1, "value": "1", "numeric": False},
            {"id": "one", "row_count": 1, "column_count": 1, "error": "ORA-20000", "numeric": False},
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                report = run_checks(target, (check,), Path(self.temporary.name), phase="preconditions", _runner=self.fake_runner(outcome))
                self.assertFalse(report.passed)

    def fake_runner(self, outcome):
        def runner(_target, driver, run_dir, **_kwargs):
            driver_source = driver.read_text(encoding="utf-8")
            phase = re.search(r"l_payload.put\('phase', '([^']+)'\)", driver_source).group(1)
            payload = {"schemaVersion": 1, "phase": phase, "complete": True, "results": [outcome]}
            output = f"CHECK_PAYLOAD_BEGIN:{phase}\n" + json.dumps(payload) + f"\nCHECK_PAYLOAD_END:{phase}\nCHECK_VERIFIED:{phase}\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()
        return runner

    def test_one_exact_numeric_one_result_passes(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        report = run_checks(target, (check,), Path(self.temporary.name), phase="postconditions", _runner=self.fake_runner({"id": "one", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}))
        self.assertTrue(report.passed)

    def test_check_driver_uses_read_only_transaction_and_hex_encodes_query_text(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("table-exists", "SELECT COUNT(*) FROM all_tables WHERE owner = :target_schema", 1)
        observed = {}

        def runner(_target, driver, run_dir):
            observed["driver"] = driver.read_text(encoding="utf-8")
            payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [{"id": "table-exists", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        report = run_checks(target, (check,), Path(self.temporary.name), _runner=runner)

        self.assertTrue(report.passed)
        self.assertIn("SET TRANSACTION READ ONLY;", observed["driver"])
        self.assertIn("EXIT SUCCESS ROLLBACK", observed["driver"])
        self.assertNotIn(check.sql, observed["driver"])
        self.assertIn("DBMS_SQL.BIND_VARIABLE(l_cursor, ':target_schema', 'APP')", observed["driver"])

    def test_check_session_reports_its_database_identity(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        identity = {"session_user": "MIGRATOR", "current_schema": "APP", "db_name": "DEVDB"}
        observed = {}

        def runner(_target, driver, run_dir):
            observed["driver"] = driver.read_text(encoding="utf-8")
            payload = {"schemaVersion": 1, "phase": "postconditions", "complete": True, "identity": identity,
                       "results": [{"id": "one", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:postconditions\n" + json.dumps(payload) + "\nCHECK_PAYLOAD_END:postconditions\nCHECK_VERIFIED:postconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        report = run_checks(target, (check,), Path(self.temporary.name), phase="postconditions", _runner=runner)

        self.assertTrue(report.passed)
        self.assertEqual(report.coverage["identity"], identity)
        for field in ("db_unique_name", "service_name", "container_name", "edition", "database_version"):
            self.assertIn(f"l_result.put('{field}'", observed["driver"])
        self.assertIn("l_payload.put('identity', l_result);", observed["driver"])


if __name__ == "__main__":
    unittest.main()
