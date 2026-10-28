from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path


class FeatureDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        try:
            from scripts import feature_sources
        except ImportError as error:
            self.fail(f"feature source review diffs are not implemented: {error}")
        self.feature_sources = feature_sources

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_review_preserves_exact_whitespace_unicode_quotes_and_hashes(self) -> None:
        before = 'CREATE OR REPLACE PACKAGE "APP_CODE"."ÅPKG$X" AS\n  PROCEDURE old_name;\n   \n  -- trailing-\nEND;\n/\n'
        after = 'CREATE OR REPLACE PACKAGE "APP_CODE"."ÅPKG$X" AS\n  PROCEDURE new_name;\n   \n  -- trailing-\nEND;\n/\n'
        identity = {"db_name": "DEVDB", "service_name": "dev.example", "edition": "ORA$BASE"}

        diff_path, review_path = self.feature_sources.write_source_review(
            self.root, "dispatch", "APP_CODE", ("PACKAGE", "ÅPKG$X"), before, after,
            identity=identity, baseline_kind="staging-target", payload_digest="a" * 64, day="2026-10-27",
        )

        diff = diff_path.read_text(encoding="utf-8")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertIn("-  PROCEDURE old_name;", diff)
        self.assertIn("+  PROCEDURE new_name;", diff)
        self.assertIn("   \n", diff)
        self.assertIn("ÅPKG$X", diff)
        self.assertEqual(review["beforeSha256"], hashlib.sha256(before.encode("utf-8")).hexdigest())
        self.assertEqual(review["afterSha256"], hashlib.sha256(after.encode("utf-8")).hexdigest())
        self.assertEqual(review["payloadDigest"], "a" * 64)
        self.assertEqual(review["baselineKind"], "staging-target")
        self.assertEqual(review["owner"], "APP_CODE")
        self.assertEqual(review["unitType"], "PACKAGE")
        self.assertEqual(review["unitName"], "ÅPKG$X")

    def test_review_artifacts_are_digest_namespaced_and_preserve_walkthrough(self) -> None:
        daily = self.root / "ai_generate" / "2026-10-27"
        daily.mkdir(parents=True)
        walkthrough = daily / "WALKTHROUGH.md"
        walkthrough.write_text("operator notes\n", encoding="utf-8")
        first = self.feature_sources.write_source_review(
            self.root, "dispatch", "APP_CODE", ("PACKAGE BODY", "PKG"), "before\n", "after\n",
            identity={"db_unique_name": "STAGE"}, baseline_kind="staging-target", payload_digest="b" * 64,
            day="2026-10-27",
        )
        second = self.feature_sources.write_source_review(
            self.root, "dispatch", "APP_CODE", ("PACKAGE BODY", "PKG"), "before\n", "different after\n",
            identity={"db_unique_name": "STAGE"}, baseline_kind="staging-target", payload_digest="b" * 64,
            day="2026-10-27",
        )

        self.assertNotEqual(first, second)
        self.assertEqual(walkthrough.read_text(encoding="utf-8"), "operator notes\n")
        self.assertEqual(len(list((daily / "feature-diffs").rglob("source.diff"))), 2)
        self.assertTrue(all(path.is_file() for path in (*first, *second)))

    def test_review_directory_component_is_bounded_for_long_unit_names(self) -> None:
        name = "A" * 40
        diff_path, review_path = self.feature_sources.write_source_review(
            self.root, "dispatch", "APP_CODE", ("PACKAGE", name),
            "before source\n", "after source\n",
            identity={"db_unique_name": "STAGE"}, baseline_kind="staging-target",
            payload_digest="d" * 64, day="2026-10-27",
        )

        component = diff_path.parent.name
        self.assertEqual(diff_path.parent, review_path.parent)
        self.assertTrue(component.isascii())
        self.assertLessEqual(len(component.encode("ascii")), 96)

    def test_long_and_colliding_unit_slugs_get_distinct_bounded_real_paths(self) -> None:
        names = (
            "A" * 128,
            "Å" * 60,
            "shared-prefix-identifier-" + "x" * 32 + "A",
            "shared-prefix-identifier-" + "x" * 32 + "B",
            "CaseSensitiveIdentifier-" + "Q" * 24,
            "casesensitiveidentifier-" + "q" * 24,
        )
        directories = []
        for index, name in enumerate(names):
            self.assertLessEqual(len(name.encode("utf-8")), 128)
            before, after = f"before {index}\n", f"after {index}\n"
            diff_path, review_path = self.feature_sources.write_source_review(
                self.root, "dispatch", "APP_CODE", ("PACKAGE", name), before, after,
                identity={"db_unique_name": "STAGE"}, baseline_kind="staging-target",
                payload_digest="e" * 64, day="2026-10-27",
            )
            component = diff_path.parent.name
            self.assertTrue(component.isascii())
            self.assertLessEqual(len(component.encode("ascii")), 96)
            self.assertEqual(diff_path.parent, review_path.parent)
            self.assertTrue(diff_path.is_file() and review_path.is_file())
            self.assertIn(name, diff_path.read_text(encoding="utf-8"))
            review = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(review["unitName"], name)
            self.assertEqual(review["beforeSha256"], hashlib.sha256(before.encode()).hexdigest())
            self.assertEqual(review["afterSha256"], hashlib.sha256(after.encode()).hexdigest())
            directories.append(component)

        self.assertEqual(len(directories), len(set(directories)))


if __name__ == "__main__":
    unittest.main()
