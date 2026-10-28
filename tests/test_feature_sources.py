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
            refreshed = self.feature_sources.capture_package(
                self.root, "dispatch", "APP_CODE", "PKG", environ=ENVIRON, refresh=True,
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
