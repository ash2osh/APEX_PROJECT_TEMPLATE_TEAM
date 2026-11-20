import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

try:
    from scripts import migration_archive as archive
except ImportError:
    archive = None


CHECKS = b'{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n'
IDENTITY = {"session_user": "APP_DEV", "current_schema": "APP_DEV", "db_name": "DB",
            "db_unique_name": "DB_A", "service_name": "SERVICE", "container_id": "3",
            "container_name": "PDB_A", "edition": "ORA$BASE", "database_version": "19.0"}
RUN_ID = "migration-attempt-archive-test"


def add_folder(root: Path, relative: str) -> Path:
    folder = root / relative
    folder.mkdir(parents=True)
    (folder / "001-create-table.sql").write_bytes(b"CREATE TABLE T (ID NUMBER);\n")
    (folder / "checks.json").write_bytes(CHECKS)
    return folder


def verified_receipt(folder: Path, migration, *, schema: str = "APP_DEV", environment: str = "dev") -> dict:
    return {
        "schemaVersion": 1,
        "state": "verified",
        "environment": environment,
        "migration": folder.name,
        "createdDate": migration.date,
        "family": migration.family,
        "revision": migration.revision,
        "files": [{"name": item.name, "sequence": item.sequence, "sha256": item.sha256} for item in migration.files],
        "checksSha256": migration.checks_sha256,
        "payloadDigest": migration.payload_digest,
        "target": {"environment": environment, **IDENTITY, "current_schema": schema},
        "applyStartedAt": "2026-10-01T10:00:00+00:00",
        "applyCompletedAt": "2026-10-01T10:01:00+00:00",
        "verifiedAt": "2026-10-01T10:02:00+00:00",
        "checks": [{"id": "ready", "passed": True}],
        "verifier": "test",
        "normalization": "test",
    }


def add_pending_attempt_event(folder: Path, relative: str, migration, *, run_id: str = RUN_ID) -> Path:
    event_id = str(uuid.uuid4())
    event = {
        "schemaVersion": 1,
        "eventId": event_id,
        "attemptId": str(uuid.uuid4()),
        "runId": run_id,
        "predecessorEventId": None,
        "recordedAt": "2026-10-01T09:59:00Z",
        "migrationPath": relative,
        "environment": "dev",
        "target": {"environment": "dev", "connection": "fake-dev", "expected_user": "APP_DEV", "current_schema": "APP_DEV"},
        "identity": dict(IDENTITY),
        "files": [{"name": item.name, "sequence": item.sequence, "sha256": item.sha256} for item in migration.files],
        "checksSha256": migration.checks_sha256,
        "payloadDigest": migration.payload_digest,
        "state": "pending",
        "writeAttempted": True,
        "evidence": [],
    }
    path = folder / f"attempt-event.{event_id}.json"
    path.write_text(json.dumps(event, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


class ArchiveInterruptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()
        self.relative = "migrations/APP_DEV/2026-10-01_interrupt-family-r001"
        self.folder = add_folder(self.root, self.relative)
        self.destination = self.root / "migrations-archive/APP_DEV" / self.folder.name
        self.index = self.root / "migrations/APP_DEV/.archived.json"

    def require_archive(self):
        self.assertIsNotNone(archive, "migration archiving is not implemented")
        return archive

    def assert_nothing_archived(self) -> None:
        self.assertTrue(self.folder.is_dir(), "the folder must be back in its source place")
        self.assertFalse(self.destination.exists(), "the archive destination must be rolled back")
        self.assertFalse(self.index.exists(), "no archive index may be written")

    def test_keyboard_interrupt_while_writing_the_index_rolls_the_moved_folder_back(self) -> None:
        api = self.require_archive()

        def interrupt(path: Path, source: bytes) -> None:
            raise KeyboardInterrupt

        with patch.object(api, "_atomic_write", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                api.archive_migrations(self.root, [self.relative], yes=True)

        self.assert_nothing_archived()

    def test_interrupt_between_the_rename_and_its_bookkeeping_still_rolls_back(self) -> None:
        api = self.require_archive()
        real_replace = os.replace
        interrupted = []

        def replace_then_interrupt(source, destination):
            real_replace(source, destination)
            if not interrupted and Path(destination) == self.destination:
                interrupted.append(True)
                raise KeyboardInterrupt

        with patch.object(api.os, "replace", new=replace_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                api.archive_migrations(self.root, [self.relative], yes=True)

        self.assertTrue(interrupted, "the interrupt was not reached")
        self.assert_nothing_archived()


class ReceiptScheduleMatchTests(unittest.TestCase):
    """A verified run for a same-named folder in another schema must not decide this schema's pending attempt."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def require_archive(self):
        self.assertIsNotNone(archive, "migration archiving is not implemented")
        return archive

    def test_verified_run_in_another_schema_does_not_block_a_pending_attempt_in_this_schema(self) -> None:
        api = self.require_archive()
        relative = "migrations/APP_DEV/2026-11-19_seed-r001"
        folder = add_folder(self.root, relative)
        add_folder(self.root, "migrations/APP_STAGE/2026-11-19_seed-r001")
        migration = api.load_migration(self.root, relative)
        receipt = verified_receipt(folder, migration, schema="APP_DEV")
        (folder / "status.dev.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8", newline="\n")
        add_pending_attempt_event(folder, relative, migration)
        other = self.root / "scratch" / "migration-attempt-other-schema"
        other.mkdir(parents=True)
        other_target = {"environment": "dev", **IDENTITY, "current_schema": "APP_STAGE"}
        (other / "run-manifest.json").write_text(json.dumps({
            "schemaVersion": 2,
            "state": "verified",
            "target": other_target,
            "migrations": [{"folder": "2026-11-19_seed-r001", "writeAttempted": True, "state": "verified"}],
        }) + "\n", encoding="utf-8", newline="\n")

        self.assertTrue(api._attempt_matches_receipt_target(
            self.root, folder, "dev", receipt["target"], receipt["verifiedAt"],
        ))


class ConcurrentIndexChangeTests(unittest.TestCase):
    """An index changed by another archive run after validation must not be overwritten."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()
        self.relative = "migrations/APP_DEV/2026-10-01_concurrent-family-r001"
        self.folder = add_folder(self.root, self.relative)
        self.destination = self.root / "migrations-archive/APP_DEV" / self.folder.name
        self.index = self.root / "migrations/APP_DEV/.archived.json"
        self.other_run_index = (
            b'{"archived": [{"folder": "2026-09-01_other-r001", "family": "other", "revision": 1}], "schemaVersion": 1}\n'
        )

    def require_archive(self):
        self.assertIsNotNone(archive, "migration archiving is not implemented")
        return archive

    def assert_other_run_index_kept_and_folder_retained(self, error: str) -> None:
        # The existing rollback keeps a moved folder in the archive when the index is not in its planned state
        # (it is listed for recovery) rather than moving it back; the other run's index bytes stay untouched.
        self.assertIn("archived folders retained for recovery", error)
        self.assertFalse(self.folder.exists(), "the folder is retained in the archive, not moved back")
        self.assertTrue(self.destination.is_dir(), "the archived folder must still be at its destination")
        self.assertEqual(self.index.read_bytes(), self.other_run_index, "the other run's index bytes must be untouched")

    def test_index_changed_after_validation_aborts_before_the_write(self) -> None:
        api = self.require_archive()
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_bytes(b'{"archived": [], "schemaVersion": 1}\n')
        real_replace = os.replace
        changed = []

        def move_then_let_another_run_finish(source, destination):
            real_replace(source, destination)
            if not changed and Path(destination) == self.destination:
                changed.append(True)
                self.index.write_bytes(self.other_run_index)

        with patch.object(api.os, "replace", new=move_then_let_another_run_finish):
            with self.assertRaises(api.MigrationArchiveError) as caught:
                api.archive_migrations(self.root, [self.relative], yes=True)

        self.assertTrue(changed, "the other run's index change was not reached")
        self.assertIn("archived index changed while archiving; rerun the archive", str(caught.exception))
        self.assert_other_run_index_kept_and_folder_retained(str(caught.exception))

    def test_index_created_after_validation_is_not_overwritten(self) -> None:
        api = self.require_archive()
        self.index.parent.mkdir(parents=True, exist_ok=True)
        real_replace = os.replace
        changed = []

        def move_then_create_index(source, destination):
            real_replace(source, destination)
            if not changed and Path(destination) == self.destination:
                changed.append(True)
                self.index.write_bytes(self.other_run_index)

        with patch.object(api.os, "replace", new=move_then_create_index):
            with self.assertRaises(api.MigrationArchiveError) as caught:
                api.archive_migrations(self.root, [self.relative], yes=True)

        self.assertTrue(changed, "the other run's index creation was not reached")
        self.assertIn("archived index changed while archiving; rerun the archive", str(caught.exception))
        self.assert_other_run_index_kept_and_folder_retained(str(caught.exception))


if __name__ == "__main__":
    unittest.main()
