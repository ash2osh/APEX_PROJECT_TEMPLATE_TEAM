"""Contract tests for the agent conversation styles in .agents/styles/."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from scripts.local_config import AGENT_STYLES

ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / ".agents" / "styles"
SECTIONS = (
    "## 1. Interaction protocol",
    "## 2. Decision format",
    "## 3. Answer and status format",
    "## 4. Delegation and review",
    "## 5. Evidence and honesty",
    "## 6. Scope control",
    "## 7. Language and input tolerance",
    "## 8. Never overrides",
)
NEVER_OVERRIDES = (
    "This style changes communication only; the rules in `AGENTS.md` and "
    "`.agents/rules/agent-safety.md` always win."
)
PERSONAL_SECTIONS = ("## Preferences", "## Decision habits", "## Corrections", "## Vocabulary")
PRIVATE = (
    re.compile(r"https?://"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"RUNERP|ABSH|NATREC|DEV26|DEV2\b", re.IGNORECASE),
)
STYLE_LINE_BUDGET = 70
PERSONAL_LINE_BUDGET = 40


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AgentStyleFileTests(unittest.TestCase):
    def test_every_allowed_style_has_a_file_with_all_sections_and_the_override_sentence(self) -> None:
        for name in AGENT_STYLES:
            with self.subTest(style=name):
                text = read(STYLES / f"{name}.md")
                for heading in SECTIONS:
                    self.assertIn(heading, text)
                self.assertIn(NEVER_OVERRIDES, text)
                self.assertLessEqual(len(text.splitlines()), STYLE_LINE_BUDGET)

    def test_style_files_contain_no_private_details(self) -> None:
        for path in sorted(STYLES.glob("*.md")):
            text = read(path)
            for pattern in PRIVATE:
                with self.subTest(file=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_asharif_style_carries_the_short_example(self) -> None:
        text = read(STYLES / "asharif-style.md")
        self.assertIn("Short example", text)
        for marker in ("**A.", "**B.", "**C.", "(recommended)"):
            self.assertIn(marker, text)

    def test_personal_layer_placeholder_has_the_sections_and_no_entries(self) -> None:
        text = read(STYLES / "personal.md")
        for heading in PERSONAL_SECTIONS:
            self.assertIn(heading, text)
        self.assertLessEqual(len(text.splitlines()), PERSONAL_LINE_BUDGET)
        outside_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        self.assertEqual([l for l in outside_comments.splitlines() if l.startswith("- ")], [])

    def test_styles_readme_lists_every_style_and_the_precedence(self) -> None:
        text = read(STYLES / "README.md")
        for name in AGENT_STYLES:
            self.assertIn(f"`{name}`", text)
        self.assertIn("personal.md", text)
        self.assertIn("AGENT_STYLE_LEARNING", text)


class AgentStyleOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(read(ROOT / "template-manifest.json"))

    def test_style_files_are_template_owned_and_personal_layer_is_project_owned(self) -> None:
        for name in (*AGENT_STYLES, "README"):
            self.assertIn(f".agents/styles/{name}.md", self.manifest["templateOwned"])
        self.assertIn(".agents/styles/personal.md", self.manifest["projectOwned"])
        self.assertNotIn(".agents/styles/personal.md", self.manifest["templateOwned"])
        self.assertFalse([p for p in self.manifest["templateOwned"] if p == ".agents/styles/**"])


class EnvExampleTests(unittest.TestCase):
    def test_env_example_documents_both_keys_with_defaults(self) -> None:
        lines = read(ROOT / ".env.example").splitlines()
        self.assertIn("AGENT_STYLE=standard", lines)
        self.assertIn("AGENT_STYLE_LEARNING=on", lines)


if __name__ == "__main__":
    unittest.main()
