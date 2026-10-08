import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fake_sqlcl import BASH
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


def plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*\|?\s*", " ", text)


MULTI = {
    "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=ONE,TWO",
    "TABLES_SQLCL_CONNECTION=docker-demo": "TABLES_SQLCL_CONNECTION=conn-one,conn-two",
    "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=ONE,TWO",
    "CODE_SCHEMA=DEMO": "CODE_SCHEMA=ONE,TWO",
    "CODE_SQLCL_CONNECTION=docker-demo": "CODE_SQLCL_CONNECTION=conn-one,conn-two",
    "CODE_EXPECTED_USER=DEMO": "CODE_EXPECTED_USER=ONE,TWO",
    "APEX_PARSING_SCHEMA=DEMO": "APEX_PARSING_SCHEMA=ONE,TWO",
    "APEX_SQLCL_CONNECTION=docker-demo": "APEX_SQLCL_CONNECTION=conn-one,conn-two",
    "APEX_EXPECTED_USER=DEMO": "APEX_EXPECTED_USER=ONE,TWO",
}

PROBE = (
    'printf "%s|%s|%s|%s|%s|%s|%s\\n" "$PROJECT_MULTI_SCHEMA" "$PROJECT_SCHEMAS" '
    '"$TABLES_SCHEMA" "$TABLES_SQLCL_CONNECTION" "$CODE_SCHEMA" "$CODE_EXPECTED_USER" "$APEX_SQLCL_CONNECTION"'
)


def write_env(directory: Path, replacements: dict[str, str], extra: str = "") -> Path:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    path = directory / ".env"
    path.write_text(text + extra, encoding="utf-8")
    return path


def load(env_path: Path, probe: str = PROBE, project_schema: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PROJECT_SCHEMA", None)
    if project_schema is not None:
        environment["PROJECT_SCHEMA"] = project_schema
    return subprocess.run(
        [BASH, "-c", f'set -e; source "$1" "$2"; {probe}', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


RETIRED_UC_ERROR = "project environment error: uc-apx settings are retired; remove INSTALL_UC_APX and UC_APX_SKILLS_AGENT from .env"


def write_without_uc(directory: Path, extra: str = "") -> Path:
    path = write_env(directory, {})
    text = "\n".join(line for line in path.read_text().splitlines()
                     if not line.startswith(("INSTALL_UC_APX=", "UC_APX_SKILLS_AGENT=")))
    path.write_text(text + "\n" + extra, encoding="utf-8")
    return path


def write_without_workspace_username(directory: Path, extra: str = "") -> Path:
    path = write_env(directory, {})
    lines = [line for line in path.read_text().splitlines() if not line.startswith("APEX_WORKSPACE_USERNAME=")]
    path.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
    return path


class BashEnvironmentListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_workspace_developer_is_independent_literal_optional_text(self) -> None:
        for username in ("dev@example.com", "o'neil", "ΔΗΜΗΤΡΗΣ", "demo"):
            with self.subTest(username=username):
                path = write_without_workspace_username(self.directory, 'APEX_WORKSPACE_USERNAME="' + username + '"\n')
                result = load(path, 'printf "%s|%s|%s" "$APEX_WORKSPACE_USERNAME" "$DEVELOPER_NAME" "$APEX_EXPECTED_USER"')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, username + "|ALICE|DEMO")

    def test_missing_workspace_developer_does_not_inherit_or_fall_back(self) -> None:
        path = write_without_workspace_username(self.directory)
        with patch.dict(os.environ, {"APEX_WORKSPACE_USERNAME": "INHERITED"}):
            result = load(path, 'printf "%s" "${APEX_WORKSPACE_USERNAME:-}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_fresh_config_does_not_export_retired_uc_settings(self) -> None:
        result = load(write_without_uc(self.directory),
                      'printf "%s|%s" "${INSTALL_UC_APX+set}" "${UC_APX_SKILLS_AGENT+set}"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("|", result.stdout)

    def test_retired_uc_keys_require_explicit_config_cleanup(self) -> None:
        for setting in ("INSTALL_UC_APX=false", "INSTALL_UC_APX=true", "UC_APX_SKILLS_AGENT=universal"):
            with self.subTest(setting=setting):
                result = load(write_without_uc(self.directory, setting + "\n"), "echo LOADED")
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(RETIRED_UC_ERROR, result.stderr.strip())
                self.assertNotIn("LOADED", result.stdout)

    def test_single_values_load_exactly_as_before(self) -> None:
        result = load(write_env(self.directory, {}))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("false|DEMO|DEMO|docker-demo|DEMO|DEMO|docker-demo\n", result.stdout)

    def test_lists_load_and_stay_unnarrowed_without_a_selection(self) -> None:
        result = load(write_env(self.directory, MULTI))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("true|ONE,TWO|ONE,TWO|conn-one,conn-two|ONE,TWO|ONE,TWO|conn-one,conn-two\n", result.stdout)

    def test_project_schema_narrows_every_profile_to_its_entry(self) -> None:
        result = load(write_env(self.directory, MULTI), project_schema="TWO")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("true|ONE,TWO|TWO|conn-two|TWO|TWO|conn-two\n", result.stdout)

    def test_project_code_schemas_stays_unnarrowed(self) -> None:
        result = load(
            write_env(self.directory, MULTI),
            probe='printf "%s|%s\\n" "$PROJECT_CODE_SCHEMAS" "$CODE_SCHEMA"',
            project_schema="TWO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ONE,TWO|TWO\n", result.stdout)

    def test_a_profile_that_does_not_list_the_schema_is_blanked(self) -> None:
        replacements = {
            **MULTI,
            "TABLES_SCHEMA=ONE,TWO": "TABLES_SCHEMA=ONE",
            "TABLES_SQLCL_CONNECTION=conn-one,conn-two": "TABLES_SQLCL_CONNECTION=conn-one",
            "TABLES_EXPECTED_USER=ONE,TWO": "TABLES_EXPECTED_USER=ONE",
        }
        result = load(write_env(self.directory, replacements), project_schema="TWO")
        self.assertEqual(0, result.returncode, result.stderr)
        fields = result.stdout.rstrip("\n").split("|")
        self.assertEqual(["true", "ONE,TWO"], fields[0:2])
        self.assertEqual(["", ""], fields[2:4], "tables schema and connection must be empty")
        self.assertEqual(["TWO", "TWO"], fields[4:6])

    def test_unknown_project_schema_lists_the_configured_ones(self) -> None:
        result = load(write_env(self.directory, MULTI), project_schema="NOPE")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not configured", result.stderr)
        self.assertIn("ONE,TWO", result.stderr)

    def test_lowercase_project_schema_is_rejected(self) -> None:
        result = load(write_env(self.directory, MULTI), project_schema="two")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("PROJECT_SCHEMA must be an uppercase Oracle identifier", result.stderr)

    def test_unequal_list_lengths_name_the_keys(self) -> None:
        replacements = {**MULTI, "CODE_EXPECTED_USER=ONE,TWO": "CODE_EXPECTED_USER=ONE"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CODE_SQLCL_CONNECTION, CODE_EXPECTED_USER and CODE_SCHEMA must list the same number of entries", result.stderr)

    def test_empty_entries_and_trailing_commas_are_rejected(self) -> None:
        for bad in ("ONE,,TWO", "ONE,TWO,", ",ONE,TWO"):
            with self.subTest(value=bad):
                replacements = {**MULTI, "CODE_SCHEMA=ONE,TWO": f"CODE_SCHEMA={bad}"}
                result = load(write_env(self.directory, replacements))
                self.assertNotEqual(0, result.returncode)
                self.assertIn("CODE_SCHEMA must not contain empty entries", result.stderr)

    def test_duplicate_schemas_are_rejected(self) -> None:
        replacements = {**MULTI, "APEX_PARSING_SCHEMA=ONE,TWO": "APEX_PARSING_SCHEMA=ONE,ONE"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("APEX_PARSING_SCHEMA must not contain duplicate values", result.stderr)

    def test_each_list_entry_is_validated(self) -> None:
        replacements = {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,two"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CODE_SCHEMA must be an uppercase Oracle identifier", result.stderr)
        replacements = {**MULTI, "CODE_SQLCL_CONNECTION=conn-one,conn-two": "CODE_SQLCL_CONNECTION=conn-one,bad name"}
        result = load(write_env(self.directory, replacements))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("CODE_SQLCL_CONNECTION contains unsupported characters", result.stderr)

    def test_staging_lists_narrow_by_name(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\nSTAGING_SCHEMA=ONE,TWO\n"
        result = load(
            write_env(self.directory, MULTI, extra),
            probe='printf "%s|%s|%s\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"',
            project_schema="TWO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("stage-two|STWO|TWO\n", result.stdout)

    def test_staging_without_the_selected_schema_is_blanked_in_a_multi_project(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one\nSTAGING_EXPECTED_USER=SONE\nSTAGING_SCHEMA=ONE\n"
        result = load(
            write_env(self.directory, MULTI, extra),
            probe='printf "[%s][%s][%s]\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"',
            project_schema="TWO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("[][][]\n", result.stdout)

    def test_single_schema_project_keeps_a_differently_named_staging_schema(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\nSTAGING_SCHEMA=APP_STAGE\n"
        result = load(
            write_env(self.directory, {}, extra),
            probe='printf "%s\\n" "$STAGING_SCHEMA"',
            project_schema="DEMO",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("APP_STAGE\n", result.stdout)

    def test_only_the_projects_dev_schema_keeps_a_differently_named_staging_schema(self) -> None:
        # Split profile: tables live in DATA, code and APEX in DEMO. DATA is a
        # configured schema but not the project's DEV (CODE) schema, so it must
        # not map onto the single staging entry.
        replacements = {
            "TABLES_SCHEMA=DEMO": "TABLES_SCHEMA=DATA",
            "TABLES_EXPECTED_USER=DEMO": "TABLES_EXPECTED_USER=DATA",
        }
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\nSTAGING_SCHEMA=APP_STAGE\n"
        env_path = write_env(self.directory, replacements, extra)
        probe = 'printf "[%s][%s][%s]\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"'
        dev = load(env_path, probe=probe, project_schema="DEMO")
        self.assertEqual(0, dev.returncode, dev.stderr)
        self.assertEqual("[stage-db][STAGE_DEPLOYER][APP_STAGE]\n", dev.stdout)
        other = load(env_path, probe=probe, project_schema="DATA")
        self.assertEqual(0, other.returncode, other.stderr)
        self.assertEqual("[][][]\n", other.stdout)

    def test_several_staging_connections_require_a_staging_schema_list(self) -> None:
        extra = "\nSTAGING_SQLCL_CONNECTION=stage-one,stage-two\nSTAGING_EXPECTED_USER=SONE,STWO\n"
        result = load(write_env(self.directory, MULTI, extra))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("STAGING_SCHEMA is required", result.stderr)

    def test_require_single_refuses_a_multi_project_without_a_selection(self) -> None:
        env_path = write_env(self.directory, MULTI)
        refused = load(env_path, probe='project_env_require_single "unit test"')
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("--schema", refused.stderr)
        self.assertIn("ONE,TWO", refused.stderr)
        allowed = load(env_path, probe='project_env_require_single "unit test"; echo ok', project_schema="ONE")
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual("ok\n", allowed.stdout)

    def test_require_single_allows_a_single_schema_project(self) -> None:
        result = load(write_env(self.directory, {}), probe='project_env_require_single "unit test"; echo ok')
        self.assertEqual(0, result.returncode, result.stderr)

    def test_split_profile_without_lists_is_not_multi(self) -> None:
        replacements = {"CODE_SCHEMA=DEMO": "CODE_SCHEMA=OTHER", "CODE_EXPECTED_USER=DEMO": "CODE_EXPECTED_USER=OTHER"}
        result = load(write_env(self.directory, replacements))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.startswith("false|DEMO,OTHER|"), result.stdout)


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellEnvironmentListTests(unittest.TestCase):
    def run_probe(self, env_path: Path, probe: str, project_schema: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("PROJECT_SCHEMA", None)
        if project_schema is not None:
            environment["PROJECT_SCHEMA"] = project_schema
        script = f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{env_path}"; {probe}'
        return subprocess.run([PWSH, "-NoProfile", "-Command", script], env=environment, text=True, capture_output=True, check=False)

    def test_workspace_developer_matches_bash_literal_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for username in ("dev@example.com", "o'neil", "ΔΗΜΗΤΡΗΣ", "demo"):
                with self.subTest(username=username):
                    path = write_without_workspace_username(Path(temporary), 'APEX_WORKSPACE_USERNAME="' + username + '"\n')
                    result = self.run_probe(path, '"$($env:APEX_WORKSPACE_USERNAME)|$($env:DEVELOPER_NAME)|$($env:APEX_EXPECTED_USER)"')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), username + "|ALICE|DEMO")

    def test_retired_uc_keys_match_bash_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for setting in ("INSTALL_UC_APX=false", "INSTALL_UC_APX=true", "UC_APX_SKILLS_AGENT=universal"):
                with self.subTest(setting=setting):
                    path = write_without_uc(Path(temporary), setting + "\n")
                    result = self.run_probe(path, "'LOADED'")
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(RETIRED_UC_ERROR, plain(result.stderr))
                    self.assertNotIn("LOADED", result.stdout)

    def test_fresh_config_does_not_export_retired_uc_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_without_uc(Path(temporary))
            result = self.run_probe(path, '"$($env:INSTALL_UC_APX)|$($env:UC_APX_SKILLS_AGENT)"')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("|", result.stdout.strip())

    def test_lists_narrowing_and_guard_match_the_bash_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = write_env(Path(temporary), MULTI)
            probe = '"$($env:PROJECT_MULTI_SCHEMA)|$($env:PROJECT_SCHEMAS)|$($env:TABLES_SCHEMA)|$($env:CODE_SQLCL_CONNECTION)"'
            unselected = self.run_probe(env_path, probe)
            self.assertEqual(0, unselected.returncode, unselected.stderr)
            self.assertEqual("true|ONE,TWO|ONE,TWO|conn-one,conn-two", unselected.stdout.strip())
            narrowed = self.run_probe(env_path, probe, "TWO")
            self.assertEqual("true|ONE,TWO|TWO|conn-two", narrowed.stdout.strip())
            refused = self.run_probe(env_path, 'Assert-ProjectEnvSingleSchema -Label "unit test"')
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("--schema", plain(refused.stderr))
            unknown = self.run_probe(env_path, probe, "NOPE")
            self.assertNotEqual(0, unknown.returncode)
            self.assertIn("not configured", plain(unknown.stderr))

    def test_project_code_schemas_stays_unnarrowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = write_env(Path(temporary), MULTI)
            probe = '"$($env:PROJECT_CODE_SCHEMAS)|$($env:CODE_SCHEMA)"'
            narrowed = self.run_probe(env_path, probe, "TWO")
            self.assertEqual("ONE,TWO|TWO", narrowed.stdout.strip())

    def test_unequal_lengths_and_empty_entries_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unequal = write_env(Path(temporary), {**MULTI, "CODE_EXPECTED_USER=ONE,TWO": "CODE_EXPECTED_USER=ONE"})
            result = self.run_probe(unequal, "'loaded'")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must list the same number of entries", plain(result.stderr))
            empty = write_env(Path(temporary), {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,,TWO"})
            result = self.run_probe(empty, "'loaded'")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must not contain empty entries", plain(result.stderr))

    def test_refusals_match_the_bash_loader_word_for_word(self) -> None:
        # Both loaders are also run directly (and by the helper scripts); the PowerShell one
        # used to drop the "project environment error:" prefix on most of its refusals.
        cases = {
            "developer": {"DEVELOPER_NAME=ALICE": "DEVELOPER_NAME=lower"},
            "environment": {"DB_ENVIRONMENT=development": "DB_ENVIRONMENT=dev"},
            "duplicate": {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,ONE"},
            "lengths": {**MULTI, "CODE_EXPECTED_USER=ONE,TWO": "CODE_EXPECTED_USER=ONE"},
            "empty entry": {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,,TWO"},
            "lowercase": {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,two"},
        }
        for label, replacements in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                env_path = write_env(Path(temporary), replacements)
                bash = load(env_path, "true")
                powershell = self.run_probe(env_path, "'loaded'")
                self.assertNotEqual(0, bash.returncode)
                self.assertNotEqual(0, powershell.returncode)
                message = bash.stderr.strip()
                self.assertTrue(message.startswith("project environment error: "), message)
                self.assertIn(message, plain(powershell.stderr))


if __name__ == "__main__":
    unittest.main()
