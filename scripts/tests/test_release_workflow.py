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
        self.assertIn("runs-on: [self-hosted, team-apex, test]", release)
        self.assertEqual(release.count("run-release-test"), 1)
        self.assertNotIn("TEAM_TEST_HISTORY_JSON", release)
        self.assertNotIn("apply_release.sh", release)
        self.assertNotIn("test-plan.json", release)
        self.assertNotIn("apply-report.json", release)
        self.assertIn("sign-test-evidence", release)
        self.assertIn("--archive scratch/release/release.tar", release)
        self.assertIn("TEAM_TEST_ENV_CONTENT: ${{ secrets.TEAM_TEST_ENV_CONTENT }}", release)
        self.assertIn("gen-runbook", release)
        self.assertIn("TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}", release)
        self.assertIn("cancel-in-progress: false", integration)
        self.assertIn("concurrency:", release)
        self.assertIn("group: example-team-apex-test", release)
        self.assertIn("cancel-in-progress: false", release)
        self.assertNotIn("id-token: write", release)
        self.assertNotIn("production-secrets", release)
        self.assertIn("schema/v[0-9]+.[0-9]+.[0-9]+", release)
        self.assertIn("app/**/v[0-9]+.[0-9]+.[0-9]+", release)
        self.assertIn("--kind", release)

    def test_release_generates_and_signs_test_evidence_in_order(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        ordered = [release.index(token) for token in ("run-release-test", "sign-test-evidence", "gen-runbook")]
        self.assertEqual(ordered, sorted(ordered))
        self.assertIn("TEAM_TEST_SIGNING_KEY_CONTENT: ${{ secrets.TEAM_TEST_SIGNING_KEY_CONTENT }}", release)
        self.assertIn("$RUNNER_TEMP/test-signing-key.pem", release)
        self.assertIn("umask 077", release)
        self.assertIn("chmod 600", release)
        self.assertIn("if: always()", release)
        self.assertIn("find \"$RUNNER_TEMP\" -maxdepth 1 -type f", release)
        self.assertIn("-name test-signing-key.pem", release)
        self.assertNotIn("TEAM_TEST_EVIDENCE", release)
        self.assertNotIn("TEAM_TEST_SIGNATURE", release)
        self.assertIn("$RUNNER_TEMP/test-evidence.json", release)
        self.assertIn("$RUNNER_TEMP/test-evidence.sig", release)
        self.assertIn("if-no-files-found: error", release)
        self.assertIn("test.env", release)
        self.assertIn("test-trust-key.pem", release)
        self.assertIn("production-history.json", release)
        self.assertIn("Remove protected handoff inputs", release)

    def test_release_tags_and_namespacing(self):
        from teamlib.release import ReleaseError, validate_release_identity
        commit_a = "a" * 40
        commit_b = "b" * 40
        # Valid schema tag
        validate_release_identity("schema/v1.0.0", "1.0.0", commit_a, {}, kind="schema")
        validate_release_identity("refs/tags/schema/v1.0.0", "1.0.0", commit_a, {}, kind="schema")
        # Valid app tag
        validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")
        validate_release_identity("refs/tags/app/hr/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")

        # Tag / version mismatch
        with self.assertRaises(ReleaseError):
            validate_release_identity("schema/v1.0.1", "1.0.0", commit_a, {}, kind="schema")
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/hr/v1.0.1", "1.0.0", commit_a, {}, kind="app", alias="hr")

        # Tag / alias mismatch
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/payroll/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")

        # Tag / kind mismatch
        with self.assertRaises(ReleaseError):
            validate_release_identity("schema/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_a, {}, kind="schema")

        # Namespacing in release records: schema/v1.0.0 and app/hr/v1.0.0 do not collide
        records = {
            "schema/v1.0.0": {"source_commit": commit_a, "archive_digest": "1" * 64},
            "app/hr/v1.0.0": {"source_commit": commit_b, "archive_digest": "2" * 64},
        }
        validate_release_identity("schema/v1.0.0", "1.0.0", commit_a, records, kind="schema")
        validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_b, records, kind="app", alias="hr")
        with self.assertRaises(ReleaseError):
            validate_release_identity("schema/v1.0.0", "1.0.0", commit_b, records, kind="schema")
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_a, records, kind="app", alias="hr")


if __name__ == "__main__":
    unittest.main()
