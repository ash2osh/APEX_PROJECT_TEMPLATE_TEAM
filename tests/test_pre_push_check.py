import tempfile
import unittest
from pathlib import Path

from scripts.pre_push_check import changed_test_files, existing


class PrePushCheckTests(unittest.TestCase):
    def test_changed_test_files_keeps_only_top_level_test_modules_once(self):
        paths = [
            "tests/test_a.py", "tests/test_a.py", "tests/test_b.py", "tests/helper.py", "tests/known_flaky.txt",
            "tests/fixtures/test_c.py", "scripts/test_d.py", "docs/x.md",
        ]
        self.assertEqual(changed_test_files(paths), ["tests/test_a.py", "tests/test_b.py"])

    def test_existing_skips_folders_a_downstream_project_does_not_have(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scripts").mkdir()
            (root / "tools").mkdir()
            self.assertEqual(existing(("scripts", "tools", "tests"), root), ["scripts", "tools"])


if __name__ == "__main__":
    unittest.main()
