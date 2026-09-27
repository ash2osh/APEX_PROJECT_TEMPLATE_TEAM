from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "APEX_THEME_FACTORY",
    "THEME_FACTORY_CHROME",
    "app 102",
    "localhost:8181",
    "page 406",
)


class ChromeDevToolsDocumentationTests(unittest.TestCase):
    def test_browser_skill_uses_daemon_cli(self):
        path = ROOT / ".agents/skills/chrome-devtools-mcp/SKILL.md"
        self.assertTrue(path.is_file(), "project browser skill must exist")
        skill = path.read_text(encoding="utf-8")
        self.assertIn("tools/chrome_devtools_client.py", skill)
        self.assertIn("tools/chrome_mcp_daemon.py", skill)
        self.assertIn("list_pages", skill)
        self.assertIn("does not start", skill)

    def test_browser_docs_are_project_neutral(self):
        path = ROOT / "docs/CHROME_DEVTOOLS_MCP.md"
        self.assertTrue(path.is_file(), "portable Chrome daemon guide must exist")
        docs = path.read_text(encoding="utf-8")
        for required in (
            "chrome-devtools-mcp",
            "python3 tools/chrome_mcp_daemon.py",
            "python3 tools/chrome_devtools_client.py",
            "CHROME_MCP_SOCKET",
            "CHROME_MCP_TIMEOUT",
            "CHROME_MCP_EXECUTABLE",
            "does not start",
            "take_snapshot",
            "take_screenshot",
            "list_console_messages",
            "list_network_requests",
        ):
            with self.subTest(required=required):
                self.assertIn(required, docs)
        for forbidden in FORBIDDEN:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), docs.lower())

    def test_project_entry_points_link_browser_docs(self):
        browser_skill_path = ".agents/skills/chrome-devtools-mcp/SKILL.md"
        guide_path = "docs/CHROME_DEVTOOLS_MCP.md"
        project_rule = (ROOT / ".agents/rules/project.md").read_text(encoding="utf-8")
        project_agents = (ROOT / "AGENTS.project.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("chrome-devtools-mcp", project_rule)
        self.assertIn("chrome-devtools-mcp", project_agents)
        self.assertIn("../skills/chrome-devtools-mcp/SKILL.md", project_rule)
        self.assertIn("../../docs/CHROME_DEVTOOLS_MCP.md", project_rule)
        self.assertIn(browser_skill_path, project_agents)
        self.assertIn(guide_path, project_agents)
        self.assertIn(f"]({guide_path})", readme)
        self.assertIn(f"]({browser_skill_path})", readme)

        links = (
            (ROOT / "AGENTS.project.md", guide_path),
            (ROOT / "AGENTS.project.md", browser_skill_path),
            (ROOT / "README.md", guide_path),
            (ROOT / "README.md", browser_skill_path),
            (ROOT / ".agents/rules/project.md", "../../docs/CHROME_DEVTOOLS_MCP.md"),
            (ROOT / ".agents/rules/project.md", "../skills/chrome-devtools-mcp/SKILL.md"),
            (ROOT / ".agents/skills/chrome-devtools-mcp/SKILL.md", "../../../docs/CHROME_DEVTOOLS_MCP.md"),
        )
        for source, relative in links:
            with self.subTest(source=source.name, target=relative):
                self.assertTrue((source.parent / relative).resolve().is_file())


if __name__ == "__main__":
    unittest.main()
