import json
import re
import subprocess
import _test_subprocess_safety  # noqa: F401
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
    ROOT / "docs" / "release-workflow.md",
    ROOT / "docs" / "compare-env.md",
    ROOT / "docs" / "publish-rules.md",
    ROOT / "docs" / "GETTING_STARTED.md",
    ROOT / "docs" / "EXAMPLES.md",
    ROOT / "docs" / "TROUBLESHOOTING.md",
    ROOT / "docs" / "known-limitations.md",
    ROOT / "docs" / "login-acts-as-schema.md",
    ROOT / "docs" / "template-feedback.md",
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
    ("scripts/publish_app.sh", "second post-import APEX export failed"),
    ("scripts/application_lifecycle.py", "automation restore did not verify"),
    ("scripts/verify_publish_state.py", "APEXlang source file set does not match the post-import re-export"),
    ("scripts/verify_publish_state.py", "APEXlang source bytes do not match the post-import re-export"),
    ("scripts/verify_publish_state.py", "post-import APEXlang source was not stable across the second post-import re-export"),
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
    def test_compare_env_guide_documents_the_read_only_catalog_contract(self) -> None:
        guide = (ROOT / "docs" / "compare-env.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/compare-env.md", readme)
        for phrase in (
            "does not write to either database",
            "SQLcl 26.3.0.0",
            "Use `--section` more than once",
            "not compared",
            "not a source export",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)

    def test_active_team_guidance_uses_the_current_cli(self) -> None:
        contents = {path: path.read_text(encoding="utf-8") for path in DOCS}
        for path, document in contents.items():
            with self.subTest(path=path.relative_to(ROOT)):
                for term in RETIRED_TERMS:
                    self.assertNotIn(term, document)

        readme = contents[ROOT / "README.md"]
        for command in (
            "doctor",
            "export <id>",
            "publish <id>",
            "release list",
            "release new",
            "release collect",
            "release check",
            "release run",
            "--confirm-target",
            "--env dev",
            "compare-schema",
            "compare-env --from dev --to staging",
            "--emit-dba-script",
            "backup-db",
            "deploy 100 --env staging",
            "--manual",
        ):
            with self.subTest(command=command):
                self.assertIn(command, readme)
        # The export and publish rules moved out of the README workflow sections (section 9 of the getting started
        # guide and publish-rules.md own them now), so these assertions read those documents.
        publish_rules = (ROOT / "docs" / "publish-rules.md").read_text(encoding="utf-8")
        getting_started = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
        self.assertIn("observed as `ACTIVE` before import", publish_rules)
        self.assertIn("--strict-bytes", publish_rules)
        self.assertIn("--no-restore-automations", publish_rules)
        self.assertIn("accept normalized `.apx` source", publish_rules)
        self.assertIn("requires a second export with the same normalized source", publish_rules)
        self.assertIn("fails and leaves the old baseline in place", publish_rules)
        self.assertIn("APEX does not stamp `last_updated_on` while importing", publish_rules)
        self.assertIn("[ASHARIF-2026-09-26r001]", publish_rules)
        self.assertIn("Commit the stamped", getting_started)
        self.assertIn("scripts/team.sh export 100", getting_started)
        self.assertIn("scripts/team.sh publish 100 --env dev", getting_started)
        self.assertIn("DEVELOPER_NAME", contents[ROOT / "AGENTS.md"])
        self.assertIn("scripts/team.sh upgrade-template", readme)
        self.assertIn("AGENTS.project.md", readme)
        self.assertIn(".template-new", readme)
        self.assertIn("python3 /tmp/apex-template/scripts/upgrade_template.py", readme)
        release_guide = contents[ROOT / "docs" / "release-workflow.md"]
        for required in (
            "01-ddl/", "02-code/", "03-ords/", "04-verify/",
            "baseSha256", "--confirm-target", "PLSQL_CODE_TYPE",
            "scratch/backups/", "scratch/release-reports/", "status",
        ):
            with self.subTest(release_guide=required):
                self.assertIn(required, release_guide)
        limitations = contents[ROOT / "docs" / "known-limitations.md"]
        for limitation in ("whitespace-only lines", "trailing hyphen", "patch levels", "compare-env", "ORA_HASH"):
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

    def test_release_startup_picker_and_zero_prompt_rules_are_documented(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for text in (agents, claude):
            self.assertIn("scripts/team.sh release list", text)
            self.assertIn("--confirm-target", text)
        self.assertIn("Create a new release folder", agents)
        self.assertIn("Create a new release folder", claude)
        self.assertIn("zero interactive", agents.lower())

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
        # `apex generate` can leave source differences beyond the documented normalizer; the
        # guide says to export canonical source and reconcile before the normal workflow.
        guide = (ROOT / "docs" / "publish-rules.md").read_text(encoding="utf-8")
        for phrase in ("apex generate", "extra blank line", "deinstall", "scripts/team.sh export <id>"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)

    def test_getting_started_gives_the_python_check_that_works_on_windows(self) -> None:
        # A python.org or winget install provides python.exe and py, but no python3.
        guide = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
        self.assertIn("py -3 --version", guide)

    def test_automations_rule_lets_an_agent_pass_yes_and_no_file_says_exact_typed_confirmation(self) -> None:
        # Owner decision 2026-11-15: --yes replaces the typed confirmation for automations enable|disable|run-once.
        agents = " ".join((ROOT / "AGENTS.md").read_text(encoding="utf-8").split())
        self.assertIn("automations enable|disable|run-once", agents)
        self.assertIn("`acknowledgedBy: --yes`", agents)
        safety = " ".join((ROOT / ".agents" / "rules" / "agent-safety.md").read_text(encoding="utf-8").split())
        self.assertIn("automations enable|disable|run-once", safety)
        self.assertIn("`acknowledgedBy: --yes`", safety)
        for relative in ("AGENTS.md", ".agents/rules/agent-safety.md", "README.md", "docs/known-limitations.md", "scripts/automation_operations.py"):
            text = " ".join((ROOT / relative).read_text(encoding="utf-8").split()).lower()
            for withdrawn in ("no flag replaces it", "exact typed confirmation", "after typed target/action confirmation"):
                self.assertNotIn(withdrawn, text, f"{relative} still states the withdrawn rule: {withdrawn}")

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
        self.assertIn("releases/<slug>/", context)
        self.assertIn("docs/release-workflow.md", context)
        self.assertIn("do not enforce", context)
        self.assertNotIn("app_context/<alias>/", context)
        self.assertNotIn("release builder resolves", context)

    def test_release_workflow_guide_documents_all_six_steps_and_guards(self) -> None:
        guide = (ROOT / "docs" / "release-workflow.md").read_text(encoding="utf-8")
        for required in (
            "Step 1", "Step 2", "Step 3", "Step 4", "Step 5", "Step 6",
            "release.json", "01-ddl/", "02-code/", "03-ords/", "04-verify/",
            "baseSha256", "--confirm-target", "PLSQL_CODE_TYPE",
            "forbiddenPatterns", "scratch/release-diffs/", "scratch/backups/",
            "scratch/release-reports/", '"deployed"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)

    def test_ci_runs_behavioral_unittest_suite(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "template-checks.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/ci_run_tests.py", workflow)
        self.assertIn("python scripts/ci_run_tests.py", workflow)
        linux_job = workflow.split("  offline:", 1)[1].split("  offline-latest-python:", 1)[0]
        windows_job = workflow.split("  windows-tests:", 1)[1].split("  windows-offline:", 1)[0]
        self.assertIn("CI_TEST_JOBS: '4'", linux_job)
        self.assertIn("CI_TEST_JOBS: '2'", windows_job)
        self.assertIn('"discover", "-s", "tests", "-v"', (ROOT / "scripts" / "ci_run_tests.py").read_text(encoding="utf-8"))

    def test_ci_shards_windows_and_scopes_each_run(self) -> None:
        # ASHARIF 2026-11-26: three Windows shards, one required `windows-offline` result, docs-only and push runs
        # skip the heavy jobs, and the full matrix runs nightly.
        workflow = (ROOT / ".github" / "workflows" / "template-checks.yml").read_text(encoding="utf-8")
        headers = [(match.start(), match.group(1)) for match in re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):$", workflow)]
        jobs = {}
        for index, (start, name) in enumerate(headers):
            end = headers[index + 1][0] if index + 1 < len(headers) else len(workflow)
            jobs[name] = workflow[start:end]
        self.assertIn("schedule:", workflow)
        self.assertIn("full: ${{ steps.scope.outputs.full }}", jobs["changes"])
        self.assertIn("shard: [1, 2, 3]", jobs["windows-tests"])
        self.assertIn("CI_TEST_SHARD: ${{ matrix.shard }}/3", jobs["windows-tests"])
        self.assertIn("python scripts/ci_run_tests.py", jobs["windows-tests"])
        self.assertIn("needs: [changes, windows-tests]", jobs["windows-offline"])
        self.assertIn("if: always()", jobs["windows-offline"])
        for heavy in ("windows-tests", "offline-latest-python", "windows-powershell-51"):
            with self.subTest(job=heavy):
                self.assertIn("if: needs.changes.outputs.full == 'true'", jobs[heavy])
        # `offline` and `macos-installer` are required checks and always run.
        self.assertNotIn("needs:", jobs["offline"])
        self.assertNotIn("needs:", jobs["macos-installer"])

    def test_ci_runs_the_suite_on_the_oldest_and_the_newest_linux_python(self) -> None:
        # `offline` keeps its name (a required check) on the oldest supported Python; the newest 3.x runs beside it.
        workflow = (ROOT / ".github" / "workflows" / "template-checks.yml").read_text(encoding="utf-8")
        headers = [(match.start(), match.group(1)) for match in re.finditer(r"(?m)^  ([A-Za-z0-9_-]+):$", workflow)]
        jobs = {}
        for index, (start, name) in enumerate(headers):
            end = headers[index + 1][0] if index + 1 < len(headers) else len(workflow)
            jobs[name] = workflow[start:end]
        self.assertIn("python-version: '3.10'", jobs["offline"])
        self.assertIn("runs-on: ubuntu-latest", jobs["offline-latest-python"])
        self.assertIn("python-version: '3.x'", jobs["offline-latest-python"])
        self.assertIn("python3 -m compileall -q scripts/ tools/ tests/", jobs["offline-latest-python"])
        self.assertIn("python3 scripts/ci_run_tests.py", jobs["offline-latest-python"])
        self.assertIn("CI_TEST_JOBS: '4'", jobs["offline-latest-python"])
        self.assertNotRegex(jobs["offline"], r"(?m)^    (strategy|matrix):")
        self.assertIn("actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0", jobs["offline-latest-python"])
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", jobs["offline-latest-python"])

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

    def test_publish_targets_dev_and_deploy_targets_staging_and_prod(self) -> None:
        team_sh = (ROOT / "scripts" / "team.sh").read_text(encoding="utf-8")
        team_ps1 = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        publish_rules = (ROOT / "docs" / "publish-rules.md").read_text(encoding="utf-8")
        getting_started = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")

        for script_text in (team_sh, team_ps1):
            self.assertIn("publish <app_id> [--env dev] [--force]", script_text)
            self.assertIn("import to DEV", script_text)
            self.assertIn("deploy <app_id> --env <staging|prod>", script_text)

        self.assertIn("publish targets DEV only", publish_rules)
        self.assertIn("scripts/team.sh deploy <id> --env <staging|prod>", publish_rules)
        self.assertIn("publish_app.sh`/`publish_app.ps1` script is an internal implementation helper", publish_rules)
        self.assertIn("Imports to shared DEV only", readme)
        self.assertIn("`scripts/team.sh publish` targets DEV only", getting_started)

    def test_targets_report_is_in_both_full_usage_screens_and_the_guides_but_not_everyday(self) -> None:
        # `targets` is the offline report of which profile each operation uses. It belongs in the full usage of both
        # wrappers, in the README command reference and in the guides that explain the profiles, and not in the
        # short "Everyday commands" list.
        for script in ("team.sh", "team.ps1"):
            with self.subTest(script=script):
                self.assertIn("targets [--format text|json]", (ROOT / "scripts" / script).read_text(encoding="utf-8"))
        everyday = subprocess.run(
            [BASH, str(ROOT / "scripts" / "team.sh")], capture_output=True, text=True, check=True
        ).stdout
        self.assertIsNone(re.search(r"(?m)^  targets\b", everyday), "targets must not be an everyday command")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("scripts/team.sh targets [--format text\\|json]", readme)
        getting_started = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
        self.assertIn("scripts/team.sh targets", getting_started)
        self.assertIn("never connects to a database and never starts SQLcl", getting_started)

    def test_getting_started_runs_verify_local_after_the_exports_not_before_the_first_one(self) -> None:
        # verify-local reports every configured app with no folder as a failure until it is exported. A new developer
        # therefore runs it at the end of step 8, after every listed app is exported, and step 7 checks only the
        # configuration and the connection.
        text = (ROOT / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")

        def step(number: int) -> str:
            start = text.index(f"\n## {number}. ")
            return text[start:text.index("\n## ", start + 1)]

        step7, step8 = step(7), step(8)
        self.assertNotIn("scripts/team.sh verify-local", step7, "step 7 must not run verify-local before the exports")
        self.assertIn("scripts/team.sh targets", step7)
        self.assertIn("scripts/team.sh doctor", step7)
        self.assertIn("Export every app ID listed in `APEX_APP_ID`", step8)
        verify_at = step8.index("scripts/team.sh verify-local")
        self.assertEqual(-1, step8.find("scripts/team.sh verify-local", verify_at + 1))
        last_export = max(match.start() for match in re.finditer(r"scripts/team\.sh export \d+", step8))
        self.assertLess(last_export, verify_at, "verify-local must follow every export in step 8")
        self.assertLess(verify_at, step8.index("git add apps/"), "the exports and descriptors are committed after verify-local")
        self.assertIn("TROUBLESHOOTING.md#local-readiness", step8)
        troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
        self.assertIn("## Local readiness", troubleshooting)


if __name__ == "__main__":
    unittest.main()
