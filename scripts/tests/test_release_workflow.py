from __future__ import annotations

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
        self.assertIn("ci-replay", database)
        self.assertIn("verify-release", release)
        self.assertIn("release.tar", release)
        self.assertIn("apply_release.sh", release)
        self.assertIn("TEAM_TEST_ENV_FILE", release)
        self.assertIn("gen-runbook", release)
        self.assertIn("TEST_EVIDENCE", release)
        self.assertIn("cancel-in-progress: false", integration)
        self.assertNotIn("production-secrets", release)

    def test_contract_and_reference_scripts_are_executable(self):
        for path in (ROOT / "ci" / "provisioners" / "docker_pdb.sh", ROOT / "scripts" / "ci_replay_runner.py"):
            self.assertTrue(path.stat().st_mode & 0o111, path)
        contract = (ROOT / "ci" / "runner-contract.json").read_text(encoding="utf-8")
        self.assertIn("@sha256:", contract)
        self.assertIn('"credentials": false', contract)


if __name__ == "__main__":
    unittest.main()
