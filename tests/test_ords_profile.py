"""The optional ORDS profile: ORDS_SCHEMA, ORDS_SQLCL_CONNECTION, ORDS_EXPECTED_USER.

Both loaders (load_env.sh and load_env.ps1) must agree on every case: all three
keys absent means ORDS is disabled and nothing else changes; a partial or
invalid profile is refused with the same words; lists are position-aligned and
narrowed by --schema like every other profile.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")

ORDS_ONE = "\nORDS_SCHEMA=REST_API\nORDS_SQLCL_CONNECTION=dev-rest\nORDS_EXPECTED_USER=REST_API\n"
ORDS_TWO = "\nORDS_SCHEMA=REST_ONE,REST_TWO\nORDS_SQLCL_CONNECTION=dev-rest-one,dev-rest-two\nORDS_EXPECTED_USER=REST_ONE,REST_TWO\n"
KEYS = ("ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER")

BASH_PROBE = (
    'printf "%s|%s|%s|%s|%s|%s|%s\\n" "$PROJECT_ORDS_CONFIGURED" "$PROJECT_MULTI_SCHEMA" "$PROJECT_SCHEMAS" '
    '"$ORDS_SCHEMA" "$ORDS_SQLCL_CONNECTION" "$ORDS_EXPECTED_USER" "$TABLES_SCHEMA"'
)
POWERSHELL_PROBE = (
    '"$($env:PROJECT_ORDS_CONFIGURED)|$($env:PROJECT_MULTI_SCHEMA)|$($env:PROJECT_SCHEMAS)|'
    '$($env:ORDS_SCHEMA)|$($env:ORDS_SQLCL_CONNECTION)|$($env:ORDS_EXPECTED_USER)|$($env:TABLES_SCHEMA)"'
)


def plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*\|?\s*", " ", text)


def write_env(directory: Path, extra: str = "", drop: tuple[str, ...] = ()) -> Path:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not any(line.startswith(f"{key}=") for key in drop)]
    path = directory / ".env"
    path.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
    return path


def environment(project_schema: str | None, **extra: str) -> dict[str, str]:
    result = os.environ.copy()
    for key in (*KEYS, "PROJECT_SCHEMA", "PROJECT_ORDS_CONFIGURED"):
        result.pop(key, None)
    if project_schema is not None:
        result["PROJECT_SCHEMA"] = project_schema
    result.update(extra)
    return result


STAGING_BASH_PROBE = 'printf "[%s][%s][%s]\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"'
STAGING_POWERSHELL_PROBE = '"[$($env:STAGING_SQLCL_CONNECTION)][$($env:STAGING_EXPECTED_USER)][$($env:STAGING_SCHEMA)]"'


def load_bash(env_path: Path, project_schema: str | None = None, probe: str = BASH_PROBE, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f'set -e; source "$1" "$2"; {probe}', "bash", str(ROOT / "scripts" / "load_env.sh"), str(env_path)],
        env=environment(project_schema, **extra), text=True, capture_output=True, check=False,
    )


def load_powershell(env_path: Path, project_schema: str | None = None, probe: str = POWERSHELL_PROBE, **extra: str) -> subprocess.CompletedProcess[str]:
    script = f'. "{ROOT / "scripts" / "load_env.ps1"}" -EnvFile "{env_path}"; {probe}'
    return subprocess.run(
        [PWSH, "-NoProfile", "-Command", script],
        env=environment(project_schema, **extra), text=True, capture_output=True, check=False,
    )


class OrdsProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def loaders(self):
        yield "bash", load_bash
        if PWSH:
            yield "powershell", load_powershell

    def assert_loaded(self, env_path: Path, expected: str, project_schema: str | None = None, staging: bool = False, **extra: str) -> None:
        for shell, load in self.loaders():
            with self.subTest(shell=shell):
                probe = {"bash": STAGING_BASH_PROBE, "powershell": STAGING_POWERSHELL_PROBE}[shell] if staging else {"bash": BASH_PROBE, "powershell": POWERSHELL_PROBE}[shell]
                result = load(env_path, project_schema, probe=probe, **extra)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(expected, result.stdout.strip())

    def assert_refused(self, env_path: Path, message: str, project_schema: str | None = None) -> None:
        refusals = []
        for shell, load in self.loaders():
            with self.subTest(shell=shell):
                result = load(env_path, project_schema)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn(message, plain(result.stderr))
                refusals.append(plain(result.stderr).strip())
        if len(refusals) == 2:
            self.assertTrue(refusals[0].startswith("project environment error: "), refusals[0])
            self.assertIn(refusals[0], refusals[1])

    def test_all_three_keys_omitted_leaves_ords_disabled_and_changes_nothing_else(self) -> None:
        self.assert_loaded(write_env(self.directory), "false|false|DEMO||||DEMO")

    def test_migration_runtime_limits_load_in_both_shells(self) -> None:
        env_path = write_env(
            self.directory,
            "\nMIGRATION_APPLY_TIMEOUT_SECONDS=240\n"
            "MIGRATION_CHECK_TIMEOUT_SECONDS=90.5\n"
            "MIGRATION_CHECK_BATCH_BYTES=1048576\n",
        )
        for shell, load in self.loaders():
            with self.subTest(shell=shell):
                probe = (
                    'printf "%s|%s|%s\\n" "$MIGRATION_APPLY_TIMEOUT_SECONDS" '
                    '"$MIGRATION_CHECK_TIMEOUT_SECONDS" "$MIGRATION_CHECK_BATCH_BYTES"'
                    if shell == "bash" else
                    '"$($env:MIGRATION_APPLY_TIMEOUT_SECONDS)|$($env:MIGRATION_CHECK_TIMEOUT_SECONDS)|$($env:MIGRATION_CHECK_BATCH_BYTES)"'
                )
                result = load(env_path, probe=probe)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("240|90.5|1048576", result.stdout.strip())

    def test_a_stale_shell_value_never_enables_ords(self) -> None:
        self.assert_loaded(write_env(self.directory), "false|false|DEMO||||DEMO", ORDS_SCHEMA="LEAK", ORDS_SQLCL_CONNECTION="leak", ORDS_EXPECTED_USER="LEAK")

    def test_a_complete_profile_enables_ords_and_lists_its_schema(self) -> None:
        self.assert_loaded(
            write_env(self.directory, ORDS_ONE),
            "true|false|DEMO,REST_API|REST_API|dev-rest|REST_API|DEMO",
        )

    def test_a_partial_profile_is_refused_whichever_key_is_missing(self) -> None:
        for present in (("ORDS_SCHEMA",), ("ORDS_SQLCL_CONNECTION",), ("ORDS_EXPECTED_USER",),
                        ("ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION"), ("ORDS_SCHEMA", "ORDS_EXPECTED_USER"),
                        ("ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER")):
            with self.subTest(present=present):
                values = {"ORDS_SCHEMA": "REST_API", "ORDS_SQLCL_CONNECTION": "dev-rest", "ORDS_EXPECTED_USER": "REST_API"}
                extra = "\n" + "\n".join(f"{key}={values[key]}" for key in present) + "\n"
                self.assert_refused(
                    write_env(self.directory, extra),
                    "ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER must be configured together",
                )

    def test_an_empty_value_is_refused_rather_than_read_as_disabled(self) -> None:
        for key in KEYS:
            with self.subTest(key=key):
                text = ORDS_ONE.replace(f"{key}=" + {"ORDS_SCHEMA": "REST_API", "ORDS_SQLCL_CONNECTION": "dev-rest", "ORDS_EXPECTED_USER": "REST_API"}[key], f"{key}=")
                self.assert_refused(write_env(self.directory, text), f"{key} must not be empty")

    def test_values_are_validated_like_every_other_profile(self) -> None:
        cases = (
            (ORDS_ONE.replace("ORDS_SCHEMA=REST_API", "ORDS_SCHEMA=rest_api"), "ORDS_SCHEMA must be an uppercase Oracle identifier"),
            (ORDS_ONE.replace("ORDS_EXPECTED_USER=REST_API", "ORDS_EXPECTED_USER=1BAD"), "ORDS_EXPECTED_USER must be an uppercase Oracle identifier"),
            (ORDS_ONE.replace("dev-rest", "bad name"), "ORDS_SQLCL_CONNECTION contains unsupported characters"),
            (ORDS_TWO.replace("REST_ONE,REST_TWO\nORDS_SQLCL", "REST_ONE,REST_ONE\nORDS_SQLCL"), "ORDS_SCHEMA must not contain duplicate values"),
            (ORDS_TWO.replace("dev-rest-one,dev-rest-two", "dev-rest-one"), "ORDS_SQLCL_CONNECTION, ORDS_EXPECTED_USER and ORDS_SCHEMA must list the same number of entries"),
            (ORDS_TWO.replace("REST_ONE,REST_TWO\nORDS_SQLCL", "REST_ONE,,REST_TWO\nORDS_SQLCL"), "ORDS_SCHEMA must not contain empty entries"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                self.assert_refused(write_env(self.directory, text), message)

    def test_the_expected_user_must_be_the_rest_schema_entry_for_entry(self) -> None:
        # The session user must equal both, so a profile where they differ can never succeed.
        message = "ORDS_EXPECTED_USER must equal ORDS_SCHEMA"
        self.assert_refused(write_env(self.directory, ORDS_ONE.replace("ORDS_EXPECTED_USER=REST_API", "ORDS_EXPECTED_USER=DEPLOYER")), message)
        self.assert_refused(
            write_env(self.directory, ORDS_TWO.replace("ORDS_EXPECTED_USER=REST_ONE,REST_TWO", "ORDS_EXPECTED_USER=REST_TWO,REST_ONE")),
            message,
        )

    def test_an_ords_only_schema_is_configured_and_selectable(self) -> None:
        env_path = write_env(self.directory, ORDS_ONE)
        # Narrowed to the REST schema, the tables, code and APEX profiles list nothing for it.
        self.assert_loaded(env_path, "true|false|DEMO,REST_API|REST_API|dev-rest|REST_API|", project_schema="REST_API")

    def test_several_ords_schemas_make_the_project_multi_schema_and_narrow_by_name(self) -> None:
        env_path = write_env(self.directory, ORDS_TWO)
        self.assert_loaded(env_path, "true|true|DEMO,REST_ONE,REST_TWO|REST_ONE,REST_TWO|dev-rest-one,dev-rest-two|REST_ONE,REST_TWO|DEMO")
        self.assert_loaded(env_path, "true|true|DEMO,REST_ONE,REST_TWO|REST_TWO|dev-rest-two|REST_TWO|", project_schema="REST_TWO")

    def test_a_schema_the_ords_profile_does_not_list_blanks_it_but_keeps_it_configured(self) -> None:
        env_path = write_env(self.directory, ORDS_ONE)
        self.assert_loaded(env_path, "true|false|DEMO,REST_API||||DEMO", project_schema="DEMO")

    STAGING_ONE = "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\nSTAGING_SCHEMA=APP_STAGE\n"

    def test_independent_ords_lists_keep_the_single_dev_schema_staging_mapping(self) -> None:
        # One DEV (CODE) schema may map to a differently named staging schema. Several ORDS
        # schemas are a separate, independent list and must not switch that mapping off.
        for ords in (ORDS_ONE, ORDS_TWO):
            with self.subTest(ords=ords.splitlines()[1]):
                env_path = write_env(self.directory, ords + self.STAGING_ONE)
                self.assert_loaded(env_path, "[stage-db][STAGE_DEPLOYER][APP_STAGE]", project_schema="DEMO", staging=True)

    def test_an_ords_only_schema_gets_no_staging_mapping(self) -> None:
        env_path = write_env(self.directory, ORDS_TWO + self.STAGING_ONE)
        self.assert_loaded(env_path, "[][][]", project_schema="REST_ONE", staging=True)
        self.assert_loaded(env_path, "[][][]", project_schema="REST_TWO", staging=True)

    def test_several_code_schemas_still_switch_the_staging_mapping_off(self) -> None:
        # The control: lists of the profiles that map DEV onto staging still count.
        text = ("\nCODE_SCHEMA=DEMO,OTHER\nCODE_SQLCL_CONNECTION=docker-demo,other-conn\nCODE_EXPECTED_USER=DEMO,OTHER\n"
                + ORDS_TWO + self.STAGING_ONE)
        env_path = write_env(self.directory, text, drop=("CODE_SCHEMA", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER"))
        self.assert_loaded(env_path, "[][][]", project_schema="DEMO", staging=True)

    def test_ords_lists_still_make_the_project_multi_schema_for_commands_that_need_one_schema(self) -> None:
        env_path = write_env(self.directory, ORDS_TWO + self.STAGING_ONE)
        self.assert_loaded(env_path, "true|true|DEMO,REST_ONE,REST_TWO|REST_ONE,REST_TWO|dev-rest-one,dev-rest-two|REST_ONE,REST_TWO|DEMO")

    def test_unknown_schema_lists_the_ords_schemas_too(self) -> None:
        self.assert_refused(write_env(self.directory, ORDS_ONE), "DEMO,REST_API", project_schema="NOPE")

    def test_ords_keys_are_not_accepted_with_a_duplicate_or_inline_comment(self) -> None:
        self.assert_refused(write_env(self.directory, ORDS_ONE + "ORDS_SCHEMA=OTHER\n"), "duplicate setting")
        self.assert_refused(write_env(self.directory, ORDS_ONE.replace("dev-rest", "dev-rest # note")), "inline comment")


if __name__ == "__main__":
    unittest.main()
