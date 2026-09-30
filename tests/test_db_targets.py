import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import db_targets as targets
except ImportError:
    targets = None


ROOT = Path(__file__).resolve().parents[1]
SQLCL_ALIAS_TEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


BASE_ENV = {
    "CODE_SQLCL_CONNECTION": "dev-db",
    "CODE_EXPECTED_USER": "APP_LOGIN",
    "CODE_SCHEMA": "APP_DEV",
    "DB_ENVIRONMENT": "development",
    "STAGING_SQLCL_CONNECTION": "stage-db",
    "STAGING_EXPECTED_USER": "STAGE_DEPLOYER",
    "STAGING_SCHEMA": "APP_STAGE",
    "PROD_SQLCL_CONNECTION": "prod-db",
    "PROD_EXPECTED_USER": "PROD_DEPLOYER",
    "PROD_SCHEMA": "APP_PROD",
}


class DatabaseTargetTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(targets, "explicit environment target resolution is not implemented")
        return targets

    def test_staging_schema_is_independent_of_login_and_dev_owner(self) -> None:
        target = self.api().resolve_target(BASE_ENV, "staging", "migration")
        self.assertEqual(
            (target.connection, target.expected_user, target.schema, target.classification),
            ("stage-db", "STAGE_DEPLOYER", "APP_STAGE", "staging"),
        )

    def test_production_uses_explicit_connection_login_and_schema(self) -> None:
        target = self.api().resolve_target(BASE_ENV, "prod", "migration")
        self.assertEqual(
            (target.connection, target.expected_user, target.schema, target.classification),
            ("prod-db", "PROD_DEPLOYER", "APP_PROD", "production"),
        )

    def test_dev_migration_requires_development_or_test_classification(self) -> None:
        api = self.api()
        self.assertEqual(api.resolve_target(BASE_ENV, "dev", "migration").schema, "APP_DEV")
        test_env = {**BASE_ENV, "DB_ENVIRONMENT": "test"}
        self.assertEqual(api.resolve_target(test_env, "dev", "migration").classification, "test")
        for classification in ("staging", "production"):
            with self.subTest(classification=classification):
                with self.assertRaises(api.TargetResolutionError):
                    api.resolve_target({**BASE_ENV, "DB_ENVIRONMENT": classification}, "dev", "migration")

    def test_read_target_preserves_dev_classification(self) -> None:
        target = self.api().resolve_target({**BASE_ENV, "DB_ENVIRONMENT": "test"}, "dev", "read")
        self.assertEqual((target.environment, target.classification), ("dev", "test"))

    def test_missing_or_invalid_target_settings_never_fall_back_to_dev_schema(self) -> None:
        api = self.api()
        for key, value in (
            ("STAGING_SCHEMA", None),
            ("STAGING_SCHEMA", ""),
            ("STAGING_SCHEMA", "app_stage"),
            ("STAGING_SCHEMA", "APP STAGE"),
            ("STAGING_SQLCL_CONNECTION", None),
            ("STAGING_EXPECTED_USER", None),
        ):
            with self.subTest(key=key, value=value):
                env = dict(BASE_ENV)
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
                with self.assertRaises(api.TargetResolutionError):
                    api.resolve_target(env, "staging", "read")

    def test_unsupported_environment_and_operation_fail(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(BASE_ENV, "production", "read")
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(BASE_ENV, "prod", "write")


MULTI_ENV = {
    "DB_ENVIRONMENT": "development",
    "CODE_SQLCL_CONNECTION": "conn-a,conn-b,conn-c",
    "CODE_EXPECTED_USER": "AAA,BBB,CCC",
    "CODE_SCHEMA": "AAA,BBB,CCC",
    "STAGING_SQLCL_CONNECTION": "stage-a,stage-b",
    "STAGING_EXPECTED_USER": "SAAA,SBBB",
    "STAGING_SCHEMA": "AAA,BBB",
    "PROD_SQLCL_CONNECTION": "prod-a",
    "PROD_EXPECTED_USER": "PDEPLOY",
    "PROD_SCHEMA": "AAA",
}


class MultiSchemaTargetTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(targets, "explicit environment target resolution is not implemented")
        return targets

    def test_split_list_treats_empty_and_missing_as_no_entries(self) -> None:
        api = self.api()
        self.assertEqual((), api.split_list(""))
        self.assertEqual((), api.split_list(None))
        self.assertEqual(("A",), api.split_list("A"))
        self.assertEqual(("A", "B"), api.split_list("A,B"))

    def test_dev_target_is_selected_by_schema_name(self) -> None:
        target = self.api().resolve_target(MULTI_ENV, "dev", "read", schema="BBB")
        self.assertEqual(("conn-b", "BBB", "BBB"), (target.connection, target.expected_user, target.schema))

    def test_several_schemas_without_a_name_is_an_error_that_points_at_schema(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(MULTI_ENV, "dev", "read")
        self.assertIn("--schema", str(raised.exception))

    def test_unknown_schema_lists_the_configured_ones(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(MULTI_ENV, "dev", "read", schema="ZZZ")
        self.assertIn("AAA, BBB, CCC", str(raised.exception))

    def test_staging_uses_the_same_schema_name_in_its_own_list(self) -> None:
        target = self.api().resolve_target(MULTI_ENV, "staging", "read", schema="BBB")
        self.assertEqual(("stage-b", "SBBB", "BBB"), (target.connection, target.expected_user, target.schema))

    def test_schema_missing_from_a_target_list_is_not_deployable_there(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(MULTI_ENV, "staging", "read", schema="CCC")
        self.assertIn("STAGING_SCHEMA", str(raised.exception))

    def test_single_entry_target_list_is_not_reused_for_a_different_schema(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(MULTI_ENV, "prod", "read", schema="BBB")

    def test_blank_narrowed_profile_reports_the_schema_is_not_listed(self) -> None:
        api = self.api()
        narrowed = {**MULTI_ENV, "STAGING_SQLCL_CONNECTION": "", "STAGING_EXPECTED_USER": "", "STAGING_SCHEMA": ""}
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(narrowed, "staging", "read", schema="CCC")
        self.assertIn("not listed", str(raised.exception))

    def test_unequal_list_lengths_are_rejected(self) -> None:
        api = self.api()
        broken = {**MULTI_ENV, "CODE_EXPECTED_USER": "AAA,BBB"}
        with self.assertRaises(api.TargetResolutionError) as raised:
            api.resolve_target(broken, "dev", "read", schema="AAA")
        self.assertIn("same number of entries", str(raised.exception))

    def test_duplicate_schemas_are_rejected(self) -> None:
        api = self.api()
        broken = {**MULTI_ENV, "CODE_SCHEMA": "AAA,AAA,CCC"}
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(broken, "dev", "read", schema="AAA")

    def test_single_schema_project_still_maps_dev_name_to_a_differently_named_staging_schema(self) -> None:
        # Existing single-schema projects may name staging differently from DEV.
        target = self.api().resolve_target(BASE_ENV, "staging", "read", schema="APP_DEV")
        self.assertEqual("APP_STAGE", target.schema)

    def test_single_schema_project_refuses_an_unknown_schema_for_staging_and_prod(self) -> None:
        # The differently-named-staging exception covers the project's own DEV
        # schema only; it must never turn an arbitrary name into a target.
        api = self.api()
        for environment in ("staging", "prod"):
            with self.subTest(environment=environment):
                with self.assertRaises(api.TargetResolutionError) as raised:
                    api.resolve_target(BASE_ENV, environment, "read", schema="OTHER")
                self.assertIn("OTHER", str(raised.exception))

    def test_single_schema_dev_still_rejects_a_wrong_schema_name(self) -> None:
        api = self.api()
        with self.assertRaises(api.TargetResolutionError):
            api.resolve_target(BASE_ENV, "dev", "read", schema="OTHER")

    def test_configured_schemas_lists_the_environment_schemas(self) -> None:
        api = self.api()
        self.assertEqual(("AAA", "BBB", "CCC"), api.configured_schemas(MULTI_ENV, "dev"))
        self.assertEqual(("AAA", "BBB"), api.configured_schemas(MULTI_ENV, "staging"))
        with self.assertRaises(api.TargetResolutionError):
            api.configured_schemas(MULTI_ENV, "production")

    def test_batch_schema_rules(self) -> None:
        api = self.api()
        multi = {**MULTI_ENV, "PROJECT_MULTI_SCHEMA": "true"}
        self.assertEqual("BBB", api.batch_schema(["BBB", "BBB"], None, multi))
        self.assertEqual("BBB", api.batch_schema([None], "BBB", BASE_ENV))
        self.assertIsNone(api.batch_schema([None], None, BASE_ENV))
        with self.assertRaises(api.TargetResolutionError):
            api.batch_schema(["AAA", "BBB"], None, multi)
        with self.assertRaises(api.TargetResolutionError) as flat:
            api.batch_schema([None], None, multi)
        self.assertIn("migrations/<SCHEMA>/", str(flat.exception))
        with self.assertRaises(api.TargetResolutionError):
            api.batch_schema(["AAA"], "BBB", multi)

    def test_batch_schema_ignores_multiple_schemas_in_other_profiles(self) -> None:
        values = {**BASE_ENV, "PROJECT_MULTI_SCHEMA": "true"}
        self.assertIsNone(self.api().batch_schema([None], None, values))

    def test_batch_schema_uses_raw_code_schema_after_narrowing(self) -> None:
        narrowed = {**MULTI_ENV, "CODE_SCHEMA": "AAA", "PROJECT_CODE_SCHEMAS": "AAA,BBB,CCC"}
        with self.assertRaises(self.api().TargetResolutionError) as raised:
            self.api().batch_schema([None], "AAA", narrowed)
        self.assertIn("migrations/<SCHEMA>/", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class ProductionMarkerTests(unittest.TestCase):
    PRODUCTION = (
        "prod", "PROD", "prod1", "prod-db", "prod_db", "erp-prod", "hr.live", "production",
        "PRODDB", "proddb2", "ERPPROD", "erpprd01", "erpprod.example.com", "hr_live01",
        "prod-preprod", "preprod-live",
    )
    NOT_PRODUCTION = (
        "docker-demo", "dev", "product-dev", "products", "olive", "deliver", "livewire-dev",
        "reproduce", "prodigy", "freepdb1", "PREPROD", "preprod", "nonprod", "dev-nonprod",
        "uat_preprod", "pre-prod", "non_prd", "NONPROD01", "preproddb", "preproduction",
        "erp.preprod.example.com",
    )

    def test_python_marker(self) -> None:
        from scripts.db_targets import looks_like_production_identity
        for value in self.PRODUCTION:
            with self.subTest(value=value):
                self.assertTrue(looks_like_production_identity(value))
        for value in self.NOT_PRODUCTION:
            with self.subTest(value=value):
                self.assertFalse(looks_like_production_identity(value))

    def run_guard(self, command: list[str], connection: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            for name in ("check_db_target.sh", "check_db_target.ps1", "load_env.sh", "load_env.ps1"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
            (root / ".env").write_text(
                env_text.replace("TABLES_SQLCL_CONNECTION=docker-demo", f"TABLES_SQLCL_CONNECTION={connection}"),
                encoding="utf-8",
            )
            environment = {key: value for key, value in os.environ.items() if key != "PROJECT_SCHEMA"}
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            return subprocess.run(
                [*command[:-1], str(root / "scripts" / command[-1]), "read", "tables"],
                env=environment, text=True, capture_output=True, check=False,
            )

    def assert_guard_matches_python(self, command: list[str]) -> None:
        for value, production in [*((v, True) for v in self.PRODUCTION), *((v, False) for v in self.NOT_PRODUCTION)]:
            if not SQLCL_ALIAS_TEST.fullmatch(value):
                continue
            with self.subTest(value=value):
                result = self.run_guard(command, value)
                refused = "resembles production" in result.stderr
                self.assertEqual(production, refused, result.stdout + result.stderr)
                if not production:
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_bash_guard_matches_python(self) -> None:
        self.assert_guard_matches_python(["bash", "check_db_target.sh"])

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_powershell_guard_matches_python(self) -> None:
        self.assert_guard_matches_python(["pwsh", "-NoProfile", "-NonInteractive", "-File", "check_db_target.ps1"])

    def test_every_guard_uses_the_same_marker(self) -> None:
        from scripts.db_targets import NON_PRODUCTION_MARKER_RE, PRODUCTION_MARKER_RE
        python = PRODUCTION_MARKER_RE.pattern.replace("[^A-Za-z0-9]", "[^[:alnum:]]")
        bash_source = (ROOT / "scripts" / "check_db_target.sh").read_text(encoding="utf-8")
        bash = re.search(r"^production_marker='([^']+)'$", bash_source, re.MULTILINE).group(1)
        self.assertEqual(python, bash)
        self.assertIn(f"non_production_marker='{NON_PRODUCTION_MARKER_RE.pattern}'", bash_source)
        powershell_source = (ROOT / "scripts" / "check_db_target.ps1").read_text(encoding="utf-8")
        powershell = re.search(r"\$productionPattern = '\(\?i\)([^']+)'", powershell_source).group(1)
        self.assertEqual(PRODUCTION_MARKER_RE.pattern, powershell)
        self.assertIn(f"$nonProductionPattern = '(?i){NON_PRODUCTION_MARKER_RE.pattern}'", powershell_source)
        sql_pattern = python.replace("[0-9]", "[[:digit:]]")
        for name in ("verify_db_access.sql", "verify_migration_access.sql", "publish_app.sql"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertIn(f"'{sql_pattern}'", source)
                self.assertIn(f"c_non_production_marker CONSTANT VARCHAR2(64) := '{NON_PRODUCTION_MARKER_RE.pattern}'", source)
                for field in ("DB_NAME", "DB_UNIQUE_NAME", "SERVICE_NAME"):
                    self.assertIn(f"resembles_production(SYS_CONTEXT('USERENV', '{field}'))", source) if name != "verify_migration_access.sql" else self.assertIn(f"'{field}'", source)
                self.assertNotIn("REGEXP_LIKE(SYS_CONTEXT", source)
