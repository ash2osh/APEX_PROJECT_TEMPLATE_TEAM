import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")

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
        ["bash", "-c", f'set -e; source "$1" "$2"; {probe}', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class BashEnvironmentListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

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

    def test_lists_narrowing_and_guard_match_the_bash_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = write_env(Path(temporary), MULTI)
            probe = '"$($env:PROJECT_MULTI_SCHEMA)|$($env:PROJECT_SCHEMAS)|$($env:TABLES_SCHEMA)|$($env:CODE_SQLCL_CONNECTION)"'
            plain = self.run_probe(env_path, probe)
            self.assertEqual(0, plain.returncode, plain.stderr)
            self.assertEqual("true|ONE,TWO|ONE,TWO|conn-one,conn-two", plain.stdout.strip())
            narrowed = self.run_probe(env_path, probe, "TWO")
            self.assertEqual("true|ONE,TWO|TWO|conn-two", narrowed.stdout.strip())
            refused = self.run_probe(env_path, 'Assert-ProjectEnvSingleSchema -Label "unit test"')
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("--schema", refused.stderr)
            unknown = self.run_probe(env_path, probe, "NOPE")
            self.assertNotEqual(0, unknown.returncode)
            self.assertIn("not configured", unknown.stderr)

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
            self.assertIn("must list the same number of entries", result.stderr)
            empty = write_env(Path(temporary), {**MULTI, "CODE_SCHEMA=ONE,TWO": "CODE_SCHEMA=ONE,,TWO"})
            result = self.run_probe(empty, "'loaded'")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("must not contain empty entries", result.stderr)


if __name__ == "__main__":
    unittest.main()
