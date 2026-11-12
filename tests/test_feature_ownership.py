from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401
from scripts import baseline, feature_claims, feature_cli, feature_compare, feature_sources
from test_feature_sources import (
    ENVIRON, FUNCTION_SOURCE, PROCEDURE_SOURCE, TRIGGER_SOURCE, TYPE_BODY_SOURCE, TYPE_SOURCE, capture_catalog,
)

LIVE_ENVIRON = {**ENVIRON, "DB_ENVIRONMENT": "development"}
SPEC = ["PACKAGE PKG AS\n", "  PROCEDURE RUN;\n", "END PKG;\n"]
BODY = ["PACKAGE BODY PKG AS\n", "  PROCEDURE RUN IS BEGIN NULL; END;\n", "END PKG;\n"]


def units(spec=SPEC, body=BODY, name="PKG"):
    return {("PACKAGE", name): list(spec), ("PACKAGE BODY", name): list(body)}


def checks(*names: str) -> dict:
    post = []
    for number, name in enumerate(names, start=1):
        for kind in ("PACKAGE", "PACKAGE BODY"):
            post.append({
                "id": f"{name.lower()}-{kind.split()[-1].lower()}-{number}",
                "sql": f"SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER = :target_schema AND OBJECT_NAME = '{name}' AND OBJECT_TYPE = '{kind}' AND STATUS = 'VALID'",
                "expected": 1,
            })
    return {"schemaVersion": 1, "preconditions": [], "postconditions": post}


class FeatureTree(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, feature: str, catalog=None, **options):
        catalog = catalog or capture_catalog(units())
        with patch.object(baseline, "_capture", return_value=catalog) as read:
            paths = feature_sources.capture_package(
                self.root, feature, "APP_CODE", "PKG", environ=LIVE_ENVIRON, **options,
            )
        return paths, read

    def write_checks(self, feature: str, document: dict) -> Path:
        path = self.root / "features" / feature / "APP_CODE" / "checks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path

    def copy_feature(self, source: str, target: str) -> None:
        shutil.copytree(self.root / "features" / source, self.root / "features" / target)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class ClaimTests(FeatureTree):
    def test_stored_units_and_ddl_objects_are_claims_and_data_scripts_are_not(self) -> None:
        self.capture("dispatch")
        self.write("features/dispatch/APP_DATA/ddl.sql", "CREATE TABLE ITEMS (ID NUMBER);\nCREATE SEQUENCE ITEMS_SEQ;\nALTER TABLE ITEMS ADD (COLOR VARCHAR2(20));\n")
        self.write("features/dispatch/APP_DATA/data/001-seed.sql", "INSERT INTO ITEMS (ID) VALUES (1);\nCOMMIT;\n")

        claims, notes = feature_claims.collect_claims(self.root)

        self.assertEqual(notes, [])
        self.assertEqual(
            sorted((claim.schema, claim.kind, claim.name) for claim in claims),
            [
                ("APP_CODE", "PACKAGE", "PKG"),
                ("APP_CODE", "PACKAGE BODY", "PKG"),
                ("APP_DATA", "COLUMN", "ITEMS.COLOR"),
                ("APP_DATA", "SEQUENCE", "ITEMS_SEQ"),
                ("APP_DATA", "TABLE", "ITEMS"),
            ],
        )

    def test_the_same_package_in_two_features_is_a_duplicate(self) -> None:
        self.capture("dispatch")
        self.copy_feature("dispatch", "pickup")

        claims, _notes = feature_claims.collect_claims(self.root)

        found = feature_claims.duplicates(claims)
        self.assertEqual(sorted(found), [("APP_CODE", "PACKAGE", "PKG"), ("APP_CODE", "PACKAGE BODY", "PKG")])
        self.assertEqual({claim.feature for claim in found[("APP_CODE", "PACKAGE", "PKG")]}, {"dispatch", "pickup"})

    def test_two_features_may_add_different_columns_to_one_table_but_not_the_same_column(self) -> None:
        self.write("features/one/APP_DATA/ddl.sql", "ALTER TABLE ITEMS ADD (COLOR VARCHAR2(20));\n")
        self.write("features/two/APP_DATA/ddl.sql", "ALTER TABLE ITEMS ADD (SIZE_CODE VARCHAR2(20));\n")
        claims, _notes = feature_claims.collect_claims(self.root)
        self.assertEqual(feature_claims.duplicates(claims), {})

        self.write("features/two/APP_DATA/ddl.sql", "ALTER TABLE ITEMS ADD (COLOR VARCHAR2(20));\n")
        claims, _notes = feature_claims.collect_claims(self.root)
        self.assertEqual(list(feature_claims.duplicates(claims)), [("APP_DATA", "COLUMN", "ITEMS.COLOR")])

    def test_a_file_that_cannot_be_analysed_is_listed_with_its_feature_and_path(self) -> None:
        self.write("features/one/APP_DATA/ddl.sql", "THIS IS NOT SQL AT ALL (\n")
        claims, unanalysed = feature_claims.collect_claims(self.root)
        self.assertEqual(claims, [])
        self.assertEqual([(item.feature, item.path) for item in unanalysed], [("one", "features/one/APP_DATA/ddl.sql")])
        self.assertTrue(unanalysed[0].reason)

    def test_two_files_of_one_feature_that_define_the_same_package_are_a_same_feature_duplicate(self) -> None:
        self.capture("dispatch")
        spec = self.root / "features/dispatch/APP_CODE/packages/pkg.pks.sql"
        self.write("features/dispatch/APP_CODE/packages/pkg-copy.pks.sql", spec.read_text(encoding="utf-8"))

        claims, _unanalysed = feature_claims.collect_claims(self.root)

        self.assertEqual(list(feature_claims.same_feature_duplicates(claims)), [("dispatch", ("APP_CODE", "PACKAGE", "PKG"))])
        self.assertEqual(feature_claims.duplicates(claims), {})

    def test_one_file_listing_the_same_object_twice_is_not_a_same_feature_duplicate(self) -> None:
        self.write("features/one/APP_DATA/ddl.sql", "CREATE TABLE ITEMS (ID NUMBER);\nCREATE TABLE ITEMS (ID NUMBER);\n")
        claims, _unanalysed = feature_claims.collect_claims(self.root)
        self.assertEqual(feature_claims.same_feature_duplicates(claims), {})

    def test_checked_objects_reads_both_orders_of_the_catalog_columns(self) -> None:
        sql = "SELECT 1 FROM ALL_OBJECTS WHERE OBJECT_TYPE = 'PACKAGE' AND OBJECT_NAME = 'PKG'"
        self.assertEqual(feature_claims.checked_objects(sql), [("PACKAGE", "PKG")])
        sql = "SELECT 1 FROM ALL_OBJECTS WHERE OBJECT_NAME = 'PKG' AND OBJECT_TYPE = 'PACKAGE BODY'"
        self.assertEqual(feature_claims.checked_objects(sql), [("PACKAGE BODY", "PKG")])


class CompareTests(unittest.TestCase):
    def test_three_way_comparison_separates_the_files_own_edits_from_dev_changes(self) -> None:
        base = "PACKAGE PKG AS\n  PROCEDURE RUN;\nEND PKG;\n"
        edited = "PACKAGE PKG AS\n  PROCEDURE RUN2;\nEND PKG;\n"
        dev_added = "PACKAGE PKG AS\n  PROCEDURE RUN;\n  PROCEDURE NEW_ONE;\nEND PKG;\n"
        self.assertEqual(feature_compare.with_baseline(base, base, base).state, feature_compare.EQUAL)
        self.assertEqual(feature_compare.with_baseline(edited, base, base).state, feature_compare.AHEAD)
        self.assertEqual(feature_compare.with_baseline(base, dev_added, base).state, feature_compare.BEHIND)
        drift = feature_compare.with_baseline(edited, dev_added, base)
        self.assertEqual(drift.state, feature_compare.DIVERGED)
        self.assertEqual(drift.missing, ("PROCEDURE NEW_ONE;",))
        self.assertEqual(feature_compare.with_baseline(dev_added, dev_added, base).state, feature_compare.EQUAL)


    def test_comments_blank_lines_whitespace_case_and_the_create_header_are_ignored(self) -> None:
        feature = 'ALTER SESSION SET PLSQL_DEBUG = FALSE;\nCREATE OR REPLACE PACKAGE "APP_CODE"."PKG" AS\n  -- note\n  procedure   run;\n\nEND PKG;\n'
        live = "PACKAGE PKG AS\n  PROCEDURE RUN;\nEND PKG;\n"
        self.assertEqual(feature_compare.compare(feature, live).state, feature_compare.EQUAL)

    def test_states(self) -> None:
        base = "PACKAGE PKG AS\n  PROCEDURE RUN;\nEND PKG;\n"
        more = "PACKAGE PKG AS\n  PROCEDURE RUN;\n  PROCEDURE NEW_ONE;\nEND PKG;\n"
        other = "PACKAGE PKG AS\n  PROCEDURE RUN;\n  PROCEDURE OTHER;\nEND PKG;\n"
        self.assertEqual(feature_compare.compare(base, more).state, feature_compare.BEHIND)
        self.assertEqual(feature_compare.compare(more, base).state, feature_compare.AHEAD)
        self.assertEqual(feature_compare.compare(other, more).state, feature_compare.DIVERGED)
        comparison = feature_compare.compare(more, base)
        self.assertEqual(comparison.only_feature, ("PROCEDURE NEW_ONE;",))
        self.assertEqual(comparison.only_live, ())


class DoctorTests(FeatureTree):
    def run_doctor(self, *arguments: str, environ=None) -> tuple[int, str]:
        import contextlib
        import io

        buffer = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            status = feature_cli.main(["doctor", *arguments], repo_root=self.root, environ=environ if environ is not None else {})
        return status, buffer.getvalue() + errors.getvalue()

    def test_clean_repository_exits_zero(self) -> None:
        self.capture("dispatch")
        status, output = self.run_doctor()
        self.assertEqual(status, 0)
        self.assertIn("result: 0 error(s), 0 warning(s)", output)
        self.assertIn("live: not run", output)

    def test_double_ownership_is_an_error_and_names_both_features_and_files(self) -> None:
        self.capture("dispatch")
        self.copy_feature("dispatch", "pickup")
        status, output = self.run_doctor()
        self.assertEqual(status, 1)
        self.assertIn("DOUBLE OWNERSHIP", output)
        self.assertIn("APP_CODE PACKAGE BODY PKG: held by dispatch, pickup", output)
        self.assertIn("features/dispatch/APP_CODE/packages/pkg.pkb.sql", output)

    def test_feature_option_limits_ownership_findings_to_duplicates_that_involve_it(self) -> None:
        self.capture("dispatch")
        self.copy_feature("dispatch", "pickup")
        self.write("features/other/APP_DATA/ddl.sql", "CREATE TABLE T1 (ID NUMBER);\n")
        status, _output = self.run_doctor("--feature", "other")
        self.assertEqual(status, 0)
        status, _output = self.run_doctor("--feature", "pickup")
        self.assertEqual(status, 1)

    def test_a_file_the_doctor_cannot_read_is_an_error_so_a_double_ownership_hidden_in_it_fails_the_check(self) -> None:
        self.capture("dispatch")
        spec = self.root / "features/dispatch/APP_CODE/packages/pkg.pks.sql"
        hidden = self.root / "features/pickup/APP_CODE/packages/pkg.pks.sql"
        hidden.parent.mkdir(parents=True)
        hidden.write_bytes(spec.read_bytes().replace(b"\n", b"\r\n"))

        status, output = self.run_doctor()

        self.assertEqual(status, 1, output)
        self.assertIn("NOT ANALYSED", output)
        self.assertIn("features/pickup/APP_CODE/packages/pkg.pks.sql: the objects this file defines are unknown", output)
        self.assertIn("LF line endings", output)
        self.assertNotIn("note:", output)
        self.assertIn("result: 1 error(s), 0 warning(s)", output)

    def test_a_file_with_bytes_that_are_not_utf8_is_an_error(self) -> None:
        self.capture("dispatch")
        bad = self.root / "features/pickup/APP_DATA/ddl.sql"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"CREATE TABLE ITEMS (\xff NUMBER);\n")
        status, output = self.run_doctor()
        self.assertEqual(status, 1)
        self.assertIn("NOT ANALYSED", output)
        self.assertIn("features/pickup/APP_DATA/ddl.sql", output)

    def test_the_feature_option_limits_unanalysed_files_to_that_feature(self) -> None:
        self.write("features/other/APP_DATA/ddl.sql", "THIS IS NOT SQL AT ALL (\n")
        status, _output = self.run_doctor("--feature", "pickup")
        self.assertEqual(status, 0)
        status, output = self.run_doctor("--feature", "other")
        self.assertEqual(status, 1)
        self.assertIn("NOT ANALYSED", output)

    def test_two_files_of_one_feature_defining_the_same_object_are_a_duplicate_file_error(self) -> None:
        self.write("features/dispatch/APP_DATA/ddl.sql", "CREATE TABLE ITEMS (ID NUMBER);\n")
        self.write("features/dispatch/APP_DATA/ddl-again.sql", "CREATE TABLE ITEMS (ID NUMBER);\n")
        status, output = self.run_doctor()
        self.assertEqual(status, 1)
        self.assertIn("DUPLICATE FILE", output)
        self.assertIn("two files of feature dispatch define it; keep one", output)
        self.assertIn("features/dispatch/APP_DATA/ddl-again.sql", output)
        status, _output = self.run_doctor("--feature", "pickup")
        self.assertEqual(status, 0)

    def test_orphan_check_is_a_warning_not_an_error(self) -> None:
        self.capture("dispatch")
        self.write_checks("dispatch", checks("PKG", "GONE_PKG"))
        status, output = self.run_doctor()
        self.assertEqual(status, 0)
        self.assertIn("ORPHAN CHECK", output)
        self.assertIn("GONE_PKG", output)
        self.assertNotIn("names PACKAGE PKG,", output)

    def test_live_reports_equal_behind_ahead_diverged_and_missing_files(self) -> None:
        self.capture("dispatch")
        spec_path = self.root / "features/dispatch/APP_CODE/packages/pkg.pks.sql"
        body_path = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"

        status, output = self._live(units())
        self.assertEqual(status, 0, output)
        self.assertIn("2 stored source file(s) compared with DEV", output)

        newer = units(spec=SPEC[:2] + ["  PROCEDURE ADDED_ON_DEV;\n"] + SPEC[2:])
        status, output = self._live(newer)
        self.assertEqual(status, 1)
        self.assertRegex(output, r"BEHIND DEV +features/dispatch/APP_CODE/packages/pkg\.pks\.sql: DEV changed since the last capture: 1 line\(s\) added on DEV are missing from the file, 0 line")

        spec_path.write_text(spec_path.read_text(encoding="utf-8").replace("PROCEDURE RUN;", "PROCEDURE RUN;\n  PROCEDURE UNAPPLIED;"), encoding="utf-8")
        # A file ahead of DEV is new work not compiled yet: reported, but not an error and not a reason to stop a freeze.
        status, output = self._live(units())
        self.assertEqual(status, 0, output)
        self.assertIn("AHEAD OF DEV", output)
        self.assertIn("not on DEV yet", output)

        status, output = self._live(newer)
        self.assertEqual(status, 1)
        self.assertIn("DIVERGED", output)

        body_path.unlink()
        missing = {("PACKAGE", "PKG"): list(SPEC)}
        spec_path.write_text(spec_path.read_text(encoding="utf-8").replace("PROCEDURE RUN;\n  PROCEDURE UNAPPLIED;", "PROCEDURE RUN;"), encoding="utf-8")
        status, output = self._live(missing)
        self.assertEqual(status, 0, output)

    def test_an_edit_to_existing_code_is_new_work_when_dev_has_not_changed_since_the_capture(self) -> None:
        self.capture("dispatch")
        body_path = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        body_path.write_text(body_path.read_text(encoding="utf-8").replace("NULL;", "RETURN;"), encoding="utf-8")
        status, output = self._live(units())
        self.assertEqual(status, 0, output)
        self.assertIn("AHEAD OF DEV", output)

    def test_without_a_retained_baseline_an_edited_line_is_a_divergence(self) -> None:
        self.capture("dispatch")
        shutil.rmtree(self.root / "scratch")
        body_path = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        body_path.write_text(body_path.read_text(encoding="utf-8").replace("NULL;", "RETURN;"), encoding="utf-8")
        status, output = self._live(units())
        self.assertEqual(status, 1)
        self.assertIn("DIVERGED", output)
        self.assertIn("no retained DEV baseline", output)

    def test_a_line_dev_removed_since_the_capture_that_the_file_still_holds_is_an_error(self) -> None:
        self.capture("dispatch")
        removed = units(spec=[SPEC[0], SPEC[2]])
        status, output = self._live(removed)
        self.assertEqual(status, 1)
        self.assertIn("BEHIND DEV", output)
        self.assertIn("1 line(s) removed on DEV are still in it", output)

    def test_dev_changes_the_file_already_contains_are_not_reported(self) -> None:
        self.capture("dispatch")
        newer = units(spec=SPEC[:2] + ["  PROCEDURE ADDED_ON_DEV;\n"] + SPEC[2:])
        self.capture("dispatch", catalog=capture_catalog(newer), refresh=True)
        status, output = self._live(newer)
        self.assertEqual(status, 0, output)
        self.assertNotIn("DEV changed", output)

    def test_a_package_that_is_not_on_dev_is_an_error(self) -> None:
        self.capture("dispatch")
        status, output = self._live({("PACKAGE", "OTHER"): ["PACKAGE OTHER AS\n", "END OTHER;\n"]})
        self.assertEqual(status, 1)
        self.assertIn("NOT ON DEV", output)

    def test_an_accepted_difference_is_reported_as_accepted_only_while_the_lines_match(self) -> None:
        self.capture("dispatch")
        body_path = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        body_path.write_text(body_path.read_text(encoding="utf-8").replace("NULL;", "RETURN;"), encoding="utf-8")
        accepted = self.write("features/dispatch/accepted-differences.json", json.dumps([{
            "schema": "APP_CODE", "type": "PACKAGE BODY", "name": "PKG",
            "decision": "owner kept the production rule, 2026-01-01",
            "featureOnly": ["PROCEDURE RUN IS BEGIN RETURN; END;"], "liveOnly": ["PROCEDURE RUN IS BEGIN NULL; END;"],
        }]))

        status, output = self._live(units())
        self.assertEqual(status, 0, output)
        self.assertIn("ACCEPTED", output)
        self.assertIn("owner kept the production rule", output)

        # Without a retained DEV baseline the file's own edit cannot be told from a DEV change: a stale entry fails.
        shutil.rmtree(self.root / "scratch")
        entry = json.loads(accepted.read_text(encoding="utf-8"))
        entry[0]["featureOnly"] = ["SOMETHING ELSE"]
        accepted.write_text(json.dumps(entry), encoding="utf-8")
        status, output = self._live(units())
        self.assertEqual(status, 1)
        self.assertIn("no longer matches", output)
        self.assertIn("no retained DEV baseline", output)

    def test_an_invalid_accepted_differences_file_stops_the_live_check(self) -> None:
        self.capture("dispatch")
        body_path = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        body_path.write_text(body_path.read_text(encoding="utf-8").replace("NULL;", "RETURN;"), encoding="utf-8")
        self.write("features/dispatch/accepted-differences.json", json.dumps([{"schema": "APP_CODE"}]))
        status, output = self._live(units())
        self.assertEqual(status, 2)
        self.assertIn("fields must be exactly", output)

    def test_live_without_a_dev_profile_does_not_claim_to_have_run(self) -> None:
        self.capture("dispatch")
        status, output = self.run_doctor("--live", environ={})
        self.assertEqual(status, 2)
        self.assertIn("the live check did not run", output)

    def test_live_failure_of_the_database_read_is_exit_two(self) -> None:
        self.capture("dispatch")
        with patch.object(baseline, "_capture", side_effect=baseline.BaselineError("connection refused")):
            status, output = self.run_doctor("--live", environ=LIVE_ENVIRON)
        self.assertEqual(status, 2)
        self.assertIn("connection refused", output)

    def test_live_never_runs_without_the_flag(self) -> None:
        self.capture("dispatch")
        with patch.object(baseline, "_capture") as read:
            status, _output = self.run_doctor(environ=LIVE_ENVIRON)
        self.assertEqual(status, 0)
        read.assert_not_called()

    def _live(self, catalog_units) -> tuple[int, str]:
        with patch.object(baseline, "_capture", return_value=capture_catalog(catalog_units)):
            return self.run_doctor("--live", environ=LIVE_ENVIRON)


class MoveAndRefreshTests(FeatureTree):
    def test_a_package_another_feature_holds_is_not_captured_a_second_time(self) -> None:
        self.capture("dispatch")
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "held by feature dispatch.*--move-from dispatch"):
            self.capture("pickup")
        self.assertFalse((self.root / "features/pickup").exists())

    def test_move_from_must_name_the_holder_and_not_the_receiver(self) -> None:
        self.capture("dispatch")
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "feature other does not hold"):
            self.capture("pickup", move_from="other")
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "receives the package"):
            self.capture("pickup", move_from="pickup")
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "keep-file only applies to a move"):
            self.capture("dispatch", keep_file=True, refresh=True)

    def test_a_move_of_an_equal_file_takes_the_dev_version_and_removes_the_old_files_checks_and_baseline(self) -> None:
        self.capture("dispatch")
        self.write_checks("dispatch", checks("PKG", "KEEP_PKG"))
        report: list[str] = []

        paths, _read = self.capture("pickup", move_from="dispatch", report=report)

        self.assertEqual([path.name for path in paths], ["pkg.pks.sql", "pkg.pkb.sql"])
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertFalse((self.root / "features/dispatch/APP_CODE/packages/pkg.pks.sql").exists())
        self.assertFalse((self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql").exists())
        remaining = json.loads((self.root / "features/dispatch/APP_CODE/checks.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [check["id"] for check in remaining["postconditions"]],
            ["keep_pkg-package-2", "keep_pkg-body-2"],
        )
        self.assertFalse((self.root / "scratch/feature-source/dispatch/APP_CODE/capture-pkg.json").exists())
        self.assertTrue((self.root / "scratch/feature-source/pickup/APP_CODE/capture-pkg.json").is_file())
        self.assertTrue(any("removed 2 check(s) of dispatch" in line for line in report), report)
        claims, _notes = feature_claims.collect_claims(self.root)
        self.assertEqual(feature_claims.duplicates(claims), {})
        self.assertEqual({claim.feature for claim in claims}, {"pickup"})

    def test_a_move_of_a_file_that_is_behind_dev_proceeds_and_reports_what_it_takes(self) -> None:
        self.capture("dispatch")
        newer = units(body=BODY[:2] + ["  PROCEDURE ADDED_ON_DEV IS BEGIN NULL; END;\n"] + BODY[2:])
        report: list[str] = []

        paths, _read = self.capture("pickup", catalog=capture_catalog(newer), move_from="dispatch", report=report)

        self.assertIn("ADDED_ON_DEV", paths[1].read_text(encoding="utf-8"))
        self.assertTrue(any("behind dev, 1 line(s) only on DEV are taken, 0 line(s) only in the old file are dropped" in line for line in report), report)

    def test_a_move_of_a_file_with_unapplied_lines_is_refused_and_changes_nothing(self) -> None:
        self.capture("dispatch")
        old_body = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        old_body.write_text(old_body.read_text(encoding="utf-8").replace("END PKG;", "  PROCEDURE UNAPPLIED IS BEGIN NULL; END;\nEND PKG;"), encoding="utf-8")
        before = old_body.read_bytes()

        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "(?s)move refused.*1 line\\(s\\) that DEV does not have.*UNAPPLIED.*--discard-unapplied.*--keep-file"):
            self.capture("pickup", move_from="dispatch")

        self.assertEqual(old_body.read_bytes(), before)
        self.assertFalse((self.root / "features/pickup").exists())

    def test_discard_unapplied_erases_the_lines_knowingly(self) -> None:
        self.capture("dispatch")
        old_body = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        old_body.write_text(old_body.read_text(encoding="utf-8").replace("END PKG;", "  PROCEDURE UNAPPLIED IS BEGIN NULL; END;\nEND PKG;"), encoding="utf-8")

        paths, _read = self.capture("pickup", move_from="dispatch", discard_unapplied=True)

        self.assertNotIn("UNAPPLIED", paths[1].read_text(encoding="utf-8"))
        self.assertFalse(old_body.exists())

    def test_keep_file_moves_the_bytes_unchanged_without_reading_dev_and_takes_the_accepted_difference_along(self) -> None:
        self.capture("dispatch")
        old_body = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        old_body.write_text(old_body.read_text(encoding="utf-8").replace("NULL;", "RETURN;"), encoding="utf-8")
        moved_bytes = old_body.read_bytes()
        self.write_checks("dispatch", checks("PKG"))
        entry = {
            "schema": "APP_CODE", "type": "PACKAGE BODY", "name": "PKG", "decision": "owner decision",
            "featureOnly": ["PROCEDURE RUN IS BEGIN RETURN; END;"], "liveOnly": ["PROCEDURE RUN IS BEGIN NULL; END;"],
        }
        self.write("features/dispatch/accepted-differences.json", json.dumps([entry]))

        with patch.object(baseline, "_capture") as read:
            paths = feature_sources.capture_package(
                self.root, "pickup", "APP_CODE", "PKG", environ=LIVE_ENVIRON, move_from="dispatch", keep_file=True,
            )

        read.assert_not_called()
        self.assertEqual(paths[1].read_bytes(), moved_bytes)
        self.assertFalse(old_body.exists())
        self.assertFalse((self.root / "features/dispatch/accepted-differences.json").exists())
        moved = json.loads((self.root / "features/pickup/accepted-differences.json").read_text(encoding="utf-8"))
        self.assertEqual(moved, [entry])

    def test_a_dev_move_drops_the_accepted_difference_because_the_dev_file_replaces_it(self) -> None:
        self.capture("dispatch")
        entry = {
            "schema": "APP_CODE", "type": "PACKAGE BODY", "name": "PKG", "decision": "owner decision",
            "featureOnly": [], "liveOnly": [],
        }
        self.write("features/dispatch/accepted-differences.json", json.dumps([entry]))
        report: list[str] = []
        self.capture("pickup", move_from="dispatch", report=report)
        self.assertFalse((self.root / "features/dispatch/accepted-differences.json").exists())
        self.assertFalse((self.root / "features/pickup/accepted-differences.json").exists())
        self.assertTrue(any("dropped 1 accepted difference" in line for line in report), report)

    def test_a_move_of_a_package_that_is_not_on_dev_points_to_keep_file(self) -> None:
        self.capture("dispatch")
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "not found.*--keep-file"):
            self.capture("pickup", catalog=capture_catalog({("PACKAGE", "OTHER"): ["PACKAGE OTHER AS\n", "END OTHER;\n"]}), move_from="dispatch")
        self.assertTrue((self.root / "features/dispatch/APP_CODE/packages/pkg.pks.sql").is_file())

    def test_a_refresh_that_erases_nothing_proceeds_without_the_flag(self) -> None:
        self.capture("dispatch")
        newer = units(body=BODY[:2] + ["  PROCEDURE ADDED_ON_DEV IS BEGIN NULL; END;\n"] + BODY[2:])
        paths, _read = self.capture("dispatch", catalog=capture_catalog(newer), refresh=True)
        self.assertIn("ADDED_ON_DEV", paths[1].read_text(encoding="utf-8"))

    def test_a_refresh_names_the_accepted_difference_it_would_erase(self) -> None:
        self.capture("dispatch")
        old_body = self.root / "features/dispatch/APP_CODE/packages/pkg.pkb.sql"
        old_body.write_text(old_body.read_text(encoding="utf-8").replace("NULL;", "RETURN;"), encoding="utf-8")
        self.write("features/dispatch/accepted-differences.json", json.dumps([{
            "schema": "APP_CODE", "type": "PACKAGE BODY", "name": "PKG", "decision": "kept on purpose",
            "featureOnly": ["X"], "liveOnly": ["Y"],
        }]))
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "(?s)refresh refused.*an accepted difference is recorded for it: kept on purpose"):
            self.capture("dispatch", refresh=True)


class FreezeGateTests(FeatureTree):
    def freeze(self, *extra: str, environ=None, steps_features=("pickup",)) -> tuple[int, str]:
        import contextlib
        import io
        from types import SimpleNamespace

        manifest_path = self.root / "releases/current.json"
        steps = tuple(
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": [f"features/{name}/APP_CODE/packages/pkg.pks.sql"]}
            for name in steps_features
        )
        capsule = SimpleNamespace(
            payload_digest="a" * 64, engine_digest="b" * 64, source_commit="c" * 40,
            root=self.root / "scratch/release-capsules/example",
        )
        manifest = SimpleNamespace(path=manifest_path, steps=steps)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(feature_cli.rollout, "load_manifest", return_value=manifest), \
             patch.object(feature_cli, "load_feature_checkpoint", return_value=object()), \
             patch.object(feature_cli.release_capsule, "build_capsule", return_value=capsule) as build, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = feature_cli.main(["freeze", str(manifest_path), *extra], repo_root=self.root, environ=environ if environ is not None else {})
        self.build = build
        return status, out.getvalue() + err.getvalue()

    def record(self) -> dict:
        return json.loads((self.root / "scratch/freeze-checks/example.json").read_text(encoding="utf-8"))

    def test_a_duplicate_with_a_selected_feature_stops_the_freeze_before_a_capsule_is_built(self) -> None:
        self.capture("dispatch")
        self.copy_feature("dispatch", "pickup")
        status, output = self.freeze()
        self.assertEqual(status, 1)
        self.assertIn("DOUBLE OWNERSHIP", output)
        self.build.assert_not_called()

    def test_a_duplicate_between_unselected_features_does_not_stop_the_freeze(self) -> None:
        self.capture("dispatch")
        self.copy_feature("dispatch", "legacy")
        self.copy_feature("dispatch", "other")
        self.write("features/pickup/APP_DATA/ddl.sql", "CREATE TABLE ONLY_HERE (ID NUMBER);\n")
        status, _output = self.freeze()
        self.assertEqual(status, 0)
        self.build.assert_called_once()
        self.assertIn("not run", self.record()["live"])

    def test_a_file_that_cannot_be_analysed_refuses_the_freeze_even_when_its_feature_is_not_selected(self) -> None:
        self.capture("pickup")
        self.write("features/dispatch/APP_DATA/ddl.sql", "THIS IS NOT SQL AT ALL (\n")
        with patch.object(baseline, "_capture") as read:
            status, output = self.freeze(environ=LIVE_ENVIRON)
        self.assertEqual(status, 1)
        self.assertIn("NOT ANALYSED", output)
        self.assertIn("features/dispatch/APP_DATA/ddl.sql", output)
        self.assertIn("analysis - refused", output)
        read.assert_not_called()
        self.build.assert_not_called()
        self.assertFalse((self.root / "scratch/freeze-checks/example.json").exists())

    def test_two_files_of_a_selected_feature_that_define_one_object_refuse_the_freeze(self) -> None:
        self.capture("pickup")
        self.write("features/pickup/APP_DATA/ddl.sql", "CREATE TABLE ITEMS (ID NUMBER);\n")
        self.write("features/pickup/APP_DATA/ddl-again.sql", "CREATE TABLE ITEMS (ID NUMBER);\n")
        status, output = self.freeze()
        self.assertEqual(status, 1)
        self.assertIn("DUPLICATE FILE", output)
        self.assertIn("features/pickup/APP_DATA/ddl-again.sql", output)
        self.build.assert_not_called()

    def test_a_same_feature_duplicate_in_an_unselected_feature_does_not_stop_the_freeze(self) -> None:
        self.capture("pickup")
        self.write("features/legacy/APP_DATA/ddl.sql", "CREATE TABLE ITEMS (ID NUMBER);\n")
        self.write("features/legacy/APP_DATA/ddl-again.sql", "CREATE TABLE ITEMS (ID NUMBER);\n")
        status, output = self.freeze()
        self.assertEqual(status, 0, output)
        self.build.assert_called_once()

    def test_a_freeze_that_passes_records_that_every_feature_file_was_analysed(self) -> None:
        self.capture("pickup")
        status, output = self.freeze()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.record()["analysis"], "every feature SQL file was analysed")
        self.assertIn("analysis - every feature SQL file was analysed", output)

    def test_a_partial_live_comparison_prints_which_schema_was_not_compared(self) -> None:
        self.capture("pickup")
        package = self.root / "features/pickup/APP_DATA/packages/pkg2.pks.sql"
        package.parent.mkdir(parents=True)
        package.write_text("CREATE OR REPLACE PACKAGE PKG2 AS\n  PROCEDURE RUN;\nEND PKG2;\n/\n", encoding="utf-8")
        with patch.object(baseline, "_capture", return_value=capture_catalog(units())):
            status, output = self.freeze(environ=LIVE_ENVIRON)
        self.assertEqual(status, 0, output)
        self.assertIn("note: no DEV profile for APP_DATA; their stored source files were not compared with DEV", output)
        self.assertEqual(self.record()["live"], "2 stored source file(s) compared with DEV, 0 accepted difference(s); not compared (no DEV profile): APP_DATA")

    def test_without_a_dev_profile_the_freeze_says_the_live_comparison_did_not_run(self) -> None:
        self.capture("pickup")
        status, output = self.freeze()
        self.assertEqual(status, 0)
        self.assertEqual(self.record()["live"], "not run: no DEV profile for APP_CODE")
        self.assertIn("live comparison with DEV not run", output)

    def test_a_file_behind_dev_stops_the_freeze_and_a_reason_skips_the_live_check(self) -> None:
        self.capture("pickup")
        newer = units(spec=SPEC[:2] + ["  PROCEDURE ADDED_ON_DEV;\n"] + SPEC[2:])
        with patch.object(baseline, "_capture", return_value=capture_catalog(newer)):
            status, output = self.freeze(environ=LIVE_ENVIRON)
        self.assertEqual(status, 1)
        self.assertIn("BEHIND DEV", output)
        self.build.assert_not_called()

        with patch.object(baseline, "_capture") as read:
            status, output = self.freeze("--no-live", "teammate compiled it on DEV, refresh follows", environ=LIVE_ENVIRON)
        self.assertEqual(status, 0)
        read.assert_not_called()
        self.assertEqual(self.record()["live"], "skipped: teammate compiled it on DEV, refresh follows")

    def test_a_file_ahead_of_dev_is_new_work_and_does_not_stop_the_freeze(self) -> None:
        self.capture("pickup")
        spec = self.root / "features/pickup/APP_CODE/packages/pkg.pks.sql"
        spec.write_text(spec.read_text(encoding="utf-8").replace("PROCEDURE RUN;", "PROCEDURE RUN;\n  PROCEDURE NEW_WORK;"), encoding="utf-8")
        with patch.object(baseline, "_capture", return_value=capture_catalog(units())):
            status, output = self.freeze(environ=LIVE_ENVIRON)
        self.assertEqual(status, 0, output)
        self.build.assert_called_once()

    def test_an_equal_file_freezes_and_the_record_says_how_many_files_were_compared(self) -> None:
        self.capture("pickup")
        with patch.object(baseline, "_capture", return_value=capture_catalog(units())):
            status, _output = self.freeze(environ=LIVE_ENVIRON)
        self.assertEqual(status, 0)
        self.assertEqual(self.record()["live"], "2 stored source file(s) compared with DEV, 0 accepted difference(s)")

    def test_an_unreachable_dev_database_stops_the_freeze_with_the_way_out(self) -> None:
        self.capture("pickup")
        with patch.object(baseline, "_capture", side_effect=baseline.BaselineError("connection refused")):
            status, output = self.freeze(environ=LIVE_ENVIRON)
        self.assertEqual(status, 2)
        self.assertIn("--no-live", output)
        self.build.assert_not_called()

    def test_an_empty_no_live_reason_is_refused(self) -> None:
        self.capture("pickup")
        status, output = self.freeze("--no-live", "  ")
        self.assertEqual(status, 2)
        self.assertIn("--no-live needs a reason", output)


class StoredUnitOwnershipTests(FeatureTree):
    """Triggers, functions, procedures and types are claimed, held, moved, compared with DEV and frozen like packages."""

    def capture_unit(self, feature: str, group: str, name: str, units: dict, **options):
        with patch.object(baseline, "_capture", return_value=capture_catalog(units)):
            return feature_sources.capture_unit(self.root, feature, "APP_CODE", group, name, environ=LIVE_ENVIRON, **options)

    def capture_all(self, feature: str = "shop") -> list[str]:
        paths = [
            *self.capture_unit(feature, "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE}),
            *self.capture_unit(feature, "function", "GET_ONE", {("FUNCTION", "GET_ONE"): FUNCTION_SOURCE}),
            *self.capture_unit(feature, "procedure", "RUN_IT", {("PROCEDURE", "RUN_IT"): PROCEDURE_SOURCE}),
            *self.capture_unit(feature, "type", "ORDER_T", {("TYPE", "ORDER_T"): TYPE_SOURCE, ("TYPE BODY", "ORDER_T"): TYPE_BODY_SOURCE}),
        ]
        return [path.relative_to(self.root).as_posix() for path in paths]

    def dev_units(self) -> dict:
        return {
            ("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE,
            ("FUNCTION", "GET_ONE"): FUNCTION_SOURCE,
            ("PROCEDURE", "RUN_IT"): PROCEDURE_SOURCE,
            ("TYPE", "ORDER_T"): TYPE_SOURCE,
            ("TYPE BODY", "ORDER_T"): TYPE_BODY_SOURCE,
        }

    def doctor(self, *arguments: str, catalog_units: dict | None = None) -> tuple[int, str]:
        import contextlib
        import io

        buffer = io.StringIO()
        with patch.object(baseline, "_capture", return_value=capture_catalog(catalog_units or self.dev_units())), \
             contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            status = feature_cli.main(["doctor", *arguments], repo_root=self.root, environ=LIVE_ENVIRON)
        return status, buffer.getvalue()

    def test_every_stored_unit_type_is_claimed_as_its_own_object(self) -> None:
        self.capture_all()
        claims, unanalysed = feature_claims.collect_claims(self.root)
        self.assertEqual(unanalysed, [])
        self.assertEqual(
            sorted((claim.kind, claim.name) for claim in claims),
            [("FUNCTION", "GET_ONE"), ("PROCEDURE", "RUN_IT"), ("TRIGGER", "ORDERS_TRG"), ("TYPE", "ORDER_T"), ("TYPE BODY", "ORDER_T")],
        )

    def test_a_checkpoint_loads_each_captured_unit_as_its_own_stored_source_unit(self) -> None:
        from scripts import feature_manifest

        paths = self.capture_all()
        self.write_checks("shop", {"schemaVersion": 1, "preconditions": [], "postconditions": [
            {"id": "shop-ready", "sql": "SELECT 1 FROM dual", "expected": 1},
        ]})
        checkpoint = feature_manifest.load_feature_checkpoint(
            self.root, {"type": "feature-migrate", "schema": "APP_CODE", "inputs": paths},
        )
        feature_manifest.validate_release_units([checkpoint])
        self.assertEqual(
            sorted((item.category, item.unit[1], item.unit[2]) for item in checkpoint.inputs),
            [
                ("stored-source", "FUNCTION", "GET_ONE"),
                ("stored-source", "PROCEDURE", "RUN_IT"),
                ("stored-source", "TRIGGER", "ORDERS_TRG"),
                ("stored-source", "TYPE", "ORDER_T"),
                ("stored-source", "TYPE BODY", "ORDER_T"),
            ],
        )

    def test_a_trigger_another_feature_holds_needs_move_from(self) -> None:
        self.capture_all("shop")
        with self.assertRaisesRegex(feature_sources.FeatureSourcesError, "trigger ORDERS_TRG is held by feature shop.*--move-from shop"):
            self.capture_unit("pickup", "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE})
        self.assertFalse((self.root / "features/pickup").exists())

    def test_doctor_compares_every_captured_unit_with_dev_and_reports_an_equal_one_as_clean(self) -> None:
        self.capture_all()
        status, output = self.doctor("--live")
        self.assertEqual(status, 0, output)
        self.assertIn("5 stored source file(s) compared with DEV", output)

    def test_a_feature_holding_only_a_standalone_procedure_reports_stored_source_files_in_the_doctor_summary(self) -> None:
        self.capture_unit("shop", "procedure", "RUN_IT", {("PROCEDURE", "RUN_IT"): PROCEDURE_SOURCE})
        status, output = self.doctor("--live")
        self.assertEqual(status, 0, output)
        self.assertIn("live: 1 stored source file(s) compared with DEV, 0 accepted difference(s)", output)
        self.assertNotIn("package file", output)

    def test_a_trigger_that_dev_changed_since_the_capture_is_behind_dev(self) -> None:
        self.capture_all()
        # Comment-only changes are ignored by the comparison, so the DEV change adds code.
        newer = {**self.dev_units(), ("TRIGGER", "ORDERS_TRG"): [*TRIGGER_SOURCE[:-1], "  DEV_ONLY_LINE := 1;\n", TRIGGER_SOURCE[-1]]}
        status, output = self.doctor("--live", catalog_units=newer)
        self.assertEqual(status, 1)
        self.assertRegex(output, r"BEHIND DEV +features/shop/APP_CODE/triggers/orders_trg\.trg\.sql")

    def test_a_function_or_trigger_that_is_not_on_dev_is_an_error(self) -> None:
        self.capture_all()
        without = {key: value for key, value in self.dev_units().items() if key not in {("FUNCTION", "GET_ONE"), ("TRIGGER", "ORDERS_TRG")}}
        status, output = self.doctor("--live", catalog_units=without)
        self.assertEqual(status, 1)
        self.assertIn("NOT ON DEV", output)
        self.assertIn("FUNCTION GET_ONE was not found in APP_CODE on DEV", output)
        self.assertIn("TRIGGER ORDERS_TRG was not found in APP_CODE on DEV", output)

    def test_a_type_body_on_dev_without_its_specification_is_not_on_dev_for_both_files_as_packages_are(self) -> None:
        self.capture_all()
        units = {key: value for key, value in self.dev_units().items() if key != ("TYPE", "ORDER_T")}
        status, output = self.doctor("--live", catalog_units=units)
        self.assertEqual(status, 1)
        self.assertIn("TYPE ORDER_T was not found in APP_CODE on DEV", output)
        self.assertIn("TYPE BODY ORDER_T was not found in APP_CODE on DEV", output)

    def test_freeze_gate_selects_each_stored_unit_type_and_refuses_a_file_that_dev_would_undo(self) -> None:
        paths = self.capture_all()
        steps = [{"type": "feature-migrate", "schema": "APP_CODE", "inputs": paths}]
        with patch.object(baseline, "_capture", return_value=capture_catalog(self.dev_units())):
            refused, record = feature_cli._freeze_gate(self.root, LIVE_ENVIRON, steps, None)
        self.assertIsNone(refused)
        self.assertEqual(record["live"], "5 stored source file(s) compared with DEV, 0 accepted difference(s)")

        import contextlib
        import io

        newer = {**self.dev_units(), ("FUNCTION", "GET_ONE"): [*FUNCTION_SOURCE[:-1], "  DEV_ONLY_LINE := 1;\n", FUNCTION_SOURCE[-1]]}
        printed = io.StringIO()
        with patch.object(baseline, "_capture", return_value=capture_catalog(newer)), contextlib.redirect_stdout(printed):
            refused, _record = feature_cli._freeze_gate(self.root, LIVE_ENVIRON, steps, None)
        self.assertEqual(refused, 1)
        self.assertRegex(printed.getvalue(), r"BEHIND DEV +features/shop/APP_CODE/functions/get_one\.fnc\.sql")

    def test_a_moved_trigger_leaves_no_duplicate_claim_and_its_old_checks_are_removed(self) -> None:
        self.capture_all("shop")
        self.write_checks("shop", {"schemaVersion": 1, "preconditions": [], "postconditions": [{
            "id": "orders-trg", "sql": "SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER = :target_schema AND OBJECT_NAME = 'ORDERS_TRG' AND OBJECT_TYPE = 'TRIGGER' AND STATUS = 'VALID'",
            "expected": 1,
        }]})
        self.capture_unit("pickup", "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE}, move_from="shop")

        claims, _unanalysed = feature_claims.collect_claims(self.root)
        self.assertEqual(feature_claims.duplicates(claims), {})
        self.assertEqual({claim.feature for claim in claims if claim.kind == "TRIGGER"}, {"pickup"})
        remaining = json.loads((self.root / "features/shop/APP_CODE/checks.json").read_text(encoding="utf-8"))
        self.assertEqual(remaining["postconditions"], [])


if __name__ == "__main__":
    unittest.main()
