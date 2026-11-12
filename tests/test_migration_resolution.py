"""Owner resolution of a locked migration folder: sidecar record, evidence, supersession and the command.

scripts/migration_resolution.py does not exist until phase 2. Every test that needs it calls
`ResolutionTestCase.require()`, so a missing module fails that one test cleanly instead of
breaking the import of this file. Tests for the command (phase 3) and supersession (phase 3)
use `main()` and the same helpers.
"""

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import _test_subprocess_safety  # noqa: F401
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "migration_attempts_v1"
PENDING_FOLDER = "migrations/APP_ONE/2026-10-12_archive-family-r002"
VERIFIED_FOLDER = "migrations/APP_ONE/2026-10-13_archive-family-r003"
NOT_STARTED_FOLDER = "migrations/APP_ONE/2026-10-14_archive-family-r004"
EVIDENCE = "evidence/owner-check.txt"
EVIDENCE_BYTES = b"Owner check: target inspected read-only; sanitized output only.\n"
ACK_PREFIX = "RECONCILE-MIGRATION"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResolutionTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(FIXTURE / "repo", self.root)
        (self.root / "scratch").mkdir()
        shutil.copyfile(FIXTURE / "no-write-sqlcl-output.log", self.root / "scratch" / "no-write-sqlcl-output.log")
        (self.root / "evidence").mkdir()
        (self.root / "evidence" / "owner-check.txt").write_bytes(EVIDENCE_BYTES)

    def require(self):
        try:
            from scripts import migration_resolution
        except ImportError as error:
            self.fail(f"scripts/migration_resolution.py is not implemented yet: {error}")
        return migration_resolution

    def load(self, folder):
        try:
            from scripts.migration_manifest import load_migration
        except ImportError as error:
            self.fail(f"migration manifest is unavailable: {error}")
        try:
            return load_migration(self.root, folder)
        except Exception as error:  # the test reports the refusal, not an unhandled error
            self.fail(f"load_migration refused {folder}: {error}")

    def event(self, folder, state):
        for path in sorted((self.root / folder).glob("attempt-event.*.json")):
            event = json.loads(path.read_text(encoding="utf-8"))
            if event["state"] == state:
                return event
        self.fail(f"no {state} attempt event in {folder}")

    def record(self, folder, *, outcome="failed-no-target-effect", environment="dev", **overrides):
        migration = self.load(folder)
        pending = self.event(folder, "pending")
        record = {
            "schemaVersion": 1,
            "environment": environment,
            "outcome": outcome,
            "attemptId": pending["attemptId"],
            "runId": pending["runId"],
            "payloadDigest": migration.payload_digest,
            "checksSha256": migration.checks_sha256,
            "target": {
                "environment": environment,
                "connection": "fake-dev",
                "expected_user": "APP_ONE",
                "current_schema": "APP_ONE",
            },
            "evidence": {"path": EVIDENCE, "sha256": sha256(EVIDENCE_BYTES)},
            "recordedAt": "2026-11-11T10:00:00Z",
        }
        record.update(overrides)
        return record

    def write_raw(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def snapshot(self):
        """Every file under the repository except scratch/, as bytes keyed by relative path."""
        result = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.relative_to(self.root).parts[0] != "scratch":
                result[path.relative_to(self.root).as_posix()] = path.read_bytes()
        return result

    def reconcile(self, args, *, ack=None, ack_variable=True, input_effect=None, terminal=True):
        """Run the command in-process with the typed acknowledgment supplied through the test-only path.

        ``ack`` is a string, or a callable that receives the stdout printed so far (for a
        scratch-only attempt, whose id the command prints and the owner copies).
        """
        resolution = self.require()
        environ = {"TEAM_ACK_FROM_STDIN": "1"} if ack_variable else {}
        output, errors = io.StringIO(), io.StringIO()
        if input_effect is None:
            def input_effect(prompt=""):
                return ack(output.getvalue()) if callable(ack) else ack
        with patch.dict(os.environ, environ), contextlib.ExitStack() as stack:
            if not ack_variable:
                os.environ.pop("TEAM_ACK_FROM_STDIN", None)
            stack.enter_context(patch("scripts.terminal_confirm.stdin_is_terminal", return_value=terminal))
            stack.enter_context(patch("builtins.input", side_effect=input_effect))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            command = getattr(resolution, "main", None)
            if command is None:
                self.fail("the reconcile command is not implemented yet (phase 3)")
            try:
                status = command(list(args), repo_root=self.root)
            except SystemExit as error:
                status = error.code
        return status, output.getvalue(), errors.getvalue()

    def reconcile_args(self, folder=PENDING_FOLDER, outcome="failed-no-target-effect", extra=()):
        return [
            folder, "--env", "dev", "--outcome", outcome,
            "--evidence", str(self.root / EVIDENCE), *extra,
        ]

    def ack_for(self, folder=PENDING_FOLDER):
        return f"{ACK_PREFIX} {self.event(folder, 'pending')['attemptId']}"


class ResolutionRecordTests(ResolutionTestCase):
    """Phase 2: the sidecar schema, its binding to the folder, the atomic writer and the evidence rules."""

    def test_record_round_trips_through_the_atomic_writer_and_binds_to_the_folder(self):
        resolution = self.require()
        folder = self.root / PENDING_FOLDER
        record = self.record(PENDING_FOLDER)
        path = folder / "resolution.dev.json"

        resolution.write_resolution(path, record)

        self.assertEqual(resolution.read_resolution(path), record)
        self.assertEqual(resolution.validate_resolution(path, self.load(PENDING_FOLDER)), record)

    def test_writer_never_replaces_an_existing_record(self):
        resolution = self.require()
        path = self.root / PENDING_FOLDER / "resolution.dev.json"
        resolution.write_resolution(path, self.record(PENDING_FOLDER))
        original = path.read_bytes()

        with self.assertRaises(resolution.MigrationResolutionError):
            resolution.write_resolution(path, self.record(PENDING_FOLDER, outcome="failed-target-effect-unknown"))

        self.assertEqual(path.read_bytes(), original)

    def test_record_field_set_and_values_are_exact(self):
        resolution = self.require()
        folder = self.root / PENDING_FOLDER
        record = self.record(PENDING_FOLDER)
        cases = {
            "extra field": dict(record, note="free text"),
            "missing runId": {key: value for key, value in record.items() if key != "runId"},
            "string schemaVersion": dict(record, schemaVersion="1"),
            "unknown outcome": dict(record, outcome="unknown"),
            "attempt id not a UUID": dict(record, attemptId="not-a-uuid"),
            "run id outside the pattern": dict(record, runId="scratch/../x"),
            "non-UTC timestamp": dict(record, recordedAt="2026-11-11T10:00:00+02:00"),
            "future timestamp": dict(record, recordedAt="2999-01-01T00:00:00Z"),
            "target environment differs": dict(record, target=dict(record["target"], environment="staging")),
            "evidence extra key": dict(record, evidence=dict(record["evidence"], classification="x")),
            "evidence inside migrations": dict(record, evidence={"path": f"{PENDING_FOLDER}/notes.txt", "sha256": record["evidence"]["sha256"]}),
            "supersededBy on a failure": dict(record, supersededBy={"folder": "x", "receiptSha256": "0" * 64}),
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                path = folder / "resolution.dev.json"
                self.write_raw(path, bad)
                with self.assertRaises(resolution.MigrationResolutionError):
                    resolution.read_resolution(path)
                path.unlink()

    def test_superseded_needs_supersededBy_and_only_superseded_may_carry_it(self):
        resolution = self.require()
        path = self.root / PENDING_FOLDER / "resolution.dev.json"
        bare = self.record(PENDING_FOLDER, outcome="superseded")
        self.write_raw(path, bare)
        with self.assertRaises(resolution.MigrationResolutionError):
            resolution.read_resolution(path)

        named = dict(bare, supersededBy={"folder": "2026-10-13_archive-family-r003", "receiptSha256": "a" * 64})
        self.write_raw(path, named)
        self.assertEqual(resolution.read_resolution(path), named)

    def test_record_must_match_the_folder_digests_and_the_file_environment(self):
        resolution = self.require()
        folder = self.root / PENDING_FOLDER
        migration = self.load(PENDING_FOLDER)
        path = folder / "resolution.dev.json"
        cases = {
            "payload digest": dict(self.record(PENDING_FOLDER), payloadDigest="0" * 64),
            "checks digest": dict(self.record(PENDING_FOLDER), checksSha256="0" * 64),
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                self.write_raw(path, bad)
                with self.assertRaises(resolution.MigrationResolutionError):
                    resolution.validate_resolution(path, migration)
                path.unlink()

        staged_name = folder / "resolution.staging.json"
        self.write_raw(staged_name, self.record(PENDING_FOLDER))
        with self.assertRaises(resolution.MigrationResolutionError):
            resolution.validate_resolution(staged_name, migration)

    def test_a_folder_never_holds_a_receipt_and_a_resolution_for_one_environment(self):
        resolution = self.require()
        folder = self.root / PENDING_FOLDER
        path = folder / "resolution.dev.json"
        self.write_raw(path, self.record(PENDING_FOLDER))
        (folder / "status.dev.json").write_bytes(b"{}\n")

        with self.assertRaises(resolution.MigrationResolutionError):
            resolution.validate_resolution(path, self.load(PENDING_FOLDER))

    def test_evidence_must_be_a_regular_repository_file_outside_migrations_and_scratch(self):
        resolution = self.require()
        good = {"path": EVIDENCE, "sha256": sha256(EVIDENCE_BYTES)}
        resolution.validate_evidence_file(self.root, good)

        (self.root / PENDING_FOLDER / "owner.txt").write_bytes(b"inside migrations\n")
        (self.root / "scratch" / "owner.txt").write_bytes(b"inside scratch\n")
        (self.root / "outside.txt").write_bytes(b"outside the repository\n")
        cases = {
            "missing file": {"path": "evidence/missing.txt", "sha256": good["sha256"]},
            "digest mismatch": {"path": EVIDENCE, "sha256": "0" * 64},
            "inside migrations": {"path": f"{PENDING_FOLDER}/owner.txt", "sha256": sha256(b"inside migrations\n")},
            "inside scratch": {"path": "scratch/owner.txt", "sha256": sha256(b"inside scratch\n")},
            "parent escape": {"path": "../outside.txt", "sha256": sha256(b"outside the repository\n")},
            "absolute path": {"path": str(self.root / EVIDENCE), "sha256": good["sha256"]},
            "backslash path": {"path": "evidence\\owner-check.txt", "sha256": good["sha256"]},
        }
        for label, evidence in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(resolution.MigrationResolutionError):
                    resolution.validate_evidence_file(self.root, evidence)

        link = self.root / "evidence" / "linked.txt"
        try:
            link.symlink_to(self.root / EVIDENCE)
        except OSError:
            pass  # no symlink privilege on this platform; the other cases still run
        else:
            with self.subTest(case="symbolic link"):
                with self.assertRaises(resolution.MigrationResolutionError):
                    resolution.validate_evidence_file(self.root, {"path": "evidence/linked.txt", "sha256": good["sha256"]})

    def test_a_valid_sidecar_is_accepted_on_discovery_and_leaves_the_payload_digest_unchanged(self):
        before = self.load(PENDING_FOLDER).payload_digest
        self.write_raw(self.root / PENDING_FOLDER / "resolution.dev.json", self.record(PENDING_FOLDER))

        after = self.load(PENDING_FOLDER)

        self.assertEqual(after.payload_digest, before)

    def test_discovery_refuses_an_invalid_unknown_or_linked_sidecar(self):
        from scripts.migration_manifest import MigrationManifestError, load_migration

        self.load(PENDING_FOLDER)
        self.write_raw(self.root / PENDING_FOLDER / "resolution.dev.json", self.record(PENDING_FOLDER))
        self.load(PENDING_FOLDER)
        folder = self.root / PENDING_FOLDER
        (folder / "resolution.dev.json").unlink()
        cases = {
            "malformed record": ("resolution.dev.json", {"schemaVersion": 1}),
            "unknown environment": ("resolution.test.json", self.record(PENDING_FOLDER, environment="test")),
        }
        for label, (name, value) in cases.items():
            with self.subTest(case=label):
                self.write_raw(folder / name, value)
                with self.assertRaises(MigrationManifestError):
                    load_migration(self.root, PENDING_FOLDER)
                (folder / name).unlink()

        target = folder / "resolution.dev.json"
        try:
            target.symlink_to(self.root / EVIDENCE)
        except OSError:
            return
        with self.subTest(case="symbolic link"):
            with self.assertRaises(MigrationManifestError):
                load_migration(self.root, PENDING_FOLDER)


class ReconcileCommandTests(ResolutionTestCase):
    """Phase 3: the reconcile command. Its acknowledgment is typed by the owner; tests supply it through the test-only path."""

    def test_reconcile_writes_the_sidecar_and_one_closing_event_and_edits_nothing_else(self):
        before = self.snapshot()
        status, _out, errors = self.reconcile(self.reconcile_args(), ack=self.ack_for())

        self.assertEqual(status, 0, errors)
        folder = self.root / PENDING_FOLDER
        record = json.loads((folder / "resolution.dev.json").read_text(encoding="utf-8"))
        self.assertEqual(record["outcome"], "failed-no-target-effect")
        self.assertEqual(record["attemptId"], self.event(PENDING_FOLDER, "pending")["attemptId"])
        closing = self.event(PENDING_FOLDER, "failed-no-target-effect")
        self.assertEqual(closing["predecessorEventId"], self.event(PENDING_FOLDER, "pending")["eventId"])
        after = self.snapshot()
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(set(after) - set(before), {
            f"{PENDING_FOLDER}/resolution.dev.json",
            f"{PENDING_FOLDER}/attempt-event.{closing['eventId']}.json",
        })
        self.assertFalse((folder / "status.dev.json").exists())

    def test_a_wrong_acknowledgment_refuses_and_writes_nothing(self):
        before = self.snapshot()
        for ack in ("RECONCILE-MIGRATION 00000000-0000-4000-8000-000000000000", "reconcile-migration", "yes", ""):
            with self.subTest(ack=ack):
                status, _out, _errors = self.reconcile(self.reconcile_args(), ack=ack)
                self.assertEqual(status, 2)
                self.assertEqual(self.snapshot(), before)

    def test_redirected_acknowledgment_is_refused_without_the_test_variable_and_never_read(self):
        before = self.snapshot()

        def must_not_read(prompt=""):
            raise AssertionError("the acknowledgment must not be read from redirected input")

        status, _out, _errors = self.reconcile(
            self.reconcile_args(), ack_variable=False, terminal=False, input_effect=must_not_read,
        )

        self.assertEqual(status, 2)
        self.assertEqual(self.snapshot(), before)

    def test_missing_symbolic_or_changed_evidence_refuses_and_writes_nothing(self):
        before = self.snapshot()
        missing = self.reconcile_args(extra=())
        missing[missing.index("--evidence") + 1] = str(self.root / "evidence" / "missing.txt")
        status, _out, _errors = self.reconcile(missing, ack=self.ack_for(), input_effect=AssertionError)
        self.assertEqual(status, 2)
        self.assertEqual(self.snapshot(), before)

        def change_evidence_while_typing(prompt=""):
            (self.root / EVIDENCE).write_bytes(b"changed after review\n")
            return self.ack_for()

        status, _out, _errors = self.reconcile(self.reconcile_args(), input_effect=change_evidence_while_typing)
        self.assertEqual(status, 2)
        changed = {key: value for key, value in self.snapshot().items() if key != EVIDENCE}
        self.assertEqual(changed, {key: value for key, value in before.items() if key != EVIDENCE})
        self.assertFalse((self.root / PENDING_FOLDER / "resolution.dev.json").exists())

        (self.root / "evidence" / "owner-check.txt").write_bytes(EVIDENCE_BYTES)
        try:
            (self.root / "evidence" / "linked.txt").symlink_to(self.root / EVIDENCE)
        except OSError:
            return
        linked = self.reconcile_args()
        linked[linked.index("--evidence") + 1] = str(self.root / "evidence" / "linked.txt")
        status, _out, _errors = self.reconcile(linked, input_effect=AssertionError)
        self.assertEqual(status, 2)
        self.assertFalse((self.root / PENDING_FOLDER / "resolution.dev.json").exists())

    def test_a_target_with_a_verified_receipt_is_refused(self):
        before = self.snapshot()
        status, _out, errors = self.reconcile(
            self.reconcile_args(VERIFIED_FOLDER), ack=self.ack_for(VERIFIED_FOLDER), input_effect=AssertionError,
        )

        self.assertEqual(status, 2)
        self.assertIn("verified", errors)
        self.assertEqual(self.snapshot(), before)

    def test_a_non_pending_or_apply_not_started_attempt_is_refused(self):
        (self.root / VERIFIED_FOLDER / "status.dev.json").unlink()
        before = self.snapshot()
        status, _out, errors = self.reconcile(
            self.reconcile_args(VERIFIED_FOLDER), ack=self.ack_for(VERIFIED_FOLDER), input_effect=AssertionError,
        )
        self.assertEqual(status, 2)
        self.assertIn("pending", errors)

        status, _out, errors = self.reconcile(
            self.reconcile_args(NOT_STARTED_FOLDER), ack=self.ack_for(NOT_STARTED_FOLDER), input_effect=AssertionError,
        )
        self.assertEqual(status, 2)
        self.assertIn("apply-not-started", errors)
        self.assertEqual(self.snapshot(), before)

    def test_a_second_resolution_for_the_same_environment_is_refused(self):
        status, _out, errors = self.reconcile(self.reconcile_args(), ack=self.ack_for())
        self.assertEqual(status, 0, errors)
        first = (self.root / PENDING_FOLDER / "resolution.dev.json").read_bytes()
        before = self.snapshot()

        status, _out, errors = self.reconcile(
            self.reconcile_args(outcome="failed-target-effect-unknown"), ack=self.ack_for(), input_effect=AssertionError,
        )

        self.assertEqual(status, 2)
        self.assertIn("resolution", errors.lower())
        self.assertEqual((self.root / PENDING_FOLDER / "resolution.dev.json").read_bytes(), first)
        self.assertEqual(self.snapshot(), before)

    def test_folder_bytes_that_changed_since_the_attempt_refuse_the_resolution(self):
        sql = self.root / PENDING_FOLDER / "001-create-table.sql"
        sql.write_bytes(sql.read_bytes() + b"-- edited after the failed apply\n")
        before = self.snapshot()

        status, _out, _errors = self.reconcile(self.reconcile_args(), ack=self.ack_for(), input_effect=AssertionError)

        self.assertEqual(status, 2)
        self.assertEqual(self.snapshot(), before)

    def test_several_pending_attempts_need_an_explicit_attempt_id(self):
        folder = self.root / PENDING_FOLDER
        pending = self.event(PENDING_FOLDER, "pending")
        second = dict(pending, eventId="6f1c2a4e-9b3d-4c1e-8a7f-2d5b9e0c0b01", attemptId="6f1c2a4e-9b3d-4c1e-8a7f-2d5b9e0c0b02")
        self.write_raw(folder / f"attempt-event.{second['eventId']}.json", second)

        status, _out, errors = self.reconcile(self.reconcile_args(), ack=f"{ACK_PREFIX} {second['attemptId']}", input_effect=AssertionError)
        self.assertEqual(status, 2)
        self.assertIn(pending["attemptId"], errors)
        self.assertIn(second["attemptId"], errors)

        status, _out, errors = self.reconcile(
            self.reconcile_args(extra=("--attempt", second["attemptId"])), ack=f"{ACK_PREFIX} {second['attemptId']}",
        )
        self.assertEqual(status, 0, errors)
        self.assertEqual(json.loads((folder / "resolution.dev.json").read_text(encoding="utf-8"))["attemptId"], second["attemptId"])

    def test_a_retained_scratch_manifest_names_the_attempt_when_no_event_is_in_this_checkout(self):
        folder = self.root / PENDING_FOLDER
        before = self.snapshot()
        for path in folder.glob("attempt-event.*.json"):
            path.unlink()
        run = self.root / "scratch" / "migration-attempt-fixture-r002"
        run.mkdir()
        migration = self.load(PENDING_FOLDER)
        self.write_raw(run / "run-manifest.json", {
            "schemaVersion": 2,
            "state": "apply-failed-or-unknown",
            "target": {
                "environment": "dev", "connection": "fake-dev", "expected_user": "APP_ONE", "current_schema": "APP_ONE",
            },
            "migrations": [{
                "folder": folder.name,
                "payloadDigest": migration.payload_digest,
                "checksSha256": migration.checks_sha256,
                "attemptEventIds": [],
                "writeAttempted": True,
                "state": "apply-failed-or-unknown",
            }],
        })

        def acknowledge_printed_run(printed):
            # With no attempt event in this checkout the acknowledgment names the run id (D4).
            found = re.search(r"migration-attempt-[A-Za-z0-9_-]+", printed)
            return f"{ACK_PREFIX} {found.group(0)}" if found else ""

        status, _out, errors = self.reconcile(self.reconcile_args(), ack=acknowledge_printed_run)

        self.assertEqual(status, 0, errors)
        record = json.loads((folder / "resolution.dev.json").read_text(encoding="utf-8"))
        self.assertEqual(record["runId"], "migration-attempt-fixture-r002")
        self.assertIsNone(record["attemptId"])
        self.assertEqual(record["outcome"], "failed-no-target-effect")
        self.assertEqual(self.snapshot().keys() - before.keys(), {f"{PENDING_FOLDER}/resolution.dev.json"})

    def test_no_known_attempt_refuses_with_the_reason(self):
        folder = self.root / PENDING_FOLDER
        for path in folder.glob("attempt-event.*.json"):
            path.unlink()
        before = self.snapshot()

        status, _out, errors = self.reconcile(self.reconcile_args(), ack=f"{ACK_PREFIX} 6f1c2a4e-9b3d-4c1e-8a7f-2d5b9e0c0c02", input_effect=AssertionError)

        self.assertEqual(status, 2)
        self.assertIn("no pending attempt", errors)
        self.assertEqual(self.snapshot(), before)

    def test_bad_outcome_or_superseded_without_its_revision_is_refused(self):
        before = self.snapshot()
        status, _out, _errors = self.reconcile(self.reconcile_args(outcome="bogus"), ack=self.ack_for(), input_effect=AssertionError)
        self.assertEqual(status, 2)

        status, _out, errors = self.reconcile(
            self.reconcile_args(outcome="superseded"), ack=self.ack_for(), input_effect=AssertionError,
        )
        self.assertEqual(status, 2)
        self.assertIn("--superseded-by", errors)
        self.assertEqual(self.snapshot(), before)

    def test_reconcile_never_starts_a_process(self):
        def no_process(*_args, **_kwargs):
            raise AssertionError("reconcile must not start SQLcl or any other process")

        with patch.object(subprocess, "run", side_effect=no_process), patch.object(subprocess, "Popen", side_effect=no_process):
            status, _out, errors = self.reconcile(self.reconcile_args(), ack=self.ack_for())

        self.assertEqual(status, 0, errors)


class SupersessionTests(ResolutionTestCase):
    """Phase 3, D5: `--outcome superseded` passes only for a later verified revision of the same environment."""

    def supersede(self, later):
        return self.reconcile(
            self.reconcile_args(outcome="superseded", extra=("--superseded-by", later)),
            ack=self.ack_for(),
            input_effect=None,
        )

    def test_refused_without_a_later_verified_revision(self):
        before = self.snapshot()
        status, _out, _errors = self.supersede("2026-10-14_archive-family-r004")
        self.assertEqual(status, 2)
        status, _out, _errors = self.supersede("2026-10-30_archive-family-r999")
        self.assertEqual(status, 2)
        self.assertEqual(self.snapshot(), before)

    def test_refused_when_the_later_revision_is_verified_only_in_another_environment(self):
        from scripts import migration_manifest

        receipt_path = self.root / VERIFIED_FOLDER / "status.dev.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["environment"] = "staging"
        receipt["target"] = dict(receipt["target"], environment="staging")
        migration_manifest.install_receipt(self.root / VERIFIED_FOLDER / "status.staging.json", receipt)
        receipt_path.unlink()
        before = self.snapshot()

        status, _out, _errors = self.supersede("2026-10-13_archive-family-r003")

        self.assertEqual(status, 2)
        self.assertEqual(self.snapshot(), before)

    def test_refused_when_the_later_receipt_is_unreadable(self):
        (self.root / VERIFIED_FOLDER / "status.dev.json").write_bytes(b"not a receipt\n")
        before = self.snapshot()

        status, _out, _errors = self.supersede("2026-10-13_archive-family-r003")

        self.assertEqual(status, 2)
        self.assertEqual(self.snapshot(), before)

    def test_accepted_for_a_visible_later_revision_and_records_its_receipt_digest(self):
        status, _out, errors = self.supersede("2026-10-13_archive-family-r003")

        self.assertEqual(status, 0, errors)
        record = json.loads((self.root / PENDING_FOLDER / "resolution.dev.json").read_text(encoding="utf-8"))
        self.assertEqual(record["outcome"], "superseded")
        self.assertEqual(record["supersededBy"], {
            "folder": "2026-10-13_archive-family-r003",
            "receiptSha256": sha256((self.root / VERIFIED_FOLDER / "status.dev.json").read_bytes()),
        })

    def test_the_record_still_names_the_later_receipt_after_both_revisions_are_archived(self):
        """Archive keeps the superseded folder and its sidecar together, and the later revision after it.

        Decision 3 (contiguous prefix) means a visible revision may never sit below an archived one,
        so the superseded folder is always visible when it is resolved. This test archives the
        superseded folder, then the later one, and checks the moved sidecar still names the later receipt.
        """
        from scripts import migration_archive

        status, _out, errors = self.supersede("2026-10-13_archive-family-r003")
        self.assertEqual(status, 0, errors)
        receipt_digest = sha256((self.root / VERIFIED_FOLDER / "status.dev.json").read_bytes())

        migration_archive.archive_migrations(self.root, [PENDING_FOLDER], yes=True)
        migration_archive.archive_migrations(self.root, [VERIFIED_FOLDER], yes=True)

        archived_pending = self.root / "migrations-archive" / "APP_ONE" / "2026-10-12_archive-family-r002"
        archived_later = self.root / "migrations-archive" / "APP_ONE" / "2026-10-13_archive-family-r003"
        record = json.loads((archived_pending / "resolution.dev.json").read_text(encoding="utf-8"))
        self.assertEqual(record["supersededBy"]["receiptSha256"], receipt_digest)
        self.assertEqual(sha256((archived_later / "status.dev.json").read_bytes()), receipt_digest)


class SidecarIsTheRecordTests(ResolutionTestCase):
    """Phase 3-4: the sidecar is written first, so the lock report and the archive read the same record afterwards."""

    def test_a_failed_closing_event_write_leaves_a_folder_that_reads_as_resolved(self):
        from scripts import migration_revision

        resolution = self.require()
        with patch.object(resolution, "write_attempt_event", side_effect=OSError("disk full")):
            status, _out, errors = self.reconcile(self.reconcile_args(), ack=self.ack_for())

        self.assertEqual(status, 2)
        self.assertIn("closing attempt event", errors)
        self.assertTrue((self.root / PENDING_FOLDER / "resolution.dev.json").is_file())
        report = migration_revision.inspect_migration_lock(self.root, PENDING_FOLDER)
        self.assertEqual(report.status, "locked")
        self.assertIn("resolution:failed-no-target-effect:dev", report.states)
        self.assertIn("attempt-event:failed-no-target-effect:dev", report.states)
        problems = [state for state in report.states if state.startswith(("resolution-", "attempt-event-", "attempt-events-", "attempt-state-"))]
        self.assertEqual(problems, [])

    def test_a_closing_resolution_event_without_its_sidecar_is_reported_and_archive_refuses_it(self):
        from scripts import migration_archive, migration_revision

        resolution = self.require()
        pending = self.event(PENDING_FOLDER, "pending")
        closing = resolution._closing_event(pending, "failed-no-target-effect", {"path": EVIDENCE, "sha256": sha256(EVIDENCE_BYTES)})
        resolution.write_attempt_event(self.root / PENDING_FOLDER, closing)

        report = migration_revision.inspect_migration_lock(self.root, PENDING_FOLDER)

        self.assertIn("resolution-record-missing:dev", report.states)
        with self.assertRaises(migration_archive.MigrationArchiveError):
            migration_archive.archive_migrations(self.root, [PENDING_FOLDER])

    def test_a_scratch_only_resolution_archives_as_resolved_and_the_run_is_not_reported_uncertain(self):
        from scripts import migration_archive, migration_revision

        folder = self.root / PENDING_FOLDER
        for path in folder.glob("attempt-event.*.json"):
            path.unlink()
        run = self.root / "scratch" / "migration-attempt-fixture-r002"
        run.mkdir()
        migration = self.load(PENDING_FOLDER)
        self.write_raw(run / "run-manifest.json", {
            "schemaVersion": 2,
            "state": "apply-failed-or-unknown",
            "target": {"environment": "dev", "connection": "fake-dev", "expected_user": "APP_ONE", "current_schema": "APP_ONE"},
            "migrations": [{
                "folder": folder.name, "payloadDigest": migration.payload_digest, "checksSha256": migration.checks_sha256,
                "attemptEventIds": ["6f1c2a4e-9b3d-4c1e-8a7f-2d5b9e0c0d01"], "writeAttempted": True, "state": "apply-failed-or-unknown",
            }],
        })
        status, _out, errors = self.reconcile(self.reconcile_args(), ack=f"{ACK_PREFIX} migration-attempt-fixture-r002")
        self.assertEqual(status, 0, errors)

        report = migration_revision.inspect_migration_lock(self.root, PENDING_FOLDER)
        self.assertEqual(report.status, "locked")
        self.assertEqual(report.states, ("resolution:failed-no-target-effect:dev",))

        plan = migration_archive.archive_migrations(self.root, [PENDING_FOLDER])
        self.assertEqual(plan.entries[0].evidence, ("resolved:failed-no-target-effect:dev",))


if __name__ == "__main__":
    unittest.main()
