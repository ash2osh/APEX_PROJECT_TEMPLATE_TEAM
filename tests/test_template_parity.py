import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "template_parity.py"

if TOOL.is_file():
    import importlib.util

    spec = importlib.util.spec_from_file_location("template_parity", TOOL)
    template_parity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(template_parity)


def commit(repo: Path, files: dict) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "c"], check=True)


@unittest.skipUnless(TOOL.is_file(), "template repository tool (not shipped to projects)")
class TemplateParityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.this, self.other = base / "this", base / "other"
        for repo in (self.this, self.other):
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        commit(self.this, {"same.txt": "x\n", "release.txt": "26.2\n", "fix.txt": "old\n", "only-here.txt": "a\n"})
        commit(self.other, {"same.txt": "x\n", "release.txt": "26.1\n", "fix.txt": "new\n"})
        self.list = base / "parity.json"

    def tearDown(self):
        self.temporary.cleanup()

    def run_tool(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = template_parity.main([str(self.other), *args], root=self.this, list_path=self.list)
        return code, out.getvalue()

    def report(self):
        code, output = self.run_tool("--format", "json")
        return code, {name: [row["path"] for row in rows] for name, rows in json.loads(output).items()}

    def test_lists_only_shared_files_that_differ_as_unreviewed(self):
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["unreviewed"], ["fix.txt", "release.txt"])

    def test_marked_files_pass_until_either_side_changes(self):
        self.assertEqual(self.run_tool("--mark", "release.txt", "--status", "intended", "--reason", "release")[0], 0)
        self.assertEqual(self.run_tool("--mark", "fix.txt", "--status", "missed", "--reason", "port it")[0], 0)
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual((report["intended"], report["missed"]), (["release.txt"], ["fix.txt"]))
        commit(self.other, {"release.txt": "26.1.1\n"})
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["changed"], ["release.txt"])

    def test_an_entry_for_files_that_now_match_is_stale(self):
        self.run_tool("--mark", "release.txt", "--status", "intended", "--reason", "release")
        self.run_tool("--mark", "fix.txt", "--status", "missed", "--reason", "port it")
        commit(self.this, {"fix.txt": "new\n"})
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["stale"], ["fix.txt"])

    def test_mark_refuses_identical_or_unshared_files_and_needs_a_reason(self):
        self.assertEqual(self.run_tool("--mark", "same.txt", "--status", "intended", "--reason", "r")[0], 2)
        self.assertEqual(self.run_tool("--mark", "only-here.txt", "--status", "intended", "--reason", "r")[0], 2)
        with self.assertRaises(SystemExit):
            self.run_tool("--mark", "fix.txt", "--status", "missed")
        self.assertFalse(self.list.exists())


if __name__ == "__main__":
    unittest.main()
