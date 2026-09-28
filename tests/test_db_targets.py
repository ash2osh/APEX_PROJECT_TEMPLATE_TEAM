import unittest

try:
    from scripts import db_targets as targets
except ImportError:
    targets = None


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


if __name__ == "__main__":
    unittest.main()
