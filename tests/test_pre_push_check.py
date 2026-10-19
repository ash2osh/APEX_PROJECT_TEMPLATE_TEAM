import tempfile
import unittest
from pathlib import Path

from scripts.pre_push_check import changed_python_files, changed_shell_scripts, changed_test_files, existing


class PrePushCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("scripts/a.py", "scripts/run.sh", "scripts/sub/b.py", "tools/t.py", "tests/test_a.py", "tests/helper.py", "docs/x.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_changed_test_files_keeps_only_top_level_test_modules_once(self):
        paths = [
            "tests/test_a.py", "tests/test_a.py", "tests/test_b.py", "tests/helper.py", "tests/known_flaky.txt",
            "tests/fixtures/test_c.py", "scripts/test_d.py", "docs/x.md",
        ]
        self.assertEqual(changed_test_files(paths), ["tests/test_a.py", "tests/test_b.py"])

    def test_changed_python_files_keeps_existing_code_files_in_the_code_folders(self):
        paths = ["scripts/a.py", "scripts/a.py", "scripts/sub/b.py", "tools/t.py", "tests/helper.py", "docs/x.py", "scripts/gone.py", "scripts/run.sh"]
        self.assertEqual(changed_python_files(paths, self.root), ["scripts/a.py", "scripts/sub/b.py", "tests/helper.py", "tools/t.py"])

    def test_changed_shell_scripts_keeps_existing_top_level_scripts(self):
        paths = ["scripts/run.sh", "scripts/missing.sh", "scripts/sub/x.sh", "docs/run.sh", "scripts/a.py"]
        self.assertEqual(changed_shell_scripts(paths, self.root), ["scripts/run.sh"])

    def test_existing_skips_folders_a_downstream_project_does_not_have(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scripts").mkdir()
            (root / "tools").mkdir()
            self.assertEqual(existing(("scripts", "tools", "tests"), root), ["scripts", "tools"])


if __name__ == "__main__":
    unittest.main()
