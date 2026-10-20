import hashlib
import json
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from scripts.migration_revision import inspect_migration_lock


class MigrationAttemptEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.folder = self.repo / "migrations" / "2026-10-12_sample-r001"
        self.folder.mkdir(parents=True)
        (self.folder / "001-create-table.sql").write_text(
            "CREATE TABLE T (ID NUMBER);\n", encoding="utf-8", newline="\n"
        )
        (self.folder / "checks.json").write_text(
            '{"schemaVersion":1,"preconditions":[],"postconditions":'
            '[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
            encoding="utf-8",
            newline="\n",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def make_event(
        self,
        *,
        state="pending",
        attempt_id=None,
        predecessor=None,
        write_attempted=True,
        evidence=None,
    ):
        return {
            "schemaVersion": 1,
            "eventId": str(uuid.uuid4()),
            "attemptId": str(attempt_id or uuid.uuid4()),
            "runId": "migration-attempt-test-run",
            "predecessorEventId": predecessor,
            "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "migrationPath": self.folder.relative_to(self.repo).as_posix(),
            "environment": "dev",
            "target": {
                "environment": "dev",
                "connection": "fake-dev",
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

    def require_attempts(self):
        try:
            from scripts import migration_attempts
        except ImportError as error:
            self.fail(f"durable migration attempt reader/writer is not implemented: {error}")
        return migration_attempts

    def install_event(self, event):
        path = self.folder / f"attempt-event.{event['eventId']}.json"
        path.write_text(
            json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    def test_pending_event_survives_missing_scratch(self):
        pending = self.make_event()
        self.install_event(pending)
        scratch_attempt = self.repo / "scratch" / "migration-attempt-legacy"
        scratch_attempt.mkdir(parents=True)
        (scratch_attempt / "run-manifest.json").write_text(
            json.dumps({
                "schemaVersion": 1,
                "state": "frozen",
                "target": {"environment": "dev", "current_schema": "DEMO"},
                "migrations": [{"folder": self.folder.name, "writeAttempted": False, "state": "frozen"}],
            }) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(self.repo / "scratch")

        report = inspect_migration_lock(self.repo, f"migrations/{self.folder.name}")

        self.assertEqual(report.status, "locked")
        self.assertIn("attempt-event:pending:dev", " ".join(report.states))

        terminal = self.make_event(
            state="verified",
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
        )
        self.install_event(terminal)
        report = inspect_migration_lock(self.repo, f"migrations/{self.folder.name}")
        self.assertEqual(report.status, "locked")
        self.assertIn("attempt-event:verified:dev", " ".join(report.states))

    def test_legacy_scratch_attempt_remains_locked(self):
        attempt = self.repo / "scratch" / "migration-attempt-legacy"
        attempt.mkdir(parents=True)
        (attempt / "run-manifest.json").write_text(
            json.dumps({
                "schemaVersion": 1,
                "state": "apply-failed-or-unknown",
                "target": {"environment": "dev", "current_schema": "DEMO"},
                "migrations": [{"folder": self.folder.name, "writeAttempted": True, "state": "apply-failed-or-unknown"}],
            }) + "\n",
            encoding="utf-8",
        )

        report = inspect_migration_lock(self.repo, f"migrations/{self.folder.name}")

        self.assertEqual(report.status, "locked")
        self.assertIn("attempt:apply-failed-or-unknown:dev", " ".join(report.states))

    def test_no_write_evidence_must_match_digest(self):
        output = b"ORA-12154: TNS could not resolve the connect identifier\n"
        evidence_path = self.repo / "scratch" / "sqlcl-output.log"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_bytes(output)
        pending = self.make_event()
        self.install_event(pending)
        terminal = self.make_event(
            state="apply-not-started",
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
            write_attempted=False,
            evidence=[{
                "kind": "no-write-sqlcl-output",
                "path": "scratch/sqlcl-output.log",
                "sha256": hashlib.sha256(output).hexdigest(),
                "classification": "connection-failed-before-payload",
            }],
        )
        self.install_event(terminal)

        retained = inspect_migration_lock(self.repo, f"migrations/{self.folder.name}")
        self.assertEqual(retained.status, "unlocked")
        evidence_path.unlink()

        missing = inspect_migration_lock(self.repo, f"migrations/{self.folder.name}")
        self.assertEqual(missing.status, "unknown")
        self.assertIn("no-write-evidence-unavailable", " ".join(missing.states))

    def test_unknown_attempt_never_becomes_unlocked(self):
        event_id = str(uuid.uuid4())
        (self.folder / f"attempt-event.{event_id}.json").write_text("{}\n", encoding="utf-8")

        report = inspect_migration_lock(self.repo, f"migrations/{self.folder.name}")

        self.assertEqual(report.status, "unknown")
        self.assertIn("attempt-events-invalid", " ".join(report.states))

    def test_writer_persists_and_reads_validated_event_without_replacement(self):
        api = self.require_attempts()
        pending = self.make_event()

        path = api.write_attempt_event(self.folder, pending)

        self.assertEqual(api.read_attempt_events(self.folder), (pending,))
        original = path.read_bytes()
        with self.assertRaises(api.MigrationAttemptError):
            api.write_attempt_event(self.folder, pending)
        self.assertEqual(path.read_bytes(), original)

    def test_event_reducer_rejects_branched_or_source_changed_chains(self):
        api = self.require_attempts()
        pending = self.make_event()
        first = self.make_event(
            state="verified",
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
        )
        second = self.make_event(
            state="verified",
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
        )

        with self.assertRaises(api.MigrationAttemptError):
            api.reduce_attempt_events((pending, first, second))

        changed = self.make_event(
            state="verified",
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
        )
        changed["payloadDigest"] = "d" * 64
        with self.assertRaises(api.MigrationAttemptError):
            api.reduce_attempt_events((pending, changed))

    def test_no_write_event_reducer_requires_matching_retained_sqlcl_output(self):
        api = self.require_attempts()
        output = b"ORA-01017: invalid username/password; logon denied\n"
        evidence_path = self.repo / "scratch" / "apply-output.log"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_bytes(output)
        pending = self.make_event()
        terminal = self.make_event(
            state="apply-not-started",
            attempt_id=pending["attemptId"],
            predecessor=pending["eventId"],
            write_attempted=False,
            evidence=[{
                "kind": "no-write-sqlcl-output",
                "path": "scratch/apply-output.log",
                "sha256": hashlib.sha256(output).hexdigest(),
                "classification": "connection-failed-before-payload",
            }],
        )

        self.assertEqual(api.reduce_attempt_events((pending, terminal)).status, "unlocked")
        self.assertTrue(api.validate_no_write_evidence(self.repo, terminal))
        evidence_path.unlink()
        self.assertFalse(api.validate_no_write_evidence(self.repo, terminal))

    def test_pre_payload_refusal_evidence_unlocks_only_while_retained_and_matching(self):
        cases = (
            (
                "identity refusal",
                "MIGRATION_IDENTITY_VERIFIED\n"
                "ORA-20987: MIGRATION_IDENTITY_GUARD_REFUSED: target differs\n",
                "identity-guard-refused",
            ),
            (
                "connection refusal",
                "ORA-12154: TNS could not resolve the connect identifier\n",
                "connection-failed-before-payload",
            ),
            (
                "SQLcl startup refusal",
                "unable to start SQLcl: executable was not found\n",
                "sqlcl-start-failed",
            ),
        )
        for index, (label, text, classification) in enumerate(cases, start=1):
            with self.subTest(reason=label):
                folder = self.repo / "migrations" / f"2026-10-12_sample-{index:02}-r001"
                folder.mkdir()
                (folder / "001-create-table.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8")
                (folder / "checks.json").write_text(
                    '{"schemaVersion":1,"preconditions":[],"postconditions":'
                    '[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
                    encoding="utf-8",
                )
                output = text.encode("utf-8")
                output_path = self.repo / "scratch" / f"sqlcl-output-{index}.log"
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(output)
                attempt_id = str(uuid.uuid4())
                root = self.make_event()
                root.update({"attemptId": attempt_id, "migrationPath": folder.relative_to(self.repo).as_posix()})
                (folder / f"attempt-event.{root['eventId']}.json").write_text(json.dumps(root) + "\n", encoding="utf-8")
                terminal = self.make_event(
                    state="apply-not-started",
                    attempt_id=attempt_id,
                    predecessor=root["eventId"],
                    write_attempted=False,
                    evidence=[{
                        "kind": "no-write-sqlcl-output",
                        "path": f"scratch/{output_path.name}",
                        "sha256": hashlib.sha256(output).hexdigest(),
                        "classification": classification,
                    }],
                )
                terminal["migrationPath"] = folder.relative_to(self.repo).as_posix()
                (folder / f"attempt-event.{terminal['eventId']}.json").write_text(json.dumps(terminal) + "\n", encoding="utf-8")

                valid = inspect_migration_lock(self.repo, f"migrations/{folder.name}")
                self.assertEqual(valid.status, "unlocked")
                output_path.write_bytes(output + b"tampered\n")
                invalid = inspect_migration_lock(self.repo, f"migrations/{folder.name}")
                self.assertEqual(invalid.status, "unknown")


if __name__ == "__main__":
    unittest.main()
