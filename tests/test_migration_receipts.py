import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.migrate import MigrationApplyError, build_receipt
from scripts.migration_checks import CheckReport
from scripts.migration_manifest import MigrationManifestError, install_receipt, load_migration, validate_receipt
from scripts.schema_catalog import SchemaSnapshot


class MigrationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "migrations" / "2026-09-28_create-customers-r001"
        self.folder.mkdir(parents=True)
        (self.folder / "001-create-table.sql").write_text("CREATE TABLE CUSTOMERS (ID NUMBER);\n", encoding="utf-8")
        (self.folder / "checks.json").write_text(
            '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"customers-exist","sql":"SELECT 1 FROM dual","expected":1}]}\n',
            encoding="utf-8",
        )
        self.migration = load_migration(self.root, "migrations/2026-09-28_create-customers-r001")
        self.target = {
            "environment": "staging",
            "connection": "stage-profile",
            "expected_user": "STAGE_LOGIN",
            "session_user": "STAGE_LOGIN",
            "current_schema": "APP_STAGE",
            "db_name": "STAGEDB",
            "db_unique_name": "STAGEDB_UNIQUE",
            "service_name": "stage.service",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "19.0",
        }
        started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=2)
        self.evidence = {
            "committed": True,
            "payloadDigest": self.migration.payload_digest,
            "applyStartedAt": started.isoformat().replace("+00:00", "Z"),
            "applyCompletedAt": (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "identity": {key: value for key, value in self.target.items() if key not in {"environment", "connection", "expected_user"}},
        }
        self.snapshot = SchemaSnapshot(
            self.evidence["identity"],
            {},
            {},
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]},
            (started + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
            (started + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        )
        self.checks = CheckReport(True, True, ({"id": "customers-exist", "row_count": 1, "column_count": 1, "numeric": True, "value": 1, "passed": True},), (), {"complete": True})
        self.catalog = ({"id": "catalog-001-001-create-table-customers", "kind": "catalog", "passed": True, "rows": 1, "value": 1, "observed": {"name": "CUSTOMERS"}},)

    def tearDown(self):
        self.temporary.cleanup()

    def test_verified_receipt_records_content_target_and_both_verification_sources(self):
        receipt = build_receipt(self.migration, self.target, self.evidence, self.snapshot, self.checks, self.catalog)
        path = self.folder / "status.staging.json"

        self.assertFalse(path.exists())
        install_receipt(path, receipt)
        observed = validate_receipt(path, self.migration, self.target)

        self.assertEqual(observed["state"], "verified")
        self.assertEqual({result.get("kind", "authored") for result in observed["checks"]}, {"authored", "catalog"})
        self.assertEqual(observed["files"][0]["sha256"], self.migration.files[0].sha256)
        self.assertEqual(observed["payloadDigest"], self.migration.payload_digest)
        self.assertNotIn("developer", str(observed).casefold())
        self.assertNotIn("password", str(observed).casefold())

    def test_receipt_refuses_uncommitted_or_unverified_results(self):
        with self.assertRaises(MigrationApplyError):
            build_receipt(self.migration, self.target, {**self.evidence, "committed": False}, self.snapshot, self.checks, self.catalog)
        failed_checks = CheckReport(False, True, (), ({"code": "CHECK_FAILED"},), {"complete": True})
        with self.assertRaises(MigrationApplyError):
            build_receipt(self.migration, self.target, self.evidence, self.snapshot, failed_checks, self.catalog)
        failed_catalog = ({**self.catalog[0], "passed": False, "rows": 0, "value": 0},)
        with self.assertRaises(MigrationApplyError):
            build_receipt(self.migration, self.target, self.evidence, self.snapshot, self.checks, failed_catalog)

    def test_atomic_receipt_install_never_overwrites_verified_status(self):
        receipt = build_receipt(self.migration, self.target, self.evidence, self.snapshot, self.checks, self.catalog)
        path = self.folder / "status.staging.json"
        install_receipt(path, receipt)
        original = path.read_bytes()

        with self.assertRaises(MigrationManifestError):
            install_receipt(path, {**receipt, "payloadDigest": "changed"})

        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
