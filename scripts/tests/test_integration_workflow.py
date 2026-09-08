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


if __name__ == "__main__":
    unittest.main()
