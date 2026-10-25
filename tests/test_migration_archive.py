import json
import contextlib
import io
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from scripts import migration_archive as archive
except ImportError:
    archive = None


CHECKS = b'{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n'


def add_folder(root: Path, relative: str) -> Path:
    folder = root / relative
    folder.mkdir(parents=True)
    (folder / "001-create-table.sql").write_bytes(b"CREATE TABLE T (ID NUMBER);\n")
    (folder / "checks.json").write_bytes(CHECKS)
    return folder


def add_attempt(root: Path, folder: Path, *, schema: str = "APP_DEV") -> Path:
    attempt = root / "scratch" / "migration-attempt-archive-test"
    attempt.mkdir(parents=True)
    manifest = {
        "schemaVersion": 1,
        "state": "write-attempted",
        "target": {"environment": "dev", "current_schema": schema},
        "migrations": [{"folder": folder.name, "writeAttempted": True, "state": "write-attempted"}],
    }
    (attempt / "run-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return attempt


def verified_receipt(folder: Path, migration, *, schema: str = "APP_DEV") -> dict:
    return {
        "schemaVersion": 1,
        "state": "verified",
        "environment": "dev",
        "migration": folder.name,
        "createdDate": migration.date,
        "family": migration.family,
        "revision": migration.revision,
        "files": [{"name": item.name, "sequence": item.sequence, "sha256": item.sha256} for item in migration.files],
        "checksSha256": migration.checks_sha256,
        "payloadDigest": migration.payload_digest,
        "target": {"environment": "dev", "current_schema": schema},
        "applyStartedAt": "2026-10-01T10:00:00+00:00",
        "applyCompletedAt": "2026-10-01T10:01:00+00:00",
        "verifiedAt": "2026-10-01T10:02:00+00:00",
        "checks": [{"id": "ready", "passed": True}],
        "verifier": "test",
        "normalization": "test",
    }


def add_attempt_event(folder: Path, relative: str, migration) -> Path:
    event_id = str(uuid.uuid4())
    event = {
        "schemaVersion": 1,
        "eventId": event_id,
        "attemptId": str(uuid.uuid4()),
        "runId": "migration-attempt-archive-test",
        "predecessorEventId": None,
        "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "migrationPath": relative,
        "environment": "dev",
        "target": {"environment": "dev", "connection": "fake-dev", "expected_user": "APP_DEV", "current_schema": "APP_DEV"},
        "identity": None,
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


class MigrationArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def require_archive(self):
        self.assertIsNotNone(archive, "migration archiving is not implemented")
        return archive

    def test_default_run_prints_a_dry_run_plan_without_moving_files(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")

        plan = api.archive_migrations(self.root, ["migrations/APP_DEV/2026-10-01_archive-family-r001"])

        self.assertTrue(folder.is_dir())
        self.assertFalse((self.root / "migrations-archive").exists())
        self.assertEqual(plan.entries[0].family, "archive-family")
        self.assertEqual(plan.entries[0].revision, 1)
        self.assertEqual(plan.entries[0].evidence, ("no-attempt-or-receipt",))
        self.assertEqual(
            plan.entries[0].relative_destination,
            "migrations-archive/APP_DEV/2026-10-01_archive-family-r001",
        )

    def test_cli_prints_the_default_dry_run_plan_and_returns_success(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = api.main(
                ["--repo-root", str(self.root), "migrations/APP_DEV/2026-10-01_archive-family-r001"]
            )

        self.assertEqual(status, 0)
        self.assertIn("dry run", output.getvalue())
        self.assertIn("archive-family r001", output.getvalue())
        self.assertIn("no-attempt-or-receipt", output.getvalue())
        self.assertIn("migrations-archive/APP_DEV/2026-10-01_archive-family-r001", output.getvalue())
        self.assertTrue(folder.is_dir())

    def test_yes_moves_folder_bytes_and_writes_the_schema_archive_index(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        migration = api.load_migration(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        receipt = verified_receipt(folder, migration)
        (folder / "status.dev.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        event = add_attempt_event(folder, "migrations/APP_DEV/2026-10-01_archive-family-r001", migration)
        event_bytes = event.read_bytes()
        before = {path.name: path.read_bytes() for path in folder.iterdir()}

        plan = api.archive_migrations(
            self.root,
            ["migrations/APP_DEV/2026-10-01_archive-family-r001"],
            reason="released",
            yes=True,
        )

        moved = self.root / plan.entries[0].relative_destination
        self.assertFalse(folder.exists())
        self.assertEqual({path.name: path.read_bytes() for path in moved.iterdir()}, before)
        self.assertEqual((moved / event.name).read_bytes(), event_bytes)
        index = json.loads((self.root / "migrations/APP_DEV/.archived.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schemaVersion"], 1)
        self.assertEqual(len(index["archived"]), 1)
        record = index["archived"][0]
        self.assertEqual(record["folder"], folder.name)
        self.assertEqual(record["family"], "archive-family")
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["payloadDigest"], plan.entries[0].payload_digest)
        self.assertEqual(record["reason"], "released")
        self.assertEqual(record["destination"], plan.entries[0].relative_destination)
        self.assertRegex(record["archivedAt"], r"^\d{4}-\d{2}-\d{2}$")

    def test_custom_destination_is_used_and_the_index_extends_on_later_archives(self) -> None:
        api = self.require_archive()
        first = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        second = add_folder(self.root, "migrations/APP_DEV/2026-10-02_archive-family-r002")

        first_plan = api.archive_migrations(
            self.root,
            ["migrations/APP_DEV/2026-10-01_archive-family-r001"],
            destination="release-history",
            yes=True,
        )
        second_plan = api.archive_migrations(
            self.root,
            ["migrations/APP_DEV/2026-10-02_archive-family-r002"],
            destination="release-history",
            yes=True,
        )

        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(first_plan.entries[0].relative_destination, "release-history/APP_DEV/2026-10-01_archive-family-r001")
        self.assertEqual(second_plan.entries[0].relative_destination, "release-history/APP_DEV/2026-10-02_archive-family-r002")
        index = json.loads((self.root / "migrations/APP_DEV/.archived.json").read_text(encoding="utf-8"))
        self.assertEqual([record["revision"] for record in index["archived"]], [1, 2])

    def test_attempt_without_any_verified_receipt_is_refused_as_unknown(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        add_attempt(self.root, folder)

        with self.assertRaisesRegex(api.MigrationArchiveError, "attempt record.*no verified receipt.*unknown"):
            api.archive_migrations(self.root, ["migrations/APP_DEV/2026-10-01_archive-family-r001"])

        self.assertTrue(folder.is_dir())

    def test_allow_unverified_attempts_archives_a_failed_attempt_and_records_it(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        add_attempt(self.root, folder)

        plan = api.archive_migrations(
            self.root,
            ["migrations/APP_DEV/2026-10-01_archive-family-r001"],
            allow_unverified_attempts=True,
            yes=True,
        )

        self.assertIn("attempt-without-verified-receipt (accepted)", plan.entries[0].evidence)
        self.assertFalse(folder.exists())
        self.assertTrue((self.root / "migrations-archive/APP_DEV/2026-10-01_archive-family-r001").is_dir())

    def test_unverified_receipt_without_an_attempt_is_reported_and_preserved(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        (folder / "status.dev.json").write_bytes(b"unverified receipt bytes\n")

        plan = api.archive_migrations(self.root, ["migrations/APP_DEV/2026-10-01_archive-family-r001"])

        self.assertIn("status.dev.json (unverified)", plan.entries[0].evidence)
        self.assertTrue(folder.is_dir())

    def test_attempt_with_a_verified_receipt_can_be_archived(self) -> None:
        api = self.require_archive()
        folder = add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        add_attempt(self.root, folder)
        migration = api.load_migration(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")
        receipt = verified_receipt(folder, migration)
        (folder / "status.dev.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")

        result = api.archive_migrations(self.root, ["migrations/APP_DEV/2026-10-01_archive-family-r001"])

        self.assertTrue(any(item.startswith("status.dev.json") for item in result.entries[0].evidence))
        self.assertTrue(folder.is_dir())

    def test_only_the_contiguous_low_prefix_or_the_whole_family_can_be_archived(self) -> None:
        api = self.require_archive()
        names = [f"migrations/APP_DEV/2026-10-0{revision}_archive-family-r{revision:03d}" for revision in (1, 2, 3)]
        for name in names:
            add_folder(self.root, name)

        with self.assertRaisesRegex(api.MigrationArchiveError, "contiguous low prefix"):
            api.archive_migrations(self.root, [names[1]])
        with self.assertRaisesRegex(api.MigrationArchiveError, "contiguous low prefix"):
            api.archive_migrations(self.root, [names[0], names[2]])

        plan = api.archive_migrations(self.root, names)
        self.assertEqual([item.revision for item in plan.entries], [1, 2, 3])

    def test_duplicate_and_unknown_folders_are_refused(self) -> None:
        api = self.require_archive()
        name = "migrations/APP_DEV/2026-10-01_archive-family-r001"
        add_folder(self.root, name)

        with self.assertRaisesRegex(api.MigrationArchiveError, "selected more than once"):
            api.archive_migrations(self.root, [name, name])
        with self.assertRaisesRegex(api.MigrationArchiveError, "unknown migration folder"):
            api.archive_migrations(self.root, ["migrations/APP_DEV/2026-10-02_missing-family-r001"])

    def test_archive_index_unknown_keys_and_invalid_shapes_are_refused(self) -> None:
        api = self.require_archive()
        add_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r002")
        index = self.root / "migrations/APP_DEV/.archived.json"
        index.write_text('{"schemaVersion":1,"archived":[],"extra":true}\n', encoding="utf-8")

        with self.assertRaisesRegex(api.MigrationArchiveError, "archived index.*unknown key"):
            api.archive_migrations(self.root, ["migrations/APP_DEV/2026-10-01_archive-family-r002"])

    def test_all_and_keep_select_only_the_requested_schema_scope(self) -> None:
        api = self.require_archive()
        kept = add_folder(self.root, "migrations/APP_DEV/2026-10-01_first-family-r001")
        selected = add_folder(self.root, "migrations/APP_DEV/2026-10-01_second-family-r001")
        add_folder(self.root, "migrations/APP_STAGE/2026-10-01_third-family-r001")

        plan = api.archive_migrations(self.root, all_folders=True, keep=[kept.name], schema="APP_DEV")

        self.assertEqual([item.folder for item in plan.entries], [selected.resolve()])

    def test_multi_schema_selection_requires_schema_or_schema_named_paths(self) -> None:
        api = self.require_archive()
        one = add_folder(self.root, "migrations/APP_DEV/2026-10-01_first-family-r001")
        two = add_folder(self.root, "migrations/APP_STAGE/2026-10-01_second-family-r001")

        with self.assertRaisesRegex(api.MigrationArchiveError, "--schema or schema-qualified folder paths"):
            api.archive_migrations(self.root, [one.name, two.name])
        with self.assertRaisesRegex(api.MigrationArchiveError, "--schema"):
            api.archive_migrations(self.root, all_folders=True)

        explicit = api.archive_migrations(
            self.root,
            ["migrations/APP_DEV/2026-10-01_first-family-r001", "migrations/APP_STAGE/2026-10-01_second-family-r001"],
        )
        self.assertEqual(len(explicit.entries), 2)

    def test_destination_inside_migrations_and_existing_target_are_refused(self) -> None:
        api = self.require_archive()
        name = "migrations/APP_DEV/2026-10-01_archive-family-r001"
        add_folder(self.root, name)

        with self.assertRaisesRegex(api.MigrationArchiveError, "destination must be outside migrations/"):
            api.archive_migrations(self.root, [name], destination="migrations/archive")

        target = self.root / "migrations-archive" / "APP_DEV" / "2026-10-01_archive-family-r001"
        target.mkdir(parents=True)
        with self.assertRaisesRegex(api.MigrationArchiveError, "archive target already exists"):
            api.archive_migrations(self.root, [name])

    def test_flat_layout_writes_the_root_archived_index(self) -> None:
        api = self.require_archive()
        add_folder(self.root, "migrations/2026-10-01_archive-family-r001")

        plan = api.archive_migrations(
            self.root,
            ["migrations/2026-10-01_archive-family-r001"],
            schema="APP_DEV",
            yes=True,
        )

        self.assertTrue((self.root / "migrations/.archived.json").is_file())
        self.assertEqual(plan.entries[0].relative_destination, "migrations-archive/2026-10-01_archive-family-r001")


    def test_failed_index_restore_keeps_archived_folder_and_index_in_sync(self) -> None:
        api = self.require_archive()
        first = add_folder(self.root, "migrations/APP_DEV/2026-10-01_first-family-r001")
        second = add_folder(self.root, "migrations/APP_STAGE/2026-10-01_second-family-r001")
        first_index = self.root / "migrations/APP_DEV/.archived.json"
        second_index = self.root / "migrations/APP_STAGE/.archived.json"
        first_index.write_text('{"archived": [], "schemaVersion": 1}\n', encoding="utf-8")
        original_atomic_write = api._atomic_write
        first_index_writes = 0

        def fail_during_index_restore(path: Path, source: bytes) -> None:
            nonlocal first_index_writes
            if path == first_index:
                first_index_writes += 1
                if first_index_writes == 1:
                    original_atomic_write(path, source)
                    return
                raise OSError("could not restore first archive index")
            if path == second_index:
                raise OSError("second archive index write failed")
            original_atomic_write(path, source)

        with patch.object(api, "_atomic_write", side_effect=fail_during_index_restore):
            with self.assertRaises(api.MigrationArchiveError) as caught:
                api.archive_migrations(
                    self.root,
                    [
                        "migrations/APP_DEV/2026-10-01_first-family-r001",
                        "migrations/APP_STAGE/2026-10-01_second-family-r001",
                    ],
                    yes=True,
                )

        first_destination = self.root / "migrations-archive/APP_DEV" / first.name
        second_destination = self.root / "migrations-archive/APP_STAGE" / second.name
        first_records = json.loads(first_index.read_text(encoding="utf-8"))["archived"]
        self.assertEqual([record["folder"] for record in first_records], [first.name])
        self.assertFalse(first.exists(), "a folder still indexed as archived must not be rolled back")
        self.assertTrue(first_destination.is_dir())
        self.assertTrue(second.is_dir(), "the schema whose index failed before install should roll back")
        self.assertFalse(second_destination.exists())
        self.assertIn("rollback is incomplete", str(caught.exception))

    def test_failed_folder_rollback_installs_the_matching_archive_index(self) -> None:
        api = self.require_archive()
        first = add_folder(self.root, "migrations/APP_DEV/2026-10-01_first-family-r001")
        second = add_folder(self.root, "migrations/APP_STAGE/2026-10-01_second-family-r001")
        second_index = self.root / "migrations/APP_STAGE/.archived.json"
        second_destination = self.root / "migrations-archive/APP_STAGE" / second.name
        original_atomic_write = api._atomic_write
        original_replace = api.os.replace
        second_index_writes = 0

        def fail_first_second_index_write(path: Path, source: bytes) -> None:
            nonlocal second_index_writes
            if path == second_index:
                second_index_writes += 1
                if second_index_writes == 1:
                    raise OSError("second archive index write failed once")
            original_atomic_write(path, source)

        def fail_second_folder_rollback(source: Path, destination: Path) -> None:
            if Path(source) == second_destination and Path(destination) == second:
                raise OSError("second folder rollback failed")
            original_replace(source, destination)

        with patch.object(api, "_atomic_write", side_effect=fail_first_second_index_write):
            with patch.object(api.os, "replace", side_effect=fail_second_folder_rollback):
                with self.assertRaises(api.MigrationArchiveError) as caught:
                    api.archive_migrations(
                        self.root,
                        [
                            "migrations/APP_DEV/2026-10-01_first-family-r001",
                            "migrations/APP_STAGE/2026-10-01_second-family-r001",
                        ],
                        yes=True,
                    )

        self.assertTrue(first.is_dir())
        self.assertFalse((self.root / "migrations-archive/APP_DEV" / first.name).exists())
        self.assertFalse(second.exists())
        self.assertTrue(second_destination.is_dir())
        second_records = [] if not second_index.exists() else json.loads(second_index.read_text(encoding="utf-8"))["archived"]
        self.assertEqual([record["folder"] for record in second_records], [second.name])
        self.assertIn("rollback is incomplete", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
