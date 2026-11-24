import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import compare_schema
from scripts.schema_catalog import CatalogError, ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


def inventory(owner):
    identity = {
        "session_user": owner, "current_schema": owner, "db_name": owner + "DB", "db_unique_name": owner + "DB_UNIQUE",
        "service_name": owner + "_service", "container_id": "3", "container_name": "APP_PDB", "edition": "ORA$BASE",
        "database_version": "19.0",
    }
    objects = {ObjectKey(owner, "CUSTOMERS", "TABLE"): {
        "owner": owner, "name": "CUSTOMERS", "type": "TABLE", "status": "VALID", "last_ddl_time": "2026-09-27T00:00:00"}}
    return SchemaInventory(identity, objects, {"ownerComplete": True, "path": "OWNER_SESSION", "catalogs": ["ALL_OBJECTS"]},
                           "2026-09-28T10:00:00Z", "2026-09-28T10:00:02Z")


CATALOGS = ["ALL_OBJECTS", "ALL_TABLES", "ALL_TAB_COLUMNS", "ALL_TAB_COLS", "ALL_TAB_IDENTITY_COLS", "ALL_VIEWS", "ALL_SEQUENCES",
            "ALL_CONSTRAINTS", "ALL_CONS_COLUMNS", "ALL_INDEXES", "ALL_IND_COLUMNS", "ALL_TRIGGERS"]


def snapshot_of(observed, keys=()):
    owner = observed.identity["current_schema"]
    definitions = [
        ObjectDefinition(
            ObjectKey(owner, name, object_type),
            {"columns": [{"name": "ID", "data_type": "NUMBER", "nullable": "N", "column_id": 1}]},
            f"CREATE TABLE {owner}.{name} (ID NUMBER NOT NULL)", (), True)
        for name, object_type in keys if object_type == "TABLE" and ObjectKey(owner, name, object_type) in observed.objects
    ]
    return SchemaSnapshot(observed.identity, observed.objects, {item.key: item for item in definitions},
                          {**observed.coverage, "catalogs": CATALOGS}, observed.started_at, observed.completed_at)


VALUES = {
    "DB_ENVIRONMENT": "development",
    "CODE_SQLCL_CONNECTION": "dev_profile", "CODE_EXPECTED_USER": "APP_DEV", "CODE_SCHEMA": "APP_DEV",
    "STAGING_SQLCL_CONNECTION": "staging_profile", "STAGING_EXPECTED_USER": "APP_STAGE", "STAGING_SCHEMA": "APP_STAGE",
}


class CompareSchemaScratchTests(unittest.TestCase):
    """A read-only command keeps its SQLcl logs only when it could not finish."""

    def run_compare(self, root, inventory_fn):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(compare_schema, "ROOT", root), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = compare_schema.main(
                ["--from", "dev", "--to", "staging", "--object", "CUSTOMERS"], environ=VALUES,
                capture_inventory_fn=inventory_fn, capture_snapshot_fn=lambda target, observed, keys, run_dir: snapshot_of(observed, keys),
            )
        return code, stderr.getvalue(), sorted(path.name for path in (root / "scratch").glob("compare-schema-*"))

    def test_a_complete_comparison_leaves_no_scratch_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, _, leftovers = self.run_compare(
                Path(temporary), lambda target, run_dir: inventory("APP_DEV" if target.environment == "dev" else "APP_STAGE"))
            self.assertEqual(code, 0)
            self.assertEqual(leftovers, [])

    def test_a_comparison_that_could_not_finish_keeps_its_logs(self):
        def unavailable(target, run_dir):
            if target.environment == "staging":
                raise CatalogError("SQLcl could not connect")
            return inventory("APP_DEV")

        with tempfile.TemporaryDirectory() as temporary:
            code, _, leftovers = self.run_compare(Path(temporary), unavailable)
            self.assertEqual(code, 2)
            self.assertEqual(len(leftovers), 1)

    def test_an_interrupted_comparison_says_nothing_changed_and_leaves_no_traceback_or_directory(self):
        def interrupted(target, run_dir):
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as temporary:
            code, stderr, leftovers = self.run_compare(Path(temporary), interrupted)
            self.assertEqual(code, 130)
            self.assertIn("nothing was changed", stderr)
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
