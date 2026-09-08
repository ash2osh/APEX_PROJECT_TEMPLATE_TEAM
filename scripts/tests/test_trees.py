from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from teamlib.trees import (
    TreeError,
    assert_source_clean,
    read_export_tree,
    read_git_tree,
    receipt_satisfied,
    tree_contains,
    tree_digest,
)


class TreeSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-trees-test-")
        self.root = Path(self.temp.name)
        self.fixture = self.root / "export"
        (self.fixture / "pages").mkdir(parents=True)
        (self.fixture / "shared-components" / "static-files").mkdir(parents=True)
        (self.fixture / "deployments").mkdir()
        (self.fixture / "logs").mkdir()
        (self.fixture / "pages" / "p00001-home.apx").write_bytes(b"")
        (self.fixture / "application.apx").write_bytes(b"application\r\n")
        (self.fixture / "apexlang.json").write_bytes(b'{"version":1}\r\n')
        (self.fixture / "shared-components" / "static-files" / "icons").mkdir()
        (self.fixture / "shared-components" / "static-files" / "icons" / "app-icon-32.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\xff"
        )
        (self.fixture / "shared-components" / "static-files" / "x.png").write_bytes(b"a\r\nb")
        (self.fixture / "deployments" / "default.json").write_text('{"connection":"secret"}', encoding="utf-8")
        (self.fixture / "logs" / "export.log").write_text("diagnostic", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_zero_byte_file_is_present_not_absent(self):
        tree = read_export_tree(self.fixture)
        self.assertIn("pages/p00001-home.apx", tree)
        self.assertEqual(tree["pages/p00001-home.apx"], b"")

    def test_absent_path_is_not_a_key(self):
        tree = read_export_tree(self.fixture)
        self.assertNotIn("pages/p00002-missing.apx", tree)

    def test_zero_byte_and_absent_digest_differently(self):
        self.assertNotEqual(tree_digest({"a": b""}), tree_digest({}))

    def test_binary_bytes_are_exact(self):
        tree = read_export_tree(self.fixture)
        blob = tree["shared-components/static-files/icons/app-icon-32.png"]
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")

    def test_binary_is_not_line_ending_normalised(self):
        tree = read_export_tree(self.fixture)
        self.assertIn(b"\r\n", tree["shared-components/static-files/x.png"])

    def test_apexlang_is_normalised_only_for_owned_text(self):
        tree = read_export_tree(self.fixture)
        self.assertEqual(tree["application.apx"], b"application\n")
        self.assertEqual(tree["apexlang.json"], b'{"version":1}\n')

    def test_deployment_and_logs_are_excluded(self):
        tree = read_export_tree(self.fixture)
        self.assertNotIn("deployments/default.json", tree)
        self.assertNotIn("logs/export.log", tree)

    def test_unexpected_metadata_is_rejected(self):
        (self.fixture / "secrets.env").write_text("PASSWORD=x", encoding="utf-8")
        with self.assertRaises(TreeError):
            read_export_tree(self.fixture)

    def test_symlink_is_rejected(self):
        os.symlink(self.fixture / "application.apx", self.fixture / "link.apx")
        with self.assertRaises(TreeError):
            read_export_tree(self.fixture)

    def test_digest_is_order_independent(self):
        self.assertEqual(tree_digest({"a": b"1", "b": b"2"}), tree_digest({"b": b"2", "a": b"1"}))

    def test_digest_separates_path_from_content(self):
        self.assertNotEqual(tree_digest({"ab": b"c"}), tree_digest({"a": b"bc"}))

    def test_contains_allows_extra_paths_in_superset(self):
        self.assertTrue(tree_contains({"a": b"1"}, {"a": b"1", "b": b"2"}))

    def test_contains_rejects_missing_or_changed_path(self):
        self.assertFalse(tree_contains({"a": b"1"}, {}))
        self.assertFalse(tree_contains({"a": b"1"}, {"a": b"2"}))

    def test_receipt_rejects_tombstone_resurrection(self):
        result = {"application.apx": b"app demo"}
        old = {**result, "pages/p7.apx": b"old page"}
        self.assertFalse(receipt_satisfied(result, {"pages/p7.apx"}, old))
        self.assertTrue(receipt_satisfied(result, {"pages/p7.apx"}, result))
        self.assertTrue(receipt_satisfied(result, {"pages/p7.apx"}, {**result, "pages/p8.apx": b"new"}))


class GitTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-git-tree-test-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Tree Test"], check=True)
        (self.repo / "apps" / "checkout" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"page\n")
        (self.repo / "apps" / "checkout" / "empty.bin").write_bytes(b"")
        (self.repo / "outside.txt").write_text("outside", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)
        self.commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_exact_git_blobs_relative_to_alias(self):
        tree = read_git_tree(self.repo, self.commit, "checkout")
        self.assertEqual(tree["pages/home.apx"], b"page\n")
        self.assertEqual(tree["empty.bin"], b"")
        self.assertNotIn("outside.txt", tree)

    def test_corrupt_ref_does_not_become_empty_tree(self):
        with self.assertRaises(TreeError):
            read_git_tree(self.repo, "not-a-commit", "checkout")

    def test_source_clean_checks_untracked_and_ignored_owned_files(self):
        (self.repo / ".gitignore").write_text("apps/checkout/ignored.apx\n", encoding="utf-8")
        (self.repo / "apps" / "checkout" / "new.apx").write_text("new", encoding="utf-8")
        (self.repo / "apps" / "checkout" / "ignored.apx").write_text("ignored", encoding="utf-8")
        with self.assertRaises(TreeError):
            assert_source_clean(self.repo, "checkout")

    def test_unrelated_ignored_file_does_not_block(self):
        (self.repo / ".gitignore").write_text("outside-ignored.txt\n", encoding="utf-8")
        (self.repo / "outside-ignored.txt").write_text("ignored", encoding="utf-8")
        assert_source_clean(self.repo, "checkout")


if __name__ == "__main__":
    unittest.main()
