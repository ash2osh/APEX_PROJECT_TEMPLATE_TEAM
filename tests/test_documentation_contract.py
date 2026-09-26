import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / ".agents" / "workflows" / "team-flow.md",
    ROOT / ".agents" / "rules" / "agent-safety.md",
)
RETIRED_TERMS = (
    "prepare-publish",
    "ack-publish",
    "publish-app --prepared",
    "TEAM_CHECKOUT_UUID",
    "TEAM_APP_MUTEX",
    "TEAM_MIGRATION_MEMBER",
    "METADATA_SCHEMA",
    "team.py",
    "export-app",
)


class DocumentationContractTests(unittest.TestCase):
    def test_active_team_guidance_uses_the_current_cli(self) -> None:
        contents = {path: path.read_text(encoding="utf-8") for path in DOCS}
        for path, document in contents.items():
            with self.subTest(path=path.relative_to(ROOT)):
                for term in RETIRED_TERMS:
                    self.assertNotIn(term, document)

        readme = contents[ROOT / "README.md"]
        for command in (
            "doctor",
            "export 100",
            "publish 100",
            "check-conflicts",
            "migrate migrations/",
            "backup-db",
            "deploy 100 --env staging",
            "--manual",
        ):
            with self.subTest(command=command):
                self.assertIn(command, readme)

    def test_active_markdown_links_resolve(self) -> None:
        for path in DOCS:
            content = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
                if "://" in target or target.startswith("#"):
                    continue
                relative_target = target.split("#", 1)[0]
                if not relative_target:
                    continue
                resolved = (path.parent / relative_target).resolve()
                with self.subTest(file=path.relative_to(ROOT), target=target):
                    self.assertTrue(resolved.exists(), f"broken local Markdown link: {target}")


if __name__ == "__main__":
    unittest.main()
