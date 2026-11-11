from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401 (keeps tests away from a real SQLcl)

from scripts import feature_cli, feature_convert
from scripts import migration_manifest as manifest


CHECKS = {
    "schemaVersion": 1,
    "preconditions": [],
    "postconditions": [{"id": "after", "sql": "SELECT 1 FROM dual", "expected": 1}],
}


class FeatureConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # Resolve the root the way the CLI does, so Windows 8.3 temporary paths compare equal.
        self.root = Path(self.temporary.name).resolve()
        (self.root / "migrations").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_folder(self, relative: str, files: Mapping[str, str]) -> str:
        folder = self.root / relative
        folder.mkdir(parents=True)
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8", newline="\n")
        (folder / "checks.json").write_text(json.dumps(CHECKS) + "\n", encoding="utf-8", newline="\n")
        return relative

    def install_dev_receipt(self, relative: str) -> None:
        folder = self.root / relative
        migration = manifest.load_migration(self.root, relative)
        receipt = {
            "schemaVersion": 1,
            "state": "verified",
            "environment": "dev",
            "migration": folder.name,
            "createdDate": migration.date,
            "family": migration.family,
            "revision": migration.revision,
            "files": [{"name": item.name, "sequence": item.sequence, "sha256": item.sha256} for item in migration.files],
            "checksSha256": migration.checks_sha256,
            "payloadDigest": migration.payload_digest,
            "target": {"environment": "dev", "connection": "local-fixture", "expected_user": "APP", "current_schema": "APP"},
            "applyStartedAt": "2026-10-12T10:00:00Z",
            "applyCompletedAt": "2026-10-12T10:01:00Z",
            "verifiedAt": "2026-10-12T10:02:00Z",
            "checks": [{"id": check.id, "passed": True} for check in migration.postconditions],
            "verifier": "fixture-verifier-v1",
            "normalization": "fixture-normalization-v1",
        }
        manifest.install_receipt(folder / "status.dev.json", receipt)

    def tree_digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*"), key=lambda item: item.relative_to(self.root).as_posix()):
            relative = path.relative_to(self.root).as_posix()
            digest.update(relative.encode())
            if path.is_symlink():
                digest.update(b"L" + os.readlink(path).encode())
            elif path.is_file():
                digest.update(b"F" + path.read_bytes())
            elif path.is_dir():
                digest.update(b"D")
        return digest.hexdigest()

    def run_cli(self, *arguments: str, environ: Mapping[str, str] | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = feature_cli.main(
                ["convert-plan", *arguments], repo_root=self.root, environ={} if environ is None else environ,
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def folder_record(self, report: Mapping, relative: str) -> dict:
        return next(record for record in report["folders"] if record["path"] == relative)

    def statement(self, record: Mapping, file_name: str, index: int) -> dict:
        return next(item for item in record["statements"] if item["file"] == file_name and item["statement"] == index)

    def test_stored_units_name_the_capture_option_for_their_type_and_dedupe_a_package(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-05_units-r001", {
            "001-pkg.sql": "CREATE OR REPLACE PACKAGE item_lookup AS\n  PROCEDURE p;\nEND item_lookup;\n/\n",
            "002-pkg-body.sql": "CREATE OR REPLACE PACKAGE BODY item_lookup AS\n  PROCEDURE p IS BEGIN NULL; END p;\nEND item_lookup;\n/\n",
            "003-type.sql": "CREATE OR REPLACE TYPE point_t AS OBJECT (x NUMBER);\n/\n",
            "004-trigger.sql": "CREATE OR REPLACE TRIGGER orders_trg BEFORE INSERT ON orders FOR EACH ROW BEGIN NULL; END;\n/\n",
            "005-proc.sql": "CREATE OR REPLACE PROCEDURE log_it AS BEGIN NULL; END;\n/\n",
            "006-fn.sql": "CREATE OR REPLACE FUNCTION calc RETURN NUMBER AS BEGIN RETURN 1; END;\n/\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertTrue(record["analysed"], record["problem"])
        units = {(unit["group"], unit["name"]): unit for unit in record["storedUnits"]}
        self.assertEqual(
            {("package", "ITEM_LOOKUP"), ("type", "POINT_T"), ("trigger", "ORDERS_TRG"), ("procedure", "LOG_IT"), ("function", "CALC")},
            set(units),
        )
        self.assertEqual(["PACKAGE", "PACKAGE BODY"], units[("package", "ITEM_LOOKUP")]["kinds"])
        self.assertEqual("scripts/team.sh feature capture demo --package ITEM_LOOKUP --schema APP_CODE --env dev",
                         units[("package", "ITEM_LOOKUP")]["capture"])
        self.assertEqual("scripts/team.sh feature capture demo --type POINT_T --schema APP_CODE --env dev",
                         units[("type", "POINT_T")]["capture"])
        self.assertIn("--trigger ORDERS_TRG --schema APP_CODE --env dev", units[("trigger", "ORDERS_TRG")]["capture"])
        self.assertIn("--procedure LOG_IT --schema APP_CODE --env dev", units[("procedure", "LOG_IT")]["capture"])
        self.assertIn("--function CALC --schema APP_CODE --env dev", units[("function", "CALC")]["capture"])
        self.assertEqual("convertible", record["verdict"])

    def test_one_time_ddl_is_judged_statement_by_statement_with_a_reason_for_each_refusal(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-05_tables-r001", {
            "001-tables.sql": (
                "CREATE TABLE items (id NUMBER, name VARCHAR2(40));\n"
                "CREATE OR REPLACE VIEW item_names AS SELECT name FROM items;\n"
                "DROP TABLE old_items;\n"
            ),
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual("ddl-form", self.statement(record, "001-tables.sql", 1)["disposition"])
        self.assertEqual("ddl-form", self.statement(record, "001-tables.sql", 2)["disposition"])
        dropped = self.statement(record, "001-tables.sql", 3)
        self.assertEqual("refused", dropped["disposition"])
        self.assertIn("destructive", dropped["reason"])
        self.assertEqual(3, dropped["line"])
        self.assertEqual("partly convertible", record["verdict"])
        self.assertEqual([], record["dataOnlyFiles"])

    def test_a_stored_unit_inside_a_mixed_file_is_captured_while_its_table_is_copied(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-23_mixed-r001", {
            "001-mixed.sql": "CREATE TABLE mixed_t (id NUMBER);\nCREATE OR REPLACE PACKAGE mixed_pkg AS\n  PROCEDURE p;\nEND mixed_pkg;\n/\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual("ddl-form", self.statement(record, "001-mixed.sql", 1)["disposition"])
        self.assertEqual("capture", self.statement(record, "001-mixed.sql", 2)["disposition"])
        self.assertEqual([("package", "MIXED_PKG", ["PACKAGE"])],
                         [(unit["group"], unit["name"], unit["kinds"]) for unit in record["storedUnits"]])
        self.assertEqual("convertible", record["verdict"])

    def test_a_compiler_setting_before_a_unit_is_covered_but_one_without_a_unit_is_refused(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-24_settings-r001", {
            "001-tuned.sql": (
                "ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\n"
                "CREATE OR REPLACE FUNCTION tuned RETURN NUMBER AS BEGIN RETURN 1; END;\n/\n"
            ),
            "002-loose.sql": "ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\nCREATE TABLE loose_t (id NUMBER);\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual("covered-by-capture", self.statement(record, "001-tuned.sql", 1)["disposition"])
        self.assertEqual("capture", self.statement(record, "001-tuned.sql", 2)["disposition"])
        self.assertEqual("refused", self.statement(record, "002-loose.sql", 1)["disposition"])
        self.assertEqual("partly convertible", record["verdict"])

    def test_a_recompile_of_an_existing_unit_is_covered_by_its_capture(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-25_recompile-r001", {
            "001-recompile.sql": "ALTER PACKAGE recomp_pkg COMPILE;\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual("covered-by-capture", self.statement(record, "001-recompile.sql", 1)["disposition"])
        self.assertEqual("scripts/team.sh feature capture demo --package RECOMP_PKG --schema APP_CODE --env dev",
                         record["storedUnits"][0]["capture"])
        self.assertEqual("convertible", record["verdict"])

    def test_data_only_files_stay_migrations_and_a_data_only_folder_stays_a_migration(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-06_load-r001", {
            "001-load.sql": "INSERT INTO items (id) VALUES (1);\nUPDATE items SET id = 2 WHERE id = 1;\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual(["001-load.sql"], record["dataOnlyFiles"])
        self.assertEqual({"data"}, {item["disposition"] for item in record["statements"]})
        self.assertEqual("stays a migration", record["verdict"])

    def test_a_folder_applied_locally_archives_its_ddl_instead_of_copying_it(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-07_applied-r001", {
            "001-table.sql": "CREATE TABLE applied (id NUMBER);\n",
        })
        self.install_dev_receipt(folder)

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual("locked", record["localEvidence"]["status"])
        self.assertEqual("archive", self.statement(record, "001-table.sql", 1)["disposition"])
        self.assertEqual("convertible", record["verdict"])
        self.assertEqual([folder], report["archive"])
        status, stdout, _stderr = self.run_cli(folder, "--feature", "demo")
        self.assertEqual(0, status)
        self.assertIn("archive, do not copy", stdout)
        self.assertIn(f"scripts/team.sh archive-migrations {folder}", stdout)

    def test_a_later_revision_is_not_archived_while_an_earlier_one_of_its_family_stays(self) -> None:
        earlier = self.write_folder("migrations/APP_CODE/2026-01-26_family-r001", {
            "001-table.sql": "CREATE TABLE family_t (id NUMBER);\nDROP TABLE family_old;\n",
        })
        later = self.write_folder("migrations/APP_CODE/2026-01-27_family-r002", {
            "001-table.sql": "CREATE TABLE family_u (id NUMBER);\n",
        })

        report = feature_convert.convert_plan(self.root, [earlier, later], feature="demo")

        self.assertEqual("convertible", self.folder_record(report, later)["verdict"])
        self.assertEqual([], report["archive"])
        self.assertIn("earlier revision", " ".join(self.folder_record(report, later)["notes"]))

    def test_a_partly_convertible_folder_is_left_out_of_the_archive_command(self) -> None:
        convertible = self.write_folder("migrations/APP_CODE/2026-01-08_whole-r001", {"001-table.sql": "CREATE TABLE whole (id NUMBER);\n"})
        partly = self.write_folder("migrations/APP_CODE/2026-01-09_partly-r001", {
            "001-table.sql": "CREATE TABLE partly (id NUMBER);\nDROP TABLE gone;\n",
        })

        report = feature_convert.convert_plan(self.root, [convertible, partly], feature="demo")

        self.assertEqual([convertible], report["archive"])
        self.assertEqual("partly convertible", self.folder_record(report, partly)["verdict"])
        self.assertIn("stays active", " ".join(self.folder_record(report, partly)["notes"]))

    def test_a_flat_folder_states_that_its_schema_is_unknown_and_uses_a_placeholder(self) -> None:
        folder = self.write_folder("migrations/2026-01-10_flat-r001", {
            "001-units.sql": "CREATE OR REPLACE FUNCTION calc_flat RETURN NUMBER AS BEGIN RETURN 1; END;\n/\n",
            "002-table.sql": "CREATE TABLE flat_items (id NUMBER);\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")
        record = self.folder_record(report, folder)
        self.assertIsNone(record["schema"])
        self.assertEqual("unresolved", record["schemaSource"])
        self.assertTrue(record["provisional"])
        self.assertIn("--function CALC_FLAT --schema <SCHEMA> --env dev", record["storedUnits"][0]["capture"])

        status, stdout, _stderr = self.run_cli(folder, "--feature", "demo")
        self.assertEqual(0, status)
        self.assertIn("schema is not known offline", stdout)
        self.assertNotIn("UNRESOLVED_SCHEMA", stdout)

    def test_feature_option_fills_the_commands_and_a_slug_placeholder_is_printed_otherwise(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-11_slug-r001", {
            "001-proc.sql": "CREATE OR REPLACE PROCEDURE slug_proc AS BEGIN NULL; END;\n/\n",
        })

        status, stdout, _stderr = self.run_cli(folder)
        self.assertEqual(0, status)
        self.assertIn("feature capture <slug> --procedure SLUG_PROC --schema APP_CODE --env dev", stdout)

        status, stdout, _stderr = self.run_cli(folder, "--feature", "inventory")
        self.assertEqual(0, status)
        self.assertIn("feature capture inventory --procedure SLUG_PROC --schema APP_CODE --env dev", stdout)

    def test_an_invalid_feature_slug_is_refused_before_any_analysis(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-12_slug-r001", {"001-table.sql": "CREATE TABLE s (id NUMBER);\n"})

        status, stdout, stderr = self.run_cli(folder, "--feature", "Not_A_Slug")

        self.assertEqual(2, status)
        self.assertIn("feature must be a lowercase hyphenated slug", stderr)
        self.assertEqual("", stdout)

    def test_exit_code_is_two_when_a_folder_cannot_be_analysed_and_the_rest_are_still_shown(self) -> None:
        good = self.write_folder("migrations/APP_CODE/2026-01-13_good-r001", {"001-table.sql": "CREATE TABLE good (id NUMBER);\n"})
        missing = "migrations/APP_CODE/2026-01-14_missing-r001"

        status, stdout, _stderr = self.run_cli(good, missing, "--format", "json")

        self.assertEqual(2, status)
        report = json.loads(stdout)
        self.assertEqual(2, report["exitCode"])
        self.assertTrue(self.folder_record(report, good)["analysed"])
        self.assertFalse(self.folder_record(report, missing)["analysed"])
        self.assertIsNotNone(self.folder_record(report, missing)["problem"])
        self.assertEqual([], [item for item in report["diagnostics"] if item["path"] == good])

    def test_all_folders_analysed_exits_zero(self) -> None:
        good = self.write_folder("migrations/APP_CODE/2026-01-15_clean-r001", {"001-table.sql": "CREATE TABLE clean (id NUMBER);\n"})

        status, _stdout, _stderr = self.run_cli(good)

        self.assertEqual(0, status)

    def test_unknown_local_evidence_is_not_analysed_and_exits_two(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-16_unknown-r001", {"001-table.sql": "CREATE TABLE unknown (id NUMBER);\n"})
        (self.root / folder / "status.dev.json").write_text("{bad json\n", encoding="utf-8")

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        record = self.folder_record(report, folder)
        self.assertEqual("unknown", record["localEvidence"]["status"])
        self.assertFalse(record["analysed"])
        self.assertIsNone(record["verdict"])
        self.assertEqual(2, report["exitCode"])

    def test_json_output_is_sorted_and_identical_across_runs(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-17_stable-r001", {
            "001-pkg.sql": "CREATE OR REPLACE PACKAGE stable_pkg AS\n  PROCEDURE p;\nEND stable_pkg;\n/\n",
            "002-table.sql": "CREATE TABLE stable (id NUMBER);\nDROP TABLE stale;\n",
        })

        first = self.run_cli(folder, "--format", "json")
        second = self.run_cli(folder, "--format", "json")

        self.assertEqual(first, second)
        document = json.loads(first[1])
        self.assertEqual(json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n", first[1])

    def test_the_planner_writes_nothing_and_never_reaches_a_database_or_capture(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-18_readonly-r001", {
            "001-units.sql": "CREATE OR REPLACE PACKAGE readonly_pkg AS\n  PROCEDURE p;\nEND readonly_pkg;\n/\n",
            "002-table.sql": "CREATE TABLE readonly_t (id NUMBER);\nINSERT INTO readonly_t VALUES (1);\n",
        })
        self.install_dev_receipt(self.write_folder("migrations/APP_CODE/2026-01-19_locked-r001", {
            "001-table.sql": "CREATE TABLE locked_t (id NUMBER);\n",
        }))
        before = self.tree_digest()

        def refuse(*_arguments, **_keywords):
            raise AssertionError("convert-plan must not run a command, capture or read DEV")

        with patch.object(subprocess, "run", side_effect=refuse), \
                patch.object(subprocess, "Popen", side_effect=refuse), \
                patch.object(feature_cli.feature_sources, "capture_unit", side_effect=refuse), \
                patch.object(feature_cli.feature_sources, "capture_package", side_effect=refuse), \
                patch.object(feature_cli.feature_doctor, "live_findings", side_effect=refuse):
            status, _stdout, _stderr = self.run_cli(folder, "migrations/APP_CODE/2026-01-19_locked-r001", "--format", "json")
            text_status, _text, _text_err = self.run_cli(folder)

        self.assertEqual(0, status)
        self.assertEqual(0, text_status)
        self.assertEqual(before, self.tree_digest())
        self.assertFalse((self.root / ".env").exists())

    def test_quoted_unit_names_are_printed_as_identifiers_the_capture_accepts(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-20_quoted-r001", {
            "001-proc.sql": 'CREATE OR REPLACE PROCEDURE "Mixed Case" AS BEGIN NULL; END;\n/\n',
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        unit = self.folder_record(report, folder)["storedUnits"][0]
        self.assertEqual("Mixed Case", unit["name"])
        self.assertIn("--procedure " + shlex.quote('"Mixed Case"'), unit["capture"])

    def test_capture_warns_that_it_reads_dev_now_for_an_unapplied_folder(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-21_unapplied-r001", {
            "001-proc.sql": "CREATE OR REPLACE PROCEDURE unapplied_proc AS BEGIN NULL; END;\n/\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        notes = " ".join(self.folder_record(report, folder)["notes"])
        self.assertIn("reads DEV", notes)
        self.assertIn("applied to DEV", notes)

    def test_a_schema_qualified_name_for_another_schema_is_refused(self) -> None:
        folder = self.write_folder("migrations/APP_CODE/2026-01-22_cross-r001", {
            "001-table.sql": "CREATE TABLE OTHER_SCHEMA.cross_t (id NUMBER);\n",
        })

        report = feature_convert.convert_plan(self.root, [folder], feature="demo")

        statement = self.statement(self.folder_record(report, folder), "001-table.sql", 1)
        self.assertEqual("refused", statement["disposition"])
        self.assertEqual("stays a migration", self.folder_record(report, folder)["verdict"])

    def test_missing_folder_argument_is_a_usage_error(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = feature_cli.main(["convert-plan"], repo_root=self.root, environ={})

        self.assertEqual(2, status)
        self.assertIn("migration-folder", stderr.getvalue())

    def test_the_planner_module_is_not_part_of_the_capsule_engine(self) -> None:
        capsule = (Path(feature_convert.__file__).resolve().parent / "release_capsule.py").read_text(encoding="utf-8")
        self.assertNotIn("feature_convert", capsule)


if __name__ == "__main__":
    unittest.main()
