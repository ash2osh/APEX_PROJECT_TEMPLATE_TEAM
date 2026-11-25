import json
import re
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from pathlib import Path

from scripts.upgrade_template import protected_project_path


ROOT = Path(__file__).resolve().parents[1]


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    # Mirrors scripts/upgrade_template.py so the test does not import it.
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


class TemplateManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "template-manifest.json").read_text(encoding="utf-8"))
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        self.tracked = [path for path in tracked.split("\0") if path]

    def classify(self, path: str) -> list[str]:
        owners = []
        if any(glob_to_regex(p).match(path) for p in self.manifest["templateOwned"]):
            owners.append("templateOwned")
        if path in self.manifest["projectOwned"]:
            owners.append("projectOwned")
        if any(glob_to_regex(p).match(path) for p in self.manifest["templateOnly"]):
            owners.append("templateOnly")
        return owners

    def assert_tracked_ownership_valid(self, tracked: list[str]) -> None:
        for path in tracked:
            owners = self.classify(path)
            with self.subTest(path=path):
                if protected_project_path(path):
                    self.assertEqual(owners, [])
                elif owners:
                    self.assertEqual(len(owners), 1, owners)

    def test_tracked_files_are_not_ambiguously_owned_and_protected_paths_are_unowned(self) -> None:
        self.assert_tracked_ownership_valid(self.tracked)

    def test_release_files_are_covered_by_template_ownership(self) -> None:
        for path in (
            "scripts/release.py",
            "tests/test_release.py",
            "docs/release-workflow.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])

    def test_release_bundles_are_protected_from_template_upgrades(self) -> None:
        for path in (
            "releases/current.json",
            "releases/2026-11-24-fix/release.json",
            "releases/2026-11-24-fix/01-ddl/001-table.sql",
            "releases/2026-11-24-fix/02-code/pkg.sql",
        ):
            with self.subTest(path=path):
                self.assertTrue(protected_project_path(path))
                self.assertEqual(self.classify(path), [])

    def test_dev_test_runtime_docs_are_owned_and_project_suites_are_protected(self) -> None:
        from scripts.upgrade_template import protected_project_path

        for path in (
            "scripts/dev_test.py",
            "scripts/dev_test_core.py",
            "scripts/dev_test_checks.py",
            "tests/test_dev_test_cli.py",
            "docs/dev-test.md",
            "docs/dev-test-examples/example.json",
            "docs/dev-test-examples/sql/create-and-seed.sql",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])
        for path in (
            "dev-tests/suites/orders.json",
            "dev-tests/suites/sql/create-orders.sql",
        ):
            with self.subTest(path=path):
                self.assertTrue(protected_project_path(path))
                self.assertEqual(self.classify(path), [])

    def test_read_only_query_files_are_covered_by_template_ownership(self) -> None:
        for path in (
            "scripts/read_only_query.py",
            "tests/test_read_only_query.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])

    def test_backup_manifest_files_are_covered_by_template_ownership(self) -> None:
        for path in (
            "scripts/backup_manifest.py",
            "tests/test_backup_manifest.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])

    def test_preflight_catalog_and_recorded_fixtures_are_template_owned(self) -> None:
        for path in (
            "scripts/schema_catalog.py",
            "scripts/schema_catalog.sql",
            "tests/test_schema_catalog.py",
            "tests/fixtures/schema_catalog/view-synonyms.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])

    def test_compare_env_files_and_recorded_catalogs_are_template_owned(self) -> None:
        for path in (
            "scripts/compare_schema.py",
            "scripts/compare_env_catalog.sql",
            "docs/compare-env.md",
            "tests/test_compare_env.py",
            "tests/fixtures/compare_env/dev.json",
            "tests/fixtures/compare_env/staging.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])

    def test_browser_mcp_feature_files_are_upgrade_owned_and_project_rules_are_protected(self) -> None:
        for path in (
            ".gitignore",
            "AGENTS.md",
            "README.md",
            "scripts/team.sh",
            "scripts/team.ps1",
            "scripts/browser_mcp.py",
            "docs/browser-mcp.md",
            ".agents/skills/chrome-devtools-mcp/SKILL.md",
            ".agents/skills/firefox-devtools-mcp/SKILL.md",
            ".claude/skills/firefox-devtools-mcp/SKILL.md",
            "tests/test_browser_mcp.py",
            "template-manifest.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])
        for path in (
            "AGENTS.project.md",
            ".agents/rules/project.md",
            ".agents/styles/personal.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["projectOwned"])
        self.assertEqual(self.classify("docs/superpowers/plans/local-plan.md"), ["templateOnly"])

    def test_template_declares_the_qualified_release_boundary(self) -> None:
        from scripts.upgrade_template import load_manifest
        manifest = load_manifest(ROOT)
        self.assertEqual(manifest["schemaVersion"], 2)
        self.assertEqual(manifest["apexRelease"], "26.2")
        self.assertEqual(manifest["minimumSqlclVersion"], "26.3.0.0")
        self.assertEqual(
            manifest["upstream"],
            "https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM_26_2.git",
        )

    def test_every_non_project_template_file_has_exactly_one_owner(self) -> None:
        for path in self.tracked:
            if protected_project_path(path):
                continue
            with self.subTest(path=path):
                self.assertEqual(len(self.classify(path)), 1, path)

    def test_downstream_data_and_lock_can_be_committed_without_template_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            downstream = Path(temporary)
            subprocess.run(["git", "init", "-q", "-b", "main", str(downstream)], check=True)
            subprocess.run(["git", "-C", str(downstream), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(downstream), "config", "user.name", "Test"], check=True)
            downstream_files = {
                "template-manifest.json": (ROOT / "template-manifest.json").read_text(encoding="utf-8"),
                ".template-lock.json": json.dumps({"schemaVersion": 1, "upstream": "template", "commit": "abc", "files": {}}),
                "apps/DEMO/100/application.apx": "app DEMO ()\n",
                "releases/2026-11-24-customers/release.json": "{}\n",
                "releases/2026-11-24-customers/01-ddl/001-create-table.sql": "CREATE TABLE CUSTOMERS (ID NUMBER);\n",
                "releases/2026-11-24-customers/02-code/pkg.sql": "CREATE OR REPLACE PACKAGE PKG AS END;\n/\n",
                "docs/architecture.md": "Downstream-owned documentation.\n",
            }
            for relative, contents in downstream_files.items():
                target = downstream / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
            subprocess.run(["git", "-C", str(downstream), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(downstream), "commit", "-q", "-m", "downstream data"],
                check=True,
            )
            tracked = subprocess.run(
                ["git", "-C", str(downstream), "ls-files"], capture_output=True, text=True, check=True
            ).stdout.splitlines()

        self.assert_tracked_ownership_valid(tracked)
        for path in (
            ".template-lock.json",
            "apps/DEMO/100/application.apx",
            "releases/2026-11-24-customers/release.json",
            "releases/2026-11-24-customers/01-ddl/001-create-table.sql",
            "releases/2026-11-24-customers/02-code/pkg.sql",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), [])

    def test_project_placeholders_exist_and_are_not_template_owned(self) -> None:
        for path in self.manifest["projectOwned"]:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())
                self.assertEqual(self.classify(path), ["projectOwned"])

    def test_release_guide_is_template_owned_and_bundle_payload_is_not(self) -> None:
        for path in (
            "docs/release-workflow.md",
            "docs/template-feedback.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), ["templateOwned"])
        for path in (
            "releases/2026-11-24-customers/release.json",
            "releases/2026-11-24-customers/01-ddl/001-create-table.sql",
            "releases/2026-11-24-customers/02-code/pkg.sql",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), [])

    def test_project_data_is_never_managed(self) -> None:
        for path in (
            "apps/DEMO/100/application.apx",
            "database/DEMO/tables/T.sql",
            "releases/2026-11-24-customers/release.json",
            "releases/2026-11-24-customers/01-ddl/001-create-table.sql",
            "app_context/100/purpose.md",
            ".env",
            ".template-lock.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.classify(path), [])

    def test_agent_entry_points_import_project_instructions(self) -> None:
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", claude)
        self.assertIn("@AGENTS.project.md", claude)
        # The release guide is linked from AGENTS.md, not imported: an import loads it into every conversation.
        self.assertNotIn("@docs/release-workflow.md", claude)
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("AGENTS.project.md", agents)
        self.assertIn(".agents/rules/project.md", agents)
        self.assertIn("docs/release-workflow.md", agents)


if __name__ == "__main__":
    unittest.main()
