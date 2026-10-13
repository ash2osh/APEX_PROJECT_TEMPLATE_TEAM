import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.migration_attempts import (
    MigrationAttemptError,
    read_attempt_events,
    reduce_attempt_events,
    validate_no_write_evidence,
    write_attempt_event,
)
from scripts.migration_revision import inspect_migration_lock


def make_event(folder: Path, *, event_id=None, attempt_id=None, predecessor=None,
               state="pending", write_attempted=True, evidence=None):
    migration_path = folder.relative_to(folder.parents[1]).as_posix()
    return {
        "schemaVersion": 1,
        "eventId": str(event_id or uuid.uuid4()),
        "attemptId": str(attempt_id or uuid.uuid4()),
        "runId": "migration-attempt-test-run",
        "predecessorEventId": str(predecessor) if predecessor else None,
        "recordedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "migrationPath": migration_path,
        "environment": "dev",
        "target": {
            "environment": "dev",
            "connection": "docker-demo2",
            "expected_user": "DEMO",
            "current_schema": "DEMO",
        },
        "identity": None,
        "files": [{"name": "001-create-table.sql", "sequence": 1, "sha256": "a" * 64}],
        "checksSha256": "b" * 64,
        "payloadDigest": "c" * 64,
        "state": state,
        "writeAttempted": write_attempted,
        "evidence": evidence or [],
    }


class MigrationAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.folder = self.repo / "migrations" / "2026-10-12_sample-r001"
        self.folder.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_pending_event_is_durable_and_verified_event_stays_locked(self):
        pending = make_event(self.folder)
        path = write_attempt_event(self.folder, pending)
        original_bytes = path.read_bytes()

        self.assertEqual(read_attempt_events(self.folder), (pending,))
        self.assertEqual(reduce_attempt_events([pending]).status, "locked")
        with self.assertRaises(MigrationAttemptError):
            write_attempt_event(self.folder, {**pending, "state": "verified"})
        self.assertEqual(path.read_bytes(), original_bytes)

        terminal = make_event(
            self.folder,
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
            state="verified",
            write_attempted=True,
        )
        self.assertEqual(reduce_attempt_events([terminal, pending]).status, "locked")

    def test_durable_pending_event_keeps_migration_locked_without_scratch_copy(self):
        pending = make_event(self.folder)
        write_attempt_event(self.folder, pending)

        report = inspect_migration_lock(
            self.repo,
            f"migrations/{self.folder.name}",
        )

        self.assertEqual(report.status, "locked")
        self.assertIn("attempt-event:pending:dev", " ".join(report.states))

    def test_apply_not_started_unlocks_only_with_retained_matching_no_write_evidence(self):
        pending = make_event(self.folder)
        output = b"ORA-01017: invalid username/password; logon denied\n"
        evidence_path = self.repo / "scratch" / "sqlcl-output.txt"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_bytes(output)
        terminal = make_event(
            self.folder,
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
            state="apply-not-started",
            write_attempted=False,
            evidence=[{
                "kind": "no-write-sqlcl-output",
                "path": "scratch/sqlcl-output.txt",
                "sha256": hashlib.sha256(output).hexdigest(),
                "classification": "connection-failed-before-payload",
            }],
        )

        self.assertEqual(reduce_attempt_events([pending, terminal]).status, "unlocked")
        self.assertTrue(validate_no_write_evidence(self.repo, terminal))
        evidence_path.unlink()
        self.assertFalse(validate_no_write_evidence(self.repo, terminal))

    def test_branched_or_source_changed_event_chain_refuses_reduction(self):
        pending = make_event(self.folder)
        left = make_event(self.folder, attempt_id=pending["attemptId"], predecessor=pending["eventId"], state="verified")
        right = make_event(self.folder, attempt_id=pending["attemptId"], predecessor=pending["eventId"], state="verified")
        with self.assertRaises(MigrationAttemptError):
            reduce_attempt_events([pending, left, right])

        changed = make_event(
            self.folder,
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
            state="verified",
        )
        changed["payloadDigest"] = "d" * 64
        with self.assertRaises(MigrationAttemptError):
            reduce_attempt_events([pending, changed])


if __name__ == "__main__":
    unittest.main()
