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
    def test_current_operator_guidance_has_no_retired_or_unsafe_paths(self):
        operator_paths = [
            ROOT / "README.md",
            ROOT / ".env.example",
            *[
                path
                for path in sorted((ROOT / "docs").glob("*.md"))
                if path.name != "design-review-resolution.md"
            ],
            ROOT / "docs" / "working-on-apex-together.html",
            ROOT / "ci" / "app-checks" / "README.md",
        ]
        operator_docs = "\n".join(
            path.read_text(encoding="utf-8") for path in operator_paths
        )
        retired_or_unsafe = (
            "scripts/tests/live",
            "ci-replay",
            "CI therefore builds a disposable schema",
            "required by migration/replay commands",
            'git commit -am "Describe the Builder change"',
            "git diff -- apps/<alias>/ .sync-state/",
        )
        for token in retired_or_unsafe:
            self.assertNotIn(token, operator_docs)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for token in (
            "git status --short --untracked-files=all",
            "git add -- apps/<alias>/",
            "git diff --cached",
            "git pull --rebase",
            "git push",
        ):
            self.assertIn(token, readme)
        self.assertNotIn("git add -A", readme)
        self.assertIn("TEAM_ASSERT|", operator_docs)
        self.assertIn("exact Git commit", operator_docs)
        self.assertIn("verified archive bytes", operator_docs)

    def test_required_workflow_documents_and_links_exist(self):
        for relative in (
            "README.md", "docs/ci.md", "docs/promotion.md", "docs/app-recovery.md",
            "docs/conflict-resolution.md", "docs/import-pause.md", "docs/migrations.md",
            "docs/design-review-resolution.md", "AGENTS.md", ".agents/workflows/team-flow.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("no import in the normal builder-first loop", readme.lower())
        self.assertIn("release.tar", (ROOT / "docs/promotion.md").read_text(encoding="utf-8"))
        self.assertIn("UNKNOWN", (ROOT / "docs/ci.md").read_text(encoding="utf-8"))

    def test_readme_structure_and_contract(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        # 1. Environment examples: one same-schema and one split-schema
        self.assertIn("APEX_APPS=hr:100:APP,payroll:200:APP", readme)
        self.assertIn("APEX_APPS=hr:100:HR_CODE,payroll:200:FIN_CODE", readme)

        # 2. Daily workflows
        self.assertIn("scripts/team.sh doctor", readme)
        self.assertIn("scripts/team.sh export-app hr", readme)
        self.assertIn("scripts/team.sh prepare-publish hr", readme)
        self.assertIn("scripts/team.sh publish-app", readme)

        # 3. Omar's explicit pause acknowledgement
        self.assertIn("--ack hr:<Omar's registered checkout UUID>", readme)

        # 4. Schema-then-HR release
        self.assertIn("build-release --kind schema", readme)
        self.assertIn("build-release --kind app --alias hr", readme)

        # 5. Unsaved Builder caveat
        self.assertIn("unsaved", readme.lower())
        self.assertIn("informational", readme.lower())

        # 6. Every fenced scripts/team.sh <command> maps to COMMAND_HELP or parser
        import argparse
        from team import COMMAND_HELP, _parser
        parser = _parser()
        subparsers_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        subcommands = set(subparsers_action.choices.keys()) | set(COMMAND_HELP.keys())
        command_pattern = re.compile(r"scripts/team\.(?:sh|py)(?:\s+--env\s+\S+)?\s+([a-z0-9_-]+)")
        for match in command_pattern.finditer(readme):
            cmd = match.group(1)
            if cmd.startswith("-"):
                continue
            self.assertIn(cmd, subcommands, f"Command {cmd} found in README is not recognized in team CLI")

    def test_agent_contract_and_runbook_alignment(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for token in (
            "APEX 26.1+",
            "APEXlang",
            "Builder",
            "prepare-publish",
            "publish-app",
            "app-scoped",
            "acknowledgement",
            "lock",
            "before/after",
            "selected",
            "production",
            ".sync-state/",
        ):
            self.assertIn(token.lower(), agents.lower(), f"AGENTS.md missing {token}")

        operator_paths = [
            ROOT / "README.md",
            ROOT / ".env.example",
            *[
                path
                for path in sorted((ROOT / "docs").glob("*.md"))
                if path.name not in {"design-review-resolution.md", "local-three-developer-e2e.md"}
            ],
            ROOT / "docs" / "working-on-apex-together.html",
            ROOT / "ci" / "app-checks" / "README.md",
        ]
        operator_docs = "\n".join(path.read_text(encoding="utf-8") for path in operator_paths)
        for forbidden in (
            "all apps pause for one import",
            "all applications pause for one import",
            "all apps deploy for one app release",
            "deploys every configured application for a single release",
        ):
            self.assertNotIn(forbidden, operator_docs.lower())

    def test_persistent_qualification_documentation_matches_the_contract(self):
        paths = (
            ROOT / "README.md",
            ROOT / "docs" / "ci.md",
            ROOT / "docs" / "promotion.md",
            ROOT / "ci" / "app-checks" / "README.md",
            ROOT / ".github" / "workflows" / "integration.yml",
            ROOT / ".github" / "workflows" / "release.yml",
        )
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        for required in (
            "run-integration",
            "run-release-test",
            "TEAM_ASSERT|",
            "self-hosted",
            "role: test",
            "observation_digest",
            "destructive-confirmation",
            "persistent",
            "does not prove a fresh installation",
            "qualify-target",
            "sign-test-evidence",
            "target_kind: persistent",
            "version 2",
            "TEAM_FLOW_RUNNER",
            "workflow_dispatch",
            "Persistent staging does not prove fresh installation",
        ):
            self.assertIn(required, text)
        for removed in (
            "ci-replay",
            "disposable Oracle",
            "fresh result",
            "upgrade result",
            "qualification_sha",
            "replay_identity",
        ):
            self.assertNotIn(removed.lower(), text.lower())
        operator_docs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in paths
            if path.name not in {"2026-09-10-repo-review-p1-remediation-and-flow-simplification-design.md"}
        )
        for obsolete in ("TEAM_TEST_HISTORY_JSON", "test-plan.json", "apply-report.json"):
            self.assertNotIn(obsolete, operator_docs)

    def test_migration_lifecycle_documentation_matches_the_contract(self):
        text = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in ("README.md", "docs/migrations.md", "docs/promotion.md")
        )
        for required in (
            ".down.sql",
            ".down.verify.sql",
            "global LIFO",
            "REVERTED",
            "applied_sequence",
            "metadata v2",
            "--destructive-confirmation",
            "payload_target_state_key",
            "confirmed: false",
            "non-production",
            "roll back",
            "APEX Builder import",
        ):
            self.assertIn(required, text)
        for forbidden in ("force flag", "automatically generated rollback"):
            self.assertNotIn(forbidden.lower(), text.lower())

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
        workflow = (ROOT / ".github/workflows/database-checks.yml").read_text(encoding="utf-8")
        self.assertIn("ruff check scripts/", workflow)

    def test_one_offline_workflow_owns_the_pull_request_gate(self):
        """Two workflows both ran the whole suite on every push and PR.

        The offline gate is a single job so the suite runs once; a second
        workflow that repeats it doubles CI time without adding a check.
        """
        workflows = sorted(path.name for path in (ROOT / ".github/workflows").glob("*.yml"))
        self.assertEqual(workflows, ["database-checks.yml", "integration.yml", "release.yml"])
        offline = (ROOT / ".github/workflows/database-checks.yml").read_text(encoding="utf-8")
        for expected in (
            "ruff check scripts/",
            "unittest discover -s scripts/tests",
            "ci-doctor --contract ci/runner-contract.json",
            "bash -n",
            "json.tool",
        ):
            with self.subTest(step=expected):
                self.assertIn(expected, offline)
        self.assertEqual(offline.count("unittest discover"), 1)

    def test_no_string_literal_imports_outside_the_command_dispatcher(self):
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "__import__(" in line and "teamlib." not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [], f"use ordinary imports: {offenders}")


class ReleaseWorkflowDependencyTests(unittest.TestCase):
    def test_test_runner_declares_the_promotion_dependency(self):
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("gen-runbook", text)
        self.assertIn(
            "runs-on: [self-hosted, team-apex, test]",
            text,
            "the protected test runner carries the signing and verification dependency",
        )
        self.assertIn("TEAM_TRUST_KEY_CONTENT", text)


if __name__ == "__main__":
    unittest.main()
