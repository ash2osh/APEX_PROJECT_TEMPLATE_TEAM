import json
import io
import shutil
import subprocess
import tempfile
import unittest
import contextlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fake_sqlcl import BASH
from scripts.migration_attempts import write_attempt_event
from scripts.migration_revision import inspect_migration_lock, main, not_started_reason, revise_folder


class MigrationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def add_folder(self, relative, *, readme=b"Original migration notes.\n", status=False):
        folder = self.root / relative
        folder.mkdir(parents=True)
        (folder / "001-create-table.sql").write_bytes(b"CREATE TABLE T (ID NUMBER);\n")
        (folder / "002-add-index.sql").write_bytes(b"CREATE INDEX T_I ON T(ID);\n")
        (folder / "checks.json").write_bytes(
            b'{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n'
        )
        (folder / "README.md").write_bytes(readme)
        if status:
            (folder / "status.dev.json").write_text('{"schemaVersion":1,"state":"verified"}\n', encoding="utf-8")
        return folder

    def write_archive_prefix(self, parent, family, revisions):
        records = [
            {
                "folder": f"2026-10-{revision:02d}_{family}-r{revision:03d}",
                "family": family,
                "revision": revision,
                "payloadDigest": f"{revision:064x}",
                "archivedAt": "2026-10-20",
                "reason": "released",
                "destination": f"migrations-archive/APP_DEV/2026-10-{revision:02d}_{family}-r{revision:03d}",
            }
            for revision in revisions
        ]
        (parent / ".archived.json").write_text(
            json.dumps({"schemaVersion": 1, "archived": records}) + "\n", encoding="utf-8"
        )

    def make_attempt_event(
        self, folder, *, environment, schema, attempt_id=None, predecessor=None,
        state="pending", write_attempted=True, evidence=None,
    ):
        return {
            "schemaVersion": 1,
            "eventId": str(uuid.uuid4()),
            "attemptId": str(attempt_id or uuid.uuid4()),
            "runId": "migration-attempt-review-test",
            "predecessorEventId": str(predecessor) if predecessor else None,
            "recordedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "migrationPath": folder.relative_to(self.root).as_posix(),
            "environment": environment,
            "target": {
                "environment": environment,
                "connection": f"{environment}-profile",
                "expected_user": schema,
                "current_schema": schema,
            },
            "identity": None,
            "files": [{"name": "001-create-table.sql", "sequence": 1, "sha256": "a" * 64}],
            "checksSha256": "b" * 64,
            "payloadDigest": "c" * 64,
            "state": state,
            "writeAttempted": write_attempted,
            "evidence": evidence or [],
        }

    def add_invalid_no_write_event(self, folder, *, environment, schema):
        attempt_id = str(uuid.uuid4())
        pending = self.make_attempt_event(
            folder, environment=environment, schema=schema, attempt_id=attempt_id,
        )
        terminal = self.make_attempt_event(
            folder, environment=environment, schema=schema, attempt_id=attempt_id,
            predecessor=pending["eventId"], state="apply-not-started", write_attempted=False,
            evidence=[{
                "kind": "no-write-sqlcl-output",
                "path": "scratch/missing-no-write-output.txt",
                "sha256": "d" * 64,
                "classification": "connection-failed-before-payload",
            }],
        )
        write_attempt_event(folder, pending)
        write_attempt_event(folder, terminal)

    def add_branched_attempt_events(self, folder, *, environment, schema):
        attempt_id = str(uuid.uuid4())
        pending = self.make_attempt_event(
            folder, environment=environment, schema=schema, attempt_id=attempt_id,
        )
        first_terminal = self.make_attempt_event(
            folder, environment=environment, schema=schema, attempt_id=attempt_id,
            predecessor=pending["eventId"], state="verified",
        )
        second_terminal = self.make_attempt_event(
            folder, environment=environment, schema=schema, attempt_id=attempt_id,
            predecessor=pending["eventId"], state="verified",
        )
        for event in (pending, first_terminal, second_terminal):
            write_attempt_event(folder, event)

    def test_revise_creates_sequential_revisions_and_preserves_old_folder_bytes(self):
        old = self.add_folder("migrations/2026-10-01_create-things-r001", status=True)
        old_bytes = {item.name: item.read_bytes() for item in old.iterdir()}

        second = revise_folder(self.root, "migrations/2026-10-01_create-things-r001", "correct generated DDL")
        third = revise_folder(self.root, "migrations/2026-10-01_create-things-r002", "fix follow-up")

        self.assertEqual(second.folder, (self.root / "migrations/2026-10-01_create-things-r002").resolve())
        self.assertEqual(third.folder, (self.root / "migrations/2026-10-01_create-things-r003").resolve())
        self.assertEqual(sorted(item.name for item in second.folder.iterdir()), ["001-create-table.sql", "002-add-index.sql", "README.md", "checks.json"])
        self.assertEqual((second.folder / "001-create-table.sql").read_bytes(), old_bytes["001-create-table.sql"])
        self.assertEqual((second.folder / "002-add-index.sql").read_bytes(), old_bytes["002-add-index.sql"])
        self.assertEqual((second.folder / "checks.json").read_bytes(), old_bytes["checks.json"])
        self.assertEqual((second.folder / "README.md").read_bytes(), b"Supersedes 2026-10-01_create-things-r001: correct generated DDL\n" + old_bytes["README.md"])
        self.assertFalse((second.folder / "status.dev.json").exists())
        self.assertEqual({item.name: item.read_bytes() for item in old.iterdir()}, old_bytes)
        self.assertIn("r001", second.order_hint)
        self.assertIn("r002", second.order_hint)

    def test_revise_defaults_reason_and_understands_schema_layout(self):
        old = self.add_folder("migrations/DEMO/2026-10-01_create-things-r001", readme=b"Notes without final newline")

        result = revise_folder(self.root, "migrations/DEMO/2026-10-01_create-things-r001")

        self.assertEqual(result.folder.parent, old.parent.resolve())
        self.assertEqual(
            (result.folder / "README.md").read_bytes(),
            b"Supersedes 2026-10-01_create-things-r001: follow-up revision\nNotes without final newline",
        )

    def test_revise_uses_existing_crlf_style_for_the_prepended_header(self):
        self.add_folder("migrations/DEMO/2026-10-01_create-things-r001", readme=b"First line\r\nSecond line\r\n")

        result = revise_folder(self.root, "migrations/DEMO/2026-10-01_create-things-r001")

        self.assertEqual(
            (result.folder / "README.md").read_bytes(),
            b"Supersedes 2026-10-01_create-things-r001: follow-up revision\r\n"
            b"First line\r\nSecond line\r\n",
        )

    def test_revise_continues_after_an_archived_prefix(self):
        parent = self.root / "migrations" / "APP_DEV"
        parent.mkdir()
        self.write_archive_prefix(parent, "archive-family", range(1, 5))
        self.add_folder("migrations/APP_DEV/2026-10-05_archive-family-r005")

        result = revise_folder(self.root, "migrations/APP_DEV/2026-10-05_archive-family-r005")

        self.assertEqual(result.folder.name, "2026-10-05_archive-family-r006")

    def test_revise_refuses_a_family_whose_revisions_are_all_archived(self):
        parent = self.root / "migrations" / "APP_DEV"
        parent.mkdir()
        self.write_archive_prefix(parent, "archive-family", [1])

        with self.assertRaisesRegex(ValueError, "unarchive an archived folder or start a new family"):
            revise_folder(self.root, "migrations/APP_DEV/2026-10-01_archive-family-r001")

    def test_revise_refuses_an_existing_newer_revision_and_a_name_without_revision_suffix(self):
        self.add_folder("migrations/2026-10-01_create-things-r001")
        revise_folder(self.root, "migrations/2026-10-01_create-things-r001", "first revision")
        with self.assertRaisesRegex(ValueError, "newer revision"):
            revise_folder(self.root, "migrations/2026-10-01_create-things-r001", "double revise")

        self.add_folder("migrations/2026-10-02_other-things-r001")
        self.add_folder("migrations/2026-10-03_other-things-r003")
        with self.assertRaisesRegex(ValueError, "newer revision"):
            revise_folder(self.root, "migrations/2026-10-02_other-things-r001", "gap")

        self.add_folder("migrations/invalid-name")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            revise_folder(self.root, "migrations/invalid-name")

    def test_team_shell_exposes_revise_and_check(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in (
            "team.sh",
            "migration_revision.py",
            "migration_attempts.py",
            "migration_manifest.py",
            "path_utils.py",
            "validate_migration.py",
        ):
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / name, scripts / name)
        folder = self.add_folder("migrations/2026-10-01_create-things-r001")

        created = subprocess.run(
            [BASH, str(scripts / "team.sh"), "revise", "migrations/2026-10-01_create-things-r001", "--reason", "fix DDL"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        self.assertIn("Created revision: migrations/2026-10-01_create-things-r002", created.stdout)
        self.assertIn("Migration order hint:", created.stdout)
        self.assertTrue((folder.parent / "2026-10-01_create-things-r002" / "README.md").is_file())

        checked = subprocess.run(
            [BASH, str(scripts / "team.sh"), "revise", "--check", "migrations/2026-10-01_create-things-r001"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("UNLOCKED", checked.stdout)

    def test_attempt_and_receipt_states_report_locked_or_unlocked(self):
        folder = self.add_folder("migrations/2026-10-01_create-things-r001")
        attempt_dir = self.root / "scratch" / "migration-attempt-test"
        attempt_dir.mkdir(parents=True)
        manifest_path = attempt_dir / "run-manifest.json"
        base = {
            "schemaVersion": 1,
            "state": "write-attempted",
            "target": {"environment": "dev", "current_schema": "DEMO"},
            "migrations": [{"folder": folder.name, "writeAttempted": True, "state": "write-attempted"}],
        }

        cases = (
            ("frozen", False, "unlocked"),
            ("frozen", True, "locked"),
            ("apply-not-started", False, "unlocked"),
            ("apply-not-started", True, "locked"),
            ("write-attempted", True, "locked"),
            ("apply-failed-or-unknown", True, "locked"),
            ("committed-unverified", True, "locked"),
            ("committed-verification-failed", True, "locked"),
            ("committed-receipt-failed", True, "locked"),
            ("committed-source-or-payload-changed", True, "locked"),
            ("verified", True, "locked"),
        )
        for state, attempted, expected in cases:
            with self.subTest(state=state):
                base["migrations"][0]["state"] = state
                base["migrations"][0]["writeAttempted"] = attempted
                manifest_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
                result = inspect_migration_lock(self.root, "migrations/2026-10-01_create-things-r001")
                self.assertEqual(result.status, expected)
                self.assertIn(state, " ".join(result.states))

        manifest_path.unlink()
        (folder / "status.dev.json").write_text('{"schemaVersion":1,"state":"verified","environment":"dev"}\n', encoding="utf-8")
        result = inspect_migration_lock(self.root, "migrations/2026-10-01_create-things-r001")
        self.assertEqual(result.status, "locked")
        self.assertIn("receipt:verified:dev", result.states)

    def test_check_prints_the_recorded_state_and_returns_a_script_friendly_status(self):
        folder = self.add_folder("migrations/2026-10-01_create-things-r001")
        attempt_dir = self.root / "scratch" / "migration-attempt-test"
        attempt_dir.mkdir(parents=True)
        manifest = {
            "schemaVersion": 1,
            "state": "apply-not-started",
            "target": {"environment": "dev", "current_schema": "DEMO"},
            "migrations": [{"folder": folder.name, "writeAttempted": False, "state": "apply-not-started"}],
        }
        (attempt_dir / "run-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(["--check", "migrations/2026-10-01_create-things-r001"], repo_root=self.root)

        self.assertEqual(status, 0)
        self.assertIn("UNLOCKED", output.getvalue())
        self.assertIn("attempt:apply-not-started:dev", output.getvalue())

        manifest["migrations"][0].update({"writeAttempted": True, "state": "apply-failed-or-unknown"})
        (attempt_dir / "run-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["--check", "migrations/2026-10-01_create-things-r001"], repo_root=self.root)

        self.assertEqual(status, 1)
        self.assertIn("LOCKED", output.getvalue())
        self.assertIn("attempt:apply-failed-or-unknown:dev", output.getvalue())

    def test_check_reports_invalid_retained_evidence_as_unknown(self):
        self.add_folder("migrations/2026-10-01_create-things-r001")
        evidence = self.root / "scratch" / "migration-attempt-test"
        evidence.mkdir(parents=True)
        (evidence / "run-manifest.json").write_text("{bad json\n", encoding="utf-8")

        result = inspect_migration_lock(self.root, "migrations/2026-10-01_create-things-r001")

        self.assertEqual(result.status, "unknown")
        self.assertIn("unreadable", " ".join(result.states))

    def test_selected_lock_scopes_receipts_and_attempts_but_default_remains_global(self):
        folder = self.add_folder("migrations/2026-10-14_scoped-lock-r001")
        (folder / "status.staging.json").write_text(json.dumps({
            "schemaVersion": 1,
            "state": "verified",
            "environment": "staging",
            "target": {"current_schema": "APP_STAGING"},
        }) + "\n", encoding="utf-8")
        dev_dir = self.root / "scratch" / "migration-attempt-dev"
        staging_dir = self.root / "scratch" / "migration-attempt-staging"
        dev_dir.mkdir(parents=True)
        staging_dir.mkdir(parents=True)
        for directory, environment, schema, attempted, state in (
            (dev_dir, "dev", "APP_DEV", False, "apply-not-started"),
            (staging_dir, "staging", "APP_STAGING", True, "write-attempted"),
        ):
            manifest = {
                "schemaVersion": 1,
                "state": state,
                "target": {"environment": environment, "current_schema": schema},
                "migrations": [{"folder": folder.name, "writeAttempted": attempted, "state": state}],
            }
            (directory / "run-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        global_lock = inspect_migration_lock(self.root, f"migrations/{folder.name}")
        selected_lock = inspect_migration_lock(
            self.root, f"migrations/{folder.name}", environment="staging", schema="APP_STAGING",
        )
        selected_dev = inspect_migration_lock(
            self.root, f"migrations/{folder.name}", environment="dev", schema="APP_DEV",
        )

        self.assertEqual("locked", global_lock.status)
        self.assertIn("receipt:verified:staging", global_lock.states)
        self.assertIn("attempt:apply-not-started:dev", global_lock.states)
        self.assertIn("attempt:write-attempted:staging", global_lock.states)
        self.assertEqual("locked", selected_lock.status)
        self.assertEqual(("receipt:verified:staging", "attempt:write-attempted:staging"), selected_lock.states)
        self.assertEqual("unlocked", selected_dev.status)
        self.assertEqual(("attempt:apply-not-started:dev",), selected_dev.states)

    def test_selected_lock_ignores_unrelated_invalid_no_write_evidence_but_global_stays_fail_closed(self):
        folder = self.add_folder("migrations/2026-10-14_unrelated-event-evidence-r001")
        self.add_invalid_no_write_event(folder, environment="dev", schema="APP_DEV")

        selected = inspect_migration_lock(
            self.root, f"migrations/{folder.name}", environment="staging", schema="APP_STAGE",
        )
        global_lock = inspect_migration_lock(self.root, f"migrations/{folder.name}")

        self.assertEqual("unlocked", selected.status, selected)
        self.assertEqual("unknown", global_lock.status, global_lock)
        self.assertTrue(any("no-write-evidence-unavailable" in state for state in global_lock.states))

    def test_selected_lock_still_rejects_invalid_no_write_evidence_for_selected_target(self):
        folder = self.add_folder("migrations/2026-10-14_selected-event-evidence-r001")
        self.add_invalid_no_write_event(folder, environment="staging", schema="APP_STAGE")

        selected = inspect_migration_lock(
            self.root, f"migrations/{folder.name}", environment="staging", schema="APP_STAGE",
        )

        self.assertEqual("unknown", selected.status, selected)
        self.assertTrue(any("no-write-evidence-unavailable" in state for state in selected.states))

    def test_selected_lock_ignores_unrelated_malformed_chain_but_global_stays_fail_closed(self):
        folder = self.add_folder("migrations/2026-10-14_unrelated-malformed-chain-r001")
        self.add_branched_attempt_events(folder, environment="dev", schema="APP_DEV")

        selected = inspect_migration_lock(
            self.root, f"migrations/{folder.name}", environment="staging", schema="APP_STAGE",
        )
        global_lock = inspect_migration_lock(self.root, f"migrations/{folder.name}")

        self.assertEqual("unlocked", selected.status, selected)
        self.assertEqual("unknown", global_lock.status, global_lock)
        self.assertTrue(any("attempt-events-invalid" in state for state in global_lock.states))

    def test_selected_lock_still_rejects_malformed_chain_for_selected_target(self):
        folder = self.add_folder("migrations/2026-10-14_selected-malformed-chain-r001")
        self.add_branched_attempt_events(folder, environment="staging", schema="APP_STAGE")

        selected = inspect_migration_lock(
            self.root, f"migrations/{folder.name}", environment="staging", schema="APP_STAGE",
        )

        self.assertEqual("unknown", selected.status, selected)
        self.assertTrue(any("attempt-events-invalid" in state for state in selected.states))

    def test_check_reports_attempt_directory_without_manifest_as_unknown(self):
        self.add_folder("migrations/2026-10-01_create-things-r001")
        (self.root / "scratch" / "migration-attempt-incomplete").mkdir(parents=True)

        result = inspect_migration_lock(self.root, "migrations/2026-10-01_create-things-r001")

        self.assertEqual(result.status, "unknown")
        self.assertIn("attempt-manifest-missing", " ".join(result.states))

    def test_check_uses_exit_two_for_unknown_evidence(self):
        self.add_folder("migrations/2026-10-01_create-things-r001")
        (self.root / "scratch" / "migration-attempt-incomplete").mkdir(parents=True)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(["--check", "migrations/2026-10-01_create-things-r001"], repo_root=self.root)

        self.assertEqual(status, 2)
        self.assertIn("UNKNOWN", output.getvalue())


class ApplyNotStartedClassificationTests(unittest.TestCase):
    def test_identity_guard_refusal_is_classified_as_not_started(self):
        output = "MIGRATION_IDENTITY_VERIFIED\nORA-20987: MIGRATION_IDENTITY_GUARD_REFUSED: target differs\n"
        self.assertEqual(not_started_reason(output), "identity-guard-refused")

    def test_connection_error_after_identity_marker_before_payload_is_not_started(self):
        output = "MIGRATION_IDENTITY_VERIFIED\nORA-12537: connection closed\n"
        self.assertEqual(not_started_reason(output), "connection-failed-before-payload")

    def test_access_guard_code_after_identity_marker_is_not_trusted_as_pre_payload(self):
        output = "MIGRATION_IDENTITY_VERIFIED\nORA-20984: session identity changed\n"
        self.assertIsNone(not_started_reason(output))

    def test_connection_failure_before_identity_or_payload_is_not_started(self):
        self.assertEqual(not_started_reason("ORA-12154: TNS could not resolve the connect identifier\n"), "connection-failed-before-payload")

    def test_failure_after_payload_marker_stays_locked_even_with_an_ora_error(self):
        output = "MIGRATION_IDENTITY_VERIFIED\nMIGRATION_PAYLOAD_STARTED:001-create-table.sql\nORA-00942: missing table\n"
        self.assertIsNone(not_started_reason(output))

    def test_cutoff_without_an_ora_error_stays_locked(self):
        output = "MIGRATION_IDENTITY_VERIFIED\nMIGRATION_PAYLOAD_STARTED:001-create-large.sql\n"
        self.assertIsNone(not_started_reason(output))

    def test_missing_or_ambiguous_output_stays_locked(self):
        self.assertIsNone(not_started_reason(""))
        self.assertIsNone(not_started_reason("MIGRATION_IDENTITY_VERIFIED\nMIGRATION_APPLY_COMPLETED\n"))


if __name__ == "__main__":
    unittest.main()
