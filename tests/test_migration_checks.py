import json
import os
import re
import signal
import sqlite3
import subprocess
import _test_subprocess_safety  # noqa: F401
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target
from scripts.migration_manifest import MigrationManifestError, QueryCheck, load_batch
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaSnapshot, parse_inventory, parse_snapshot
from scripts.migration_checks import CheckReport, analyze_batch, compiled_units, preflight, preflight_inventory_retries, render_in_session_check_driver, run_checks
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

    def test_applied_folder_whose_sql_changed_is_named_as_a_conflict(self):
        # The receipt says this folder was applied. If its SQL or checks changed since, the
        # preflight must name that change, not report a failed postcondition for the new state.
        from scripts import migration_checks

        folder = self.add_folder("2026-10-13_applied-r001", "INSERT INTO zz_t (id) VALUES (1);\n")
        migrations = load_batch(self.root, ["migrations/2026-10-13_applied-r001"])
        receipt = folder / "status.dev.json"
        receipt.write_text(json.dumps({
            "payloadDigest": "0" * 64, "checksSha256": migrations[0].checks_sha256,
        }) + "\n", encoding="utf-8")
        conflicts = migration_checks._applied_source_conflicts(self.root, migrations, "dev")
        self.assertEqual([row["code"] for row in conflicts], ["APPLIED_SOURCE_CHANGED"])
        self.assertIn("follow-up revision", conflicts[0]["message"])

        receipt.write_text(json.dumps({
            "payloadDigest": migrations[0].payload_digest, "checksSha256": migrations[0].checks_sha256,
        }) + "\n", encoding="utf-8")
        self.assertEqual(migration_checks._applied_source_conflicts(self.root, migrations, "dev"), [])

    def test_applied_source_conflicts_are_added_to_a_live_report_and_keep_error_exits(self):
        from scripts import migration_checks

        folder = self.add_folder("2026-10-13_applied-r001", "INSERT INTO zz_t (id) VALUES (1);\n")
        migrations = load_batch(self.root, ["migrations/2026-10-13_applied-r001"])
        (folder / "status.dev.json").write_text(json.dumps({"payloadDigest": "0" * 64, "checksSha256": migrations[0].checks_sha256}) + "\n", encoding="utf-8")
        clean = migration_checks.PreflightReport(0, (), (), {"complete": True})
        changed = migration_checks._with_applied_source_conflicts(clean, self.root, migrations, "dev")
        self.assertEqual(changed.exit_code, 1)
        self.assertEqual([row["code"] for row in changed.conflicts], ["APPLIED_SOURCE_CHANGED"])
        failed = migration_checks.PreflightReport(2, (), ({"code": "X"},), {"complete": False})
        self.assertEqual(migration_checks._with_applied_source_conflicts(failed, self.root, migrations, "dev").exit_code, 2)

    def test_preflight_selected_uses_explicit_values_and_reports_observed_identity(self):
        from scripts import migration_checks

        self.add_folder("2026-10-13_public-api-r001", "CREATE TABLE zz_new (id NUMBER);\n")
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "MIGRATOR",
        }
        snapshot = self.snapshot()
        snapshot.identity.update({
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_name": "APP_PDB",
            "database_version": "23.26.3.0.0",
        })
        observed = {}

        def capture(target, _keys, _run_dir, **_kwargs):
            observed["target"] = target
            return None, snapshot

        prior_environment = dict(os.environ)
        with patch.dict(os.environ, {
            "DB_ENVIRONMENT": "production",
            "CODE_SCHEMA": "OTHER",
            "CODE_SQLCL_CONNECTION": "prod-profile",
            "CODE_EXPECTED_USER": "OTHER",
        }), patch("scripts.schema_catalog.capture_inventory_snapshot_with_retries", side_effect=capture), patch(
            "scripts.migration_checks.run_checks", return_value=self.passing_checks()
        ):
            report = migration_checks.preflight_selected(
                self.root,
                ["migrations/2026-10-13_public-api-r001"],
                environment="dev",
                values=values,
            )

        self.assertEqual(0, report.exit_code, report.to_dict())
        self.assertEqual("APP", observed["target"].schema)
        self.assertEqual(snapshot.identity, report.coverage["observedIdentity"])
        self.assertEqual([], report.coverage["deferredPreconditionFolders"])
        self.assertEqual(prior_environment, dict(os.environ), "explicit values must not be injected into os.environ")

    def test_preflight_selected_fails_closed_on_incomplete_captured_identity(self):
        from scripts import migration_checks

        self.add_folder("2026-10-14_captured-identity-r001", "CREATE TABLE zz_existing (id NUMBER);\n")
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "MIGRATOR",
        }
        complete_identity = {
            "session_user": "MIGRATOR",
            "current_schema": "APP",
            "db_name": "DB_SECRET_MARKER",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "23.26.3.0.0",
        }
        cases = (
            ("missing", (), "database_version"),
            ("partial-with-conflict", (("ZZ_EXISTING", "TABLE", {}),), "service_name"),
        )

        for label, objects, incomplete_field in cases:
            with self.subTest(identity=label):
                snapshot = self.snapshot(objects)
                snapshot.identity.update(complete_identity)
                if incomplete_field == "service_name":
                    snapshot.identity[incomplete_field] = "  "
                else:
                    del snapshot.identity[incomplete_field]

                with patch("scripts.schema_catalog.capture_inventory_snapshot_with_retries", return_value=(None, snapshot)), patch(
                    "scripts.migration_checks.run_checks", return_value=self.passing_checks()
                ):
                    report = migration_checks.preflight_selected(
                        self.root,
                        ["migrations/2026-10-14_captured-identity-r001"],
                        environment="dev",
                        values=values,
                    )

                self.assertEqual(2, report.exit_code, report.to_dict())
                self.assertFalse(report.coverage["complete"])
                self.assertNotIn("observedIdentity", report.coverage)
                identity_errors = [error for error in report.errors if error.get("code") == "OBSERVED_IDENTITY_INCOMPLETE"]
                self.assertEqual(1, len(identity_errors), report.to_dict())
                self.assertEqual(
                    "Live target identity is incomplete; the observed database cannot be identified safely.",
                    identity_errors[0]["message"],
                )
                if objects:
                    self.assertTrue(any(conflict.get("code") == "LIVE_NAMESPACE_OCCUPIED" for conflict in report.conflicts))
                else:
                    self.assertEqual((), report.conflicts)
                self.assertNotIn("DB_SECRET_MARKER", json.dumps(report.to_dict()))

    def test_complete_observed_identity_requires_all_nine_nonblank_fields(self):
        from scripts import migration_checks

        identity = {
            "session_user": "APP", "current_schema": "APP", "db_name": "DEVDB",
            "db_unique_name": "DEVDB1", "service_name": "dev", "container_id": "3",
            "container_name": "APP_PDB", "edition": "ORA$BASE", "database_version": "23.26.3.0.0",
        }
        self.assertEqual(identity, migration_checks.complete_observed_identity(identity))
        for incomplete in (
            {key: value for key, value in identity.items() if key != "edition"},
            {**identity, "service_name": "  "},
            {**identity, "container_id": None},
        ):
            with self.subTest(incomplete=incomplete):
                self.assertIsNone(migration_checks.complete_observed_identity(incomplete))

    def test_preflight_selected_rejects_incomplete_profile_before_catalog_capture(self):
        from scripts import migration_checks

        self.add_folder("2026-10-13_incomplete-profile-r001", "CREATE TABLE zz_new (id NUMBER);\n")
        with patch("scripts.schema_catalog.capture_inventory_snapshot_with_retries") as capture:
            report = migration_checks.preflight_selected(
                self.root,
                ["migrations/2026-10-13_incomplete-profile-r001"],
                environment="dev",
                values={"DB_ENVIRONMENT": "development"},
            )

        self.assertEqual(2, report.exit_code)
        self.assertFalse(capture.called, "an incomplete target profile must refuse before catalog capture")
        self.assertFalse(report.coverage["complete"])

    def test_preflight_selected_detects_deleted_last_step_after_capture(self):
        from scripts import migration_checks

        folder = self.add_folder(
            "2026-10-13_deleted-final-step-r001",
            {
                "001-first.sql": "CREATE TABLE zz_first (id NUMBER);\n",
                "002-last.sql": "CREATE TABLE zz_deleted_private (id NUMBER);\n",
            },
        )
        second_step = folder / "002-last.sql"
        snapshot = self.snapshot()
        snapshot.identity.update({
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_name": "APP_PDB",
            "database_version": "23.26.3.0.0",
        })
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "MIGRATOR",
        }

        def capture(_target, _keys, _run_dir, **_kwargs):
            second_step.unlink()
            return None, snapshot

        with patch("scripts.schema_catalog.capture_inventory_snapshot_with_retries", side_effect=capture), patch(
            "scripts.migration_checks.run_checks", return_value=self.passing_checks()
        ):
            report = migration_checks.preflight_selected(
                self.root,
                [f"migrations/{folder.name}"],
                environment="dev",
                values=values,
            )

        relative_path = f"migrations/{folder.name}/002-last.sql"
        self.assertEqual(2, report.exit_code, report.to_dict())
        self.assertFalse(report.coverage["selectedInputHashesVerified"], report.coverage)
        self.assertFalse(report.coverage["complete"], report.coverage)
        self.assertTrue(
            any(error.get("code") == "SELECTED_INPUT_CHANGED" and error.get("path") == relative_path for error in report.errors),
            report.errors,
        )
        self.assertNotIn("CREATE TABLE zz_deleted_private", json.dumps(report.to_dict()))

    def test_later_preconditions_are_deferred(self):
        from scripts import migration_checks

        first_precondition = {"id": "before-create", "sql": "SELECT 1 FROM dual", "expected": 1}
        later_precondition = {
            "id": "after-create",
            "sql": "SELECT COUNT(*) FROM zz_created_by_first",
            "expected": 1,
        }
        self.add_folder(
            "2026-10-13_deferred-r001",
            "CREATE TABLE zz_created_by_first (id NUMBER);\n",
            preconditions=(first_precondition,),
        )
        self.add_folder(
            "2026-10-13_deferred-r002",
            "ALTER SYSTEM SET zz_reviewed_setting = 1 SCOPE=MEMORY;\n",
            preconditions=(later_precondition,),
        )
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "MIGRATOR",
        }
        snapshot = self.snapshot()
        snapshot.identity.update({
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_name": "APP_PDB",
            "database_version": "23.26.3.0.0",
        })
        evaluated = {}

        def capture(target, _keys, _run_dir, **_kwargs):
            return None, snapshot

        def checks(_target, selected, _run_dir, **_kwargs):
            evaluated["checks"] = tuple(check.sql for check in selected)
            return self.passing_checks()

        with patch("scripts.schema_catalog.capture_inventory_snapshot_with_retries", side_effect=capture), patch(
            "scripts.migration_checks.run_checks", side_effect=checks
        ):
            report = migration_checks.preflight_selected(
                self.root,
                ["migrations/2026-10-13_deferred-r001", "migrations/2026-10-13_deferred-r002"],
                environment="dev",
                values=values,
            )

        self.assertEqual(0, report.exit_code, report.to_dict())
        self.assertEqual((first_precondition["sql"],), evaluated["checks"])
        self.assertNotIn(later_precondition["sql"], evaluated["checks"])
        self.assertEqual(["2026-10-13_deferred-r002"], report.coverage["deferredPreconditionFolders"])
        self.assertEqual(
            ["2026-10-13_deferred-r002"],
            report.coverage["deferred_preconditions"]["folders"],
        )
        self.assertEqual("2026-10-13_deferred-r002", report.coverage["manual_review_required"][0]["migration"])
        self.assertIn(migration_checks.LIMITATION, report.limitation)

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

    def test_preflight_inventory_retry_setting_defaults_to_three_and_allows_zero(self):
        self.assertEqual(preflight_inventory_retries({}), 3)
        self.assertEqual(preflight_inventory_retries({"MIGRATION_PREFLIGHT_INVENTORY_RETRIES": "0"}), 0)
        with self.assertRaisesRegex(RuntimeError, "non-negative integer"):
            preflight_inventory_retries({"MIGRATION_PREFLIGHT_INVENTORY_RETRIES": "1.5"})

    def test_runner_preflight_retries_catalog_churn_before_resolving_a_synonym(self):
        from scripts.migrate import _preflight_snapshot

        folder = self.add_folder(
            "2026-10-02_view-through-synonym-r001",
            "CREATE VIEW ZZ_PUBLIC_CUSTOMERS_V AS SELECT ID FROM ZZ_PUBLIC_CUSTOMERS;\n",
        )
        migrations = self.migration_batch(folder.name)
        target = Target("dev", "dev-profile", "APP", "APP", "development")
        inventory_payload = json.loads((Path(__file__).parent / "fixtures" / "schema_catalog" / "owner-inventory.json").read_text(encoding="utf-8"))
        inventory_payload["identity"]["session_user"] = "APP"
        inventory_payload["identity"]["current_schema"] = "APP"
        inventory_payload["objects"] = [
            {"owner": "APP", "name": "ZZ_PRIVATE_ORDERS", "type": "SYNONYM", "status": "VALID"}
        ]
        inventory_payload["coverage"]["catalogs"] = ["ALL_OBJECTS"]
        inventory_output = (
            "CATALOG_PAYLOAD_BEGIN:inventory\n" + json.dumps(inventory_payload)
            + "\nCATALOG_PAYLOAD_END:inventory\nCATALOG_VERIFIED:inventory\n"
        )
        snapshot_payload = json.loads((Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json").read_text(encoding="utf-8"))
        changing_payload = dict(snapshot_payload)
        changing_payload["before"] = [{"owner": "APP", "name": "CONCURRENT_TABLE", "type": "TABLE"}]
        changing_payload["after"] = list(changing_payload["before"])
        snapshot_outputs = iter((changing_payload, snapshot_payload))
        calls = {"inventory": 0, "snapshot": 0}

        def capture_inventory(_target, _run_dir):
            calls["inventory"] += 1
            return parse_inventory(inventory_output, target)

        def capture_snapshot(_target, inventory, keys, _run_dir):
            calls["snapshot"] += 1
            payload = next(snapshot_outputs)
            output = (
                "CATALOG_PAYLOAD_BEGIN:snapshot\n" + json.dumps(payload)
                + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n"
            )
            return parse_snapshot(output, target, keys, inventory)

        _, snapshot, checks, report = _preflight_snapshot(
            migrations, target, self.root / "run",
            capture_inventory, capture_snapshot,
            lambda *_args, **_kwargs: self.passing_checks(),
        )

        self.assertEqual(calls, {"inventory": 2, "snapshot": 2})
        self.assertTrue(checks.passed)
        self.assertEqual(report.exit_code, 0, report.to_dict())
        self.assertTrue(snapshot.synonyms)

    def test_view_over_owner_or_public_synonym_requires_a_direct_select_grant(self):
        fixture = Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        snapshot = parse_snapshot(
            "CATALOG_PAYLOAD_BEGIN:snapshot\n"
            + json.dumps(payload, separators=(",", ":"))
            + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
            Target("dev", "dev-profile", "APP", "APP", "development"),
        )
        checks = self.passing_checks()

        for synonym_name in ("ZZ_PUBLIC_CUSTOMERS", "ZZ_PRIVATE_ORDERS"):
            with self.subTest(synonym=synonym_name):
                folder = self.add_folder(
                    f"2026-10-02_view-{synonym_name.lower().replace('_', '-')}-r001",
                    f"CREATE VIEW {synonym_name}_V AS SELECT ID FROM {synonym_name};\n",
                )
                report = preflight(load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, checks)
                self.assertEqual(report.exit_code, 0, report.to_dict())

    def test_view_over_a_missing_table_still_reports_missing_prerequisite(self):
        fixture = Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        snapshot = parse_snapshot(
            "CATALOG_PAYLOAD_BEGIN:snapshot\n"
            + json.dumps(payload, separators=(",", ":"))
            + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
            Target("dev", "dev-profile", "APP", "APP", "development"),
        )
        folder = self.add_folder(
            "2026-10-02_view-missing-r001",
            "CREATE VIEW ZZ_MISSING_V AS SELECT ID FROM ZZ_NO_SUCH_TABLE;\n",
        )

        report = preflight(load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, self.passing_checks())

        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_NO_SUCH_TABLE"),
            [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
        )

    def test_synonym_without_select_grant_does_not_satisfy_view_prerequisite(self):
        fixture = Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["synonyms"] = [
            synonym for synonym in payload["synonyms"]
            if synonym["name"] == "ZZ_PUBLIC_NO_GRANT"
        ]
        payload["grants"] = []
        snapshot = parse_snapshot(
            "CATALOG_PAYLOAD_BEGIN:snapshot\n"
            + json.dumps(payload, separators=(",", ":"))
            + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
            Target("dev", "dev-profile", "APP", "APP", "development"),
        )
        folder = self.add_folder(
            "2026-10-02_view-no-grant-r001",
            "CREATE VIEW ZZ_NO_GRANT_V AS SELECT ID FROM ZZ_PUBLIC_NO_GRANT;\n",
        )

        report = preflight(load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, self.passing_checks())

        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_PUBLIC_NO_GRANT"),
            [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
        )

    def _external_synonym_report(self, evidence, folder_name):
        fixture = Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["before"] = [{"owner": "APP", "name": "ZZ_EXTERNAL", "type": "SYNONYM", "status": "VALID"}]
        payload["after"] = [dict(payload["before"][0])]
        payload["synonyms"] = [{
            "owner": "APP", "name": "ZZ_EXTERNAL", "table_owner": "DATA_OWNER", "table_name": "COUNTER",
            **evidence,
        }]
        payload["grants"] = [{
            "owner": "DATA_OWNER", "table_name": "COUNTER", "grantee": "APP", "privilege": "SELECT",
        }]
        snapshot = parse_snapshot(
            "CATALOG_PAYLOAD_BEGIN:snapshot\n" + json.dumps(payload, separators=(",", ":"))
            + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
            Target("dev", "dev-profile", "APP", "APP", "development"),
        )
        folder = self.add_folder(
            folder_name, "CREATE VIEW ZZ_EXTERNAL_V AS SELECT ID FROM ZZ_EXTERNAL;\n",
        )
        return preflight(load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, self.passing_checks())

    def test_select_granted_external_sequence_is_not_a_view_prerequisite(self):
        report = self._external_synonym_report(
            {"target_exists": True, "target_type": "SEQUENCE"},
            "2026-10-11_view-through-external-sequence-r001",
        )

        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_EXTERNAL"),
            [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
        )

    def test_external_synonym_without_target_type_evidence_fails_closed(self):
        report = self._external_synonym_report(
            {"target_exists": True},
            "2026-10-11_view-through-unknown-external-target-r001",
        )

        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_EXTERNAL"),
            [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
        )

    def test_privileged_synonym_adapter_resolves_namespace_collisions_conservatively(self):
        adapter = (Path(__file__).resolve().parents[1] / "scripts" / "schema_catalog.sql").read_text(encoding="utf-8").upper()
        procedure_start = adapter.index("PROCEDURE ADD_DEPENDENCY_CATALOGS IS")
        procedure_end = adapter.index("PROCEDURE ADD_DEFINITION(", procedure_start)
        queries = re.findall(
            r"OPEN\s+DEPENDENCY_CURSOR\s+FOR\s+q'~(.*?)~'",
            adapter[procedure_start:procedure_end],
            flags=re.DOTALL | re.IGNORECASE,
        )
        connection = sqlite3.connect(":memory:")
        connection.create_function("NVL", 2, lambda value, fallback: fallback if value is None else value)
        connection.executescript(
            "CREATE TABLE dba_synonyms (owner TEXT, synonym_name TEXT, table_owner TEXT, table_name TEXT, db_link TEXT);"
            "CREATE TABLE dba_objects (owner TEXT, object_name TEXT, object_type TEXT, namespace INTEGER);"
            "CREATE TABLE all_synonyms (owner TEXT, synonym_name TEXT, table_owner TEXT, table_name TEXT, db_link TEXT);"
            "CREATE TABLE all_objects (owner TEXT, object_name TEXT, object_type TEXT, namespace INTEGER);"
        )
        synonym_rows = [
            ("APP", "ZZ_MV", "DATA_OWNER", "MV_TARGET", ""),
            ("APP", "ZZ_TABLE_INDEX", "DATA_OWNER", "TABLE_INDEX_TARGET", ""),
            ("APP", "ZZ_MV_INDEX", "DATA_OWNER", "MV_INDEX_TARGET", ""),
            ("APP", "ZZ_WRONG_INDEX_NS", "DATA_OWNER", "WRONG_INDEX_NS_TARGET", ""),
            ("APP", "ZZ_WRONG_MV_NS", "DATA_OWNER", "WRONG_MV_NS_TARGET", ""),
            ("APP", "ZZ_WRONG_TABLE_NS", "DATA_OWNER", "WRONG_TABLE_NS_TARGET", ""),
            ("APP", "ZZ_AMBIG", "DATA_OWNER", "AMBIG_TARGET", ""),
            ("APP", "ZZ_DUP", "DATA_OWNER", "DUP_TARGET", ""),
            ("APP", "ZZ_SEQ", "DATA_OWNER", "SEQ_TARGET", ""),
            ("APP", "ZZ_MISSING", "DATA_OWNER", "MISSING_TARGET", ""),
        ]
        object_rows = [
            ("DATA_OWNER", "MV_TARGET", "TABLE", 1),
            ("DATA_OWNER", "MV_TARGET", "MATERIALIZED VIEW", 19),
            ("DATA_OWNER", "MV_TARGET", "INDEX", 4),
            ("DATA_OWNER", "TABLE_INDEX_TARGET", "TABLE", 1),
            ("DATA_OWNER", "TABLE_INDEX_TARGET", "INDEX", 4),
            ("DATA_OWNER", "MV_INDEX_TARGET", "TABLE", 1),
            ("DATA_OWNER", "MV_INDEX_TARGET", "MATERIALIZED VIEW", 19),
            ("DATA_OWNER", "MV_INDEX_TARGET", "INDEX", 4),
            ("DATA_OWNER", "WRONG_INDEX_NS_TARGET", "TABLE", 1),
            ("DATA_OWNER", "WRONG_INDEX_NS_TARGET", "INDEX", 5),
            ("DATA_OWNER", "WRONG_MV_NS_TARGET", "TABLE", 1),
            ("DATA_OWNER", "WRONG_MV_NS_TARGET", "MATERIALIZED VIEW", 2),
            ("DATA_OWNER", "WRONG_TABLE_NS_TARGET", "TABLE", 2),
            ("DATA_OWNER", "WRONG_TABLE_NS_TARGET", "MATERIALIZED VIEW", 19),
            ("DATA_OWNER", "AMBIG_TARGET", "TABLE", 1),
            ("DATA_OWNER", "AMBIG_TARGET", "VIEW", 1),
            ("DATA_OWNER", "DUP_TARGET", "TABLE", 1),
            ("DATA_OWNER", "DUP_TARGET", "TABLE", 2),
            ("DATA_OWNER", "SEQ_TARGET", "SEQUENCE", 1),
        ]
        for synonym_catalog, object_catalog in (("dba_synonyms", "dba_objects"), ("all_synonyms", "all_objects")):
            connection.executemany(f"INSERT INTO {synonym_catalog} VALUES (?, ?, ?, ?, ?)", synonym_rows)
            connection.executemany(f"INSERT INTO {object_catalog} VALUES (?, ?, ?, ?)", object_rows)

        expected_types = {
            "ZZ_MV": (True, "MATERIALIZED VIEW"),
            "ZZ_TABLE_INDEX": (True, "TABLE"),
            "ZZ_MV_INDEX": (True, "MATERIALIZED VIEW"),
            "ZZ_WRONG_INDEX_NS": (True, None),
            "ZZ_WRONG_MV_NS": (True, None),
            "ZZ_WRONG_TABLE_NS": (True, None),
            "ZZ_AMBIG": (True, None),
            "ZZ_DUP": (True, None),
            "ZZ_SEQ": (True, "SEQUENCE"),
            "ZZ_MISSING": (False, None),
        }
        for route, query in (("dba", queries[0]), ("owner", queries[1])):
            with self.subTest(route=route):
                captured_rows = connection.execute(
                    query,
                    {"TARGET_SCHEMA": "APP", "PUBLIC_OWNER": "PUBLIC"},
                ).fetchall()
                captured = {
                    row[1]: {
                        "owner": row[0], "name": row[1], "table_owner": row[2], "table_name": row[3],
                        "db_link": row[4], "target_exists": row[5] == "Y", "target_type": row[6],
                    }
                    for row in captured_rows
                }
                self.assertEqual(
                    {name: (row["target_exists"], row["target_type"]) for name, row in captured.items()},
                    expected_types,
                )

                payload = json.loads(
                    (Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json")
                    .read_text(encoding="utf-8")
                )
                payload["before"] = [
                    {"owner": "APP", "name": name, "type": "SYNONYM", "status": "VALID"}
                    for name in captured
                ]
                payload["after"] = [dict(row) for row in payload["before"]]
                payload["synonyms"] = list(captured.values())
                payload["grants"] = [
                    {"owner": row["table_owner"], "table_name": row["table_name"], "grantee": "APP", "privilege": "SELECT"}
                    for name, row in captured.items() if name != "ZZ_MISSING"
                ]
                snapshot = parse_snapshot(
                    "CATALOG_PAYLOAD_BEGIN:snapshot\n" + json.dumps(payload, separators=(",", ":"))
                    + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
                    Target("dev", "dev-profile", "APP", "APP", "development"),
                )
                for name, accepted in (
                    ("ZZ_MV", True), ("ZZ_TABLE_INDEX", True), ("ZZ_MV_INDEX", True),
                    ("ZZ_WRONG_INDEX_NS", False),
                    ("ZZ_WRONG_MV_NS", False), ("ZZ_WRONG_TABLE_NS", False),
                    ("ZZ_AMBIG", False), ("ZZ_DUP", False), ("ZZ_SEQ", False), ("ZZ_MISSING", False),
                ):
                    with self.subTest(synonym=name):
                        folder = self.add_folder(
                            f"2026-10-11_{route}-view-through-{name.lower().replace('_', '-')}-r001",
                            f"CREATE VIEW {name}_V AS SELECT ID FROM {name};\n",
                        )
                        report = preflight(
                            load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, self.passing_checks(),
                        )
                        if accepted:
                            self.assertEqual(report.exit_code, 0, report.to_dict())
                            self.assertEqual(report.conflicts, ())
                        else:
                            self.assertIn(
                                ("MISSING_PREREQUISITE", name),
                                [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
                            )
        connection.close()

    def test_synonym_chain_to_missing_target_does_not_satisfy_view_prerequisite(self):
        fixture = Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["before"] = [
            {"owner": "APP", "name": "ZZ_OWNER_CHAIN", "type": "SYNONYM", "status": "VALID"},
            {"owner": "APP", "name": "ZZ_CHAIN_TARGET", "type": "SYNONYM", "status": "VALID"},
        ]
        payload["after"] = [dict(row) for row in payload["before"]]
        payload["synonyms"] = [
            {"owner": "APP", "name": "ZZ_OWNER_CHAIN", "table_owner": "APP", "table_name": "ZZ_CHAIN_TARGET"},
            {"owner": "APP", "name": "ZZ_CHAIN_TARGET", "table_owner": "DATA_OWNER", "table_name": "ZZ_MISSING_TARGET"},
        ]
        payload["grants"] = []
        snapshot = parse_snapshot(
            "CATALOG_PAYLOAD_BEGIN:snapshot\n"
            + json.dumps(payload, separators=(",", ":"))
            + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
            Target("dev", "dev-profile", "APP", "APP", "development"),
        )
        folder = self.add_folder(
            "2026-10-02_view-through-dangling-chain-r001",
            "CREATE VIEW ZZ_CHAIN_VIEW AS SELECT ID FROM ZZ_OWNER_CHAIN;\n",
        )

        report = preflight(load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, self.passing_checks())

        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_OWNER_CHAIN"),
            [(conflict.get("code"), conflict.get("name")) for conflict in report.conflicts],
        )

    def test_owner_synonym_takes_precedence_over_a_selectable_public_synonym(self):
        fixture = Path(__file__).parent / "fixtures" / "schema_catalog" / "view-synonyms.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["synonyms"] = [
            {"owner": "PUBLIC", "name": "ZZ_SHADOWED", "table_owner": "DATA_OWNER", "table_name": "CUSTOMERS"},
            {"owner": "APP", "name": "ZZ_SHADOWED", "table_owner": "DATA_OWNER", "table_name": "NO_GRANT_TABLE"},
        ]
        payload["before"] = [
            {"owner": "APP", "name": "ZZ_SHADOWED", "type": "SYNONYM", "status": "VALID"}
        ]
        payload["after"] = [dict(payload["before"][0])]
        payload["grants"] = [
            {"owner": "DATA_OWNER", "table_name": "CUSTOMERS", "grantee": "APP", "privilege": "SELECT"},
        ]
        snapshot = parse_snapshot(
            "CATALOG_PAYLOAD_BEGIN:snapshot\n"
            + json.dumps(payload, separators=(",", ":"))
            + "\nCATALOG_PAYLOAD_END:snapshot\nCATALOG_VERIFIED:snapshot\n",
            Target("dev", "dev-profile", "APP", "APP", "development"),
        )
        folder = self.add_folder(
            "2026-10-02_view-shadowed-synonym-r001",
            "CREATE VIEW ZZ_SHADOWED_V AS SELECT ID FROM ZZ_SHADOWED;\n",
        )

        report = preflight(load_batch(self.root, [f"migrations/{folder.name}"]), snapshot, self.passing_checks())

        self.assertIn(
            ("MISSING_PREREQUISITE", "ZZ_SHADOWED"),
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

    def test_index_can_use_column_added_to_existing_table_earlier_in_selected_batch(self):
        self.add_folder("2026-09-27_add-index-column-r001", {
            "001-add-column.sql": "ALTER TABLE ORDERS ADD NEW_COL NUMBER;\n",
            "002-create-index.sql": "CREATE INDEX ORDERS_NEW_COL_I ON ORDERS (NEW_COL);\n",
        })
        migration = self.migration_batch("2026-09-27_add-index-column-r001")
        orders = ObjectDefinition(
            ObjectKey("APP", "ORDERS", "TABLE"),
            {"columns": [{"name": "ID", "column_id": 1}]},
            "CREATE TABLE APP.ORDERS (ID NUMBER)",
            (),
            True,
        )

        report = preflight(
            migration,
            self.snapshot((("ORDERS", "TABLE", {}),), (orders,)),
            self.passing_checks(),
        )

        self.assertEqual(report.exit_code, 0, report.to_dict())

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

    @unittest.skipIf(os.name == "nt", "SIGINT process-tree cancellation regression is POSIX-specific")
    def test_interrupt_cancels_parallel_sqlcl_workers_and_their_child_processes(self):
        pid_dir = self.root / "worker-pids"
        pid_dir.mkdir()
        late_writer = self.root / "late_writer.py"
        late_writer.write_text(
            "import sys, time\nfrom pathlib import Path\ntime.sleep(1.2)\nPath(sys.argv[1]).write_text('survived')\n",
            encoding="utf-8",
        )
        binary = install(
            self.root / "bin",
            'printf "%s\\n" "$(ps -o pgid= -p $$ | tr -d \' \')" > "$FAKE_PID_DIR/group-$$"\n'
            'python3 "$FAKE_LATE_WRITER" "$FAKE_PID_DIR/late-$$" &\n'
            'child=$!\nprintf "%s\\n" "$child" > "$FAKE_PID_DIR/child-$$"\n'
            'printf "FAKE_SQLCL_STARTED\\n"\nwait "$child"\nprintf "FAKE_SQLCL_FINISHED\\n"\n',
        )
        caller = self.root / "run_checks.py"
        caller.write_text(
            "import sys\nfrom pathlib import Path\n"
            "from scripts.db_targets import Target\n"
            "from scripts.migration_checks import run_checks\n"
            "from scripts.migration_manifest import QueryCheck\n"
            "checks=tuple(QueryCheck(f'check-{i}', 'SELECT 1 FROM dual', 1) for i in range(4))\n"
            "run_checks(Target('dev','fake-dev','APP','APP','development'), checks, "
            "Path(sys.argv[1]) / 'check-runs', jobs=2, phase='preconditions')\n",
            encoding="utf-8",
        )
        repo = Path(__file__).resolve().parents[1]
        environment = fake_environment(
            binary,
            FAKE_PID_DIR=str(pid_dir),
            FAKE_LATE_WRITER=str(late_writer),
            MIGRATION_CHECK_BATCH_BYTES="5000",
            MIGRATION_CHECK_TIMEOUT_SECONDS="30",
        )
        environment["PYTHONPATH"] = str(repo)
        process = subprocess.Popen(
            [sys.executable, str(caller), str(self.root)],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while len(list(pid_dir.glob("child-*"))) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(len(list(pid_dir.glob("child-*"))), 2, "parallel SQLcl children never started")
            interrupted_at = time.monotonic()
            os.kill(process.pid, signal.SIGINT)
            try:
                returncode = process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.fail("run_checks waited for SQLcl workers instead of cancelling their process groups")
            process.communicate()
            self.assertLess(time.monotonic() - interrupted_at, 1.5)
            self.assertNotEqual(returncode, 0)
            time.sleep(1.3)
            self.assertFalse(list(pid_dir.glob("late-*")), "an SQLcl descendant survived interruption")
            for child_file in pid_dir.glob("child-*"):
                child_pid = int(child_file.read_text(encoding="utf-8"))
                proc_stat = Path(f"/proc/{child_pid}/stat")
                if proc_stat.is_file():
                    state = proc_stat.read_text(encoding="utf-8").rsplit(") ", 1)[-1][:1]
                    self.assertEqual(state, "Z", f"SQLcl child {child_pid} is still running after cancellation")
            output_logs = list((self.root / "check-runs").rglob("sqlcl-output.log"))
            self.assertGreaterEqual(len(output_logs), 2)
            for output_log in output_logs:
                output = output_log.read_text(encoding="utf-8")
                self.assertIn("FAKE_SQLCL_STARTED", output)
                self.assertNotIn("FAKE_SQLCL_FINISHED", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for group_file in pid_dir.glob("group-*"):
                try:
                    group = int(group_file.read_text(encoding="utf-8"))
                    if group != os.getpgrp():
                        os.killpg(group, signal.SIGKILL)
                except (OSError, ValueError, ProcessLookupError):
                    pass

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

    def test_opt_in_binds_do_not_widen_migration_defaults(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("run-check", "SELECT 1 FROM dual WHERE :DEVTEST_RUN_ID = 'x'", 1)
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
            observed["driver"] = driver.read_text(encoding="utf-8")
            payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [{"id": "run-check", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        # Without bindings (None): ordinary migration checks refuse :DEVTEST_RUN_ID
        report_default = run_checks(target, (check,), Path(self.temporary.name), _runner=runner)
        self.assertFalse(report_default.passed)
        self.assertFalse(report_default.complete)
        self.assertEqual(report_default.errors[0]["code"], "CHECK_UNAVAILABLE")

        # With explicit validated bindings map: accepts :DEVTEST_RUN_ID
        report_opt_in = run_checks(
            target,
            (check,),
            Path(self.temporary.name),
            bindings={"DEVTEST_RUN_ID": "x"},
            _runner=runner,
        )
        self.assertTrue(report_opt_in.passed)
        self.assertIn("DBMS_SQL.BIND_VARIABLE(l_cursor, ':DEVTEST_RUN_ID',", observed["driver"])

    def test_unicode_quotes_and_comment_payload_are_values(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        payload = "عربي مع O'Neil \n'); DROP TABLE X; --"
        check = QueryCheck("bind-test", "SELECT 1 FROM dual WHERE :DEVTEST_PAYLOAD = :DEVTEST_PAYLOAD", 1)
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
            observed["driver"] = driver.read_text(encoding="utf-8")
            res_payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [{"id": "bind-test", "row_count": 1, "column_count": 1, "value": 1, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(res_payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        report = run_checks(
            target,
            (check,),
            Path(self.temporary.name),
            bindings={"DEVTEST_PAYLOAD": payload},
            _runner=runner,
        )
        self.assertTrue(report.passed)
        driver = observed["driver"]
        # Value is hex-encoded into DBMS_SQL.BIND_VARIABLE, not rendered as raw string literal
        hex_val = payload.encode("utf-8").hex().upper()
        self.assertIn(f"HEXTORAW('{hex_val}')", driver)
        self.assertIn("DBMS_SQL.BIND_VARIABLE(l_cursor, ':DEVTEST_PAYLOAD', UTL_I18N.RAW_TO_CHAR(", driver)
        # Parsed SQL is unchanged: raw payload with injection attempt does NOT appear in SQL text
        self.assertNotIn("DROP TABLE X", driver)

    def test_query_check_supports_expected_integer_greater_than_one(self):
        target = Target("dev", "dev-profile", "MIGRATOR", "APP", "development")
        check = QueryCheck("count-two", "SELECT 2 FROM dual", 2)
        observed = {}

        def runner(_target, driver, run_dir, **_kwargs):
            observed["driver"] = driver.read_text(encoding="utf-8")
            res_payload = {"schemaVersion": 1, "phase": "preconditions", "complete": True, "results": [{"id": "count-two", "row_count": 1, "column_count": 1, "value": 2, "numeric": True}]}
            output = "CHECK_PAYLOAD_BEGIN:preconditions\n" + json.dumps(res_payload) + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
            return type("Result", (), {"returncode": 0, "output": output, "run_dir": run_dir})()

        report = run_checks(target, (check,), Path(self.temporary.name), _runner=runner)
        self.assertTrue(report.passed)
        self.assertEqual(report.results[0]["value"], 2)

        # In-session driver with fail_on_false checks expected value in PL/SQL
        in_session = render_in_session_check_driver(target, (check,), "preconditions", fail_on_false=True)
        self.assertIn("l_value != 2", in_session)

    def test_check_driver_keeps_default_bind_scope_and_supports_explicit_devtest_values(self):
        from scripts.migration_checks import _render_check_driver

        target = Target("dev", "dev-profile", "APP", "APP", "development")
        with self.assertRaisesRegex(MigrationManifestError, "target_schema"):
            _render_check_driver(
                target,
                (QueryCheck("one", "SELECT 1 FROM dual WHERE :DEVTEST_N = 1", 1),),
                "preconditions",
            )

        binding = "مرحبا 😀'"
        query = "SELECT CASE WHEN :DEVTEST_N = 7 AND :DEVTEST_TEXT IS NOT NULL THEN 1 ELSE 0 END FROM dual"
        driver = _render_check_driver(
            target,
            (QueryCheck("one", query, 1),),
            "preconditions",
            bindings={"DEVTEST_N": 7, "DEVTEST_TEXT": binding},
        )
        self.assertIn("DBMS_SQL.BIND_VARIABLE(l_cursor, ':DEVTEST_N', 7);", driver)
        self.assertIn(binding.encode("utf-8").hex().upper(), driver)
        self.assertNotIn(binding, driver)


if __name__ == "__main__":
    unittest.main()
