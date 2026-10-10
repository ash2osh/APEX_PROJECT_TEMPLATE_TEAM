import json
import re
import subprocess
import tempfile
import unittest
from fake_sqlcl import BASH
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "README.md",
    ROOT / ".agents" / "workflows" / "team-flow.md",
    ROOT / ".agents" / "rules" / "agent-safety.md",
    ROOT / "app_context" / "README.md",
    ROOT / "migrations" / "README.md",
    ROOT / "docs" / "migration-rules.md",
    ROOT / "docs" / "baseline.md",
    ROOT / "docs" / "compare-env.md",
    ROOT / "docs" / "publish-rules.md",
    ROOT / "docs" / "GETTING_STARTED.md",
    ROOT / "docs" / "EXAMPLES.md",
    ROOT / "docs" / "TROUBLESHOOTING.md",
    ROOT / "docs" / "known-limitations.md",
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
    ("scripts/publish_app.sh", "is stored under apps/"),
    ("scripts/publish_app.sh", "does not match the application's parsing schema"),
    ("scripts/publish_app.sh", "is not listed in APEX_PARSING_SCHEMA"),
    ("scripts/publish_app.sh", "is not listed in STAGING_SCHEMA"),
    ("scripts/publish_app.sh", "is parsed by"),
    ("scripts/validate_app_source.py", "application source is outside the repository"),
    ("scripts/validate_app_source.py", "symbolic links or reparse points are not supported"),
    ("scripts/validate_app_source.py", "the application path contains characters SQLcl cannot pass"),
    ("scripts/publish_app.sql", "is parsed by"),
    ("scripts/publish_app.sql", "Live application changed after the Builder drift check"),
    ("scripts/publish_app.sh", "changed while publishing; left as is"),
    ("scripts/publish_app.sh", "DEV publish needs application.apx to stamp the publish tag"),
    ("scripts/publish_app.sh", "application.apx changed while the publish tag was stamped"),
    ("scripts/publish_app.sh", "could not move application.apx to stamp the publish tag"),
    ("scripts/publish_app.ps1", "could not move application.apx to stamp the publish tag"),
    ("scripts/publish_app.sh", "could not install the stamped application.apx"),
    ("scripts/publish_app.sh", "could not remove the publish tag"),
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
            "team.sh migrate",
            "team.sh revise",
            "revise --check",
            "--rehearse",
            "--report scratch/customer-seed-rehearsal.json",
            "team.sh rollout",
            "--env dev",
            "compare-schema",
            "compare-env --from dev --to staging",
            "baseline export-source --from dev",
            "baseline export-grants --from dev",
            "baseline build --from dev --to staging",
            "--emit-dba-script",
            "status.<env>.json",
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
        self.assertIn("scripts/team.sh upgrade-template", readme)
        self.assertIn("AGENTS.project.md", readme)
        self.assertIn(".template-new", readme)
        self.assertIn("python3 /tmp/apex-template/scripts/upgrade_template.py", readme)
        self.assertIn("2026-09-27_create-customers-r001", readme)
        self.assertIn("apply-not-started", readme)
        migration_guide = contents[ROOT / "docs" / "migration-rules.md"]
        self.assertIn("MIGRATION_PAYLOAD_STARTED", migration_guide)
        self.assertIn("ORA-20987", migration_guide)
        self.assertIn("1,856", migration_guide)
        self.assertIn("other developers' pending files", migration_guide)
        self.assertIn("status.<env>.json", migration_guide)
        self.assertIn("STAGING_SCHEMA", migration_guide)
        self.assertIn("cannot reliably prove", readme)
        self.assertIn("## Rollout", migration_guide)
        self.assertIn("## Comparing full environments", migration_guide)
        self.assertIn("not compared (no DBA connection)", migration_guide)
        self.assertIn("by label, not id", migration_guide)
        self.assertIn("ERP and camp data", migration_guide)
        self.assertIn("one manifest-level confirmation", migration_guide)
        self.assertIn("rollout-manifest.example.json", migration_guide)
        for required in (
            "## Rehearsal",
            "AUTOCOMMIT OFF",
            "SAFE_FUNCTIONS",
            "not rehearsable",
            "preconditions re-run after `ROLLBACK`",
            "--report <file>",
            "scratch/migration-rehearsal-*",
        ):
            with self.subTest(required=required):
                self.assertIn(required, migration_guide)
        limitations = contents[ROOT / "docs" / "known-limitations.md"]
        for limitation in ("whitespace-only lines", "trailing hyphen", "DBMS_LOB.APPEND", "five-minute", "patch levels", "compare-env", "ORA_HASH"):
            with self.subTest(limitation=limitation):
                self.assertIn(limitation, limitations)
        compare_guide = contents[ROOT / "docs" / "compare-env.md"]
        for phrase in (
            "tables", "constraints", "object-grants", "network-aces", "ORDS",
            "not compared (no DBA connection)", "by label, not id", "ERP and camp data",
            "| 0 |", "| 1 |", "| 2 |", "500", "100,000", "128 MiB",
        ):
            with self.subTest(compare_env=phrase):
                self.assertIn(phrase, compare_guide)

    def test_publish_guide_explains_every_refusal_and_is_linked(self) -> None:
        guide = (ROOT / "docs" / "publish-rules.md").read_text(encoding="utf-8")
        for script, message in PUBLISH_REFUSALS:
            with self.subTest(message=message):
                self.assertIn(message, (ROOT / script).read_text(encoding="utf-8"))
                self.assertIn(message, guide)
        for path in (ROOT / "AGENTS.md", ROOT / "README.md", ROOT / ".agents" / "workflows" / "team-flow.md"):
            with self.subTest(pointer=path.name):
                self.assertIn("docs/publish-rules.md", path.read_text(encoding="utf-8"))

    def test_publish_guide_explains_the_first_publish_of_a_generated_starter_app(self) -> None:
        # `apex generate` writes files APEX does not export back byte for byte, so the first
        # publish imports the app and then refuses to record a baseline; the guide says what to do.
        guide = (ROOT / "docs" / "publish-rules.md").read_text(encoding="utf-8")
        for phrase in ("apex generate", "extra blank line", "deinstall", "scripts/team.sh export <id>"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)

    def test_getting_started_gives_the_python_check_that_works_on_windows(self) -> None:
        # A python.org or winget install provides python.exe and py, but no python3.
        guide = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
        self.assertIn("py -3 --version", guide)

    def test_troubleshooting_explains_the_team_ps1_refusals_about_bash(self) -> None:
        # The table quotes these messages verbatim, so rewording one must update it.
        troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
        script = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        for message in (
            "TEAM_BASH is set but is not a file",
            "Bash is required for",
            "Git Bash cannot receive an argument with a single quote",
        ):
            with self.subTest(message=message):
                self.assertIn(message, script)
                self.assertIn(message, troubleshooting)

    def test_application_context_describes_only_current_numeric_paths_and_guards(self) -> None:
        context = (ROOT / "app_context" / "README.md").read_text(encoding="utf-8")
        self.assertIn("app_context/<numeric-app-id>/", context)
        self.assertIn("apps/<schema>/<numeric-app-id>/", context)
        self.assertIn("migrations/2026-09-27_create-customers-r001/", context)
        self.assertIn("release.json is not read", context)
        self.assertIn("do not enforce", context)
        self.assertNotIn("app_context/<alias>/", context)
        self.assertNotIn("release builder resolves", context)

    def test_migration_guidance_covers_folder_order_receipts_and_comparison_limits(self) -> None:
        guide = (ROOT / "docs" / "migration-rules.md").read_text(encoding="utf-8")
        for required in (
            "YYYY-MM-DD_<migration-name>-rNNN",
            "NNN-<step-name>.sql",
            "order supplied on the command line",
            "not discovery of",
            "STAGING_SCHEMA",
            "PROD_SCHEMA",
            "two developers can both pass",
            "not reliable migration-file attribution",
            "can mimic a migration",
            "cannot set the browser's sort direction",
            "apply-not-started",
            "revise --check",
            "MIGRATION_PAYLOAD_STARTED",
            "committed-source-or-payload-changed",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)
        self.assertNotIn("migrations/<developer>/", guide)

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


def heading_slugs(markdown: str) -> set[str]:
    """GitHub-style anchors for every heading outside fenced code blocks."""
    slugs = set()
    in_fence = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            text = re.sub(r"[`*_]", "", match.group(1)).lower()
            slugs.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text)))
    return slugs


def team_commands() -> list[str]:
    """The command names `scripts/team.sh --help` lists."""
    help_text = subprocess.run(
        [BASH, str(ROOT / "scripts" / "team.sh"), "--help"], capture_output=True, text=True, check=True
    ).stdout
    commands = re.findall(r"^  ([a-z][a-z-]+)(?: |$)", help_text.split("Options:")[0], re.MULTILINE)
    return [name for name in commands if name != "help"]


class GuideContractTests(unittest.TestCase):
    """The newcomer guides must stay true to the code and the skill folders."""

    def readme_section(self, heading: str) -> str:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        start = readme.index(f"## {heading}")
        end = readme.find("\n## ", start + 1)
        return readme[start:] if end == -1 else readme[start:end]

    def test_readme_lists_every_skill_and_only_real_skills(self) -> None:
        skills = {path.name for path in (ROOT / ".agents" / "skills").iterdir() if path.is_dir()}
        mirrored = {path.name for path in (ROOT / ".claude" / "skills").iterdir() if path.is_dir()}
        self.assertEqual(skills, mirrored, ".agents/skills and .claude/skills must list the same skills")
        section = self.readme_section("Skills and agent support")
        named = set(re.findall(r"`([a-z][a-z0-9-]+)`", section))
        self.assertEqual(set(), skills - named, "skills missing from the README skills list")
        ghosts = {
            name for name in named - skills
            if re.search(rf"(^\| `{re.escape(name)}` \|)", section, re.MULTILINE)
        }
        self.assertEqual(set(), ghosts, "README lists skills that do not exist")

    def test_readme_states_the_real_number_of_skills(self) -> None:
        skills = [path for path in (ROOT / ".agents" / "skills").iterdir() if path.is_dir()]
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        counts = {int(number) for number in re.findall(r"\b(\d+) skills\b", readme)}
        self.assertEqual({len(skills)}, counts)

    def test_every_skill_folder_has_a_described_skill_file(self) -> None:
        for path in sorted((ROOT / ".agents" / "skills").iterdir()):
            if path.is_dir():
                with self.subTest(skill=path.name):
                    text = (path / "SKILL.md").read_text(encoding="utf-8")
                    self.assertRegex(text, r"(?m)^description:\s*\S")

    def test_readme_command_reference_covers_every_team_command(self) -> None:
        reference = self.readme_section("Command reference")
        commands = team_commands()
        self.assertGreaterEqual(len(commands), 9)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(f"scripts/team.sh {command}", reference)

    def test_guides_only_use_real_team_commands(self) -> None:
        commands = set(team_commands())
        for name in ("GETTING_STARTED.md", "EXAMPLES.md", "TROUBLESHOOTING.md"):
            text = (ROOT / "docs" / name).read_text(encoding="utf-8")
            for used in re.findall(r"scripts/team\.(?:sh|ps1) ([a-z][a-z-]+)", text):
                with self.subTest(file=name, command=used):
                    self.assertIn(used, commands)

    def test_local_links_point_at_real_headings(self) -> None:
        for path in DOCS:
            content = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
                if "://" in target or "#" not in target:
                    continue
                file_part, fragment = target.split("#", 1)
                linked = path if not file_part else (path.parent / file_part).resolve()
                if linked.suffix != ".md" or not linked.exists():
                    continue
                with self.subTest(file=path.relative_to(ROOT), target=target):
                    self.assertIn(fragment, heading_slugs(linked.read_text(encoding="utf-8")))

    def test_json_examples_in_the_guides_parse(self) -> None:
        for name in ("GETTING_STARTED.md", "EXAMPLES.md"):
            text = (ROOT / "docs" / name).read_text(encoding="utf-8")
            for index, block in enumerate(re.findall(r"```json\n(.*?)```", text, re.DOTALL), start=1):
                with self.subTest(file=name, block=index):
                    json.loads(block)
        baseline_example = json.loads((ROOT / "docs" / "baseline.example.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline_example["schemaVersion"], 1)
        self.assertIn("schemas", baseline_example)

    def test_deployment_descriptor_example_has_the_keys_publish_reads(self) -> None:
        text = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
        descriptors = [
            json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL) if '"workspace"' in block
        ]
        self.assertEqual(1, len(descriptors))
        descriptor = descriptors[0]
        self.assertIsInstance(descriptor["workspace"]["name"], str)
        self.assertIsInstance(descriptor["app"]["id"], int)
        self.assertRegex(descriptor["app"]["databaseSession"]["parsingSchema"], r"^[A-Z][A-Z0-9_$#]*$")

    def test_checks_json_examples_are_accepted_by_the_migration_loader(self) -> None:
        from scripts import migration_manifest

        found = 0
        for name in ("GETTING_STARTED.md", "EXAMPLES.md"):
            text = (ROOT / "docs" / name).read_text(encoding="utf-8")
            for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
                if '"schemaVersion"' not in block:
                    continue
                found += 1
                with self.subTest(file=name, block=found), tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary) / "migrations" / "2026-09-30_example-r001"
                    folder.mkdir(parents=True)
                    (folder / "001-example.sql").write_text("ALTER TABLE HR_USERS ADD (NOTES VARCHAR2(200));\n", encoding="utf-8")
                    (folder / "checks.json").write_text(block, encoding="utf-8")
                    migration_manifest.load_migration(Path(temporary), "migrations/2026-09-30_example-r001")
        self.assertGreaterEqual(found, 2)

    def test_preflight_retry_setting_and_synonym_evidence_are_documented(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        rules = (ROOT / "docs" / "migration-rules.md").read_text(encoding="utf-8")
        limitations = (ROOT / "docs" / "known-limitations.md").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        bash_wrapper = (ROOT / "scripts" / "team.sh").read_text(encoding="utf-8")
        powershell_wrapper = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        bash_loader = (ROOT / "scripts" / "load_env.sh").read_text(encoding="utf-8")
        powershell_loader = (ROOT / "scripts" / "load_env.ps1").read_text(encoding="utf-8")
        help_result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for text in (readme, rules, env_example, bash_wrapper, powershell_wrapper, bash_loader, powershell_loader, help_result.stdout):
            with self.subTest(setting="MIGRATION_PREFLIGHT_INVENTORY_RETRIES"):
                self.assertIn("MIGRATION_PREFLIGHT_INVENTORY_RETRIES", text)
        self.assertIn("default is `3` retries", rules)
        self.assertIn("status", rules)
        self.assertIn("fingerprint", rules)
        self.assertIn("one direct", limitations)


if __name__ == "__main__":
    unittest.main()
