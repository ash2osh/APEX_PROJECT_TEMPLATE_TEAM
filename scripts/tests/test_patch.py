from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from teamlib.patch import PatchError, apply_tree, recover_files


class PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-patch-test-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Patch Test"], check=True)
        (self.repo / "apps" / "checkout" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"old")
        (self.repo / "apps" / "checkout" / "deployments").mkdir()
        (self.repo / "apps" / "checkout" / "deployments" / "default.json").write_text("secret", encoding="utf-8")
        (self.repo / "unrelated.txt").write_text("unrelated", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)
        self.head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.before = {"pages/home.apx": b"old"}
        self.after = {"pages/home.apx": b"new", "pages/new.apx": b"added"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_applies_only_owned_files_and_preserves_deployment_and_unrelated(self):
        apply_tree(self.repo, "checkout", self.head, self.before, self.after, "op-1")
        self.assertEqual((self.repo / "apps" / "checkout" / "pages" / "home.apx").read_bytes(), b"new")
        self.assertEqual((self.repo / "apps" / "checkout" / "pages" / "new.apx").read_bytes(), b"added")
        self.assertEqual((self.repo / "apps" / "checkout" / "deployments" / "default.json").read_text(), "secret")
        self.assertEqual((self.repo / "unrelated.txt").read_text(), "unrelated")

    def test_refuses_changed_head_or_preimage(self):
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"user edit")
        with self.assertRaises(PatchError):
            apply_tree(self.repo, "checkout", self.head, self.before, self.after, "op-2")

    def test_failure_leaves_recovery_journal_and_restore_is_safe(self):
        import os
        os.environ["TEAM_PATCH_FAIL_AFTER"] = "1"
        try:
            with self.assertRaises(PatchError):
                apply_tree(self.repo, "checkout", self.head, self.before, self.after, "op-3")
        finally:
            os.environ.pop("TEAM_PATCH_FAIL_AFTER", None)
        journal = self.repo / ".sync-state" / "journals" / "op-3.json"
        self.assertTrue(journal.is_file())
        recover_files("op-3", "restore", repo=self.repo)
        self.assertEqual((self.repo / "apps" / "checkout" / "pages" / "home.apx").read_bytes(), b"old")
        self.assertFalse((self.repo / "apps" / "checkout" / "pages" / "new.apx").exists())
        self.assertEqual(json.loads(journal.read_text())["status"], "restored")

    def test_restore_refuses_later_user_edit(self):
        import os
        os.environ["TEAM_PATCH_FAIL_AFTER"] = "1"
        try:
            with self.assertRaises(PatchError):
                apply_tree(self.repo, "checkout", self.head, self.before, self.after, "op-4")
        finally:
            os.environ.pop("TEAM_PATCH_FAIL_AFTER", None)
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"later user edit")
        with self.assertRaises(PatchError):
            recover_files("op-4", "restore", repo=self.repo)


if __name__ == "__main__":
    unittest.main()
