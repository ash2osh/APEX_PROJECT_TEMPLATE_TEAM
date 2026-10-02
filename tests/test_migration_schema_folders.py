import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import migration_manifest as manifest
from scripts.db_targets import TargetResolutionError, resolve_target
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
CHECKS = json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}) + "\n"


def add_folder(root: Path, relative: str, sql: str = "CREATE TABLE T1 (ID NUMBER);\n") -> Path:
    folder = root / relative
    folder.mkdir(parents=True)
    (folder / "001-change.sql").write_text(sql, encoding="utf-8", newline="\n")
    (folder / "checks.json").write_text(CHECKS, encoding="utf-8")
    return folder


class SchemaFolderManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def test_schema_folder_migration_loads_with_its_schema(self) -> None:
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_create-t1-r001")
        migration = manifest.load_migration(self.root, "migrations/APP_TWO/2026-09-29_create-t1-r001")
        self.assertEqual("APP_TWO", migration.schema)
        self.assertEqual("create-t1", migration.family)

    def test_flat_folder_has_no_schema(self) -> None:
        add_folder(self.root, "migrations/2026-09-29_create-t1-r001")
        self.assertIsNone(manifest.load_migration(self.root, "migrations/2026-09-29_create-t1-r001").schema)

    def test_listing_includes_both_layouts_newest_first(self) -> None:
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_create-t1-r001")
        add_folder(self.root, "migrations/APP_THREE/2026-09-30_create-t2-r001")
        names = [path.name for path in manifest.list_migration_folders(self.root)]
        self.assertEqual(["2026-09-30_create-t2-r001", "2026-09-29_create-t1-r001"], names)

    def test_same_family_and_revision_in_two_schemas_is_not_a_duplicate(self) -> None:
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APP_THREE/2026-09-29_add-flag-r001")
        self.assertEqual(2, len(manifest.list_migration_folders(self.root)))

    def test_one_family_in_flat_and_schema_layouts_is_refused_for_the_same_schema(self) -> None:
        add_folder(self.root, "migrations/2026-09-29_rebuild-r001")
        add_folder(self.root, "migrations/APP/2026-09-30_rebuild-r001")
        # Listing and loading still work; only a command aimed at APP refuses.
        self.assertEqual(2, len(manifest.list_migration_folders(self.root)))
        for folder in ("migrations/APP/2026-09-30_rebuild-r001", "migrations/2026-09-29_rebuild-r001"):
            batch = manifest.load_batch(self.root, [folder])
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(manifest.MigrationManifestError, "both migrations/ and migrations/APP/"):
                    manifest.assert_single_layout(self.root, batch, "APP", flat_folders_apply=True)

    def test_one_family_flat_and_under_a_schema_folder_is_allowed_when_flat_cannot_run(self) -> None:
        # With several schemas configured a flat folder cannot be applied, so
        # SALES/ holds the only runnable copy.
        add_folder(self.root, "migrations/2026-09-29_add-audit-r001")
        add_folder(self.root, "migrations/SALES/2026-09-29_add-audit-r001")
        batch = manifest.load_batch(self.root, ["migrations/SALES/2026-09-29_add-audit-r001"])
        manifest.assert_single_layout(self.root, batch, "SALES", flat_folders_apply=False)

    def test_one_family_under_two_schema_folders_is_allowed(self) -> None:
        add_folder(self.root, "migrations/2026-09-29_add-audit-r001")
        add_folder(self.root, "migrations/SALES/2026-09-29_add-audit-r001")
        add_folder(self.root, "migrations/HR/2026-09-29_add-audit-r001")
        batch = manifest.load_batch(self.root, ["migrations/HR/2026-09-29_add-audit-r001"])
        # The flat copy targets APP here; HR/ and SALES/ are other schemas.
        manifest.assert_single_layout(self.root, batch, "HR", flat_folders_apply=False)

    def test_different_families_may_use_different_layouts(self) -> None:
        add_folder(self.root, "migrations/2026-09-29_create-t1-r001")
        add_folder(self.root, "migrations/APP/2026-09-30_create-t2-r001")
        self.assertEqual(2, len(manifest.list_migration_folders(self.root)))

    def test_revision_gap_is_checked_per_schema(self) -> None:
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APP_THREE/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APP_THREE/2026-09-30_add-flag-r003")
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.list_migration_folders(self.root)

    def test_lowercase_directory_is_still_a_legacy_error(self) -> None:
        (self.root / "migrations" / "old-style").mkdir()
        with self.assertRaises(manifest.MigrationManifestError) as raised:
            manifest.list_migration_folders(self.root)
        self.assertIn("legacy or invalid migration directory", str(raised.exception))

    def test_three_part_path_needs_a_schema_shaped_directory(self) -> None:
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.load_migration(self.root, "migrations/app_two/2026-09-29_create-t1-r001")
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.load_migration(self.root, "migrations/A/B/2026-09-29_create-t1-r001")

    def test_batch_family_order_is_per_schema(self) -> None:
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_add-flag-r001")
        add_folder(self.root, "migrations/APP_TWO/2026-09-30_add-flag-r002")
        batch = manifest.load_batch(self.root, ["migrations/APP_TWO/2026-09-29_add-flag-r001", "migrations/APP_TWO/2026-09-30_add-flag-r002"])
        self.assertEqual(["APP_TWO", "APP_TWO"], [item.schema for item in batch])
        with self.assertRaises(manifest.MigrationManifestError):
            manifest.load_batch(self.root, ["migrations/APP_TWO/2026-09-30_add-flag-r002", "migrations/APP_TWO/2026-09-29_add-flag-r001"])


class SchemaFolderCliTests(unittest.TestCase):
    MULTI_ENV = {
        "PROJECT_ENV_FILE": "",
        "DB_ENVIRONMENT": "development",
        "CODE_SQLCL_CONNECTION": "conn-two,conn-three",
        "CODE_EXPECTED_USER": "APP_TWO,APP_THREE",
        "CODE_SCHEMA": "APP_TWO,APP_THREE",
        "PROJECT_MULTI_SCHEMA": "true",
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def run_checker(self, *arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
        return subprocess.run(
            [BASH, str(ROOT / "scripts" / "check_conflicts.sh"), "--repo-root", str(self.root), *arguments],
            cwd=ROOT, text=True, capture_output=True, check=False, env={**base, **(environment or {})},
        )

    def test_local_check_accepts_a_schema_folder(self) -> None:
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_create-t1-r001")
        result = self.run_checker("migrations/APP_TWO/2026-09-29_create-t1-r001", "--local")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Local selected-batch analysis only", result.stdout)

    def test_migrate_refuses_a_flat_folder_when_several_schemas_are_configured(self) -> None:
        from scripts import migrate
        add_folder(self.root, "migrations/2026-09-29_create-t1-r001")
        code = migrate.main(
            ["migrations/2026-09-29_create-t1-r001", "--env", "dev"],
            environ=self.MULTI_ENV, repo_root=self.root, confirm=lambda prompt: False,
        )
        self.assertEqual(2, code)

    def test_migrate_refuses_one_family_in_both_layouts_before_connecting(self) -> None:
        from unittest.mock import patch
        from scripts import migrate
        single = {**self.MULTI_ENV, "CODE_SQLCL_CONNECTION": "conn-two", "CODE_EXPECTED_USER": "APP_TWO", "CODE_SCHEMA": "APP_TWO", "PROJECT_MULTI_SCHEMA": "false"}
        add_folder(self.root, "migrations/2026-09-29_rebuild-r001")
        add_folder(self.root, "migrations/APP_TWO/2026-09-30_rebuild-r001")
        with patch.object(migrate, "apply_batch", side_effect=AssertionError("must refuse before applying")):
            code = migrate.main(
                ["migrations/APP_TWO/2026-09-30_rebuild-r001", "--env", "dev"],
                environ=single, repo_root=self.root, confirm=lambda prompt: False,
            )
        self.assertEqual(2, code)

    def test_migrate_refuses_a_batch_that_mixes_schemas(self) -> None:
        from scripts import migrate
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_create-t1-r001")
        add_folder(self.root, "migrations/APP_THREE/2026-09-29_create-t2-r001")
        code = migrate.main(
            ["migrations/APP_TWO/2026-09-29_create-t1-r001", "migrations/APP_THREE/2026-09-29_create-t2-r001", "--env", "dev"],
            environ=self.MULTI_ENV, repo_root=self.root, confirm=lambda prompt: False,
        )
        self.assertEqual(2, code)

    def test_migrate_refuses_a_schema_option_that_disagrees_with_the_folder(self) -> None:
        from scripts import migrate
        add_folder(self.root, "migrations/APP_TWO/2026-09-29_create-t1-r001")
        code = migrate.main(
            ["migrations/APP_TWO/2026-09-29_create-t1-r001", "--env", "dev", "--schema", "APP_THREE"],
            environ=self.MULTI_ENV, repo_root=self.root, confirm=lambda prompt: False,
        )
        self.assertEqual(2, code)

    def test_resolver_picks_the_folders_schema_for_dev(self) -> None:
        target = resolve_target(self.MULTI_ENV, "dev", "migration", schema="APP_THREE")
        self.assertEqual(("conn-three", "APP_THREE", "APP_THREE"), (target.connection, target.expected_user, target.schema))
        with self.assertRaises(TargetResolutionError):
            resolve_target(self.MULTI_ENV, "dev", "migration")


class CompareSchemaOptionTests(unittest.TestCase):
    def test_compare_requires_schema_when_several_are_configured(self) -> None:
        from scripts import compare_schema
        environment = {
            "DB_ENVIRONMENT": "development",
            "CODE_SQLCL_CONNECTION": "conn-a,conn-b", "CODE_EXPECTED_USER": "AAA,BBB", "CODE_SCHEMA": "AAA,BBB",
            "STAGING_SQLCL_CONNECTION": "stage-a,stage-b", "STAGING_EXPECTED_USER": "SAAA,SBBB", "STAGING_SCHEMA": "AAA,BBB",
        }
        code = compare_schema.main(["--env", "staging", "--object", "T1"], environ=environment)
        self.assertNotEqual(0, code)


if __name__ == "__main__":
    unittest.main()
