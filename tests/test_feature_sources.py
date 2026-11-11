from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401
from scripts import baseline


IDENTITY = {
    "session_user": "APP_CODE",
    "current_schema": "APP_CODE",
    "db_name": "DEVDB",
    "db_unique_name": "DEVDB_UNIQUE",
    "service_name": "dev.example",
    "container_id": "3",
    "container_name": "PDBDEV",
    "edition": "ORA$BASE",
    "database_version": "19.0.0.0.0",
}
ENVIRON = {
    "CODE_SQLCL_CONNECTION": "dev_code",
    "CODE_EXPECTED_USER": "APP_CODE",
    "CODE_SCHEMA": "APP_CODE",
}


def setting(name: str, object_type: str) -> dict:
    return {
        "name": name,
        "type": object_type,
        "plsql_optimize_level": 2,
        "plsql_code_type": "INTERPRETED",
        "plsql_debug": "FALSE",
        "plsql_warnings": "ENABLE:ALL",
        "plsql_ccflags": "FEATURE_X:TRUE",
        "nls_length_semantics": "CHAR",
        "plscope_settings": "IDENTIFIERS:ALL",
    }


def capture_catalog(units: dict[tuple[str, str], list[str]], settings: list[dict] | None = None) -> dict:
    source_rows = [
        {"name": name, "type": object_type, "line": number, "text": text}
        for (object_type, name), lines in units.items()
        for number, text in enumerate(lines, start=1)
    ]
    return {
        "coverage": {"sections": {"baseline-source": {"complete": True}, "baseline-settings": {"complete": True}}},
        "unavailable": {},
        "identity": dict(IDENTITY),
        "sections": {"baseline-source": source_rows, "baseline-settings": settings or [setting(name, kind) for kind, name in units]},
    }


class FeatureSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        try:
            from scripts import feature_sources
        except ImportError as error:
            self.fail(f"feature source capture is not implemented: {error}")
        self.feature_sources = feature_sources

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_capture_writes_complete_spec_and_body_with_settings_to_stable_paths(self) -> None:
        units = {
            ("PACKAGE", "DISPATCH_PKG"): ["PACKAGE DISPATCH_PKG AS\n", "  PROCEDURE RUN;\n", "END DISPATCH_PKG;\n"],
            ("PACKAGE BODY", "DISPATCH_PKG"): ["PACKAGE BODY DISPATCH_PKG AS\n", "  PROCEDURE RUN IS BEGIN NULL; END;\n", "END DISPATCH_PKG;\n"],
        }
        catalog = capture_catalog(units)
        with patch.object(baseline, "_capture", return_value=catalog) as capture:
            paths = self.feature_sources.capture_package(
                self.root, "dispatch", "APP_CODE", "DISPATCH_PKG", environ=ENVIRON,
            )

        self.assertEqual([path.relative_to(self.root).as_posix() for path in paths], [
            "features/dispatch/APP_CODE/packages/dispatch_pkg.pks.sql",
            "features/dispatch/APP_CODE/packages/dispatch_pkg.pkb.sql",
        ])
        self.assertIn('CREATE OR REPLACE PACKAGE "APP_CODE"."DISPATCH_PKG"', paths[0].read_text(encoding="utf-8"))
        self.assertIn("PLSQL_CCFLAGS = 'FEATURE_X:TRUE'", paths[0].read_text(encoding="utf-8"))
        self.assertIn("PROCEDURE RUN IS BEGIN NULL; END;", paths[1].read_text(encoding="utf-8"))
        self.assertEqual(capture.call_args.args[1:4], ("dev", "APP_CODE", ("baseline-source", "baseline-settings")))
        self.assertEqual(tuple(capture.call_args.kwargs["baseline_prefixes"]), ("DISPATCH_PKG",))
        self.assertTrue((self.root / "scratch" / "feature-source" / "dispatch" / "APP_CODE").is_dir())
        self.assertFalse((self.root / "migrations").exists())

    def test_capture_supports_a_spec_only_package(self) -> None:
        units = {("PACKAGE", "SPEC_ONLY"): ["PACKAGE SPEC_ONLY AS\n", "  PROCEDURE RUN;\n", "END SPEC_ONLY;\n"]}
        with patch.object(baseline, "_capture", return_value=capture_catalog(units)):
            paths = self.feature_sources.capture_package(
                self.root, "dispatch", "APP_CODE", "SPEC_ONLY", environ=ENVIRON,
            )

        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].name.endswith(".pks.sql"))
        self.assertFalse(paths[0].with_name(paths[0].name.replace(".pks.sql", ".pkb.sql")).exists())

    def test_capture_preserves_quoted_unicode_dollar_names_and_source_lines(self) -> None:
        name = "Åpkg$X"
        units = {
            ("PACKAGE", name): [
                f'PACKAGE "{name}" AS\n',
                "  PROCEDURE RUN;\n",
                "   \n",
                "  -- keep-this-trailing-\n",
                "END;\n",
            ],
        }
        with patch.object(baseline, "_capture", return_value=capture_catalog(units)) as capture:
            (path,) = self.feature_sources.capture_package(
                self.root, "dispatch", "APP_CODE", f'"{name}"', environ=ENVIRON,
            )

        source = path.read_bytes()
        self.assertEqual(path.name, "quoted-%C3%85pkg$X.pks.sql")
        self.assertIn('"APP_CODE"."Åpkg$X"'.encode(), source)
        self.assertIn(b"   \n", source)
        self.assertIn(b"-- keep-this-trailing-\n", source)
        self.assertEqual(capture.call_args.kwargs["baseline_prefixes"], [])

    def test_capture_uses_bounded_paths_for_long_ascii_and_quoted_unicode_names(self) -> None:
        names = ("A" * 128, "Å" * 60)
        catalogs = [
            capture_catalog({("PACKAGE", name): [f'PACKAGE "{name}" AS\n', "END;\n"]})
            for name in names
        ]
        with patch.object(baseline, "_capture", side_effect=catalogs):
            paths = [
                self.feature_sources.capture_package(
                    self.root, "dispatch", "APP_CODE", name if name.isascii() else f'"{name}"', environ=ENVIRON,
                )[0]
                for name in names
            ]

        metadata_paths = sorted((self.root / "scratch/feature-source/dispatch/APP_CODE").glob("capture-*.json"))
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(metadata_paths), 2)
        for path in (*paths, *metadata_paths):
            self.assertTrue(path.is_file())
            self.assertLessEqual(len(path.name.encode("utf-8")), 255)
        self.assertEqual(len({path.name for path in paths}), 2)
        self.assertEqual(len({path.name for path in metadata_paths}), 2)

    def test_default_capture_refuses_edits_and_refresh_recaptures_explicitly(self) -> None:
        units = {("PACKAGE", "PKG"): ["PACKAGE PKG AS\n", "  PROCEDURE RUN;\n", "END PKG;\n"]}
        catalog = capture_catalog(units)
        with patch.object(baseline, "_capture", return_value=catalog):
            (path,) = self.feature_sources.capture_package(self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON)
            path.write_text(path.read_text(encoding="utf-8").replace("PROCEDURE RUN", "PROCEDURE EDITED"), encoding="utf-8")
            edited = path.read_bytes()
            with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "refresh"):
                self.feature_sources.capture_package(self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON)
            self.assertEqual(path.read_bytes(), edited)
            # A refresh would erase a line DEV does not have: it stops unless the developer discards it knowingly.
            with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "(?s)refresh refused.*PROCEDURE EDITED"):
                self.feature_sources.capture_package(
                    self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON, refresh=True,
                )
            self.assertEqual(path.read_bytes(), edited)
            refreshed = self.feature_sources.capture_package(
                self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON, refresh=True, discard_unapplied=True,
            )

        self.assertEqual(refreshed, (path,))
        self.assertIn(b"PROCEDURE RUN", path.read_bytes())
        self.assertNotIn(b"PROCEDURE EDITED", path.read_bytes())

    def test_multi_unit_capture_failure_leaves_existing_feature_files_byte_identical(self) -> None:
        units = {
            ("PACKAGE", "PKG"): ["PACKAGE PKG AS\n", "  PROCEDURE RUN;\n", "END PKG;\n"],
            ("PACKAGE BODY", "PKG"): ["PACKAGE BODY PKG AS\n", "  PROCEDURE RUN IS BEGIN NULL; END;\n", "END PKG;\n"],
        }
        catalog = capture_catalog(units)
        with patch.object(baseline, "_capture", return_value=catalog):
            paths = self.feature_sources.capture_package(self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON)
            before = [path.read_bytes() for path in paths]

        broken = capture_catalog(units, settings=[setting("PKG", "PACKAGE")])
        with patch.object(baseline, "_capture", return_value=broken):
            with self.assertRaises(self.feature_sources.FeatureSourcesError):
                self.feature_sources.capture_package(
                    self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON, refresh=True,
                )

        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_stale_mirror_is_ignored_and_first_capture_diff_uses_live_source(self) -> None:
        units = {("PACKAGE", "PKG"): ["PACKAGE PKG AS\n", "  PROCEDURE LIVE;\n", "END PKG;\n"]}
        mirror = self.root / "database" / "APP_CODE" / "packages" / "PKG_SPEC.sql"
        mirror.parent.mkdir(parents=True)
        mirror.write_text("CREATE OR REPLACE PACKAGE APP_CODE.PKG AS\n  PROCEDURE STALE;\nEND PKG;\n/\n", encoding="utf-8")
        with patch.object(baseline, "_capture", return_value=capture_catalog(units)):
            (path,) = self.feature_sources.capture_package(self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON)

        path.write_text(path.read_text(encoding="utf-8").replace("PROCEDURE LIVE", "PROCEDURE EDITED"), encoding="utf-8")
        diff_path, review_path = self.feature_sources.diff_captured_package(
            self.root, "dispatch", "APP_CODE", day="2026-10-27",
        )
        diff = diff_path.read_text(encoding="utf-8")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertIn("PROCEDURE LIVE", diff)
        self.assertIn("PROCEDURE EDITED", diff)
        self.assertNotIn("PROCEDURE STALE", diff)
        self.assertEqual(review["baselineKind"], "dev-capture")
        self.assertEqual(review["observedIdentity"], IDENTITY)
        self.assertEqual(review["beforeSha256"], hashlib.sha256(
            (self.root / "scratch" / "feature-source" / "dispatch" / "APP_CODE" / "captured" / path.name).read_bytes()
        ).hexdigest())

    def test_diff_refuses_to_fall_back_to_a_database_mirror_without_capture_baseline(self) -> None:
        source = self.root / "features" / "dispatch" / "APP_CODE" / "packages" / "pkg.pks.sql"
        source.parent.mkdir(parents=True)
        source.write_text("CREATE OR REPLACE PACKAGE APP_CODE.PKG AS END PKG;\n/\n", encoding="utf-8")
        mirror = self.root / "database" / "APP_CODE" / "packages" / "PKG_SPEC.sql"
        mirror.parent.mkdir(parents=True)
        mirror.write_text("stale mirror\n", encoding="utf-8")

        with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "capture baseline"):
            self.feature_sources.diff_captured_package(self.root, "dispatch", "APP_CODE", day="2026-10-27")

    def test_each_captured_package_keeps_its_own_baseline_for_feature_diff(self) -> None:
        first = capture_catalog({("PACKAGE", "FIRST_PKG"): ["PACKAGE FIRST_PKG AS END;\n"]})
        second = capture_catalog({("PACKAGE", "SECOND_PKG"): ["PACKAGE SECOND_PKG AS END;\n"]})
        with patch.object(baseline, "_capture", side_effect=[first, second]):
            self.feature_sources.capture_package(self.root, "dispatch", "APP_CODE", "FIRST_PKG", environ=ENVIRON)
            second_path, = self.feature_sources.capture_package(
                self.root, "dispatch", "APP_CODE", "SECOND_PKG", environ=ENVIRON,
            )

        second_path.write_text(second_path.read_text(encoding="utf-8").replace("SECOND_PKG", "EDITED_PKG"), encoding="utf-8")
        artifacts = self.feature_sources.diff_captured_package(self.root, "dispatch", "APP_CODE", day="2026-10-27")

        self.assertEqual(len(artifacts), 4)
        self.assertEqual(len(list((self.root / "ai_generate" / "2026-10-27" / "feature-diffs").rglob("review.json"))), 2)


TRIGGER_SOURCE = ["TRIGGER ORDERS_TRG\n", "BEFORE INSERT ON ORDERS\n", "FOR EACH ROW\n", "BEGIN NULL; END;\n"]
TYPE_SOURCE = ["TYPE ORDER_T AS OBJECT (\n", "  ID NUMBER\n", ");\n"]
TYPE_BODY_SOURCE = ["TYPE BODY ORDER_T AS\n", "  MEMBER FUNCTION TOTAL RETURN NUMBER IS BEGIN RETURN 1; END;\n", "END;\n"]
FUNCTION_SOURCE = ["FUNCTION GET_ONE RETURN NUMBER IS\n", "BEGIN RETURN 1; END;\n"]
PROCEDURE_SOURCE = ["PROCEDURE RUN_IT IS\n", "BEGIN NULL; END;\n"]


class StoredUnitGroupTests(unittest.TestCase):
    """Capture of triggers, procedures, functions and types, beside the package capture that existed first."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        from scripts import feature_sources
        self.feature_sources = feature_sources

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, feature: str, group: str, name: str, units: dict, **options):
        with patch.object(baseline, "_capture", return_value=capture_catalog(units)):
            return self.feature_sources.capture_unit(
                self.root, feature, "APP_CODE", group, name, environ=ENVIRON, **options,
            )

    def test_the_group_kinds_are_exactly_the_stored_source_types_the_checkpoint_engine_accepts(self) -> None:
        from scripts import feature_manifest

        kinds = {kind for group in self.feature_sources.UNIT_GROUPS for kind in group.kinds}
        self.assertEqual(kinds, set(feature_manifest.STORED_SOURCE_TYPES))

    def test_each_single_unit_group_writes_its_own_directory_suffix_and_scoped_metadata(self) -> None:
        cases = (
            ("trigger", "ORDERS_TRG", ("TRIGGER", "ORDERS_TRG"), TRIGGER_SOURCE, "triggers/orders_trg.trg.sql", "capture-trigger-orders_trg.json"),
            ("procedure", "RUN_IT", ("PROCEDURE", "RUN_IT"), PROCEDURE_SOURCE, "procedures/run_it.prc.sql", "capture-procedure-run_it.json"),
            ("function", "GET_ONE", ("FUNCTION", "GET_ONE"), FUNCTION_SOURCE, "functions/get_one.fnc.sql", "capture-function-get_one.json"),
        )
        for group, name, key, source, expected_path, expected_metadata in cases:
            with self.subTest(group=group):
                (path,) = self.capture("dispatch", group, name, {key: source})
                self.assertEqual(path.relative_to(self.root).as_posix(), f"features/dispatch/APP_CODE/{expected_path}")
                self.assertIn(f'"APP_CODE"."{name}"', path.read_text(encoding="utf-8"))
                metadata = json.loads((self.root / "scratch/feature-source/dispatch/APP_CODE" / expected_metadata).read_text(encoding="utf-8"))
                self.assertEqual(list(metadata), ["schemaVersion", "feature", "schema", "group", "unit", "identity", "units"])
                self.assertEqual((metadata["group"], metadata["unit"]), (group, name))
                self.assertEqual([unit["type"] for unit in metadata["units"]], [key[0]])

    def test_a_type_captures_its_specification_and_optional_body_to_types_with_their_own_suffixes(self) -> None:
        units = {("TYPE", "ORDER_T"): TYPE_SOURCE, ("TYPE BODY", "ORDER_T"): TYPE_BODY_SOURCE}
        paths = self.capture("dispatch", "type", "ORDER_T", units)

        self.assertEqual([path.name for path in paths], ["order_t.tps.sql", "order_t.tpb.sql"])
        self.assertTrue(all(path.parent.name == "types" for path in paths))
        self.assertIn('CREATE OR REPLACE TYPE "APP_CODE"."ORDER_T"', paths[0].read_text(encoding="utf-8"))
        self.assertIn("TYPE BODY", paths[1].read_text(encoding="utf-8"))

    def test_a_type_spec_only_capture_writes_one_file(self) -> None:
        (path,) = self.capture("dispatch", "type", "ORDER_T", {("TYPE", "ORDER_T"): TYPE_SOURCE})
        self.assertEqual(path.name, "order_t.tps.sql")

    def test_a_type_body_without_its_specification_is_refused(self) -> None:
        with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "type specification was not found in APP_CODE on DEV"):
            self.capture("dispatch", "type", "ORDER_T", {("TYPE BODY", "ORDER_T"): TYPE_BODY_SOURCE})
        self.assertFalse((self.root / "features").exists())

    def test_a_name_that_is_not_on_dev_as_the_requested_kind_is_refused_with_the_group_label(self) -> None:
        with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "trigger ORDERS_TRG was not found in APP_CODE on DEV"):
            self.capture("dispatch", "trigger", "ORDERS_TRG", {("PACKAGE", "ORDERS_TRG"): ["PACKAGE ORDERS_TRG AS\n", "END;\n"]})

    def test_an_unknown_group_is_refused_before_anything_is_read(self) -> None:
        with patch.object(baseline, "_capture") as read:
            with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "unsupported unit group"):
                self.feature_sources.capture_unit(self.root, "dispatch", "APP_CODE", "view", "V", environ=ENVIRON)
        read.assert_not_called()

    def test_a_trigger_and_a_package_with_the_same_name_keep_separate_files_and_baselines(self) -> None:
        self.capture("dispatch", "package", "ORDERS", {("PACKAGE", "ORDERS"): ["PACKAGE ORDERS AS\n", "END ORDERS;\n"]})
        self.capture("dispatch", "trigger", "ORDERS", {("TRIGGER", "ORDERS"): TRIGGER_SOURCE})

        metadata = sorted(path.name for path in (self.root / "scratch/feature-source/dispatch/APP_CODE").glob("capture-*.json"))
        self.assertEqual(metadata, ["capture-orders.json", "capture-trigger-orders.json"])
        self.assertTrue((self.root / "features/dispatch/APP_CODE/packages/orders.pks.sql").is_file())
        self.assertTrue((self.root / "features/dispatch/APP_CODE/triggers/orders.trg.sql").is_file())

        artifacts = self.feature_sources.diff_captured_package(self.root, "dispatch", "APP_CODE", day="2026-11-11")
        self.assertEqual(len(artifacts), 4)

    def test_feature_diff_covers_every_captured_group(self) -> None:
        self.capture("dispatch", "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE})
        self.capture("dispatch", "type", "ORDER_T", {("TYPE", "ORDER_T"): TYPE_SOURCE, ("TYPE BODY", "ORDER_T"): TYPE_BODY_SOURCE})
        trigger = self.root / "features/dispatch/APP_CODE/triggers/orders_trg.trg.sql"
        trigger.write_text(trigger.read_text(encoding="utf-8").replace("BEGIN NULL;", "BEGIN :NEW.ID := 1;"), encoding="utf-8")

        artifacts = self.feature_sources.diff_captured_package(self.root, "dispatch", "APP_CODE", day="2026-11-11")

        reviews = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts if path.name == "review.json"]
        self.assertEqual(sorted((row["unitType"], row["unitName"]) for row in reviews), [
            ("TRIGGER", "ORDERS_TRG"), ("TYPE", "ORDER_T"), ("TYPE BODY", "ORDER_T"),
        ])
        diffs = [path.read_text(encoding="utf-8") for path in artifacts if path.name == "source.diff"]
        self.assertTrue(any(":NEW.ID := 1" in diff for diff in diffs))

    def test_a_refresh_of_an_edited_trigger_is_refused_until_the_edit_is_discarded(self) -> None:
        self.capture("dispatch", "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE})
        (path,) = (self.root / "features/dispatch/APP_CODE/triggers").glob("*.trg.sql")
        path.write_text(path.read_text(encoding="utf-8").replace("BEGIN NULL;", "BEGIN EDITED;"), encoding="utf-8")

        with self.assertRaisesRegex(self.feature_sources.FeatureSourcesError, "refresh"):
            self.capture("dispatch", "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE})
        self.assertIn("EDITED", path.read_text(encoding="utf-8"))

        self.capture("dispatch", "trigger", "ORDERS_TRG", {("TRIGGER", "ORDERS_TRG"): TRIGGER_SOURCE}, refresh=True, discard_unapplied=True)
        self.assertNotIn("EDITED", path.read_text(encoding="utf-8"))

    def test_a_move_takes_a_function_to_the_receiving_feature_and_removes_the_old_files(self) -> None:
        self.capture("dispatch", "function", "GET_ONE", {("FUNCTION", "GET_ONE"): FUNCTION_SOURCE})
        report: list[str] = []
        (path,) = self.capture("pickup", "function", "GET_ONE", {("FUNCTION", "GET_ONE"): FUNCTION_SOURCE},
                               move_from="dispatch", report=report)

        self.assertEqual(path.relative_to(self.root).as_posix(), "features/pickup/APP_CODE/functions/get_one.fnc.sql")
        self.assertFalse((self.root / "features/dispatch/APP_CODE/functions/get_one.fnc.sql").exists())
        self.assertFalse((self.root / "scratch/feature-source/dispatch/APP_CODE/capture-function-get_one.json").exists())
        claims, _unanalysed = self.feature_sources.feature_claims.collect_claims(self.root)
        self.assertEqual([(claim.feature, claim.kind) for claim in claims], [("pickup", "FUNCTION")])

    def test_keep_file_moves_a_type_pair_unchanged_without_reading_dev(self) -> None:
        self.capture("dispatch", "type", "ORDER_T", {("TYPE", "ORDER_T"): TYPE_SOURCE, ("TYPE BODY", "ORDER_T"): TYPE_BODY_SOURCE})
        moved_before = sorted(path.read_bytes() for path in (self.root / "features/dispatch/APP_CODE/types").iterdir())

        with patch.object(baseline, "_capture") as read:
            paths = self.feature_sources.capture_unit(
                self.root, "pickup", "APP_CODE", "type", "ORDER_T", environ=ENVIRON, move_from="dispatch", keep_file=True,
            )

        read.assert_not_called()
        self.assertEqual(sorted(path.read_bytes() for path in paths), moved_before)
        self.assertEqual(sorted(path.name for path in paths), ["order_t.tpb.sql", "order_t.tps.sql"])
        self.assertEqual(list((self.root / "features/dispatch/APP_CODE/types").glob("*.sql")), [])

    def test_a_package_capture_keeps_its_metadata_shape_without_a_group_field(self) -> None:
        self.capture("dispatch", "package", "PKG", {("PACKAGE", "PKG"): ["PACKAGE PKG AS\n", "END PKG;\n"]})
        metadata = json.loads((self.root / "scratch/feature-source/dispatch/APP_CODE/capture-pkg.json").read_text(encoding="utf-8"))
        self.assertEqual(list(metadata), ["schemaVersion", "feature", "schema", "package", "identity", "units"])
