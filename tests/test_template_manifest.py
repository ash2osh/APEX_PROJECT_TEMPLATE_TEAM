import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.upgrade_template import protected_project_path


ROOT = Path(__file__).resolve().parents[1]


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    # Mirrors scripts/upgrade_template.py so the test does not import it.
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


class TemplateManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "template-manifest.json").read_text(encoding="utf-8"))
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        self.tracked = [path for path in tracked.split("\0") if path]

    def classify(self, path: str) -> list[str]:
        owners = []
        if any(glob_to_regex(p).match(path) for p in self.manifest["templateOwned"]):
            owners.append("templateOwned")
        if path in self.manifest["projectOwned"]:
            owners.append("projectOwned")
        if any(glob_to_regex(p).match(path) for p in self.manifest["templateOnly"]):
            owners.append("templateOnly")
        return owners

    def assert_tracked_ownership_valid(self, tracked: list[str]) -> None:
        for path in tracked:
            owners = self.classify(path)
            with self.subTest(path=path):
                if protected_project_path(path):
                    self.assertEqual(owners, [])
                elif owners:
                    self.assertEqual(len(owners), 1, owners)

    def test_tracked_files_are_not_ambiguously_owned_and_protected_paths_are_unowned(self) -> None:
        self.assert_tracked_ownership_valid(self.tracked)

    def test_downstream_data_and_lock_can_be_committed_without_template_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            downstream = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "main", str(downstream)], check=True)
            subprocess.run(["git", "-C", str(downstream), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(downstream), "config", "user.name", "Test"], check=True)
            downstream_files = {
                "template-manifest.json": (ROOT / "template-manifest.json").read_text(encoding="utf-8"),
                ".template-lock.json": json.dumps({"schemaVersion": 1, "upstream": "template", "commit": "abc", "files": {}}),
                "apps/DEMO/100/application.apx": "app DEMO ()\n",
                "migrations/alice/20260926_add.sql": "select 1 from dual;\n",
                "docs/architecture.md": "Downstream-owned documentation.\n",
            }
            for relative, contents in downstream_files.items():
                target = downstream / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
            subprocess.run(["git", "-C", str(downstream), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(downstream), "commit", "-q", "-m", "downstream data"],
                check=True,
            )
            tracked = subprocess.run(
                ["git", "-C", str(downstream), "ls-files"], capture_output=True, text=True, check=True
            ).stdout.splitlines()

        self.assert_tracked_ownership_valid(tracked)
        for path in (".template-lock.json", "apps/DEMO/100/application.apx", "migrations/alice/20260926_add.sql"):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), [])

    def test_project_placeholders_exist_and_are_not_template_owned(self) -> None:
        for path in self.manifest["projectOwned"]:
            with self.subTest(path=path):
                self.assertIn(path, self.tracked)
                self.assertEqual(self.classify(path), ["projectOwned"])

    def test_project_data_is_never_managed(self) -> None:
        for path in (
            "apps/DEMO/100/application.apx",
            "database/DEMO/tables/T.sql",
            "migrations/alice/20260926_add.sql",
            "app_context/100/purpose.md",
            ".env",
            ".template-lock.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), [])

    def test_agent_entry_points_import_project_instructions(self) -> None:
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", claude)
        self.assertIn("@AGENTS.project.md", claude)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("AGENTS.project.md", agents)
        self.assertIn(".agents/rules/project.md", agents)


if __name__ == "__main__":
    unittest.main()
