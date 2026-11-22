from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

IDENTITY = {
    "session_user": "APP_CODE", "current_schema": "APP_CODE", "db_name": "STAGE", "db_unique_name": "STAGE_UNIQUE",
    "service_name": "stage.example.invalid", "container_id": "3", "container_name": "STAGEPDB", "edition": "ORA$BASE",
    "database_version": "19.0.0.0",
}
OTHER = dict(IDENTITY, db_unique_name="STAGE2_UNIQUE", db_name="STAGE2", service_name="stage2.example.invalid")
PAYLOAD = hashlib.sha256(b"payload").hexdigest()
ENGINE = hashlib.sha256(b"engine").hexdigest()
SOURCE_COMMIT = "a" * 40


class RehearsalResetTests(unittest.TestCase):
    """A restored staging database no longer holds what the local ledger, receipts and attempts say."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "scratch").mkdir()
        self.capsule = SimpleNamespace(
            root=self.root / "scratch" / "release-capsules" / f"{PAYLOAD}-{SOURCE_COMMIT[:12]}",
            manifest_sha256=hashlib.sha256(b"capsule").hexdigest(), payload_digest=PAYLOAD, engine_digest=ENGINE,
            source_commit=SOURCE_COMMIT,
            checkpoints=(SimpleNamespace(schema="APP_CODE", inputs=()),),
            input_hashes=({"path": "features/demo/APP_CODE/ddl.sql", "sha256": hashlib.sha256(b"s").hexdigest()},),
            database_object_budget_seconds=120,
        )
        self.attempt_here = self._record(IDENTITY, "plan-here")
        self.attempt_other = self._record(OTHER, "plan-other")
        self._files()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- fixtures ------------------------------------------------------------------------------------------

    def _plan(self, identity: dict, digest_seed: str):
        target = {
            "environment": "staging", "connection": "stage-alias", "expectedUser": "APP_CODE", "schema": "APP_CODE",
            "classification": "service", "observed": dict(identity),
        }
        return SimpleNamespace(
            environment="staging", payload_digest=PAYLOAD, plan_digest=hashlib.sha256(digest_seed.encode()).hexdigest(),
            targets={"schema:APP_CODE": target},
            checkpoint_guards=({"checkpoint": 1, "schema": "APP_CODE", "targetKey": "schema:APP_CODE", "targetIdentity": dict(identity)},),
            recovery_attempt=None, recovery_binding=None,
        )

    def _record(self, identity: dict, digest_seed: str) -> str:
        """An unknown staging run with one unresolved attempt, as a failed or interrupted rehearsal leaves it."""
        from scripts import release_ownership, release_state

        plan = self._plan(identity, digest_seed)
        with patch.object(release_state.release_capsule, "load_capsule", return_value=self.capsule):
            run_id = release_state.start_rollout(self.root, self.capsule, plan)
        scope = release_state.target_scope_from_identity(identity)
        execution = {
            "runId": run_id, "environment": "staging", "payloadDigest": PAYLOAD, "engineDigest": ENGINE,
            "planDigest": plan.plan_digest, "checkpoint": 1, "recoveryAttempt": None,
        }
        with release_ownership.target_writer_lease(self.root, scope, execution) as lease:
            attempt_id = release_state.record_attempt(self.root, self.capsule, plan, 1, run_id=run_id, lease=lease)
        release_state.finish_rollout(self.root, run_id, outcome={
            "state": "unknown", "databaseState": "unknown", "steps": [], "checkpointVerificationIds": [],
            "budget": {"seconds": 120, "elapsedSeconds": None, "coverageComplete": False, "passed": False},
            "finalReportPath": None, "finalReportSha256": None,
        })
        return attempt_id

    def _write(self, relative: str, document: dict) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def _files(self) -> None:
        folder = "migrations/APP_CODE/2026-01-01_demo-r001"
        here, other = str(uuid.uuid4()), str(uuid.uuid4())
        self.moved = {
            "scratch/rollout-receipts/abc/staging",
            "scratch/rollout-runs/rollout-staging-1",
            "scratch/migration-attempt-here01",
            f"{folder}/status.staging.json",
            f"{folder}/attempt-event.{here}.json",
        }
        self.kept = {
            "scratch/rollout-receipts/abc/prod/step-001.json",
            "scratch/rollout-runs/rollout-prod-1/rollout-confirmation.json",
            "scratch/migration-attempt-other1/run-manifest.json",
            f"{folder}/status.dev.json",
            f"{folder}/attempt-event.{other}.json",
        }
        self._write("scratch/rollout-receipts/abc/staging/step-001.json", {"environment": "staging", "stepNumber": 1})
        self._write("scratch/rollout-receipts/abc/prod/step-001.json", {"environment": "prod", "stepNumber": 1})
        self._write("scratch/rollout-runs/rollout-staging-1/rollout-confirmation.json", {"environment": "staging"})
        self._write("scratch/rollout-runs/rollout-prod-1/rollout-confirmation.json", {"environment": "prod"})
        self._write("scratch/migration-attempt-here01/run-manifest.json", {"target": dict(IDENTITY, environment="staging")})
        self._write("scratch/migration-attempt-other1/run-manifest.json", {"target": dict(OTHER, environment="staging")})
        self._write(f"{folder}/status.staging.json", {"environment": "staging", "target": dict(IDENTITY, environment="staging")})
        self._write(f"{folder}/status.dev.json", {"environment": "dev", "target": {"db_unique_name": "DEVDB"}})
        self._write(f"{folder}/attempt-event.{here}.json", {"environment": "staging", "identity": dict(IDENTITY)})
        self._write(f"{folder}/attempt-event.{other}.json", {"environment": "staging", "identity": dict(OTHER)})

    def _index(self) -> dict:
        return json.loads((self.root / "releases" / ".local-state.json").read_text(encoding="utf-8"))

    # -- tests ---------------------------------------------------------------------------------------------

    def test_plan_selects_only_the_restored_staging_target(self) -> None:
        from scripts import rehearsal_reset

        plan = rehearsal_reset.plan_reset(self.root, IDENTITY)

        self.assertEqual((self.attempt_here,), plan.attempts)
        self.assertEqual(1, len(plan.rollouts))
        self.assertEqual(self.moved, set(plan.paths))
        self.assertEqual((), plan.tracked)
        self.assertEqual(("STAGE_UNIQUE", "3"), (plan.db_unique_name, plan.container_id))

    def test_apply_archives_unchanged_and_keeps_every_other_record(self) -> None:
        from scripts import rehearsal_reset, release_state

        original = (self.root / "releases" / ".local-state.json").read_bytes()
        plan = rehearsal_reset.plan_reset(self.root, IDENTITY)

        archive = rehearsal_reset.apply_reset(self.root, plan, IDENTITY, confirmed_by="--yes --confirm-target")

        self.assertTrue(archive.is_relative_to(self.root / "scratch" / "rehearsal-reset"))
        for relative in self.moved:
            self.assertFalse((self.root / relative).exists(), relative)
            self.assertTrue((archive / "moved" / relative).exists(), relative)
        for relative in self.kept:
            self.assertTrue((self.root / relative).is_file(), relative)
        self.assertEqual(original, (archive / "local-state.json").read_bytes())
        index = self._index()
        self.assertEqual([self.attempt_other], [row["attemptId"] for row in index["attempts"]])
        self.assertTrue(all(scope["dbUniqueName"] == "STAGE2_UNIQUE" for row in index["rollouts"] for scope in row["targetScopes"]))
        release_state._validate_index(index)
        record = json.loads((archive / "reset-record.json").read_text(encoding="utf-8"))
        self.assertEqual("STAGE_UNIQUE", record["target"]["db_unique_name"])
        self.assertEqual("--yes --confirm-target", record["confirmedBy"])
        self.assertEqual(sorted(self.moved), record["movedPaths"])
        self.assertEqual([self.attempt_here], record["ledger"]["attempts"])
        self.assertEqual(hashlib.sha256(original).hexdigest(), record["originalLedgerSha256"])

    def test_apply_refuses_while_a_writer_holds_the_target(self) -> None:
        from scripts import release_ownership, release_state, rehearsal_reset

        plan = rehearsal_reset.plan_reset(self.root, IDENTITY)
        scope = release_state.target_scope_from_identity(IDENTITY)
        with release_ownership.target_writer_lease(self.root, scope, {"purpose": "running rollout"}), \
             self.assertRaisesRegex(rehearsal_reset.RehearsalResetError, "rollout or migration is running"):
            rehearsal_reset.apply_reset(self.root, plan, IDENTITY, confirmed_by="--yes")
        for relative in self.moved:
            self.assertTrue((self.root / relative).exists(), relative)
        self.assertEqual(2, len(self._index()["attempts"]))

    def test_apply_refuses_when_the_state_changed_after_the_plan(self) -> None:
        from scripts import rehearsal_reset

        plan = rehearsal_reset.plan_reset(self.root, IDENTITY)
        self._write("scratch/migration-attempt-late01/run-manifest.json", {"target": dict(IDENTITY, environment="staging")})
        with self.assertRaisesRegex(rehearsal_reset.RehearsalResetError, "changed since it was listed"):
            rehearsal_reset.apply_reset(self.root, plan, IDENTITY, confirmed_by="--yes")
        self.assertTrue((self.root / "scratch/migration-attempt-here01").exists())

    def test_tracked_receipts_are_listed(self) -> None:
        from scripts import rehearsal_reset

        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(git + ["add", "migrations"], cwd=self.root, check=True)
        subprocess.run(git + ["commit", "-qm", "receipts"], cwd=self.root, check=True)

        plan = rehearsal_reset.plan_reset(self.root, IDENTITY)

        tracked = [path for path in self.moved if path.startswith("migrations/")]
        self.assertEqual(sorted(tracked), sorted(plan.tracked))

    def test_cli_is_staging_only_and_refuses_a_production_looking_target(self) -> None:
        from scripts import rehearsal_reset

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(2, rehearsal_reset.main(["--env", "prod", "--repo-root", str(self.root)], capture=lambda: IDENTITY))
        self.assertIn("staging only", stderr.getvalue())
        production_like = dict(IDENTITY, db_unique_name="ERP_PROD")
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, rehearsal_reset.main(["--env", "staging", "--repo-root", str(self.root)], capture=lambda: production_like))
        self.assertIn("looks like production", err.getvalue())

    def test_cli_lists_by_default_and_applies_only_with_the_observed_name(self) -> None:
        from scripts import rehearsal_reset

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, rehearsal_reset.main(["--env", "staging", "--repo-root", str(self.root)], capture=lambda: IDENTITY))
        self.assertIn("scratch/migration-attempt-here01", out.getvalue())
        self.assertTrue((self.root / "scratch/migration-attempt-here01").exists())

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            status = rehearsal_reset.main(
                ["--env", "staging", "--apply", "--confirm-target", "WRONG", "--yes", "--repo-root", str(self.root)],
                capture=lambda: IDENTITY,
            )
        self.assertEqual(2, status)
        self.assertIn("does not match the observed db_unique_name", err.getvalue())
        self.assertTrue((self.root / "scratch/migration-attempt-here01").exists())

        with contextlib.redirect_stdout(io.StringIO()):
            status = rehearsal_reset.main(
                ["--env", "staging", "--apply", "--confirm-target", "STAGE_UNIQUE", "--yes", "--repo-root", str(self.root)],
                capture=lambda: IDENTITY,
            )
        self.assertEqual(0, status)
        self.assertFalse((self.root / "scratch/migration-attempt-here01").exists())

    def test_apply_without_yes_needs_a_terminal_answer(self) -> None:
        from scripts import rehearsal_reset

        with patch.object(rehearsal_reset.terminal_confirm, "may_read_yes_no", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = rehearsal_reset.main(
                ["--env", "staging", "--apply", "--confirm-target", "STAGE_UNIQUE", "--repo-root", str(self.root)],
                capture=lambda: IDENTITY,
            )
        self.assertEqual(1, status)
        self.assertTrue((self.root / "scratch/migration-attempt-here01").exists())


if __name__ == "__main__":
    unittest.main()
