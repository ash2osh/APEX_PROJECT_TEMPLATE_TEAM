from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WorkflowContractTests(unittest.TestCase):
    def test_workflows_use_exact_sha_and_never_pull_request_target(self):
        database = (ROOT / ".github" / "workflows" / "database-checks.yml").read_text(encoding="utf-8")
        integration = (ROOT / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for workflow in (database, integration, release):
            self.assertNotIn("pull_request_target", workflow)
            self.assertIn("github.sha", workflow)
        self.assertEqual(database.count("\n  offline:"), 1)
        self.assertNotIn("ci-" + "replay", database)
        self.assertNotIn("docker", database.lower())
        self.assertNotIn("oracle/free", database.lower())
        self.assertNotIn("command -v sql", database)
        self.assertNotIn("upload-artifact", database)
        self.assertIn("verify-release", release)
        self.assertIn("release.tar", release)
        self.assertIn("apply_release.sh", release)
        self.assertIn("TEAM_TEST_ENV_FILE", release)
        self.assertIn("gen-runbook", release)
        self.assertIn("TEST_EVIDENCE", release)
        self.assertIn("cancel-in-progress: false", integration)
        self.assertNotIn("production-secrets", release)

    def test_contract_declares_only_non_secret_runner_requirements(self):
        contract = (ROOT / "ci" / "runner-contract.json").read_text(encoding="utf-8")
        self.assertIn('"cryptography": "Ed25519-qualified"', contract)
        self.assertIn('"credentials": false', contract)
        self.assertIn('"writes": false', contract)
        self.assertNotIn("provisioner", contract)
        self.assertNotIn("runner", contract)


if __name__ == "__main__":
    unittest.main()
