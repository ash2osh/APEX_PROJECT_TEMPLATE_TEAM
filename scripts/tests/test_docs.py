from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DocumentationTests(unittest.TestCase):
    def test_required_workflow_documents_and_links_exist(self):
        for relative in (
            "README.md", "docs/ci.md", "docs/promotion.md", "docs/app-recovery.md",
            "docs/conflict-resolution.md", "docs/import-pause.md", "docs/migrations.md",
            "docs/design-review-resolution.md", "AGENTS.md", ".agents/workflows/team-flow.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("no import step", readme.lower())
        self.assertIn("release.tar", (ROOT / "docs/promotion.md").read_text(encoding="utf-8"))
        self.assertIn("UNKNOWN", (ROOT / "docs/ci.md").read_text(encoding="utf-8"))

    def test_local_markdown_links_resolve(self):
        pattern = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
            for link in pattern.findall(path.read_text(encoding="utf-8")):
                if link.startswith(("http://", "https://", "mailto:")):
                    continue
                self.assertTrue((path.parent / link).resolve().is_file(), f"{path}: {link}")

    def test_lint_gate_is_configured_and_wired_into_ci(self):
        config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.ruff]", config)
        self.assertIn('"F"', config)
        workflow = (ROOT / ".github/workflows/template-checks.yml").read_text(encoding="utf-8")
        self.assertIn("ruff check scripts/", workflow)

    def test_no_string_literal_imports_outside_the_command_dispatcher(self):
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "__import__(" in line and "teamlib." not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [], f"use ordinary imports: {offenders}")


if __name__ == "__main__":
    unittest.main()
