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


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "migration_attempts_v1"
FIXTURE_FOLDERS = {
    "pending": "migrations/APP_ONE/2026-10-12_archive-family-r002",
    "verified": "migrations/APP_ONE/2026-10-13_archive-family-r003",
    "not-started": "migrations/APP_ONE/2026-10-14_archive-family-r004",
}
RESOLVED_STATES = ("failed-no-target-effect", "failed-target-effect-unknown", "superseded")
OWNER_EVIDENCE_PATH = "evidence/owner-check.txt"
PENDING_FOLDER = "migrations/APP_ONE/2026-10-12_archive-family-r002"
CHECKS_SOURCE = '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n'


def owner_evidence() -> dict:
    return {"kind": "owner-resolution", "path": OWNER_EVIDENCE_PATH, "sha256": "e" * 64}


class MigrationAttemptV1FixtureTests(unittest.TestCase):
    """Fixtures generated from the schema-1 formats before this change; they must keep loading unchanged."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(FIXTURE_ROOT / "repo", self.repo)
        (self.repo / "scratch").mkdir()
        # scratch/ is git-ignored, so the no-write SQLcl output is copied in for each test.
        shutil.copyfile(FIXTURE_ROOT / "no-write-sqlcl-output.log", self.repo / "scratch" / "no-write-sqlcl-output.log")

    def test_v1_events_receipt_index_and_no_write_evidence_load_unchanged(self):
        from scripts import migration_attempts, migration_manifest

        expected = {
            "pending": ("locked", ("attempt-event:pending:dev",)),
            "verified": ("locked", ("attempt-event:verified:dev",)),
            "not-started": ("unlocked", ("attempt-event:apply-not-started:dev",)),
        }
        for key, (status, states) in expected.items():
            with self.subTest(fixture=key):
                state = migration_attempts.reduce_attempt_events(
                    migration_attempts.read_attempt_events(self.repo / FIXTURE_FOLDERS[key])
                )
                self.assertEqual(state.status, status)
                self.assertEqual(state.states, states)

        folder = self.repo / FIXTURE_FOLDERS["not-started"]
        terminal = next(event for event in migration_attempts.read_attempt_events(folder) if event["state"] == "apply-not-started")
        self.assertTrue(migration_attempts.validate_no_write_evidence(self.repo, terminal))

        verified_folder = self.repo / FIXTURE_FOLDERS["verified"]
        migration = migration_manifest.load_migration(self.repo, FIXTURE_FOLDERS["verified"])
        receipt = json.loads((verified_folder / "status.dev.json").read_text(encoding="utf-8"))
        migration_manifest.validate_receipt(verified_folder / "status.dev.json", migration, receipt["target"])

        index = migration_manifest.load_archived_index(self.repo / "migrations" / "APP_ONE" / ".archived.json", "APP_ONE")
        self.assertEqual([record["folder"] for record in index], ["2026-10-05_archive-family-r001"])
        self.assertNotIn("evidence", index[0])
        self.assertTrue((self.repo / "migrations-archive" / "APP_ONE" / "2026-10-05_archive-family-r001" / "status.dev.json").is_file())


class OwnerResolutionAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.folder = self.repo / "migrations" / "APP_ONE" / "2026-10-12_archive-family-r002"
        self.folder.mkdir(parents=True)
        (self.folder / "001-create-table.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        (self.folder / "checks.json").write_text(CHECKS_SOURCE, encoding="utf-8", newline="\n")

    def require_attempts(self):
        try:
            from scripts import migration_attempts
        except ImportError as error:
            self.fail(f"migration attempts module is unavailable: {error}")
        return migration_attempts

    def make_event(self, *, state="pending", attempt_id=None, predecessor=None, write_attempted=None, evidence=None):
        if write_attempted is None:
            write_attempted = state != "apply-not-started"
        return {
            "schemaVersion": 1,
            "eventId": str(uuid.uuid4()),
            "attemptId": str(attempt_id or uuid.uuid4()),
            "runId": "migration-attempt-test-run",
            "predecessorEventId": predecessor,
            "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "migrationPath": PENDING_FOLDER,
            "environment": "dev",
            "target": {
                "environment": "dev",
                "connection": "fake-dev",
                "expected_user": "APP_ONE",
                "current_schema": "APP_ONE",
            },
            "identity": None,
            "files": [{"name": "001-create-table.sql", "sequence": 1, "sha256": "a" * 64}],
            "checksSha256": "b" * 64,
            "payloadDigest": "c" * 64,
            "state": state,
            "writeAttempted": write_attempted,
            "evidence": evidence or [],
        }

    def accepts(self, api, event):
        try:
            api.validate_attempt_event(event, self.folder)
        except api.MigrationAttemptError:
            return False
        return True

    def test_owner_resolution_states_validate_as_terminal_events(self):
        api = self.require_attempts()
        for state in RESOLVED_STATES:
            with self.subTest(state=state):
                pending = self.make_event()
                terminal = self.make_event(
                    state=state, attempt_id=pending["attemptId"], predecessor=pending["eventId"], evidence=[owner_evidence()],
                )
                self.assertTrue(self.accepts(api, terminal), f"{state} with one owner-resolution item is refused")
                reduced = api.reduce_attempt_events((pending, terminal))
                self.assertEqual(reduced.status, "locked")
                self.assertEqual(reduced.states, (f"attempt-event:{state}:dev",))

    def test_owner_resolution_needs_exactly_one_safe_owner_item(self):
        api = self.require_attempts()
        pending = self.make_event()
        good = owner_evidence()
        refused = {
            "no evidence": [],
            "two items": [good, dict(good)],
            "wrong kind": [dict(good, kind="no-write-sqlcl-output")],
            "extra key": [dict(good, classification="connection-failed-before-payload")],
            "migrations path": [dict(good, path=f"{PENDING_FOLDER}/notes.txt")],
            "scratch path": [dict(good, path="scratch/notes.txt")],
            "parent path": [dict(good, path="../notes.txt")],
            "absolute path": [dict(good, path="/etc/notes.txt")],
            "drive path": [dict(good, path="C:/notes.txt")],
            "doubled separator": [dict(good, path="evidence//notes.txt")],
            "backslash path": [dict(good, path="evidence\\notes.txt")],
            "uppercase digest": [dict(good, sha256="E" * 64)],
        }
        for state in RESOLVED_STATES:
            with self.subTest(state=state, case="positive"):
                self.assertTrue(self.accepts(api, self.make_event(
                    state=state, attempt_id=pending["attemptId"], predecessor=pending["eventId"], evidence=[good],
                )))
            for label, evidence in refused.items():
                with self.subTest(state=state, case=label):
                    event = self.make_event(
                        state=state, attempt_id=pending["attemptId"], predecessor=pending["eventId"], evidence=evidence,
                    )
                    self.assertFalse(self.accepts(api, event))

    def test_owner_resolution_states_keep_write_attempted_true(self):
        api = self.require_attempts()
        for state in RESOLVED_STATES:
            with self.subTest(state=state):
                pending = self.make_event()
                self.assertTrue(self.accepts(api, self.make_event(
                    state=state, attempt_id=pending["attemptId"], predecessor=pending["eventId"],
                    write_attempted=True, evidence=[owner_evidence()],
                )))
                self.assertFalse(self.accepts(api, self.make_event(
                    state=state, attempt_id=pending["attemptId"], predecessor=pending["eventId"],
                    write_attempted=False, evidence=[owner_evidence()],
                )))

    def test_a_resolution_closes_the_chain_and_nothing_may_follow_it(self):
        api = self.require_attempts()
        pending = self.make_event()
        resolved = self.make_event(
            state="failed-no-target-effect", attempt_id=pending["attemptId"],
            predecessor=pending["eventId"], evidence=[owner_evidence()],
        )
        self.assertTrue(self.accepts(api, resolved))
        self.assertEqual(api.reduce_attempt_events((pending, resolved)).status, "locked")

        after = self.make_event(state="verified", attempt_id=pending["attemptId"], predecessor=resolved["eventId"])
        with self.assertRaises(api.MigrationAttemptError):
            api.reduce_attempt_events((pending, resolved, after))
        second = self.make_event(
            state="failed-target-effect-unknown", attempt_id=pending["attemptId"],
            predecessor=pending["eventId"], evidence=[owner_evidence()],
        )
        with self.assertRaises(api.MigrationAttemptError):
            api.reduce_attempt_events((pending, resolved, second))

    def test_reducer_reports_resolved_attempts_in_a_resolved_tuple_and_keeps_them_locked(self):
        api = self.require_attempts()
        pending = self.make_event()
        resolved = self.make_event(
            state="failed-no-target-effect", attempt_id=pending["attemptId"],
            predecessor=pending["eventId"], evidence=[owner_evidence()],
        )
        self.assertTrue(self.accepts(api, resolved))

        reduced = api.reduce_attempt_events((pending, resolved))

        self.assertEqual(reduced.status, "locked")
        self.assertEqual(reduced.states, ("attempt-event:failed-no-target-effect:dev",))
        self.assertEqual(reduced.resolved, ("attempt-event:failed-no-target-effect:dev",))
        self.assertEqual(api.reduce_attempt_events((pending,)).resolved, ())


if __name__ == "__main__":
    unittest.main()
