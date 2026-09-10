from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
import shutil
import tempfile
import unittest

from teamlib.prune import prune_scratch


class PruneScratchTests(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        (root / "scratch").mkdir(parents=True)
        (root / ".sync-state" / "recovery").mkdir(parents=True)
        return root

    def test_old_captures_are_removed_and_the_newest_are_kept(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            for index in range(5):
                capture = root / "scratch" / f"apex-capture-{index:032x}"
                capture.mkdir()
                (capture / "application.apx").write_text("x", encoding="utf-8")
            report = prune_scratch(root, keep=2, dry_run=False)
            remaining = sorted(p.name for p in (root / "scratch").glob("apex-capture-*"))
            self.assertEqual(len(remaining), 2)
            self.assertEqual(report["removed"], 3)

    def test_a_capture_referenced_by_a_recovery_record_is_never_removed(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            pinned = root / "scratch" / ("apex-capture-" + "0" * 32)
            pinned.mkdir()
            for index in range(1, 5):
                (root / "scratch" / f"apex-capture-{index:032x}").mkdir()
            record = root / ".sync-state" / "recovery" / "rec-1"
            record.mkdir()
            (record / "capture.json").write_text(
                json.dumps({"work_dir": str(pinned)}), encoding="utf-8"
            )
            prune_scratch(root, keep=1, dry_run=False)
            self.assertTrue(pinned.is_dir(), "a referenced capture must survive pruning")

    def test_sqlcl_run_files_are_removed(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            work = root / "scratch" / "metadata" / "control-abc"
            work.mkdir(parents=True)
            for name in (
                ".team-driver-abc.sql", ".team-payload-abc.sql",
                ".team-stdin-abc.empty", ".team-sqlcl-abc.log",
            ):
                (work / name).write_text("x", encoding="utf-8")
            prune_scratch(root, keep=0, dry_run=False)
            self.assertEqual(sorted(p.name for p in work.glob(".team-*")), [])

    def test_dry_run_removes_nothing(self):
        with tempfile.TemporaryDirectory(prefix="team-prune-") as directory:
            root = self._repo(Path(directory))
            (root / "scratch" / "apex-capture-aaa").mkdir()
            report = prune_scratch(root, keep=0, dry_run=True)
            self.assertTrue((root / "scratch" / "apex-capture-aaa").is_dir())
            self.assertGreater(report["would_remove"], 0)


class WorkDirectoryCoverageTests(unittest.TestCase):
    def _scratch(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="team-prune-cover-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        scratch = root / "scratch"
        for name in (
            "apex-capture-1", "apex-import-1",
            "deploy-capture-1", "deploy-import-1",
            "master-checks/aaaa", "metadata/control-1", "metadata/migration-1",
        ):
            (scratch / name).mkdir(parents=True)
            (scratch / name / "metadata.sql").write_text("select 1 from dual;\n", encoding="utf-8", newline="\n")
        return root

    def test_deploy_and_metadata_directories_are_removed(self):
        root = self._scratch()
        report = prune_scratch(root, keep=0)
        self.assertGreaterEqual(report["removed"], 7)
        for name in (
            "deploy-capture-1", "deploy-import-1",
            "metadata/control-1", "metadata/migration-1", "master-checks/aaaa",
        ):
            self.assertFalse((root / "scratch" / name).exists(), name)

    def test_keep_still_retains_the_most_recent_captures(self):
        root = self._scratch()
        prune_scratch(root, keep=10)
        self.assertTrue((root / "scratch" / "apex-capture-1").exists())
        self.assertTrue((root / "scratch" / "deploy-capture-1").exists())

    def test_dry_run_removes_nothing(self):
        root = self._scratch()
        report = prune_scratch(root, keep=0, dry_run=True)
        self.assertEqual(report["removed"], 0)
        self.assertGreater(report["would_remove"], 0)
        self.assertTrue((root / "scratch" / "metadata" / "control-1").exists())


if __name__ == "__main__":
    unittest.main()
