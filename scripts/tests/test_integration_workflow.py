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
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertEqual(workflow.count("runs-on:"), 1)
        self.assertIn("runs-on: [self-hosted, team-apex, integration]", workflow)
        self.assertIn("environment: integration", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertEqual(workflow.count("run-integration"), 1)
        for removed in ("setup-state", "adopt-frontier", "check-drift", "migrate ", "deploy-app", "qualify-target"):
            self.assertNotIn(removed, workflow)
        self.assertIn("TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}", workflow)
        self.assertIn("TEAM_ENV_CONTENT: ${{ secrets.TEAM_ENV_CONTENT }}", workflow)
        self.assertIn("GITHUB_SHA", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("upload-artifact", workflow)
        self.assertIn("if-no-files-found: warn", workflow)
        self.assertIn("find \"$RUNNER_TEMP\" -maxdepth 1 -name integration.env -type f -delete", workflow)


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

    def test_run_integration_has_single_output_argument_and_is_protected(self):
        import team

        parsed = team._parser().parse_args(["run-integration", "--out", "qualification.json"])
        self.assertEqual(parsed.command, "run-integration")
        self.assertEqual(parsed.out, "qualification.json")
        self.assertIn("run-integration", team.PRODUCTION_REFUSED_COMMANDS)

    def test_run_release_test_has_archive_target_output_and_is_protected(self):
        import team

        parsed = team._parser().parse_args([
            "run-release-test", "release.tar", "--target", "targets/test.json",
            "--out", "test-evidence.json",
        ])
        self.assertEqual(parsed.command, "run-release-test")
        self.assertEqual(parsed.archive, "release.tar")
        self.assertEqual(parsed.target, "targets/test.json")
        self.assertEqual(parsed.out, "test-evidence.json")
        self.assertIn("run-release-test", team.PRODUCTION_REFUSED_COMMANDS)

    def test_migration_lifecycle_commands_share_confirmation_and_drift_options(self):
        import team

        for command in ("migrate", "undo-migration", "redo-migration"):
            argv = [command]
            if command != "migrate":
                argv.append("20260910T120000__alice__example")
            argv.extend([
                "--source", "migrations", "--dry-run",
                "--expected-inventory", "expected.json", "--actual-inventory", "actual.json",
                "--destructive-confirmation", "confirmation.json",
            ])
            parsed = team._parser().parse_args(argv)
            self.assertEqual(parsed.command, command)
            self.assertEqual(parsed.destructive_confirmation, "confirmation.json")
            self.assertTrue(parsed.dry_run)
            self.assertEqual(parsed.source, "migrations")
        for command in ("migrate", "undo-migration", "redo-migration"):
            self.assertIn(command, team.PRODUCTION_REFUSED_COMMANDS)

    def test_integration_workflow_has_one_online_gate(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
        self.assertEqual(text.count("run-integration"), 1)
        self.assertNotIn("adopt-frontier", text)
        self.assertNotIn("check-drift", text)


if __name__ == "__main__":
    unittest.main()
