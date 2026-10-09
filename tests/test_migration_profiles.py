"""Independent migration targets without changing metadata/APEX profiles."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import db_targets as targets
from scripts.local_config import read_project_env, ConfigError
from scripts import migration_checks
from scripts.schema_catalog import CatalogError
from test_db_targets import BASE_ENV
from test_env_schema_lists import write_env, load
import _no_real_sqlcl  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which('pwsh')


def profile(prefix='', schemas='API', connections='api-dev', users='API_DEPLOY'):
    return {prefix+'MIGRATION_SCHEMA': schemas, prefix+'MIGRATION_SQLCL_CONNECTION': connections,
            prefix+'MIGRATION_EXPECTED_USER': users}


class MigrationProfilesTests(unittest.TestCase):
    def test_independent_dev_lists_choose_position_and_leave_read_profile_unchanged(self):
        values = {**BASE_ENV, **profile(schemas='CUSTDATA,API', connections='data-dev,api-dev', users='DATA_DEPLOY,API_DEPLOY')}
        target = targets.resolve_target(values, 'dev', 'migration', 'API')
        self.assertEqual((target.schema, target.connection, target.expected_user), ('API', 'api-dev', 'API_DEPLOY'))
        self.assertEqual(targets.resolve_target(values, 'dev', 'read').schema, 'APP_DEV')

    def test_each_environment_uses_its_migration_profile(self):
        for env, prefix, schema, conn in [('dev', '', 'API', 'api-dev'), ('staging', 'STAGING_', 'API_STAGE', 'api-stage'), ('prod', 'PROD_', 'API_PROD', 'api-prod')]:
            with self.subTest(env=env):
                values = {**BASE_ENV, **profile(prefix, schema, conn, 'DEPLOYER')}
                target = targets.resolve_target(values, env, 'migration')
                self.assertEqual((target.schema, target.connection, target.expected_user), (schema, conn, 'DEPLOYER'))

    def test_single_migration_owner_maps_to_renamed_staging_independently_of_code(self):
        values = {**BASE_ENV, **profile(), **profile('STAGING_', 'API_STAGE', 'api-stage', 'DEPLOYER')}
        self.assertEqual(targets.resolve_target(values, 'staging', 'migration', 'API').schema, 'API_STAGE')
        with self.assertRaises(targets.TargetResolutionError):
            targets.resolve_target(values, 'staging', 'migration', 'UNLISTED')
        self.assertEqual(targets.resolve_target(values, 'staging', 'read', 'APP_DEV').schema, 'APP_STAGE')

    def test_partial_or_empty_profiles_never_fall_back(self):
        for env, prefix in [('dev', ''), ('staging', 'STAGING_'), ('prod', 'PROD_')]:
            complete = profile(prefix)
            for key in complete:
                for missing in (True, False):
                    values = {**BASE_ENV, **complete}
                    if missing: values.pop(key)
                    else: values[key] = ''
                    with self.subTest(env=env, key=key, missing=missing):
                        with self.assertRaises(targets.TargetResolutionError):
                            targets.resolve_target(values, env, 'migration')

    def test_misaligned_or_duplicate_migration_lists_refuse(self):
        for override in [{'MIGRATION_SCHEMA': 'API,DATA'}, {'MIGRATION_SCHEMA': 'API,API', 'MIGRATION_SQLCL_CONNECTION': 'a,b', 'MIGRATION_EXPECTED_USER': 'A,B'}]:
            with self.subTest(override=override):
                with self.assertRaises(targets.TargetResolutionError):
                    targets.resolve_target({**BASE_ENV, **profile(), **override}, 'dev', 'migration', 'API')

    def test_migration_profiles_keep_production_and_classification_guards(self):
        for override in [{'MIGRATION_SQLCL_CONNECTION': 'live'}, {'DB_ENVIRONMENT': 'production'}]:
            with self.subTest(override=override):
                with self.assertRaises(targets.TargetResolutionError):
                    targets.resolve_target({**BASE_ENV, **profile(), **override}, 'dev', 'migration')

    def test_migration_schema_inventory_and_flat_layout_use_migration_owners(self):
        values = {**BASE_ENV, 'CODE_SCHEMA': 'CODE_A,CODE_B', **profile()}
        self.assertEqual(targets.configured_migration_schemas(values, 'dev'), ('API',))
        self.assertTrue(targets.flat_migrations_apply(values))
        narrowed = {**values, 'PROJECT_MIGRATION_SCHEMAS': 'API,DATA'}
        self.assertFalse(targets.flat_migrations_apply(narrowed))
        self.assertEqual(targets.configured_migration_schemas(BASE_ENV, 'dev'), ('APP_DEV',))

    def test_live_conflict_checks_inspect_migration_owner(self):
        observed = []
        def capture(target, directory):
            observed.append((target.schema, target.connection, target.expected_user))
            raise CatalogError('controlled read-only fixture failure')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {**BASE_ENV, **profile()}, clear=True), patch('scripts.schema_catalog.capture_inventory', side_effect=capture):
            (Path(directory) / 'migrations').mkdir()
            report = migration_checks._live_report([], 'dev', Path(directory))
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(observed, [('API', 'api-dev', 'API_DEPLOY')])


class MigrationProfileLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_all_loaders_accept_and_narrow_a_migration_only_schema(self):
        extra = '\n'.join(f'{k}={v}' for k,v in profile(schemas='API,DATA', connections='api-dev,data-dev', users='API_DEPLOY,DATA_DEPLOY').items())+'\n'
        env_file = write_env(self.directory, {}, extra)
        values = read_project_env(env_file)
        self.assertEqual(values['MIGRATION_SCHEMA'], 'API,DATA')
        probe = 'printf "%s|%s|%s|%s" "$MIGRATION_SCHEMA" "$MIGRATION_SQLCL_CONNECTION" "$PROJECT_MIGRATION_SCHEMAS" "$CODE_SCHEMA"'
        result = load(env_file, probe, 'API')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'API|api-dev|API,DATA|')
        if PWSH:
            env = os.environ.copy(); env['PROJECT_SCHEMA']='API'; env['PROFILE_ENV_FILE']=str(env_file)
            command = f'. "{ROOT / "scripts/load_env.ps1"}" -EnvFile $env:PROFILE_ENV_FILE; "$($env:MIGRATION_SCHEMA)|$($env:MIGRATION_SQLCL_CONNECTION)|$($env:PROJECT_MIGRATION_SCHEMAS)|$($env:CODE_SCHEMA)"'
            result = subprocess.run([PWSH, '-NoProfile', '-Command', command], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'API|api-dev|API,DATA|')

    def test_optional_profile_incomplete_refuses_in_offline_and_shell_loaders(self):
        env_file = write_env(self.directory, {}, 'MIGRATION_SCHEMA=API\n')
        with self.assertRaisesRegex(ConfigError, 'together'):
            read_project_env(env_file)
        result = load(env_file, 'printf loaded')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('loaded', result.stdout)
        self.assertIn('together', result.stderr)

    def test_renamed_migration_promotion_survives_schema_narrowing(self):
        extra = {**profile(), **profile('STAGING_', 'API_STAGE', 'api-stage', 'DEPLOYER')}
        env_file = write_env(self.directory, {}, '\n'.join(f'{k}={v}' for k,v in extra.items())+'\n')
        probe = 'printf "%s|%s|%s" "$MIGRATION_SCHEMA" "$STAGING_MIGRATION_SCHEMA" "$STAGING_MIGRATION_SQLCL_CONNECTION"'
        result = load(env_file, probe, 'API')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'API|API_STAGE|api-stage')
