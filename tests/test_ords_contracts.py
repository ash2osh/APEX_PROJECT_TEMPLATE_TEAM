"""Static guarantees of the ORDS export: read-only SQL, honest documentation, template delivery.

The SQL is never run here (no database is involved), so these tests pin what a
reviewer would otherwise have to re-read: the SQL cannot change anything, it
agrees with the verifier on every marker and inventory key, the documentation
quotes messages the scripts really print, and `upgrade-template` delivers the
feature without touching a project's own files.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _windows_lf  # noqa: F401  (Path.write_text writes LF on Windows too)
from scripts import ords_export
from scripts.upgrade_template import protected_project_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ORDS_SQL = (
    "ords_export.sql", "ords_export_run.sql", "ords_export_skip.sql", "ords_inventory.sql",
    "verify_ords_access.sql", "doctor_ords.sql",
)
# Files the feature adds or changes in the template.
ORDS_TEMPLATE_FILES = (
    "scripts/ords_export.py", "scripts/ords_export.sql", "scripts/ords_export_run.sql", "scripts/ords_export_skip.sql",
    "scripts/ords_inventory.sql", "scripts/verify_ords_access.sql", "scripts/doctor_ords.sql",
    "scripts/backup_db.sh", "scripts/backup_db.ps1", "scripts/team.sh", "scripts/team.ps1", "scripts/load_env.sh", "scripts/load_env.ps1",
    "scripts/replace_mirror.sh", "scripts/replace_mirror.ps1", "scripts/check_db_target.sh", "scripts/check_db_target.ps1",
    "scripts/sqlcl_safe.sh", "scripts/invoke_sqlcl.ps1",
    "tests/test_ords_profile.py", "tests/test_ords_export.py", "tests/test_backup_ords_cli.py", "tests/test_ords_contracts.py",
    "tests/ords_fixtures.py", "tests/fake_ords_sql.py",
    "docs/ords-export.md", "docs/TROUBLESHOOTING.md", "docs/known-limitations.md", "docs/GETTING_STARTED.md",
    "README.md", "AGENTS.md", ".env.example", ".agents/skills/initialize-project/SKILL.md", ".claude/skills/initialize-project/SKILL.md",
)


def code_of(name: str) -> str:
    """The SQL without comments and without the text of string literals."""
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    return re.sub(r"'(?:[^']|'')*'", "''", "\n".join(lines))


def literals_of(name: str) -> list[str]:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    return re.findall(r"'((?:[^']|'')*)'", "\n".join(lines))


class OrdsSqlContractTests(unittest.TestCase):
    def test_the_sql_cannot_change_anything(self) -> None:
        forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|GRANT|REVOKE|TRUNCATE|COMMIT|SAVEPOINT|LOCK)\b", re.IGNORECASE)
        for name in ORDS_SQL:
            with self.subTest(script=name):
                code = code_of(name)
                self.assertIsNone(forbidden.search(code), forbidden.search(code))
                self.assertNotRegex(code, r"(?i)\bORDS(_ADMIN)?\.\w+")
                self.assertNotRegex(code, r"(?i)\bOAUTH\b")
                self.assertNotRegex(code, r"(?im)^\s*REST\s+(import|export\s+(client|module))")
                self.assertNotRegex(code, r"(?i)\bROLLBACK\b(?<!FAILURE ROLLBACK)(?<!SUCCESS ROLLBACK)")

    def test_the_only_dynamic_sql_is_a_select(self) -> None:
        for name in ORDS_SQL:
            for statement in re.findall(r"(?is)EXECUTE\s+IMMEDIATE\s+'((?:[^']|'')*)'", (SCRIPTS / name).read_text(encoding="utf-8")):
                with self.subTest(script=name):
                    self.assertTrue(statement.lstrip().upper().startswith("SELECT"), statement)

    def test_the_only_rest_command_is_export_schema(self) -> None:
        commands = []
        for name in ORDS_SQL:
            commands += re.findall(r"(?im)^\s*REST\s+(.*?)\s*$", code_of(name))
        self.assertEqual(["export schema", "export schema"], commands, "the export runs twice and nothing else is requested")

    def test_no_string_literal_holds_a_semicolon(self) -> None:
        # SQLcl's client-side statement splitter cuts a SELECT at a semicolon inside a
        # quoted literal and returns no rows without an error (see backup_db.sql).
        for name in ORDS_SQL:
            for literal in literals_of(name):
                with self.subTest(script=name, literal=literal[:40]):
                    self.assertNotIn(";", literal)

    def test_the_drivers_stop_on_error_and_end_with_rollback(self) -> None:
        for name in ("ords_export.sql", "doctor_ords.sql"):
            with self.subTest(script=name):
                contents = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", contents)
                self.assertIn("WHENEVER OSERROR EXIT FAILURE ROLLBACK", contents)
                self.assertRegex(contents, r"(?im)^EXIT SUCCESS ROLLBACK\s*$")

    def test_included_scripts_exist(self) -> None:
        for name in ORDS_SQL:
            for include in re.findall(r"(?m)^@@(\S+)", (SCRIPTS / name).read_text(encoding="utf-8")):
                if "&" in include:
                    continue
                with self.subTest(script=name, include=include):
                    self.assertTrue((SCRIPTS / include).is_file())
        export = (SCRIPTS / "ords_export.sql").read_text(encoding="utf-8")
        for dynamic in ("ords_export_run.sql", "ords_export_skip.sql"):
            self.assertIn(f"'{dynamic}'", export)
            self.assertTrue((SCRIPTS / dynamic).is_file())

    def test_the_session_must_be_the_schema_owner_in_sql_too(self) -> None:
        access = (SCRIPTS / "verify_ords_access.sql").read_text(encoding="utf-8")
        self.assertIn("IF v_session_user != v_target_schema THEN", access)
        self.assertIn("@@verify_db_access.sql", access)
        self.assertIn("CURRENT_SCHEMA is not a substitute", access)

    def test_sql_and_verifier_agree_on_every_marker_and_inventory_key(self) -> None:
        everything = "\n".join((SCRIPTS / name).read_text(encoding="utf-8") for name in ORDS_SQL)
        for marker in ("ORDS_INVENTORY:", "ORDS_EXPORT_MODE:run", "ORDS_EXPORT_MODE:skipped-not-enabled", "ORDS_EXPORT_COMPLETE:", "ORDS_ACCESS_VERIFIED:", "APEX_DOCTOR_VERIFIED:"):
            with self.subTest(marker=marker):
                self.assertIn(marker, everything)
        inventory = (SCRIPTS / "ords_inventory.sql").read_text(encoding="utf-8")
        keys = re.findall(r"'(?:,)?(\w+)='", inventory)
        self.assertEqual(list(ords_export.INVENTORY_KEYS), keys)
        for key, call, _exact in ords_export.CALL_RULES:
            self.assertIn(key, ords_export.INVENTORY_KEYS, call)

    def test_both_spool_files_the_verifier_reads_are_the_ones_the_sql_writes(self) -> None:
        run = (SCRIPTS / "ords_export_run.sql").read_text(encoding="utf-8")
        self.assertIn("SPOOL database/&&spool_schema/ords/schema.sql", run)
        self.assertIn("SPOOL verify/&&spool_schema/schema.sql", run)
        self.assertEqual("schema.sql", ords_export.SPOOL_NAME)


class OrdsDocumentationContractTests(unittest.TestCase):
    MESSAGES = (
        ("scripts/backup_db.sh", "backup-ords error: ORDS is not configured"),
        ("scripts/backup_db.ps1", "backup-ords error: ORDS is not configured"),
        ("scripts/backup_db.sh", "backup-ords error: the ORDS profile does not list schema"),
        ("scripts/backup_db.ps1", "backup-ords error: the ORDS profile does not list schema"),
        ("scripts/load_env.sh", "must be configured together"),
        ("scripts/load_env.sh", "ORDS_EXPECTED_USER must equal ORDS_SCHEMA entry for entry"),
        ("scripts/load_env.ps1", "ORDS_EXPECTED_USER must equal ORDS_SCHEMA entry for entry"),
        ("scripts/load_env.ps1", "must be configured together"),
        ("scripts/ords_export.py", "is not supported for the ORDS export"),
        ("scripts/verify_ords_access.sql", "ORDS export must authenticate as the REST schema owner"),
        ("scripts/verify_ords_access.sql", "ORDS is not installed in this database, or its metadata is not visible"),
        ("scripts/verify_ords_access.sql", "-20063"),
        ("scripts/ords_export.py", "unsupported ORDS release"),
        ("scripts/ords_export.sql", "ORDS OAuth client(s)"),
        ("scripts/ords_export.py", "changed while it was exported"),
        ("scripts/ords_export.py", "the export is incomplete or inconsistent"),
        ("scripts/ords_export.py", "is truncated"),
        ("scripts/ords_export.py", "file is missing or empty"),
        ("scripts/ords_export.py", "does not start with the generator header"),
        ("scripts/ords_export.py", "is not REST enabled yet the dictionary lists ORDS metadata"),
        ("scripts/ords_export.py", "contains OAuth client or secret material"),
        ("scripts/backup_db.sh", "refusing to back up over dirty mirror"),
    )

    def test_the_guide_quotes_only_messages_the_scripts_print(self) -> None:
        guide = (ROOT / "docs" / "ords-export.md").read_text(encoding="utf-8")
        for script, message in self.MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message, (ROOT / script).read_text(encoding="utf-8"))
                self.assertIn(message.lstrip("-"), guide.replace("ORA-", ""))

    def test_the_guide_states_what_is_exported_what_is_not_and_what_is_unverified(self) -> None:
        guide = (ROOT / "docs" / "ords-export.md").read_text(encoding="utf-8")
        for phrase in (
            "`database/<SCHEMA>/ords/schema.sql`", "REST export schema", "modules, templates, handlers", "roles and privileges",
            "OAuth clients", "client secrets", "Application data", "SQLcl", "26.1", "ORDS 25.1", "verified empty",
            "ALTER SESSION SET CURRENT_SCHEMA", "Verified against a live database", "CALL_RULES", "second export",
            "It exports; it never changes anything.", "ORDS is never modified",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)

    def test_every_entry_point_points_at_the_guide_and_lists_the_command(self) -> None:
        for path in ("README.md", "docs/TROUBLESHOOTING.md", "docs/known-limitations.md", "docs/GETTING_STARTED.md", "AGENTS.md"):
            with self.subTest(path=path):
                text = (ROOT / path).read_text(encoding="utf-8")
                self.assertIn("ords-export.md", text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for fragment in ("scripts/team.sh backup-ords", "ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER", "OAuth clients", "SQLcl 26.1"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, readme)
        for usage in ("scripts/team.sh", "scripts/team.ps1"):
            self.assertIn("backup-ords", (ROOT / usage).read_text(encoding="utf-8"))

    def test_the_example_environment_documents_an_optional_profile_that_is_off_by_default(self) -> None:
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in ("ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER"):
            self.assertRegex(example, rf"(?m)^# {key}=")
            self.assertNotRegex(example, rf"(?m)^{key}=")

    def test_both_copies_of_the_initialization_skill_cover_the_profile_and_agree(self) -> None:
        agents = (ROOT / ".agents/skills/initialize-project/SKILL.md").read_text(encoding="utf-8")
        claude = (ROOT / ".claude/skills/initialize-project/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(agents, claude)
        for fragment in ("ORDS_SCHEMA=<rest-schema>", "ORDS_SQLCL_CONNECTION=<saved-connection>", "ORDS_EXPECTED_USER=<same-as-ORDS_SCHEMA>", "read ords", "-Target ords"):
            self.assertIn(fragment, agents)

    def test_the_documented_exclusions_match_the_verifier(self) -> None:
        self.assertTrue(ords_export.OAUTH_CALL.search("  ORDS_METADATA.OAUTH.IMPORT_CLIENT(\n"))
        self.assertTrue(ords_export.OAUTH_CALL.search("  ORDS.CREATE_CLIENT(\n"))
        self.assertIsNone(ords_export.OAUTH_CALL.search("  ORDS.DEFINE_HANDLER(\n      p_source => 'token client secret');\n"))


class OrdsTemplateDeliveryTests(unittest.TestCase):
    def manifest(self) -> dict:
        return json.loads((ROOT / "template-manifest.json").read_text(encoding="utf-8"))

    def owners(self, path: str) -> list[str]:
        from test_template_manifest import glob_to_regex

        manifest = self.manifest()
        owners = []
        if any(glob_to_regex(pattern).match(path) for pattern in manifest["templateOwned"]):
            owners.append("templateOwned")
        if path in manifest["projectOwned"]:
            owners.append("projectOwned")
        if any(glob_to_regex(pattern).match(path) for pattern in manifest["templateOnly"]):
            owners.append("templateOnly")
        return owners

    def test_every_file_of_the_feature_is_template_owned_and_deliverable(self) -> None:
        for path in ORDS_TEMPLATE_FILES:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file(), path)
                self.assertEqual(["templateOwned"], self.owners(path))
                self.assertFalse(protected_project_path(path), path)

    def test_a_generated_ords_export_is_project_data_the_upgrade_never_writes(self) -> None:
        for path in ("database/DEMO/ords/schema.sql", "database/REST_API/ords/schema.sql"):
            with self.subTest(path=path):
                self.assertTrue(protected_project_path(path))
                self.assertEqual([], self.owners(path))

    def test_upgrade_delivers_the_feature_and_keeps_every_project_file(self) -> None:
        def git(cwd: Path, *args: str) -> None:
            subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

        def init(path: Path) -> None:
            path.mkdir(parents=True)
            git(path, "init", "-q", "-b", "main")
            git(path, "config", "user.email", "t@example.com")
            git(path, "config", "user.name", "t")

        def put(root: Path, files: dict[str, str]) -> None:
            for relative, text in files.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="")

        manifest_text = (ROOT / "template-manifest.json").read_text(encoding="utf-8")
        # The engine insists that the template ships every project-owned placeholder.
        placeholders = {path: "placeholder\n" for path in json.loads(manifest_text)["projectOwned"]}
        project_data = {
            ".env": "PROJECT_NAME=demo\nORDS_SCHEMA=REST_API\nORDS_SQLCL_CONNECTION=dev-rest\nORDS_EXPECTED_USER=REST_API\n",
            "AGENTS.project.md": "our project rules\n",
            "PROJECT.md": "our overview\n",
            ".agents/rules/project.md": "our rules\n",
            "apps/DEMO/100/application.apx": "app X ()\n",
            "database/DEMO/tables/T.sql": "CREATE TABLE T (ID NUMBER);\n",
            "database/REST_API/ords/schema.sql": "-- committed ORDS export\n",
            "migrations/2026-09-27_create-customers-r001/001-create-table.sql": "CREATE TABLE CUSTOMERS (ID NUMBER);\n",
            "migrations/2026-09-27_create-customers-r001/checks.json": "{\"schemaVersion\":1}\n",
            "migrations/2026-09-27_create-customers-r001/status.dev.json": "{\"state\":\"verified\"}\n",
        }
        old_template_files = {path: "old template version\n" for path in ORDS_TEMPLATE_FILES if not path.startswith(("tests/test_ords", "tests/ords_", "tests/fake_ords", "docs/ords-export", "scripts/ords_", "scripts/verify_ords", "scripts/doctor_ords"))}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            template, project = base / "template", base / "project"
            init(template)
            put(template, {"template-manifest.json": manifest_text, ".gitignore": ".env\n", **placeholders, **old_template_files})
            git(template, "add", "-A")
            git(template, "commit", "-q", "-m", "v1")
            init(project)
            put(project, {"template-manifest.json": manifest_text, ".gitignore": ".env\n", **placeholders, **old_template_files, **{k: v for k, v in project_data.items() if k != ".env"}})
            git(project, "add", "-A")
            git(project, "commit", "-q", "-m", "created from template")
            put(project, {".env": project_data[".env"]})
            engine = ROOT / "scripts" / "upgrade_template.py"

            def upgrade() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(engine), "--project-root", str(project), "--source", str(template), "--apex-release", "26.2"],
                    text=True, capture_output=True, check=False,
                )

            adopted = upgrade()
            self.assertEqual(0, adopted.returncode, adopted.stdout + adopted.stderr)
            git(project, "add", "-A")
            git(project, "commit", "-q", "-m", "adopt template lock")

            # v2 of the template: the real files of this feature.
            put(template, {path: (ROOT / path).read_text(encoding="utf-8") for path in ORDS_TEMPLATE_FILES})
            git(template, "add", "-A")
            git(template, "commit", "-q", "-m", "v2")
            result = upgrade()
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for path in ORDS_TEMPLATE_FILES:
                with self.subTest(delivered=path):
                    self.assertEqual((ROOT / path).read_bytes(), (project / path).read_bytes())
            for path, text in project_data.items():
                with self.subTest(preserved=path):
                    self.assertEqual(text, (project / path).read_text(encoding="utf-8"))
            self.assertFalse(list(project.rglob("*.template-new")), "no conflict files")
            lock = json.loads((project / ".template-lock.json").read_text(encoding="utf-8"))
            for path in ("scripts/ords_export.py", "docs/ords-export.md", "tests/test_backup_ords_cli.py"):
                self.assertIn(path, lock["files"])
            for path in project_data:
                self.assertNotIn(path, lock["files"])


if __name__ == "__main__":
    unittest.main()
