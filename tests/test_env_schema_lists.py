import itertools
import os
import re
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from fake_sqlcl import BASH
from pathlib import Path
from unittest.mock import patch

from scripts.local_config import read_project_env
from scripts.template_release import is_apex_26_2


ROOT = Path(__file__).resolve().parents[1]
PWSH = _test_subprocess_safety.working_pwsh()
# Phase 2 release switch: the uc-apx settings are retired on 26.2 only (apexRelease in template-manifest.json).
APEX_26_2 = is_apex_26_2(ROOT)


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
GREEK_USERNAME = "".join(
    chr(codepoint) for codepoint in (0x0394, 0x0397, 0x039C, 0x0397, 0x03A4, 0x03A1, 0x0397, 0x03A3)
)


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
        for username in ("dev@example.com", "o'neil", GREEK_USERNAME, "demo"):
            with self.subTest(username=username):
                path = write_without_workspace_username(self.directory, 'APEX_WORKSPACE_USERNAME="' + username + '"\n')
                probe = (
                    'printf "%s|%s|%s" "$APEX_WORKSPACE_USERNAME" "$DEVELOPER_NAME" "$APEX_EXPECTED_USER" '
                    "| od -An -tx1 | tr -d '[:space:]'"
                )
                result = load(path, probe)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = (username + "|ALICE|DEMO").encode("utf-8").hex()
                self.assertEqual(result.stdout.strip().lower(), expected)

    def test_missing_workspace_developer_does_not_inherit_or_fall_back(self) -> None:
        path = write_without_workspace_username(self.directory)
        with patch.dict(os.environ, {"APEX_WORKSPACE_USERNAME": "INHERITED"}):
            result = load(path, 'printf "%s" "${APEX_WORKSPACE_USERNAME:-}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    @unittest.skipIf(APEX_26_2, "26.2 retired the uc-apx settings")
    def test_fresh_config_exports_the_26_1_uc_defaults(self) -> None:
        result = load(write_without_uc(self.directory), 'printf "%s|%s" "$INSTALL_UC_APX" "$UC_APX_SKILLS_AGENT"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("false|universal", result.stdout)

    @unittest.skipUnless(APEX_26_2, "26.1 keeps the uc-apx settings")
    def test_fresh_config_does_not_export_retired_uc_settings(self) -> None:
        result = load(write_without_uc(self.directory),
                      'printf "%s|%s" "${INSTALL_UC_APX+set}" "${UC_APX_SKILLS_AGENT+set}"')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("|", result.stdout)

    @unittest.skipUnless(APEX_26_2, "26.1 keeps the uc-apx settings")
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

    def test_bash_loader_does_not_echo_malformed_line_contents(self) -> None:
        secret = "DO_NOT_LEAK_THIS_ENV_VALUE"
        result = load(write_env(self.directory, {}, f"INVALID LINE {secret}\n"), "echo loaded")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(secret, result.stdout + result.stderr)
        if PWSH:
            path = write_env(self.directory, {}, f"INVALID LINE {secret}\n")
            ps = subprocess.run([PWSH, "-NoProfile", "-Command", f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{path}"'], text=True, capture_output=True)
            self.assertNotEqual(ps.returncode, 0)
            self.assertNotIn(secret, ps.stdout + ps.stderr)

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

    def test_dba_sqlcl_connections_load_from_file_and_override_ambient(self) -> None:
        extra = "\nDEV_DBA_SQLCL_CONNECTION=file-dev-dba\nSTAGING_DBA_SQLCL_CONNECTION=file-stage-dba\nPROD_DBA_SQLCL_CONNECTION=file-prod-dba\n"
        env_path = write_env(self.directory, {}, extra)
        env = os.environ.copy()
        env["DEV_DBA_SQLCL_CONNECTION"] = "ambient-dev-dba"
        result = subprocess.run(
            [BASH, "-c", 'set -e; source "$1" "$2"; printf "%s|%s|%s\\n" "$DEV_DBA_SQLCL_CONNECTION" "$STAGING_DBA_SQLCL_CONNECTION" "$PROD_DBA_SQLCL_CONNECTION"', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("file-dev-dba|file-stage-dba|file-prod-dba\n", result.stdout)

    def test_dba_sqlcl_connections_ambient_ignored_when_absent_from_file(self) -> None:
        # Only the file may set these keys: an inherited value must neither be visible nor exported to children.
        env_path = write_env(self.directory, {})
        env = os.environ.copy()
        for key in ("DEV_DBA_SQLCL_CONNECTION", "STAGING_DBA_SQLCL_CONNECTION", "PROD_DBA_SQLCL_CONNECTION"):
            env[key] = f"ambient-{key.lower()}"
        result = subprocess.run(
            [BASH, "-c", 'set -e; source "$1" "$2"; printf "%s|%s|%s\\n" "${DEV_DBA_SQLCL_CONNECTION-unset}" "${STAGING_DBA_SQLCL_CONNECTION-unset}" "${PROD_DBA_SQLCL_CONNECTION-unset}"', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("unset|unset|unset\n", result.stdout)
        exported = subprocess.run(
            [BASH, "-c", 'set -e; source "$1" "$2"; for key in DEV_DBA_SQLCL_CONNECTION STAGING_DBA_SQLCL_CONNECTION PROD_DBA_SQLCL_CONNECTION; do if printenv "$key" >/dev/null; then echo "exported $key"; fi; done', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, exported.returncode, exported.stderr)
        self.assertEqual("", exported.stdout)

    def test_dba_sqlcl_connection_from_the_file_is_the_only_value_a_child_process_sees(self) -> None:
        env_path = write_env(self.directory, {}, "\nSTAGING_DBA_SQLCL_CONNECTION=file-stage-dba\n")
        env = os.environ.copy()
        env["STAGING_DBA_SQLCL_CONNECTION"] = "ambient-stage-dba"
        result = subprocess.run(
            [BASH, "-c", 'source "$1" "$2" && printenv STAGING_DBA_SQLCL_CONNECTION', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("file-stage-dba\n", result.stdout)

    @unittest.skipIf(APEX_26_2, "26.2 wording is covered by test_dba_sqlcl_connection_refusals_26_2")
    def test_dba_sqlcl_connection_refusals(self) -> None:
        # File blank
        env_path = write_env(self.directory, {}, "\nDEV_DBA_SQLCL_CONNECTION=\n")
        result = load(env_path)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DEV_DBA_SQLCL_CONNECTION must not be empty", result.stderr)

        # File comma list
        env_path = write_env(self.directory, {}, "\nDEV_DBA_SQLCL_CONNECTION=dba1,dba2\n")
        result = load(env_path)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DEV_DBA_SQLCL_CONNECTION contains unsupported characters", result.stderr)

        # Ambient blank: an inherited value is not configuration, so it is neither refused nor exported.
        env_path = write_env(self.directory, {})
        env = os.environ.copy()
        env["DEV_DBA_SQLCL_CONNECTION"] = "   "
        result = subprocess.run(
            [BASH, "-c", 'set -e; source "$1" "$2"; printf "%s\\n" "${DEV_DBA_SQLCL_CONNECTION-unset}"', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("unset\n", result.stdout)

    @unittest.skipUnless(APEX_26_2, "26.1 wording is covered by test_dba_sqlcl_connection_refusals")
    def test_dba_sqlcl_connection_refusals_26_2(self) -> None:
        # 26.2 wording: one saved SQLcl alias, checked as an alias list.
        env_path = write_env(self.directory, {}, "\nDEV_DBA_SQLCL_CONNECTION=\n")
        result = load(env_path)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DEV_DBA_SQLCL_CONNECTION must not be empty when configured", result.stderr)

        env_path = write_env(self.directory, {}, "\nDEV_DBA_SQLCL_CONNECTION=dba1,dba2\n")
        result = load(env_path)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DEV_DBA_SQLCL_CONNECTION accepts one saved SQLcl connection alias", result.stderr)

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
            for username in ("dev@example.com", "o'neil", GREEK_USERNAME, "demo"):
                with self.subTest(username=username):
                    path = write_without_workspace_username(Path(temporary), 'APEX_WORKSPACE_USERNAME="' + username + '"\n')
                    probe = (
                        '[BitConverter]::ToString([Text.Encoding]::UTF8.GetBytes('
                        '"$($env:APEX_WORKSPACE_USERNAME)|$($env:DEVELOPER_NAME)|$($env:APEX_EXPECTED_USER)"'
                        ')).Replace("-", "")'
                    )
                    result = self.run_probe(path, probe)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    expected = (username + "|ALICE|DEMO").encode("utf-8").hex()
                    self.assertEqual(result.stdout.strip().lower(), expected)

    @unittest.skipIf(APEX_26_2, "26.2 retired the uc-apx settings")
    def test_fresh_config_exports_the_26_1_uc_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_without_uc(Path(temporary))
            result = self.run_probe(path, '"$($env:INSTALL_UC_APX)|$($env:UC_APX_SKILLS_AGENT)"')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("false|universal", result.stdout.strip())

    @unittest.skipUnless(APEX_26_2, "26.1 keeps the uc-apx settings")
    def test_retired_uc_keys_match_bash_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for setting in ("INSTALL_UC_APX=false", "INSTALL_UC_APX=true", "UC_APX_SKILLS_AGENT=universal"):
                with self.subTest(setting=setting):
                    path = write_without_uc(Path(temporary), setting + "\n")
                    result = self.run_probe(path, "'LOADED'")
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(RETIRED_UC_ERROR, plain(result.stderr))
                    self.assertNotIn("LOADED", result.stdout)

    @unittest.skipUnless(APEX_26_2, "26.1 keeps the uc-apx settings")
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

    def test_dba_sqlcl_connections_load_from_file_and_override_ambient(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = write_env(Path(temporary), {}, "\nDEV_DBA_SQLCL_CONNECTION=file-dev-dba\nSTAGING_DBA_SQLCL_CONNECTION=file-stage-dba\nPROD_DBA_SQLCL_CONNECTION=file-prod-dba\n")
            env = os.environ.copy()
            env["DEV_DBA_SQLCL_CONNECTION"] = "ambient-dev-dba"
            script = f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{env_path}"; "$($env:DEV_DBA_SQLCL_CONNECTION)|$($env:STAGING_DBA_SQLCL_CONNECTION)|$($env:PROD_DBA_SQLCL_CONNECTION)"'
            result = subprocess.run([PWSH, "-NoProfile", "-Command", script], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("file-dev-dba|file-stage-dba|file-prod-dba", result.stdout.strip())

    def test_dba_sqlcl_connections_ambient_ignored_when_absent_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = write_env(Path(temporary), {})
            env = os.environ.copy()
            for key in ("DEV_DBA_SQLCL_CONNECTION", "STAGING_DBA_SQLCL_CONNECTION", "PROD_DBA_SQLCL_CONNECTION"):
                env[key] = f"ambient-{key.lower()}"
            script = f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{env_path}"; "[$($env:DEV_DBA_SQLCL_CONNECTION)|$($env:STAGING_DBA_SQLCL_CONNECTION)|$($env:PROD_DBA_SQLCL_CONNECTION)]"'
            result = subprocess.run([PWSH, "-NoProfile", "-Command", script], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("[||]", result.stdout.strip())

    def test_refusals_match_the_bash_loader_word_for_word(self) -> None:
        # Both loaders are also run directly (and by the helper scripts); the PowerShell one
        # used to drop the "project environment error:" prefix on most of its refusals.
        cases = {
            "boolean": {"INSTALL_UC_APX=false": "INSTALL_UC_APX="},
            "developer": {"DEVELOPER_NAME=ALICE": "DEVELOPER_NAME=lower"},
            "environment": {"DB_ENVIRONMENT=development": "DB_ENVIRONMENT=dev"},
            "duplicate": {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,ONE"},
            "lengths": {**MULTI, "CODE_EXPECTED_USER=ONE,TWO": "CODE_EXPECTED_USER=ONE"},
            "empty entry": {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,,TWO"},
            "lowercase": {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,two"},
            "dba blank": {"PROJECT_NAME=apex-team-template": "PROJECT_NAME=apex-team-template\nDEV_DBA_SQLCL_CONNECTION="},
            "dba comma": {"PROJECT_NAME=apex-team-template": "PROJECT_NAME=apex-team-template\nDEV_DBA_SQLCL_CONNECTION=dba1,dba2"},
        }
        if APEX_26_2:
            del cases["boolean"]  # 26.1 wording for a uc-apx value; 26.2 refuses the key as retired
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


# DEV_SCHEMA, DEV_SQLCL_CONNECTION and DEV_EXPECTED_USER fill the TABLES, CODE and APEX profiles
# when a profile has none of its own three keys. The probes print the same eleven values in the
# same order for every loader, so the outputs can be compared field by field.
DEV_FILLED_KEYS = (
    "TABLES_SCHEMA", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER", "TABLES_PREFIXES",
    "CODE_SCHEMA", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER", "CODE_PREFIXES",
    "APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER",
)
DEV_BASH_PROBE = 'printf "' + "|".join(["%s"] * len(DEV_FILLED_KEYS)) + '\\n" ' + " ".join(f'"${key}"' for key in DEV_FILLED_KEYS)
DEV_PS_PROBE = '"' + "|".join(f"$($env:{key})" for key in DEV_FILLED_KEYS) + '"'
DEV_HEADER = "PROJECT_NAME=DEMO\nDEVELOPER_NAME=ALICE\nDB_ENVIRONMENT=development\nAPEX_APP_ID=100\n"
DEV_TRIO = "DEV_SCHEMA=DEMO\nDEV_SQLCL_CONNECTION=dev-conn\nDEV_EXPECTED_USER=DEMO\n"
DEV_TRIO_MESSAGE = "configure DEV_SCHEMA, DEV_SQLCL_CONNECTION and DEV_EXPECTED_USER together"
DEV_PARITY_BODY = (
    "CODE_PREFIXES=ZZ_\nCODE_SCHEMA=OWN\nCODE_SQLCL_CONNECTION=own-conn\nCODE_EXPECTED_USER=OWN\n"
    "DEV_SCHEMA=ONE,TWO\nDEV_SQLCL_CONNECTION=conn-one,conn-two\nDEV_EXPECTED_USER=ONE,TWO\n"
)
DEV_PARITY_EXPECTED = [
    "ONE,TWO", "conn-one,conn-two", "ONE,TWO", "*",
    "OWN", "own-conn", "OWN", "ZZ_",
    "ONE,TWO", "conn-one,conn-two", "ONE,TWO",
]


def write_dev_env(directory: Path, body: str) -> Path:
    path = directory / ".env"
    path.write_text(DEV_HEADER + body, encoding="utf-8")
    return path


def ps_probe(env_path: Path, probe: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PROJECT_SCHEMA", None)
    script = f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{env_path}"; {probe}'
    return subprocess.run([PWSH, "-NoProfile", "-Command", script], env=environment, text=True, capture_output=True, check=False)


class DevTrioLoaderCases:
    """Shared cases for one loader; a subclass supplies run_loader and its probe."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def run_loader(self, env_path: Path) -> subprocess.CompletedProcess[str]:
        raise NotImplementedError

    def loaded_fields(self, body: str) -> list[str]:
        result = self.run_loader(write_dev_env(self.directory, body))
        self.assertEqual(0, result.returncode, plain(result.stderr))
        return result.stdout.strip().split("|")

    def refusal(self, body: str) -> str:
        result = self.run_loader(write_dev_env(self.directory, body))
        self.assertNotEqual(0, result.returncode, result.stdout)
        return plain(result.stderr).strip()

    def test_dev_trio_alone_fills_tables_code_and_apex(self) -> None:
        self.assertEqual(
            ["DEMO", "dev-conn", "DEMO", "*", "DEMO", "dev-conn", "DEMO", "*", "DEMO", "dev-conn", "DEMO"],
            self.loaded_fields(DEV_TRIO),
        )

    def test_dev_trio_lists_fill_all_three_profiles_aligned(self) -> None:
        body = "DEV_SCHEMA=ONE,TWO\nDEV_SQLCL_CONNECTION=conn-one,conn-two\nDEV_EXPECTED_USER=ONE,TWO\n"
        fields = self.loaded_fields(body)
        self.assertEqual(["ONE,TWO", "conn-one,conn-two", "ONE,TWO", "*"], fields[0:4])
        self.assertEqual(["ONE,TWO", "conn-one,conn-two", "ONE,TWO", "*"], fields[4:8])
        self.assertEqual(["ONE,TWO", "conn-one,conn-two", "ONE,TWO"], fields[8:11])

    def test_profile_with_its_own_values_wins_over_the_dev_trio(self) -> None:
        body = DEV_TRIO + "TABLES_SCHEMA=OWN\nTABLES_PREFIXES=OWN_\nTABLES_SQLCL_CONNECTION=own-conn\nTABLES_EXPECTED_USER=OWN\n"
        fields = self.loaded_fields(body)
        self.assertEqual(["OWN", "own-conn", "OWN", "OWN_"], fields[0:4])
        self.assertEqual(["DEMO", "dev-conn", "DEMO", "*"], fields[4:8])
        self.assertEqual(["DEMO", "dev-conn", "DEMO"], fields[8:11])

    def test_partially_configured_profile_keeps_the_existing_required_error(self) -> None:
        message = self.refusal(DEV_TRIO + "TABLES_SCHEMA=OWN\nTABLES_PREFIXES=*\n")
        self.assertIn("TABLES_SQLCL_CONNECTION is required in", message)
        self.assertNotIn(DEV_TRIO_MESSAGE, message)

    def test_partial_dev_trio_is_refused_with_the_new_message(self) -> None:
        pairs = {
            "DEV_SCHEMA": "SECRET_DEV_SCHEMA_VALUE",
            "DEV_SQLCL_CONNECTION": "SECRET-DEV-CONN",
            "DEV_EXPECTED_USER": "SECRET_DEV_USER_VALUE",
        }
        for size in (1, 2):
            for subset in itertools.combinations(pairs, size):
                with self.subTest(keys=subset):
                    body = "".join(f"{key}={pairs[key]}\n" for key in subset)
                    message = self.refusal(body)
                    self.assertIn(DEV_TRIO_MESSAGE, message)
                    for value in pairs.values():
                        self.assertNotIn(value, message)

    def test_dev_trio_without_expected_user_is_refused(self) -> None:
        message = self.refusal("DEV_SCHEMA=DEMO\nDEV_SQLCL_CONNECTION=dev-conn\n")
        self.assertIn(DEV_TRIO_MESSAGE, message)

    def test_dev_lists_must_align(self) -> None:
        message = self.refusal("DEV_SCHEMA=ONE,TWO\nDEV_SQLCL_CONNECTION=conn-one\nDEV_EXPECTED_USER=ONE,TWO\n")
        self.assertIn("must list the same number of entries", message)

    def test_dev_values_are_validated_and_never_echoed(self) -> None:
        secret = "lower_secret_dev_value"
        message = self.refusal(f"DEV_SCHEMA={secret}\nDEV_SQLCL_CONNECTION=dev-conn\nDEV_EXPECTED_USER=DEMO\n")
        self.assertIn("must be an uppercase Oracle identifier", message)
        self.assertNotIn(secret, message)

    def test_unknown_key_is_still_rejected_next_to_the_dev_trio(self) -> None:
        message = self.refusal(DEV_TRIO + "DEV_OTHER_SETTING=1\n")
        self.assertIn("unsupported setting in", message)
        self.assertIn("DEV_OTHER_SETTING", message)


class BashDevTrioTests(DevTrioLoaderCases, unittest.TestCase):
    def run_loader(self, env_path: Path) -> subprocess.CompletedProcess[str]:
        return load(env_path, DEV_BASH_PROBE)


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class PowerShellDevTrioTests(DevTrioLoaderCases, unittest.TestCase):
    def run_loader(self, env_path: Path) -> subprocess.CompletedProcess[str]:
        return ps_probe(env_path, DEV_PS_PROBE)


class DevTrioParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_python_bash_and_powershell_resolve_the_dev_trio_identically(self) -> None:
        env_path = write_dev_env(Path(self.temporary.name), DEV_PARITY_BODY)
        python_values = read_project_env(env_path)
        python_fields = [python_values[key] for key in DEV_FILLED_KEYS]
        bash = load(env_path, DEV_BASH_PROBE)
        self.assertEqual(0, bash.returncode, bash.stderr)
        bash_fields = bash.stdout.strip().split("|")
        self.assertEqual(DEV_PARITY_EXPECTED, python_fields)
        self.assertEqual(python_fields, bash_fields)
        if PWSH:
            powershell = ps_probe(env_path, DEV_PS_PROBE)
            self.assertEqual(0, powershell.returncode, powershell.stderr)
            self.assertEqual(python_fields, powershell.stdout.strip().split("|"))
        else:
            self.skipTest("PowerShell Core is not installed")


if __name__ == "__main__":
    unittest.main()
