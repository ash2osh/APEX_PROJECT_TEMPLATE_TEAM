import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / ".agents" / "workflows" / "team-flow.md",
    ROOT / ".agents" / "rules" / "agent-safety.md",
    ROOT / "app_context" / "README.md",
    ROOT / "docs" / "publish-rules.md",
)
# Each refusal the publish guide explains, and the script that prints it. The
# guide quotes these verbatim, so rewording a message must update the guide.
PUBLISH_REFUSALS = (
    ("scripts/check_builder_drift.py", "Database export baseline is unavailable"),
    ("scripts/check_builder_drift.py", "Could not read live APEX App"),
    ("scripts/check_builder_drift.py", "was created after the local export"),
    ("scripts/check_builder_drift.py", "no longer exists in the target after the local export"),
    ("scripts/check_builder_drift.py", "was re-imported since the local export"),
    ("scripts/check_builder_drift.py", "was re-imported after the local export"),
    ("scripts/check_builder_drift.py", "was modified in Builder on"),
    ("scripts/check_builder_drift.py", "changed version since the local export"),
    ("scripts/check_builder_drift.py", "matches the current database second"),
    ("scripts/check_db_target.sh", "resembles production but DB_ENVIRONMENT"),
    ("scripts/team.sh", "publish targets DEV only"),
    ("scripts/publish_app.sh", "deployment descriptor not found"),
    ("scripts/validate_app_source.py", "application source is outside the repository"),
    ("scripts/validate_app_source.py", "symbolic links or reparse points are not supported"),
    ("scripts/publish_app.sh", "DEV publish needs application.apx to stamp the publish tag"),
    ("scripts/stamp_publish_version.py", "could not stamp the application version"),
    ("scripts/publish_app.sh", "SQLcl application import failed"),
    ("scripts/publish_app.sh", "SQLcl reported a client or database error during the application import"),
    ("scripts/publish_app.sh", "SQLcl did not verify the imported application"),
    ("scripts/publish_app.sh", "SQLcl did not report a successful APEX import"),
    ("scripts/publish_app.sh", "post-import APEX export failed"),
    ("scripts/verify_publish_state.py", "APEXlang source file set does not match the post-import re-export"),
    ("scripts/verify_publish_state.py", "APEXlang source bytes do not match the post-import re-export"),
    ("scripts/verify_publish_state.py", "is not visible in the post-import state"),
    ("scripts/verify_publish_state.py", "changed while its post-import source was being verified"),
    ("scripts/verify_publish_state.py", "same database second"),
    ("scripts/verify_publish_state.py", "later than the database-time observation"),
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
        self.assertIn("After a successful DEV import", readme)
        self.assertIn("APEXlang file names and bytes", readme)
        self.assertIn("leaves the old baseline in place", readme)
        self.assertIn("commands such as `SET DEFINE`", readme)
        self.assertIn("APEX does not stamp `last_updated_on` while importing", readme)
        self.assertIn("`CREATE MLE MODULE`", readme)
        self.assertIn("[ASHARIF-2026-09-26r001]", readme)
        self.assertIn("Commit the stamped", readme)
        self.assertIn("DEVELOPER_NAME", contents[ROOT / "AGENTS.md"])

    def test_publish_guide_explains_every_refusal_and_is_linked(self) -> None:
        guide = (ROOT / "docs" / "publish-rules.md").read_text(encoding="utf-8")
        for script, message in PUBLISH_REFUSALS:
            with self.subTest(message=message):
                self.assertIn(message, (ROOT / script).read_text(encoding="utf-8"))
                self.assertIn(message, guide)
        for path in (ROOT / "AGENTS.md", ROOT / "README.md", ROOT / ".agents" / "workflows" / "team-flow.md"):
            with self.subTest(pointer=path.name):
                self.assertIn("docs/publish-rules.md", path.read_text(encoding="utf-8"))

    def test_application_context_describes_only_current_numeric_paths_and_guards(self) -> None:
        context = (ROOT / "app_context" / "README.md").read_text(encoding="utf-8")
        self.assertIn("app_context/<numeric-app-id>/", context)
        self.assertIn("apps/<schema>/<numeric-app-id>/", context)
        self.assertIn("migrations/<developer>/", context)
        self.assertIn("release.json is not read", context)
        self.assertIn("do not enforce", context)
        self.assertNotIn("app_context/<alias>/", context)
        self.assertNotIn("release builder resolves", context)

    def test_ci_runs_behavioral_unittest_suite(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "database-checks.yml").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover -s tests -v", workflow)

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
