import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target
from scripts.migration_manifest import QueryCheck, load_batch
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaSnapshot
from scripts.migration_checks import CheckReport, analyze_batch, compiled_units, preflight, run_checks
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


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
                ("OTHER", "PACKAGE", "ZZ_PKG", True),
                ("APP", "PACKAGE BODY", "Zz_Pkg", True),
                ("APP", "TRIGGER", "ZZ_TRG", True),
                ("APP", "VIEW", "ZZ_V", True),
                ("APP", "PACKAGE", "ZZ_OLD", True),
                # A plain ALTER PACKAGE ... COMPILE checks a body only if one exists.
                ("APP", "PACKAGE BODY", "ZZ_OLD", False),
                ("APP", "TYPE BODY", "ZZ_T", True),
                ("APP", "PACKAGE", "ZZ_SPEC", True),
                ("APP", "JAVA SOURCE", "ZzJava", True),
                ("APP", "MLE MODULE", "ZZ_MLE", True),
            ),
        )

    def test_compiled_units_follow_the_session_schema_across_files(self):
        self.add_folder("2026-09-30_schema-r001", {
            "001-switch.sql": "ALTER SESSION SET CURRENT_SCHEMA = other;\nCREATE OR REPLACE PROCEDURE zz_p IS BEGIN NULL; END;\n/\n",
            "002-later.sql": "CREATE OR REPLACE FUNCTION zz_f RETURN NUMBER IS BEGIN RETURN 1; END;\n/\n",
            "003-back.sql": "ALTER SESSION SET CURRENT_SCHEMA = \"APP\";\nCREATE OR REPLACE VIEW zz_v AS SELECT 1 x FROM dual;\n",
        })
        self.assertEqual(
            compiled_units(self.migration_batch("2026-09-30_schema-r001")[0], "APP"),
            (
                ("OTHER", "PROCEDURE", "ZZ_P", True),
                ("OTHER", "FUNCTION", "ZZ_F", True),
                ("APP", "VIEW", "ZZ_V", True),
            ),
        )

    def test_compiled_units_skip_what_the_migration_drops_again(self):
        self.add_folder("2026-09-30_drop-r001", (
            "CREATE OR REPLACE PROCEDURE zz_tmp IS BEGIN NULL; END;\n/\n"
            "CREATE OR REPLACE PACKAGE zz_pkg AS PROCEDURE p; END;\n/\n"
            "CREATE OR REPLACE PACKAGE BODY zz_pkg AS PROCEDURE p IS BEGIN NULL; END; END;\n/\n"
            "BEGIN zz_tmp; END;\n/\n"
            "DROP PROCEDURE zz_tmp;\n"
            "DROP PACKAGE BODY zz_pkg;\n"
            "DROP VIEW IF EXISTS zz_v;\n"
        ))
        self.assertEqual(
            compiled_units(self.migration_batch("2026-09-30_drop-r001")[0], "APP"),
            (("APP", "PACKAGE", "ZZ_PKG", True),),
        )

    def test_compiled_units_read_a_file_the_sql_tokenizer_cannot(self):
        # The apostrophe in the Java comment is not SQL; the SQL tokenizer
        # alone would give up on the whole file and name no units.
        self.add_folder("2026-09-30_java-r001", (
            "CREATE OR REPLACE AND COMPILE JAVA SOURCE NAMED \"ZzJava\" AS\n"
            "public class ZzJava {\n  // it's a comment\n  static int f() { return 1; }\n}\n/\n"
            "CREATE OR REPLACE PROCEDURE zz_p IS BEGIN NULL; END;\n/\n"
        ))
        self.assertEqual(
            compiled_units(self.migration_batch("2026-09-30_java-r001")[0], "APP"),
            (("APP", "JAVA SOURCE", "ZzJava", True), ("APP", "PROCEDURE", "ZZ_P", True)),
        )

    def test_batch_checks_only_the_first_folder_preconditions_up_front(self):
        from scripts.migration_checks import batch_preconditions, deferred_precondition_folders, _render_preflight
        pre = [{"id": "ready", "sql": "SELECT 1 FROM dual", "expected": 1}]
        self.add_folder("2026-09-30_dep-r001", "CREATE TABLE zz_dep (id NUMBER);\n", preconditions=pre)
        self.add_folder("2026-09-30_dep-r002", "ALTER TABLE zz_dep ADD (c NUMBER);\n", preconditions=[{"id": "dep-ready", "sql": "SELECT 1 FROM dual", "expected": 1}])
        batch = self.migration_batch("2026-09-30_dep-r001", "2026-09-30_dep-r002")
        self.assertEqual([check.id for check in batch_preconditions(batch)], ["ready"])
        self.assertEqual(deferred_precondition_folders(batch), ["2026-09-30_dep-r002"])
        report = preflight(batch, self.snapshot(), CheckReport(True, True, (), (), {"complete": True}))
        self.assertEqual(report.coverage["deferred_preconditions"]["folders"], ["2026-09-30_dep-r002"])
        self.assertIn("preconditions of 2026-09-30_dep-r002 are not checked here", _render_preflight(report, False))

    def test_text_report_names_the_sql_that_needs_manual_review(self):
        # Opaque SQL with declared checks passes, but only after a person reviewed those
        # checks; the JSON report said so and the text report used to say nothing.
        from scripts.migration_checks import _render_preflight
        reviewed = [{"id": "reviewed", "sql": "SELECT 1 FROM dual", "expected": 1}]
        self.add_folder("2026-09-30_opaque-r001", "ALTER SYSTEM SET zz_param = 1 SCOPE=MEMORY;\n", preconditions=reviewed)
        report = preflight(self.migration_batch("2026-09-30_opaque-r001"), self.snapshot(), CheckReport(True, True, (), (), {"complete": True}))
        self.assertEqual(report.exit_code, 0)
        self.assertTrue(report.coverage.get("manual_review_required"))
        rendered = _render_preflight(report, False)
        self.assertIn("REVIEW: 2026-09-30_opaque-r001/001-change.sql", rendered)
        self.assertIn("1 manual review item(s)", rendered)

    def test_view_may_use_a_synonym_created_earlier_in_the_batch(self):
        reviewed = [{"id": "synonym-absent", "sql": "SELECT 1 FROM dual", "expected": 1}]
        self.add_folder("2026-09-30_link-r001", {
            "001-create-synonym.sql": "CREATE SYNONYM zz_link_s FOR other.zz_link_t;\n",
            "002-create-view.sql": "CREATE VIEW zz_link_v AS SELECT id FROM zz_link_s;\n",
        }, preconditions=reviewed)
        report = preflight(self.migration_batch("2026-09-30_link-r001"), self.snapshot(), CheckReport(True, True, (), (), {"complete": True}))
        self.assertNotIn("MISSING_PREREQUISITE", [conflict.get("code") for conflict in report.conflicts], report.conflicts)

    def test_view_on_a_name_nothing_creates_is_still_a_missing_prerequisite(self):
        reviewed = [{"id": "synonym-absent", "sql": "SELECT 1 FROM dual", "expected": 1}]
        self.add_folder("2026-09-30_nolink-r001", {
            "001-create-synonym.sql": "CREATE SYNONYM zz_other_s FOR other.zz_link_t;\n",
            "002-create-view.sql": "CREATE VIEW zz_link_v AS SELECT id FROM zz_link_s;\n",
        }, preconditions=reviewed)
        report = preflight(self.migration_batch("2026-09-30_nolink-r001"), self.snapshot(), CheckReport(True, True, (), (), {"complete": True}))
        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_LINK_S"),
            [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
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

    def test_create_or_replace_view_may_replace_a_view_an_earlier_selected_folder_creates(self):
        # The folders run in the order given, so r002 replaces the view r001 made.
        # It needs the same reviewed precondition a replace of a live view needs.
        precondition = {"id": "view-is-r001", "sql": "SELECT 1 FROM dual", "expected": 1}
        self.add_folder("2026-10-01_replace-view-r001", "CREATE VIEW ZZ_V AS SELECT 1 AS A FROM dual;\n")
        self.add_folder("2026-10-01_replace-view-r002", "CREATE OR REPLACE VIEW ZZ_V AS SELECT 2 AS A FROM dual;\n", preconditions=(precondition,))

        report = preflight(self.migration_batch("2026-10-01_replace-view-r001", "2026-10-01_replace-view-r002"), self.snapshot(), self.passing_checks())

        self.assertEqual(report.exit_code, 0, report.to_dict())
        reviewed = report.coverage["manual_review_required"]
        self.assertTrue(any(item["migration"] == "2026-10-01_replace-view-r002" for item in reviewed), reviewed)

    def test_a_second_create_of_a_staged_name_is_still_a_collision_unless_it_replaces_a_staged_view(self):
        precondition = {"id": "view-is-r001", "sql": "SELECT 1 FROM dual", "expected": 1}
        for label, first, second, reviewed in (
            ("plain CREATE VIEW twice", "CREATE VIEW ZZ_V AS SELECT 1 AS A FROM dual;\n", "CREATE VIEW ZZ_V AS SELECT 2 AS A FROM dual;\n", True),
            ("replace without a reviewed precondition", "CREATE VIEW ZZ_V AS SELECT 1 AS A FROM dual;\n", "CREATE OR REPLACE VIEW ZZ_V AS SELECT 2 AS A FROM dual;\n", False),
            ("replace a staged table with a view", "CREATE TABLE ZZ_V (A NUMBER);\n", "CREATE OR REPLACE VIEW ZZ_V AS SELECT 2 AS A FROM dual;\n", True),
            ("replace a staged sequence with a view", "CREATE SEQUENCE ZZ_V;\n", "CREATE OR REPLACE VIEW ZZ_V AS SELECT 2 AS A FROM dual;\n", True),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary)
                (self.root / "migrations").mkdir()
                self.add_folder("2026-10-01_replace-view-r001", first)
                self.add_folder("2026-10-01_replace-view-r002", second, preconditions=(precondition,) if reviewed else ())

                report = preflight(self.migration_batch("2026-10-01_replace-view-r001", "2026-10-01_replace-view-r002"), self.snapshot(), self.passing_checks())

                self.assertEqual(report.exit_code, 1, report.to_dict())
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

    def test_failed_check_report_names_expected_value_observed_value_and_failed_id(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("row-count", "SELECT 1 FROM dual", 1)

        report = run_checks(
            target, (check,), Path(self.temporary.name),
            _runner=self.fake_runner({"id": "row-count", "row_count": 1, "column_count": 1, "value": 0, "numeric": True}),
        )

        self.assertEqual(report.coverage["failedCheckIds"], ["row-count"])
        self.assertEqual(report.errors[0]["check"], "row-count")
        self.assertEqual(report.errors[0]["expected"], 1)
        self.assertEqual(report.errors[0]["observedValue"], 0)

    def test_read_only_check_sessions_are_batched_under_the_configured_driver_budget(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        prefix = "SELECT CASE WHEN LENGTH('"
        suffix = "') > 0 THEN 1 ELSE 0 END FROM dual"
        query = prefix + ("x" * (24000 - len(prefix) - len(suffix))) + suffix
        checks = tuple(
            QueryCheck(f"check-{index:03}", query, 1)
            for index in range(200)
        )
        identity = {
            "session_user": "MIGRATOR", "current_schema": "APP", "db_name": "DEVDB",
            "db_unique_name": "DEVDB_UNIQUE", "service_name": "dev.service", "container_id": "3",
            "container_name": "APP_PDB", "edition": "ORA$BASE", "database_version": "19.0",
        }
        observed = []
        failed = {"check-007", "check-127"}

        def runner(_target, driver, run_dir, **kwargs):
            source = driver.read_text(encoding="utf-8")
            phase = kwargs.get("phase", "preconditions")
            ids = re.findall(r"l_result\.put\('id', '([^']+)'\)", source)
            observed.append((driver.stat().st_size, "SET TRANSACTION READ ONLY;" in source, ids))
            results = [
                {"id": check_id, "row_count": 1, "column_count": 1,
                 "numeric": True, "value": 0 if check_id in failed else 1}
                for check_id in ids
            ]
            payload = {"schemaVersion": 1, "phase": phase, "complete": True,
                       "identity": identity, "results": results}
            output = f"CHECK_PAYLOAD_BEGIN:{phase}\n" + json.dumps(payload) + f"\nCHECK_PAYLOAD_END:{phase}\nCHECK_VERIFIED:{phase}\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        budget = 2 * 1024 * 1024
        with patch.dict("os.environ", {"MIGRATION_CHECK_BATCH_BYTES": str(budget)}):
            report = run_checks(target, checks, Path(self.temporary.name), phase="postconditions", _runner=runner)

        self.assertGreater(len(observed), 1)
        self.assertTrue(all(size <= budget for size, _read_only, _ids in observed))
        self.assertTrue(all(read_only for _size, read_only, _ids in observed))
        self.assertEqual([result["id"] for result in report.results], [check.id for check in checks])
        self.assertEqual([error["check"] for error in report.errors], ["check-007", "check-127"])
        self.assertEqual(len(report.coverage["sessionIdentities"]), len(observed))
        self.assertEqual([check_id for _size, _read_only, ids in observed for check_id in ids], [check.id for check in checks])

    def test_check_driver_preserves_whitespace_hyphen_and_quote_inside_expected_text(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        expected_text = "first line\n   \ntrailing-\n'quoted text'"
        literal = expected_text.replace("'", "''")
        sql = f"SELECT CASE WHEN '{literal}' = '{literal}' THEN 1 ELSE 0 END FROM dual"
        check = QueryCheck("source-text", sql, 1)
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
            observed["driver"] = driver.read_text(encoding="utf-8")
            payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [
                {"id": "source-text", "row_count": 1, "column_count": 1, "value": 1, "numeric": True},
            ]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        report = run_checks(target, (check,), Path(self.temporary.name), _runner=runner)

        self.assertTrue(report.passed)
        chunks = re.findall(r"HEXTORAW\('([0-9A-F]+)'\)", observed["driver"])
        self.assertEqual(chunks[::2], chunks[1::2])
        reconstructed = b"".join(bytes.fromhex(chunk) for chunk in chunks[::2]).decode("utf-8")
        self.assertEqual(reconstructed, sql)

    def test_jobs_runs_independent_check_sessions_in_parallel(self):
        from threading import Barrier, Lock

        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        checks = tuple(QueryCheck(f"check-{index:03}", "SELECT 1 FROM dual", 1) for index in range(4))
        identity = {
            "session_user": "MIGRATOR", "current_schema": "APP", "db_name": "DEVDB",
            "db_unique_name": "DEVDB_UNIQUE", "service_name": "dev.service", "container_id": "3",
            "container_name": "APP_PDB", "edition": "ORA$BASE", "database_version": "19.0",
        }
        barrier = Barrier(2)
        lock = Lock()
        active = 0
        peak = 0

        def runner(_target, driver, run_dir, **kwargs):
            nonlocal active, peak
            ids = re.findall(r"l_result\.put\('id', '([^']+)'\)", driver.read_text(encoding="utf-8"))
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            phase = kwargs["phase"]
            payload = {"schemaVersion": 1, "phase": phase, "complete": True, "identity": identity,
                       "results": [{"id": check_id, "row_count": 1, "column_count": 1,
                                    "numeric": True, "value": 1} for check_id in ids]}
            output = f"CHECK_PAYLOAD_BEGIN:{phase}\n" + json.dumps(payload) + f"\nCHECK_PAYLOAD_END:{phase}\nCHECK_VERIFIED:{phase}\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        with patch.dict("os.environ", {"MIGRATION_CHECK_BATCH_BYTES": "5000"}):
            report = run_checks(target, checks, Path(self.temporary.name), jobs=2, _runner=runner)

        self.assertTrue(report.passed, report.to_dict())
        self.assertGreaterEqual(peak, 2)

    def test_a_single_check_over_the_driver_budget_fails_with_its_id_without_running_sqlcl(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("oversized-check", "SELECT 1 FROM dual", 1)
        calls = []

        def runner(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("an over-budget check must not start a SQLcl session")

        with patch.dict("os.environ", {"MIGRATION_CHECK_BATCH_BYTES": "64"}):
            report = run_checks(target, (check,), Path(self.temporary.name), _runner=runner)

        self.assertFalse(report.passed)
        self.assertEqual(report.errors[0]["check"], "oversized-check")
        self.assertIn("oversized-check", report.errors[0]["message"])
        self.assertEqual(calls, [])

    def test_empty_sqlcl_output_is_recorded_as_missing_check_output(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        binary = install(self.root / "bin", 'printf "FAKE_SQLCL_NO_CHECK_PAYLOAD\\n"\n')

        with patch.dict("os.environ", fake_environment(binary)):
            report = run_checks(target, (check,), self.root / "check-run", phase="postconditions")

        self.assertFalse(report.passed)
        self.assertFalse(report.coverage["outputAvailable"])
        self.assertTrue(report.coverage["evidence"].endswith("sqlcl-output.log"))

    def test_check_timeout_is_reported_with_the_phase_and_visible_limit(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        binary = install(self.root / "bin", 'printf "FAKE_SQLCL_STARTED\\n"\nsleep 5\n')

        with patch.dict("os.environ", fake_environment(binary, MIGRATION_CHECK_TIMEOUT_SECONDS="0.05")):
            report = run_checks(target, (check,), self.root / "timeout-run", phase="preconditions")

        self.assertFalse(report.complete)
        self.assertEqual(report.errors[0]["code"], "SQLCL_TIMEOUT")
        self.assertIn("preconditions checks did not finish within 0.05 s", report.errors[0]["message"])
        self.assertFalse(report.coverage["outputAvailable"])

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

        def runner(_target, driver, run_dir, **_kwargs):
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

    def test_check_driver_counts_clob_amounts_in_utf16_units(self):
        # A supplementary character is one character for LENGTH but two code units for a
        # CLOB, so a LENGTH append amount cuts the query text short. Both the query append
        # and the payload output loop must use LENGTH2.
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("emoji", "SELECT CASE WHEN '\U0001F600' = '\U0001F600' THEN 1 ELSE 0 END FROM dual", 1)
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
            observed["driver"] = driver.read_text(encoding="utf-8")
            payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [{"id": "emoji", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        run_checks(target, (check,), Path(self.temporary.name), _runner=runner)

        driver = observed["driver"]
        self.assertIn("DBMS_LOB.WRITEAPPEND(l_sql, LENGTH2(UTL_I18N.RAW_TO_CHAR(", driver)
        self.assertNotIn("WRITEAPPEND(l_sql, LENGTH(", driver)
        self.assertIn("l_offset := l_offset + LENGTH2(l_chunk);", driver)
        self.assertNotIn("l_offset + LENGTH(l_chunk)", driver)

    def test_check_session_cannot_commit_inside_a_stored_function(self):
        # SET TRANSACTION READ ONLY does not stop a stored function that runs as an
        # autonomous transaction: called from a check it inserted a row, or created a
        # table, and committed. The text validator cannot see a function named
        # without parentheses, so the session itself must refuse the commit.
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
            observed["driver"] = driver.read_text(encoding="utf-8")
            payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [{"id": "one", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        run_checks(target, (check,), Path(self.temporary.name), _runner=runner)

        driver = observed["driver"]
        self.assertIn("ALTER SESSION DISABLE COMMIT IN PROCEDURE;", driver)
        self.assertLess(driver.index("ALTER SESSION DISABLE COMMIT IN PROCEDURE;"), driver.index("SET TRANSACTION READ ONLY;"))
        self.assertLess(driver.index("ALTER SESSION DISABLE COMMIT IN PROCEDURE;"), driver.index("DBMS_SQL.PARSE"))

    def test_check_session_reports_its_database_identity(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("one", "SELECT 1 FROM dual", 1)
        identity = {"session_user": "MIGRATOR", "current_schema": "APP", "db_name": "DEVDB"}
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
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
