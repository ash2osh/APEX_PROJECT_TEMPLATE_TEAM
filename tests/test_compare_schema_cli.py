import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot
from scripts.compare_schema import main


def make_inventory(owner, database):
    identity = {
        "session_user": owner, "current_schema": owner, "db_name": database,
        "db_unique_name": database + "_UNIQUE", "service_name": database + "_service",
        "container_id": "3", "container_name": "APP_PDB", "edition": "ORA$BASE", "database_version": "19.0",
    }
    objects = {ObjectKey(owner, "CUSTOMERS", "TABLE"): {"owner": owner, "name": "CUSTOMERS", "type": "TABLE", "status": "VALID", "last_ddl_time": "2026-09-27T00:00:00"}}
    return SchemaInventory(identity, objects, {"ownerComplete": True, "path": "OWNER_SESSION", "catalogs": ["ALL_OBJECTS"]}, "2026-09-28T10:00:00Z", "2026-09-28T10:00:02Z")


class CompareSchemaCliTests(unittest.TestCase):
    def setUp(self):
        self.inventories = {
            "dev": make_inventory("APP_DEV", "DEVDB"),
            "staging": make_inventory("APP_STAGE", "STAGEDB"),
            "prod": make_inventory("APP_PROD", "PRODDB"),
        }
        self.targets = []
        self.values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SQLCL_CONNECTION": "dev_profile", "CODE_EXPECTED_USER": "APP_DEV", "CODE_SCHEMA": "APP_DEV",
            "STAGING_SQLCL_CONNECTION": "staging_profile", "STAGING_EXPECTED_USER": "APP_STAGE", "STAGING_SCHEMA": "APP_STAGE",
            "PROD_SQLCL_CONNECTION": "prod_profile", "PROD_EXPECTED_USER": "APP_PROD", "PROD_SCHEMA": "APP_PROD",
        }

    def capture_inventory(self, target, run_dir):
        self.targets.append(target)
        return self.inventories[target.environment]

    def capture_snapshot(self, target, inventory, keys, run_dir):
        definitions = [
            ObjectDefinition(ObjectKey(inventory.identity["current_schema"], name, object_type), {"columns": [{"name": "ID", "data_type": "NUMBER", "nullable": "N", "column_id": 1}]}, f"CREATE TABLE {inventory.identity['current_schema']}.{name} (ID NUMBER NOT NULL)", (), True)
            for name, object_type in keys if object_type == "TABLE" and ObjectKey(inventory.identity["current_schema"], name, object_type) in inventory.objects
        ]
        return SchemaSnapshot(inventory.identity, inventory.objects, {item.key: item for item in definitions}, {**inventory.coverage, "catalogs": ["ALL_OBJECTS", "ALL_TABLES", "ALL_TAB_COLUMNS", "ALL_TAB_COLS", "ALL_TAB_IDENTITY_COLS", "ALL_VIEWS", "ALL_SEQUENCES", "ALL_CONSTRAINTS", "ALL_CONS_COLUMNS", "ALL_INDEXES", "ALL_IND_COLUMNS", "ALL_TRIGGERS"]}, inventory.started_at, inventory.completed_at)

    def run_cli(self, args):
        with tempfile.TemporaryDirectory() as temporary:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(args, environ=self.values, run_dir=Path(temporary), capture_inventory_fn=self.capture_inventory, capture_snapshot_fn=self.capture_snapshot)
            return code, stdout.getvalue(), stderr.getvalue()

    def test_from_to_staging_and_staging_to_prod_route_explicit_targets(self):
        code, _, _ = self.run_cli(["--from", "dev", "--to", "staging", "--object", "CUSTOMERS"])
        self.assertEqual(code, 0)
        self.assertEqual([target.environment for target in self.targets], ["dev", "staging"])

        self.targets.clear()
        code, _, _ = self.run_cli(["--from", "staging", "--to", "prod", "--object", "CUSTOMERS"])
        self.assertEqual(code, 0)
        self.assertEqual([target.environment for target in self.targets], ["staging", "prod"])

    def test_env_prod_is_dev_to_prod_and_json_emits_one_complete_report(self):
        code, output, _ = self.run_cli(["--env", "prod", "--pattern", "CUST*", "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual([target.environment for target in self.targets], ["dev", "prod"])
        self.assertEqual(report["exit_code"], 0)
        self.assertIn("normalization", report["coverage"])

    def test_duplicate_conflicting_missing_flags_and_no_selectors_fail_before_connecting(self):
        for args in (
            ["--from", "dev", "--from", "staging", "--to", "prod", "--object", "CUSTOMERS"],
            ["--to", "staging", "--env", "prod", "--object", "CUSTOMERS"],
            ["--from", "dev", "--object", "CUSTOMERS"],
            ["--from", "dev", "--to", "dev", "--object", "CUSTOMERS"],
            ["--from", "dev", "--to", "staging"],
            ["--to", "staging", "--to", "prod", "--object", "CUSTOMERS"],
        ):
            with self.subTest(args=args):
                self.targets.clear()
                code, _, stderr = self.run_cli(args)
                self.assertEqual(code, 2)
                self.assertEqual(self.targets, [])
                self.assertTrue(stderr)

    def test_missing_stage_schema_never_falls_back_to_dev(self):
        self.values.pop("STAGING_SCHEMA")
        code, _, stderr = self.run_cli(["--env", "staging", "--object", "CUSTOMERS"])
        self.assertEqual(code, 2)
        self.assertIn("STAGING_SCHEMA", stderr)
        self.assertEqual(self.targets, [])


if __name__ == "__main__":
    unittest.main()
