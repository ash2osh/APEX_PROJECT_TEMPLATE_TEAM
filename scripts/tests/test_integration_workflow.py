from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class IntegrationWorkflowTests(unittest.TestCase):
    def test_integration_workflow_is_a_real_exact_sha_gate(self):
        workflow = (ROOT / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
        self.assertIn("needs:", workflow)
        self.assertIn("migrate", workflow)
        self.assertIn("deploy-app", workflow)
        self.assertIn("TEAM_ENV_FILE", workflow)
        self.assertIn("TEAM_APP_ALIASES", workflow)
        self.assertIn("GITHUB_SHA", workflow)
        self.assertNotIn("Use the qualified adapter", workflow)
        self.assertNotIn("echo \"Integration credentials", workflow)


class FrontierAdoptionTests(unittest.TestCase):
    def test_adopt_frontier_is_a_known_command(self):
        import team

        parsed = team._parser().parse_args(["adopt-frontier"])
        self.assertEqual(parsed.command, "adopt-frontier")

    def test_adopt_frontier_is_a_production_refused_command(self):
        import team

        self.assertIn("adopt-frontier", team.PRODUCTION_REFUSED_COMMANDS)
        self.assertIn("setup-state", team.PRODUCTION_REFUSED_COMMANDS)

    def test_qualify_target_has_exact_source_and_alias_arguments(self):
        import team

        parsed = team._parser().parse_args([
            "qualify-target", "--source-commit", "a" * 40,
            "--aliases", "employee", "--out", "qualification.json",
        ])
        self.assertEqual(parsed.command, "qualify-target")
        self.assertEqual(parsed.aliases, "employee")
        self.assertIn("qualify-target", team.PRODUCTION_REFUSED_COMMANDS)

    def test_integration_workflow_adopts_before_it_checks_drift(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
        self.assertIn("adopt-frontier", text)
        self.assertLess(
            text.index("adopt-frontier"),
            text.index("check-drift"),
            "check-drift exits 3 until a frontier exists, so adoption must come first",
        )


if __name__ == "__main__":
    unittest.main()
