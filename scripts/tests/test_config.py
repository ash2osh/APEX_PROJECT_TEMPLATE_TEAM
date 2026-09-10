from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from teamlib.config import (
    ConfigError,
    OFFLINE_COMMANDS,
    Target,
    is_offline_command,
    load_config,
    parse_apps,
    parse_env_text,
    parse_target_contract,
    profile_target,
)


BASE_ENV = """\
PROJECT_NAME=team-template
TARGET_ROLE=developer
DB_ENVIRONMENT=development
APEX_APPS=checkout:101,admin:102
TABLES_SCHEMA=APP_DATA
CODE_SCHEMA=APP_CODE
APEX_PARSING_SCHEMA=APP
METADATA_SCHEMA=APP_META
APP_OWNERSHIP_MODE=shared
APEX_WORKSPACE_ID=5402650006222933
TABLES_SQLCL_CONNECTION=docker-demo
TABLES_EXPECTED_USER=DEMO
TABLES_EXPECTED_CURRENT_SCHEMA=DEMO
TABLES_EXPECTED_DB_NAME=FREEPDB1
TABLES_EXPECTED_SERVICE=freepdb1
TABLES_EXPECTED_INSTANCE_ID=FREEPDB1
CODE_SQLCL_CONNECTION=docker-demo
CODE_EXPECTED_USER=DEMO
CODE_EXPECTED_CURRENT_SCHEMA=DEMO
CODE_EXPECTED_DB_NAME=FREEPDB1
CODE_EXPECTED_SERVICE=freepdb1
CODE_EXPECTED_INSTANCE_ID=FREEPDB1
APEX_SQLCL_CONNECTION=docker-demo
APEX_EXPECTED_USER=DEMO
APEX_EXPECTED_CURRENT_SCHEMA=DEMO
APEX_EXPECTED_DB_NAME=FREEPDB1
APEX_EXPECTED_SERVICE=freepdb1
APEX_EXPECTED_INSTANCE_ID=FREEPDB1
METADATA_SQLCL_CONNECTION=docker-demo
METADATA_EXPECTED_USER=DEMO
METADATA_EXPECTED_CURRENT_SCHEMA=DEMO
METADATA_EXPECTED_DB_NAME=FREEPDB1
METADATA_EXPECTED_SERVICE=freepdb1
METADATA_EXPECTED_INSTANCE_ID=FREEPDB1
VERIFY_SQLCL_CONNECTION=docker-demo
VERIFY_EXPECTED_USER=DEMO
VERIFY_EXPECTED_CURRENT_SCHEMA=DEMO
VERIFY_EXPECTED_DB_NAME=FREEPDB1
VERIFY_EXPECTED_SERVICE=freepDB1
VERIFY_EXPECTED_INSTANCE_ID=FREEPDB1
"""


class ParseAppsTests(unittest.TestCase):
    def test_alias_mapping(self):
        self.assertEqual(parse_apps("checkout:101,admin:102"),
                         {"checkout": 101, "admin": 102})

    def test_reject_ambiguous_or_unsafe_mapping(self):
        for value in (
            "Checkout:101", "../x:101", "x:0", "x:101,x:102",
            "x:101,y:101", "x:101, y:102", "x:101,",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_apps(value)


class EnvParserTests(unittest.TestCase):
    def test_literal_values_are_not_executed(self):
        values = parse_env_text(BASE_ENV.replace(
            "PROJECT_NAME=team-template", "PROJECT_NAME=$(touch /tmp/config-sentinel)"
        ))
        self.assertEqual(values["PROJECT_NAME"], "$(touch /tmp/config-sentinel)")

    def test_duplicate_unknown_and_control_character_keys_fail(self):
        for suffix in (
            "PROJECT_NAME=again\n",
            "NOT_ALLOWED=value\n",
            "PROJECT_NAME=bad\x00value\n",
        ):
            with self.subTest(suffix=repr(suffix)), self.assertRaises(ConfigError):
                parse_env_text(BASE_ENV + suffix)

    def test_empty_required_value_and_malformed_line_fail(self):
        for source in (BASE_ENV.replace("CODE_SCHEMA=APP_CODE", "CODE_SCHEMA="),
                       BASE_ENV + "not-a-setting\n"):
            with self.subTest(source=source[-40:]), self.assertRaises(ConfigError):
                parse_env_text(source)

    def test_partial_verify_profile_fails_when_loaded(self):
        source = BASE_ENV.replace("VERIFY_EXPECTED_SERVICE=freepDB1\n", "")
        with self.assertRaises(ConfigError):
            load_config(self._write_env(source))

    def test_profiles_may_be_equal(self):
        config = load_config(self._write_env(BASE_ENV))
        self.assertEqual(config.profiles["TABLES"].connection, "docker-demo")
        self.assertEqual(config.profiles["APEX"].expected_user, "DEMO")

    def test_instance_identity_must_match_across_schema_profiles(self):
        source = BASE_ENV.replace(
            "CODE_EXPECTED_INSTANCE_ID=FREEPDB1",
            "CODE_EXPECTED_INSTANCE_ID=OTHER",
        )
        with self.assertRaises(ConfigError):
            load_config(self._write_env(source))

    def _write_env(self, source: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="team-config-test-"))
        path = directory / ".env"
        path.write_text(source, encoding="utf-8", newline="")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return path


class CommandRoutingTests(unittest.TestCase):
    def test_offline_commands_do_not_require_env(self):
        for command in OFFLINE_COMMANDS:
            with self.subTest(command=command):
                self.assertTrue(is_offline_command(command))
        self.assertFalse(is_offline_command("export-app"))


class TargetTests(unittest.TestCase):
    def test_profile_target_has_full_local_binding_identity(self):
        config = load_config(self._write_env(BASE_ENV))
        target = profile_target(config, "APEX", alias="checkout")
        self.assertIsInstance(target, Target)
        self.assertEqual(target.alias, "checkout")
        self.assertEqual(target.app_id, 101)
        self.assertEqual(target.workspace_id, 5402650006222933)
        self.assertTrue(target.binding_digest)
        self.assertNotEqual(target.state_key, target.physical_key)

    def test_target_contract_rejects_secret_and_wrong_role(self):
        contract = {
            "version": 1,
            "project": "team-template",
            "role": "integration",
            "environment": "staging",
            "instance_id": "FREEPDB1",
            "db_name": "FREEPDB1",
            "service": "freep1",
            "session_user": "DEMO",
            "current_schema": "APP",
            "workspace_id": 5402650006222933,
            "app_ids": {"checkout": 201},
            "recovery_owner": {"role": "apex-recovery", "members": ["a", "b"]},
            "password": "oracle",
        }
        with self.assertRaises(ConfigError):
            parse_target_contract(contract, expected_role="test")

    def test_target_contract_requires_two_recovery_owner_members(self):
        contract = {
            "version": 1,
            "project": "team-template",
            "role": "integration",
            "environment": "staging",
            "instance_id": "FREEPDB1",
            "db_name": "FREEPDB1",
            "service": "freep1",
            "session_user": "DEMO",
            "current_schema": "APP",
            "workspace_id": 5402650006222933,
            "app_ids": {"checkout": 201},
            "recovery_owner": {"role": "apex-recovery", "members": ["a"]},
        }
        with self.assertRaises(ConfigError):
            parse_target_contract(contract, expected_role="integration")

    def test_target_contract_accepts_credential_free_schema(self):
        contract = {
            "version": 1,
            "project": "team-template",
            "role": "integration",
            "environment": "staging",
            "instance_id": "FREEPDB1",
            "db_name": "FREEPDB1",
            "service": "freep1",
            "session_user": "DEMO",
            "current_schema": "APP",
            "workspace_id": 5402650006222933,
            "app_ids": {"checkout": 201},
            "recovery_owner": {"role": "apex-recovery", "members": ["a", "b"]},
        }
        parsed = parse_target_contract(contract, expected_role="integration")
        self.assertEqual(parsed.app_ids, {"checkout": 201})

    def test_target_binding_cannot_override_identity_or_connection(self):
        contract = {
            "version": 1,
            "project": "team-template",
            "role": "integration",
            "environment": "staging",
            "instance_id": "FREEPDB1",
            "db_name": "FREEPDB1",
            "service": "freep1",
            "session_user": "DEMO",
            "current_schema": "APP",
            "workspace_id": 5402650006222933,
            "app_ids": {"checkout": 201},
            "recovery_owner": {"role": "apex-recovery", "members": ["a", "b"]},
            "binding": {"connection": "test-apex", "instance_id": "OTHER"},
        }
        with self.assertRaises(ConfigError):
            parse_target_contract(contract, expected_role="integration")

    def test_target_binding_schema_cannot_override_identity(self):
        contract = {
            "version": 1,
            "project": "team-template",
            "role": "integration",
            "environment": "staging",
            "instance_id": "FREEPDB1",
            "db_name": "FREEPDB1",
            "service": "freep1",
            "session_user": "DEMO",
            "current_schema": "APP",
            "workspace_id": 5402650006222933,
            "app_ids": {"checkout": 201},
            "recovery_owner": {"role": "apex-recovery", "members": ["a", "b"]},
            "binding": {"schema": "OTHER"},
        }
        with self.assertRaises(ConfigError):
            parse_target_contract(contract, expected_role="integration")

    def test_development_contract_leaves_binding_identity_in_env(self):
        parsed = parse_target_contract(
            {
                "version": 1,
                "project": "team-template",
                "role": "developer",
                "environment": "development",
                "recovery_owner": {
                    "role": "apex-recovery",
                    "members": ["a", "b"],
                },
            },
            expected_role="developer",
        )
        self.assertIsNone(parsed.workspace_id)
        self.assertEqual(parsed.app_ids, {})

    def test_production_target_contract_parses_with_production_role(self):
        parsed = parse_target_contract(
            Path(__file__).resolve().parents[2] / "targets" / "production.json",
            expected_role="production",
        )
        self.assertEqual(parsed.role, "production")
        self.assertEqual(parsed.environment, "production")

    def _write_env(self, source: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="team-target-test-"))
        path = directory / ".env"
        path.write_text(source, encoding="utf-8", newline="")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return path


class RoleEnvironmentInvariantTests(unittest.TestCase):
    def test_production_role_requires_production_environment(self):
        from teamlib.config import ConfigError, load_config
        root = Path(__file__).resolve().parents[2]
        text = (root / ".env.example").read_text(encoding="utf-8")
        mismatched = text.replace("TARGET_ROLE=developer", "TARGET_ROLE=production")
        with tempfile.TemporaryDirectory(prefix="team-config-") as directory:
            path = Path(directory) / "mismatch.env"
            path.write_text(mismatched, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "production"):
                load_config(path)

    def test_production_environment_requires_production_role(self):
        from teamlib.config import ConfigError, load_config
        root = Path(__file__).resolve().parents[2]
        text = (root / ".env.example").read_text(encoding="utf-8")
        mismatched = text.replace("DB_ENVIRONMENT=development", "DB_ENVIRONMENT=production")
        with tempfile.TemporaryDirectory(prefix="team-config-") as directory:
            path = Path(directory) / "mismatch.env"
            path.write_text(mismatched, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "production"):
                load_config(path)

    def test_the_shipped_example_and_target_contracts_still_load(self):
        from teamlib.config import load_config, parse_target_contract
        root = Path(__file__).resolve().parents[2]
        load_config(root / ".env.example")
        for name in ("development", "integration", "test", "production", "controllers", "masters"):
            path = root / "targets" / f"{name}.json"
            if path.is_file() and name not in {"controllers", "masters"}:
                parse_target_contract(path)


class TargetBindingTypeTests(unittest.TestCase):
    def _contract(self, connection):
        return {
            "version": 1,
            "project": "team",
            "role": "integration",
            "environment": "test",
            "instance_id": "INST",
            "db_name": "DB",
            "service": "svc",
            "session_user": "APP",
            "current_schema": "APP",
            "workspace_id": 1,
            "app_ids": {"checkout": 101},
            "recovery_owner": {"role": "owners", "members": ["a", "b"]},
            "binding": {"connection": connection},
        }

    def test_non_string_connection_is_a_config_error(self):
        for value in (5, True, ["alias"], {"name": "alias"}):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_target_contract(self._contract(value))

    def test_valid_connection_still_parses(self):
        contract = parse_target_contract(self._contract("integration-alias"))
        self.assertEqual(contract.binding["connection"], "integration-alias")


if __name__ == "__main__":
    unittest.main()
