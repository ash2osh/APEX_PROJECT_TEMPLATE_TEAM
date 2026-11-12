import hashlib
import json
import contextlib
import io
import shutil
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

    def test_short_yes_alias_moves_the_folder_from_the_command_line(self) -> None:
        api = self.require_archive()
        name = "migrations/APP_ONE/2026-10-02_archive-alias-r001"
        folder = add_folder(self.root, name)
        migration = api.load_migration(self.root, name)
        (folder / "status.dev.json").write_text(json.dumps(verified_receipt(folder, migration)) + "\n", encoding="utf-8")
        add_attempt_event(folder, name, migration)

        with contextlib.redirect_stdout(io.StringIO()):
            status = api.main([name, "-y"], repo_root=self.root)

        self.assertEqual(status, 0)
        self.assertFalse(folder.exists())

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


def write_sidecar(root: Path, folder: Path, migration, *, environment: str = "dev", outcome: str = "failed-no-target-effect",
                  run_id: str = "migration-attempt-archive-test", superseded_by: dict | None = None) -> dict:
    """A committed resolution sidecar with the folder's current digests and the pending attempt's identifiers."""
    evidence = root / "evidence" / "owner-check.txt"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_bytes(b"Owner check: sanitized output only.\n")
    pending = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("attempt-event.*.json"))
        if json.loads(path.read_text(encoding="utf-8"))["state"] == "pending"
    )
    record = {
        "schemaVersion": 1,
        "environment": environment,
        "outcome": outcome,
        "attemptId": pending["attemptId"],
        "runId": run_id,
        "payloadDigest": migration.payload_digest,
        "checksSha256": migration.checks_sha256,
        "target": {"environment": environment, "connection": "fake-dev", "expected_user": "APP_ONE", "current_schema": "APP_ONE"},
        "evidence": {"path": "evidence/owner-check.txt", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()},
        "recordedAt": "2026-11-11T10:00:00Z",
    }
    if superseded_by is not None:
        record["supersededBy"] = superseded_by
    (folder / f"resolution.{environment}.json").write_text(json.dumps(record) + "\n", encoding="utf-8", newline="\n")
    return record


class ResolvedArchiveTests(unittest.TestCase):
    """Phase 4 (D6): resolved folders archive without a flag; unresolved ones only with --preserve-unresolved."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def require_archive(self):
        self.assertIsNotNone(archive, "migration archiving is not implemented")
        return archive

    def call(self, api, *args, **kwargs):
        try:
            return api.archive_migrations(self.root, *args, **kwargs)
        except TypeError as error:
            self.fail(f"archive option is not implemented yet: {error}")
        except api.MigrationArchiveError as error:
            self.fail(f"archive refused: {error}")

    def pending_folder(self, name: str, api) -> Path:
        folder = add_folder(self.root, f"migrations/APP_ONE/{name}")
        add_attempt_event(folder, f"migrations/APP_ONE/{name}", api.load_migration(self.root, f"migrations/APP_ONE/{name}"))
        return folder

    def index_records(self) -> list[dict]:
        return json.loads((self.root / "migrations" / "APP_ONE" / ".archived.json").read_text(encoding="utf-8"))["archived"]

    def test_a_resolved_folder_archives_without_a_flag_and_its_plan_names_the_outcome(self) -> None:
        api = self.require_archive()
        folder = self.pending_folder("2026-10-01_archive-family-r001", api)
        write_sidecar(self.root, folder, api.load_migration(self.root, "migrations/APP_ONE/2026-10-01_archive-family-r001"))

        plan = self.call(api, ["migrations/APP_ONE/2026-10-01_archive-family-r001"], yes=True)

        self.assertIn("resolved:failed-no-target-effect:dev", plan.entries[0].evidence)
        self.assertFalse(folder.exists())
        self.assertIn("resolved:failed-no-target-effect:dev", self.index_records()[0].get("evidence", []))

    def test_preserve_unresolved_archives_an_attempt_with_no_receipt_and_records_it_as_unresolved(self) -> None:
        api = self.require_archive()
        folder = self.pending_folder("2026-10-01_archive-family-r001", api)

        plan = self.call(api, ["migrations/APP_ONE/2026-10-01_archive-family-r001"], preserve_unresolved=True, yes=True)

        self.assertIn("unresolved (preserved)", plan.entries[0].evidence)
        self.assertNotIn("attempt-without-verified-receipt (accepted)", plan.entries[0].evidence)
        self.assertFalse(folder.exists())
        self.assertIn("unresolved (preserved)", self.index_records()[0].get("evidence", []))

    def test_the_word_accepted_appears_only_with_allow_unverified_attempts(self) -> None:
        api = self.require_archive()
        self.pending_folder("2026-10-01_archive-family-r001", api)
        selected = ["migrations/APP_ONE/2026-10-01_archive-family-r001"]

        preserved = self.call(api, selected, preserve_unresolved=True)
        allowed = api.archive_migrations(self.root, selected, allow_unverified_attempts=True)

        self.assertFalse(any("accepted" in item for item in preserved.entries[0].evidence))
        self.assertIn("attempt-without-verified-receipt (accepted)", allowed.entries[0].evidence)

    def test_all_reports_every_refusal_with_its_command_and_moves_nothing(self) -> None:
        api = self.require_archive()
        first = self.pending_folder("2026-10-01_archive-family-r001", api)
        second = self.pending_folder("2026-10-02_archive-family-r002", api)

        with self.assertRaises(api.MigrationArchiveError) as raised:
            api.archive_migrations(self.root, all_folders=True, yes=True)

        message = str(raised.exception)
        self.assertIn("2026-10-01_archive-family-r001", message)
        self.assertIn("2026-10-02_archive-family-r002", message)
        self.assertIn("scripts/team.sh reconcile-migration", message)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())

    def test_skip_refused_archives_the_eligible_low_prefix_and_lists_the_rest(self) -> None:
        api = self.require_archive()
        resolved = self.pending_folder("2026-10-01_archive-family-r001", api)
        write_sidecar(self.root, resolved, api.load_migration(self.root, "migrations/APP_ONE/2026-10-01_archive-family-r001"))
        verified = add_folder(self.root, "migrations/APP_ONE/2026-10-02_archive-family-r002")
        verified_migration = api.load_migration(self.root, "migrations/APP_ONE/2026-10-02_archive-family-r002")
        (verified / "status.dev.json").write_text(json.dumps(verified_receipt(verified, verified_migration)) + "\n", encoding="utf-8")
        refused = self.pending_folder("2026-10-03_archive-family-r003", api)

        plan = self.call(api, all_folders=True, skip_refused=True, yes=True)

        self.assertFalse(resolved.exists())
        self.assertFalse(verified.exists())
        self.assertTrue(refused.is_dir())
        self.assertTrue(any("2026-10-03_archive-family-r003" in item for item in getattr(plan, "skipped", ())))

    def test_a_prefix_ending_in_a_superseded_revision_archives(self) -> None:
        api = self.require_archive()
        superseded = self.pending_folder("2026-10-01_archive-family-r001", api)
        later = add_folder(self.root, "migrations/APP_ONE/2026-10-02_archive-family-r002")
        later_migration = api.load_migration(self.root, "migrations/APP_ONE/2026-10-02_archive-family-r002")
        (later / "status.dev.json").write_text(json.dumps(verified_receipt(later, later_migration)) + "\n", encoding="utf-8")
        write_sidecar(
            self.root, superseded, api.load_migration(self.root, "migrations/APP_ONE/2026-10-01_archive-family-r001"),
            outcome="superseded",
            superseded_by={
                "folder": "2026-10-02_archive-family-r002",
                "receiptSha256": hashlib.sha256((later / "status.dev.json").read_bytes()).hexdigest(),
            },
        )

        self.call(api, ["migrations/APP_ONE/2026-10-01_archive-family-r001"], yes=True)

        self.assertFalse(superseded.exists())
        self.assertTrue(later.is_dir())

    def test_an_index_record_without_evidence_loads_and_older_records_are_kept_byte_for_byte(self) -> None:
        api = self.require_archive()
        first = add_folder(self.root, "migrations/APP_ONE/2026-10-01_archive-family-r001")
        first_migration = api.load_migration(self.root, "migrations/APP_ONE/2026-10-01_archive-family-r001")
        (first / "status.dev.json").write_text(json.dumps(verified_receipt(first, first_migration)) + "\n", encoding="utf-8")
        api.archive_migrations(self.root, ["migrations/APP_ONE/2026-10-01_archive-family-r001"], yes=True)
        before = self.index_records()
        second = add_folder(self.root, "migrations/APP_ONE/2026-10-02_archive-family-r002")
        second_migration = api.load_migration(self.root, "migrations/APP_ONE/2026-10-02_archive-family-r002")
        (second / "status.dev.json").write_text(json.dumps(verified_receipt(second, second_migration)) + "\n", encoding="utf-8")

        api.archive_migrations(self.root, ["migrations/APP_ONE/2026-10-02_archive-family-r002"], yes=True)

        after = self.index_records()
        self.assertEqual(after[0], before[0])
        self.assertEqual([record["folder"] for record in after], ["2026-10-01_archive-family-r001", "2026-10-02_archive-family-r002"])

    def test_the_command_line_offers_preserve_unresolved_and_skip_refused(self) -> None:
        api = self.require_archive()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            api.main(["--help"])
        for flag in ("--preserve-unresolved", "--skip-refused"):
            with self.subTest(flag=flag):
                self.assertIn(flag, output.getvalue())


if __name__ == "__main__":
    unittest.main()


class NoWriteAndUnknownArchiveTests(unittest.TestCase):
    """A proven no-write attempt needs no resolution; --preserve-unresolved also covers an unknown lock."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "migration_attempts_v1"
    NOT_STARTED = "migrations/APP_ONE/2026-10-14_archive-family-r004"
    PENDING = "migrations/APP_ONE/2026-10-12_archive-family-r002"
    VERIFIED = "migrations/APP_ONE/2026-10-13_archive-family-r003"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(self.FIXTURE / "repo", self.root)

    def keep_no_write_log(self) -> None:
        (self.root / "scratch").mkdir()
        shutil.copyfile(self.FIXTURE / "no-write-sqlcl-output.log", self.root / "scratch" / "no-write-sqlcl-output.log")

    def test_an_attempt_that_provably_never_wrote_archives_without_a_flag(self) -> None:
        self.keep_no_write_log()

        plan = archive.archive_migrations(self.root, [self.PENDING, self.VERIFIED, self.NOT_STARTED], preserve_unresolved=True)

        by_folder = {entry.folder.name: entry.evidence for entry in plan.entries}
        not_started = by_folder["2026-10-14_archive-family-r004"]
        self.assertIn("apply-not-started (no write)", not_started)
        self.assertNotIn("unresolved (preserved)", not_started)
        self.assertIn("unresolved (preserved)", by_folder["2026-10-12_archive-family-r002"])

    def test_a_no_write_attempt_whose_log_is_gone_is_unknown_and_still_refused_without_a_flag(self) -> None:
        with self.assertRaises(archive.MigrationArchiveError) as refused:
            archive.archive_migrations(self.root, [self.PENDING, self.VERIFIED, self.NOT_STARTED], preserve_unresolved=False, skip_refused=False)
        self.assertIn("2026-10-14_archive-family-r004", str(refused.exception))
        # Its attempt is closed, so the refusal must not send the owner to a command that would refuse it.
        r004 = next(line for line in str(refused.exception).splitlines() if "2026-10-14_archive-family-r004" in line)
        self.assertNotIn("reconcile-migration", r004)
        self.assertIn("--preserve-unresolved", r004)

    def test_preserve_unresolved_archives_an_unknown_lock_without_calling_it_accepted(self) -> None:
        plan = archive.archive_migrations(self.root, [self.PENDING, self.VERIFIED, self.NOT_STARTED], preserve_unresolved=True)

        not_started = {entry.folder.name: entry.evidence for entry in plan.entries}["2026-10-14_archive-family-r004"]
        self.assertIn("unresolved (preserved)", not_started)
        self.assertFalse(any("accepted" in item for item in not_started))
