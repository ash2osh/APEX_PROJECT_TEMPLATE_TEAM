"""Offline tests for scripts/release_state.py (per-environment release state)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import _test_subprocess_safety  # noqa: F401

from scripts import release_state as rs


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReleaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bundle = self.root / "releases" / "rel-a"
        self.bundle.mkdir(parents=True)

    def test_text_sha256_normalises_line_endings(self) -> None:
        self.assertEqual(rs.text_sha256("a\r\nb\rc\n"), rs.text_sha256("a\nb\nc\n"))
        self.assertEqual(rs.text_sha256("a\nb\n"), _sha("a\nb\n"))
        self.assertNotEqual(rs.text_sha256("a\n"), rs.text_sha256("a\n\n"))

    def test_select_steps_full_delta_and_none(self) -> None:
        current = {"01-ddl/a.sql": "s1", "02-code/b.sql": "s2", "02-code/c.sql": "s3"}
        self.assertEqual(rs.select_steps(current, None, full=False), (list(current), []))
        state = {"steps": {"01-ddl/a.sql": {"sha256": "s1"}, "02-code/b.sql": {"sha256": "old"}}}
        self.assertEqual(
            rs.select_steps(current, state, full=False),
            (["02-code/b.sql", "02-code/c.sql"], ["01-ddl/a.sql"]),
        )
        self.assertEqual(rs.select_steps(current, state, full=True), (list(current), []))

    def test_merge_state_keeps_skipped_drops_removed(self) -> None:
        previous = {
            "schemaVersion": 1, "slug": "rel-a", "env": "staging",
            "steps": {
                "01-ddl/a.sql": {"sha256": "s1", "at": "T0", "commit": "c0"},
                "02-code/gone.sql": {"sha256": "g", "at": "T0", "commit": "c0"},
                "02-code/b.sql": {"sha256": "old", "liveSha256": "L0", "at": "T0", "commit": "c0"},
            },
            "apps": {"130": {"sourceSha256": "A0", "at": "T0"}},
        }
        current = {"01-ddl/a.sql": "s1", "02-code/b.sql": "s2"}
        merged = rs.merge_state(
            previous, slug="rel-a", env="staging", current=current, applied=["02-code/b.sql"],
            live_hashes={"02-code/b.sql": "L1"}, app_shas_deployed={}, run={"mode": "delta"},
            at="T1", commit="c1",
        )
        self.assertEqual(merged["steps"]["01-ddl/a.sql"], {"sha256": "s1", "at": "T0", "commit": "c0"})
        self.assertEqual(merged["steps"]["02-code/b.sql"], {"sha256": "s2", "liveSha256": "L1", "at": "T1", "commit": "c1"})
        self.assertNotIn("02-code/gone.sql", merged["steps"])
        self.assertEqual(merged["apps"], {"130": {"sourceSha256": "A0", "at": "T0"}})
        self.assertEqual(merged["lastRun"], {"mode": "delta"})
        self.assertEqual((merged["slug"], merged["env"], merged["schemaVersion"]), ("rel-a", "staging", 1))

        merged2 = rs.merge_state(
            None, slug="rel-a", env="staging", current=current, applied=list(current),
            live_hashes={}, app_shas_deployed={130: "A1"}, run={}, at="T2", commit="c2",
        )
        self.assertEqual(merged2["apps"], {"130": {"sourceSha256": "A1", "at": "T2"}})
        self.assertNotIn("liveSha256", merged2["steps"]["01-ddl/a.sql"])

    def test_load_state_rejects_corrupt_and_wrong_slug(self) -> None:
        self.assertIsNone(rs.load_state(self.bundle, "rel-a", "staging"))
        path = rs.state_path(self.bundle, "staging")
        self.assertEqual(path, self.bundle / "state" / "staging.json")
        path.parent.mkdir()
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(rs.StateError, "state/staging.json"):
            rs.load_state(self.bundle, "rel-a", "staging")
        path.write_text(json.dumps({"schemaVersion": 1, "slug": "other", "env": "staging", "steps": {}}), encoding="utf-8")
        with self.assertRaisesRegex(rs.StateError, "slug"):
            rs.load_state(self.bundle, "rel-a", "staging")
        path.write_text(json.dumps({"schemaVersion": 1, "slug": "rel-a", "env": "dev", "steps": {}}), encoding="utf-8")
        with self.assertRaisesRegex(rs.StateError, "env"):
            rs.load_state(self.bundle, "rel-a", "staging")
        path.write_text(json.dumps({"schemaVersion": 2, "slug": "rel-a", "env": "staging", "steps": {}}), encoding="utf-8")
        with self.assertRaisesRegex(rs.StateError, "schemaVersion"):
            rs.load_state(self.bundle, "rel-a", "staging")
        path.write_text(json.dumps({"schemaVersion": 1, "slug": "rel-a", "env": "staging", "steps": []}), encoding="utf-8")
        with self.assertRaisesRegex(rs.StateError, "steps"):
            rs.load_state(self.bundle, "rel-a", "staging")
        with self.assertRaises(ValueError):
            rs.state_path(self.bundle, "qa")

    def test_write_state_atomic_sorted_lf(self) -> None:
        rs.write_state(self.bundle, "dev", {"slug": "rel-a", "env": "dev", "schemaVersion": 1, "steps": {"b": {}, "a": {}}})
        raw = (self.bundle / "state" / "dev.json").read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))
        text = raw.decode("utf-8")
        self.assertLess(text.index('"a"'), text.index('"b"'))
        self.assertLess(text.index('"env"'), text.index('"slug"'))
        self.assertEqual([p.name for p in (self.bundle / "state").iterdir()], ["dev.json"])

    def test_staging_mismatches_and_effective_status(self) -> None:
        current = {"01-ddl/a.sql": "s1", "02-code/b.sql": "s2"}
        apps = {130: "A1"}
        staging = {"steps": {"01-ddl/a.sql": {"sha256": "s1"}, "02-code/b.sql": {"sha256": "s2"}},
                   "apps": {"130": {"sourceSha256": "A1"}}}
        self.assertEqual(rs.staging_mismatches(current, apps, staging), [])
        self.assertEqual(rs.staging_mismatches(current, apps, None), ["01-ddl/a.sql", "02-code/b.sql", "app 130"])
        changed = {"01-ddl/a.sql": "s1", "02-code/b.sql": "NEW"}
        self.assertEqual(rs.staging_mismatches(changed, {130: "A2"}, staging), ["02-code/b.sql", "app 130"])

        self.assertEqual(rs.effective_status({"status": "staged"}, current, apps, staging), ("staged", []))
        self.assertEqual(rs.effective_status({"status": "staged"}, changed, apps, staging), ("stale", ["02-code/b.sql"]))
        self.assertEqual(rs.effective_status({"status": "draft"}, changed, apps, staging), ("draft", ["02-code/b.sql"]))
        self.assertEqual(rs.effective_status({"status": "deployed"}, changed, apps, staging), ("deployed", []))

    def test_app_source_sha256_changes_with_content(self) -> None:
        empty = rs.app_source_sha256(self.root, 130)
        self.assertEqual(empty, hashlib.sha256(b"").hexdigest())
        app_dir = self.root / "apps" / "APP_CODE" / "130"
        (app_dir / "pages").mkdir(parents=True)
        (app_dir / "application.apx").write_text("app\n", encoding="utf-8")
        (app_dir / "pages" / "p1.apx").write_text("page\n", encoding="utf-8")
        first = rs.app_source_sha256(self.root, 130)
        self.assertNotEqual(first, empty)
        self.assertEqual(rs.app_source_sha256(self.root, 130, app_dir), first)
        (app_dir / "pages" / "p1.apx").write_text("page2\n", encoding="utf-8")
        self.assertNotEqual(rs.app_source_sha256(self.root, 130), first)

    # ASHARIF 2026-11-25: failed-run record for --resume
    def test_partial_record_round_trip_and_resume_split(self) -> None:
        bundle = self.root / "releases" / "rel"
        current = {"01-ddl/a.sql": "s1", "01-ddl/b.sql": "s2", "02-code/c.sql": "s3"}
        self.assertIsNone(rs.load_partial(bundle, "rel", "dev"))
        partial = rs.build_partial(slug="rel", env="dev", current=current, finished=["01-ddl/a.sql"],
                                   previous=None, at="T1", commit="c1", error="ORA-00904")
        rs.write_partial(bundle, "dev", partial)
        loaded = rs.load_partial(bundle, "rel", "dev")
        self.assertEqual(loaded["steps"], {"01-ddl/a.sql": {"sha256": "s1", "at": "T1", "commit": "c1"}})
        ddl = ["01-ddl/a.sql", "01-ddl/b.sql"]
        self.assertEqual(rs.resumable_steps(loaded, current, ddl), (["01-ddl/a.sql"], []))
        edited = {**current, "01-ddl/a.sql": "s9"}
        self.assertEqual(rs.resumable_steps(loaded, edited, ddl), ([], ["01-ddl/a.sql"]))
        # A second failure keeps earlier finished steps that still match and adds the new ones
        again = rs.build_partial(slug="rel", env="dev", current=current, finished=["01-ddl/b.sql"],
                                 previous=loaded, at="T2", commit="c2", error="x")
        self.assertEqual(set(again["steps"]), {"01-ddl/a.sql", "01-ddl/b.sql"})
        dropped = rs.build_partial(slug="rel", env="dev", current=edited, finished=[],
                                   previous=loaded, at="T2", commit="c2", error="x")
        self.assertEqual(dropped["steps"], {})
        self.assertTrue(rs.clear_partial(bundle, "dev"))
        self.assertFalse(rs.clear_partial(bundle, "dev"))
        with self.assertRaises(ValueError):
            rs.partial_path(bundle, "prod")

    def test_partial_record_rejects_wrong_bundle(self) -> None:
        bundle = self.root / "releases" / "rel"
        rs.write_partial(bundle, "staging", rs.build_partial(
            slug="other", env="staging", current={}, finished=[], previous=None, at="T", commit="c", error=""))
        with self.assertRaisesRegex(rs.StateError, "does not match bundle"):
            rs.load_partial(bundle, "rel", "staging")


if __name__ == "__main__":
    unittest.main()
