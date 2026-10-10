"""Tests for scripts/local_config.py: strict literal parsing and validation of .env files.

Matches the behavior of scripts/load_env.sh and scripts/load_env.ps1 without shell
evaluation, command substitution, or disclosing secret values in errors.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.local_config import ConfigError, read_project_env


VALID_BASE_CONFIG = """# Valid baseline test config
PROJECT_NAME=DEMO
DEVELOPER_NAME=ASHARIF
DB_ENVIRONMENT=development
APEX_APP_ID=100
TABLES_SCHEMA=DEMO
TABLES_PREFIXES=*
TABLES_SQLCL_CONNECTION=DEV
TABLES_EXPECTED_USER=DEMO
CODE_SCHEMA=DEMO
CODE_PREFIXES=*
CODE_SQLCL_CONNECTION=DEV
CODE_EXPECTED_USER=DEMO
APEX_PARSING_SCHEMA=DEMO
APEX_SQLCL_CONNECTION=DEV
APEX_EXPECTED_USER=DEMO
"""


class LocalConfigTests(unittest.TestCase):
    def write_env(self, content: str, encoding: str = "utf-8", prefix: bytes = b"") -> Path:
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: Path(temp_dir).exists() and [p.unlink() for p in Path(temp_dir).iterdir()] and Path(temp_dir).rmdir())
        env_file = Path(temp_dir) / ".env"
        env_file.write_bytes(prefix + content.encode(encoding))
        return env_file

    def test_valid_base_config_parses_expected_keys(self) -> None:
        path = self.write_env(VALID_BASE_CONFIG)
        values = read_project_env(path)
        self.assertEqual(values["PROJECT_NAME"], "DEMO")
        self.assertEqual(values["DEVELOPER_NAME"], "ASHARIF")
        self.assertEqual(values["DB_ENVIRONMENT"], "development")
        self.assertEqual(values["APEX_APP_ID"], "100")
        self.assertEqual(values["TABLES_SCHEMA"], "DEMO")
        self.assertEqual(values["TABLES_PREFIXES"], "*")

    def test_migration_runtime_limits_are_optional_configuration_keys(self) -> None:
        content = VALID_BASE_CONFIG + (
            "MIGRATION_APPLY_TIMEOUT_SECONDS=300\n"
            "MIGRATION_CHECK_TIMEOUT_SECONDS=45.5\n"
            "MIGRATION_CHECK_BATCH_BYTES=2097152\n"
            "MIGRATION_PREFLIGHT_INVENTORY_RETRIES=3\n"
        )
        values = read_project_env(self.write_env(content))
        self.assertEqual(values["MIGRATION_APPLY_TIMEOUT_SECONDS"], "300")
        self.assertEqual(values["MIGRATION_CHECK_TIMEOUT_SECONDS"], "45.5")
        self.assertEqual(values["MIGRATION_CHECK_BATCH_BYTES"], "2097152")
        self.assertEqual(values["MIGRATION_PREFLIGHT_INVENTORY_RETRIES"], "3")

    def test_preflight_inventory_retries_accepts_zero_and_rejects_invalid_counts(self) -> None:
        values = read_project_env(self.write_env(VALID_BASE_CONFIG + "MIGRATION_PREFLIGHT_INVENTORY_RETRIES=0\n"))
        self.assertEqual(values["MIGRATION_PREFLIGHT_INVENTORY_RETRIES"], "0")
        for value in ("-1", "1.5", "many"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                read_project_env(self.write_env(VALID_BASE_CONFIG + f"MIGRATION_PREFLIGHT_INVENTORY_RETRIES={value}\n"))

    def test_utf8_bom_stripped_cleanly(self) -> None:
        path = self.write_env(VALID_BASE_CONFIG, prefix=b"\xef\xbb\xbf")
        values = read_project_env(path)
        self.assertEqual(values["PROJECT_NAME"], "DEMO")

    def test_utf16_bom_rejected(self) -> None:
        for bom in (b"\xff\xfe", b"\xfe\xff"):
            with self.subTest(bom=bom):
                path = self.write_env(VALID_BASE_CONFIG, prefix=bom)
                with self.assertRaises(ConfigError) as ctx:
                    read_project_env(path)
                self.assertIn("is UTF-16; save it as UTF-8", str(ctx.exception))

    def test_outer_quotes_stripped_matching_only(self) -> None:
        content = VALID_BASE_CONFIG + 'APEX_WORKSPACE_USERNAME="ASHARIF"\n'
        path = self.write_env(content)
        values = read_project_env(path)
        self.assertEqual(values["APEX_WORKSPACE_USERNAME"], "ASHARIF")

        content_single = VALID_BASE_CONFIG + "APEX_WORKSPACE_USERNAME='ASHARIF'\n"
        path_single = self.write_env(content_single)
        values_single = read_project_env(path_single)
        self.assertEqual(values_single["APEX_WORKSPACE_USERNAME"], "ASHARIF")

        # Inner quotes preserved
        content_inner = VALID_BASE_CONFIG.replace("PROJECT_NAME=DEMO", "PROJECT_NAME=\"DEMO 'APP'\"")
        path_inner = self.write_env(content_inner)
        values_inner = read_project_env(path_inner)
        self.assertEqual(values_inner["PROJECT_NAME"], "DEMO 'APP'")

    def test_unterminated_quote_rejected(self) -> None:
        for quote in ('"', "'"):
            content = VALID_BASE_CONFIG + f"APEX_WORKSPACE_USERNAME={quote}\n"
            path = self.write_env(content)
            with self.assertRaises(ConfigError) as ctx:
                read_project_env(path)
            self.assertIn("has an unterminated quoted value", str(ctx.exception))

    def test_no_eval_command_substitution_preserved_literally(self) -> None:
        raw_cmd = "$(whoami)"
        content = VALID_BASE_CONFIG.replace("PROJECT_NAME=DEMO", f'PROJECT_NAME="{raw_cmd}"')
        path = self.write_env(content)
        values = read_project_env(path)
        self.assertEqual(values["PROJECT_NAME"], raw_cmd)

        raw_backticks = "`id`"
        content_bt = VALID_BASE_CONFIG.replace("PROJECT_NAME=DEMO", f'PROJECT_NAME="{raw_backticks}"')
        path_bt = self.write_env(content_bt)
        values_bt = read_project_env(path_bt)
        self.assertEqual(values_bt["PROJECT_NAME"], raw_backticks)

    def test_inline_comment_unquoted_rejected_quoted_allowed(self) -> None:
        content_bad = VALID_BASE_CONFIG.replace("PROJECT_NAME=DEMO", "PROJECT_NAME=DEMO # inline comment")
        path_bad = self.write_env(content_bad)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_bad)
        self.assertIn("has an inline comment", str(ctx.exception))

        content_good = VALID_BASE_CONFIG.replace("PROJECT_NAME=DEMO", 'PROJECT_NAME="DEMO # keep literal"')
        path_good = self.write_env(content_good)
        values_good = read_project_env(path_good)
        self.assertEqual(values_good["PROJECT_NAME"], "DEMO # keep literal")

    def test_values_and_raw_lines_never_leaked_in_errors(self) -> None:
        secret = "SECRET_PASSWORD_12345"
        invalid_line = f"THIS IS NOT A VALID LINE {secret}"
        content = VALID_BASE_CONFIG + invalid_line + "\n"
        path = self.write_env(content)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path)
        error_msg = str(ctx.exception)
        self.assertNotIn(secret, error_msg)
        self.assertNotIn(invalid_line, error_msg)
        self.assertIn("invalid line in", error_msg)

    def test_unknown_setting_rejected_without_value_leak(self) -> None:
        secret = "VERY_CONFIDENTIAL_VAL"
        content = VALID_BASE_CONFIG + f"UNKNOWN_CUSTOM_SETTING={secret}\n"
        path = self.write_env(content)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path)
        error_msg = str(ctx.exception)
        self.assertIn("unsupported setting in", error_msg)
        self.assertIn("UNKNOWN_CUSTOM_SETTING", error_msg)
        self.assertNotIn(secret, error_msg)

    def test_duplicate_setting_rejected_without_value_leak(self) -> None:
        secret = "SECRET_DUPLICATE_VAL"
        content = VALID_BASE_CONFIG + f"DEVELOPER_NAME={secret}\n"
        path = self.write_env(content)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path)
        error_msg = str(ctx.exception)
        self.assertIn("duplicate setting in", error_msg)
        self.assertIn("DEVELOPER_NAME", error_msg)
        self.assertNotIn(secret, error_msg)

    def test_retired_uc_apx_settings_rejected(self) -> None:
        for key in ("INSTALL_UC_APX", "UC_APX_SKILLS_AGENT"):
            for val in ("false", "true", "invalid"):
                with self.subTest(key=key, val=val):
                    content = VALID_BASE_CONFIG + f"{key}={val}\n"
                    path = self.write_env(content)
                    with self.assertRaises(ConfigError) as ctx:
                        read_project_env(path)
                    self.assertEqual(
                        str(ctx.exception),
                        "uc-apx settings are retired; remove INSTALL_UC_APX and UC_APX_SKILLS_AGENT from .env"
                    )

    def test_required_key_missing_or_whitespace_only(self) -> None:
        content_missing = "\n".join(line for line in VALID_BASE_CONFIG.splitlines() if not line.startswith("DEVELOPER_NAME="))
        path_missing = self.write_env(content_missing)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_missing)
        self.assertIn("DEVELOPER_NAME is required in", str(ctx.exception))

        content_empty = VALID_BASE_CONFIG.replace("DEVELOPER_NAME=ASHARIF", "DEVELOPER_NAME=   ")
        path_empty = self.write_env(content_empty)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_empty)
        self.assertIn("DEVELOPER_NAME is required in", str(ctx.exception))

    def test_developer_name_validation(self) -> None:
        for bad_name in ("asharif", "A" * 31, "ASH-ARIF", "1ASHARIF"):
            with self.subTest(bad_name=bad_name):
                content = VALID_BASE_CONFIG.replace("DEVELOPER_NAME=ASHARIF", f"DEVELOPER_NAME={bad_name}")
                path = self.write_env(content)
                with self.assertRaises(ConfigError) as ctx:
                    read_project_env(path)
                self.assertIn("DEVELOPER_NAME must be uppercase letters, digits, or underscores", str(ctx.exception))

    def test_db_environment_validation(self) -> None:
        for env in ("development", "test", "staging", "production"):
            content = VALID_BASE_CONFIG.replace("DB_ENVIRONMENT=development", f"DB_ENVIRONMENT={env}")
            path = self.write_env(content)
            self.assertEqual(read_project_env(path)["DB_ENVIRONMENT"], env)

        content_bad = VALID_BASE_CONFIG.replace("DB_ENVIRONMENT=development", "DB_ENVIRONMENT=local")
        path_bad = self.write_env(content_bad)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_bad)
        self.assertIn("DB_ENVIRONMENT must be development, test, staging, or production", str(ctx.exception))

    def test_apex_app_id_validation(self) -> None:
        content_multi = VALID_BASE_CONFIG.replace("APEX_APP_ID=100", "APEX_APP_ID=100,200")
        path_multi = self.write_env(content_multi)
        self.assertEqual(read_project_env(path_multi)["APEX_APP_ID"], "100,200")

        # duplicate
        content_dup = VALID_BASE_CONFIG.replace("APEX_APP_ID=100", "APEX_APP_ID=100,100")
        path_dup = self.write_env(content_dup)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_dup)
        self.assertIn("APEX_APP_ID must not contain duplicate values", str(ctx.exception))

        # invalid format
        for bad_id in ("0", "-100", "100, 200", "100,,200", "abc"):
            with self.subTest(bad_id=bad_id):
                content_bad = VALID_BASE_CONFIG.replace("APEX_APP_ID=100", f"APEX_APP_ID={bad_id}")
                path_bad = self.write_env(content_bad)
                with self.assertRaises(ConfigError) as ctx:
                    read_project_env(path_bad)
                self.assertIn("APEX_APP_ID must be a comma-separated list of positive integers", str(ctx.exception))

    def test_aligned_profiles_and_csv_checks(self) -> None:
        # Unaligned count in TABLES profile
        content_unaligned = VALID_BASE_CONFIG.replace("TABLES_SCHEMA=DEMO", "TABLES_SCHEMA=DEMO,HR")
        path_unaligned = self.write_env(content_unaligned)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_unaligned)
        self.assertIn("TABLES_SQLCL_CONNECTION, TABLES_EXPECTED_USER and TABLES_SCHEMA must list the same number of entries", str(ctx.exception))

        # Empty entry in CSV
        content_empty_csv = VALID_BASE_CONFIG.replace("TABLES_SCHEMA=DEMO", "TABLES_SCHEMA=DEMO,")
        path_empty_csv = self.write_env(content_empty_csv)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_empty_csv)
        self.assertIn("TABLES_SCHEMA must not contain empty entries", str(ctx.exception))

    def test_staging_and_prod_profiles(self) -> None:
        # Partial staging: connection without user
        content_partial = VALID_BASE_CONFIG + "STAGING_SQLCL_CONNECTION=STG\n"
        path_partial = self.write_env(content_partial)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_partial)
        self.assertIn("STAGING_SQLCL_CONNECTION and STAGING_EXPECTED_USER must be configured together", str(ctx.exception))

        # Complete staging
        content_complete = VALID_BASE_CONFIG + "STAGING_SQLCL_CONNECTION=STG\nSTAGING_EXPECTED_USER=STG_USER\nSTAGING_SCHEMA=STG_APP\n"
        path_complete = self.write_env(content_complete)
        values = read_project_env(path_complete)
        self.assertEqual(values["STAGING_SQLCL_CONNECTION"], "STG")

    def test_ords_profile(self) -> None:
        # 1 of 3 ORDS keys present
        content_partial = VALID_BASE_CONFIG + "ORDS_SCHEMA=ORDS_PUBLIC\n"
        path_partial = self.write_env(content_partial)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_partial)
        self.assertIn("ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER must be configured together", str(ctx.exception))

        # ORDS user != schema
        content_diff = VALID_BASE_CONFIG + "ORDS_SCHEMA=ORDS_PUBLIC\nORDS_SQLCL_CONNECTION=ORDS_CONN\nORDS_EXPECTED_USER=DIFFERENT_USER\n"
        path_diff = self.write_env(content_diff)
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(path_diff)
        self.assertIn("ORDS_EXPECTED_USER must equal ORDS_SCHEMA entry for entry", str(ctx.exception))

        # Complete and valid ORDS
        content_valid = VALID_BASE_CONFIG + "ORDS_SCHEMA=ORDS_PUBLIC\nORDS_SQLCL_CONNECTION=ORDS_CONN\nORDS_EXPECTED_USER=ORDS_PUBLIC\n"
        path_valid = self.write_env(content_valid)
        values = read_project_env(path_valid)
        self.assertEqual(values["ORDS_SCHEMA"], "ORDS_PUBLIC")

    def test_file_not_found_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            read_project_env(Path("/nonexistent/path/to/.env"))
        self.assertIn("configuration file not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
