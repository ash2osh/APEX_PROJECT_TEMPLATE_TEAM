from __future__ import annotations

import copy
import hashlib
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release_ownership
from test_release_promotion import ReleasePromotionTests


class ReleaseCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReleasePromotionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def _partial_run(self, *, state_name: str, database_state: str, label: str) -> str:
        fixture = self.fixture
        state = fixture.state_module()
        plan = fixture.make_prod_plan()
        with patch.object(state.release_capsule, "load_capsule", return_value=fixture.capsule):
            run_id = state.start_rollout(fixture.root, fixture.capsule, plan)

        scope = state.target_scope_from_identity(plan.checkpoint_guards[0]["targetIdentity"])
        lease_identity = {
            "runId": run_id, "environment": "prod",
            "payloadDigest": fixture.payload_digest, "engineDigest": fixture.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": 1, "recoveryAttempt": None,
        }
        evidence_path = fixture.root / "scratch" / "reports" / f"{label}-{run_id}-checkpoint.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps({"featureWrite": "verified", "scenario": label}) + "\n", encoding="utf-8")
        evidence = [evidence_path.relative_to(fixture.root).as_posix()]
        with patch.object(state.release_capsule, "load_capsule", return_value=fixture.capsule):
            with release_ownership.target_writer_lease(fixture.root, scope, lease_identity) as lease:
                attempt_id = state.record_attempt(
                    fixture.root, fixture.capsule, plan, 1, run_id=run_id, lease=lease,
                )
                state.finish_attempt(
                    fixture.root, attempt_id, state="verified", evidence=evidence,
                    metrics={"objectSeconds": 1.0, "budgetSeconds": 120, "budgetCoverageComplete": True, "sqlclSessions": 1},
                )
                verification_id = state.record_checkpoint_verification(
                    fixture.root, fixture.capsule, plan, 1,
                    run_id=run_id, attempt_id=attempt_id, lease=lease,
                    evidence=evidence, metrics={"objectSeconds": 1.0, "sessionCount": 1},
                )

        receipt_path = f"scratch/rollout-receipts/{label}-{run_id}/step-001.json"
        receipt_file = fixture.root / receipt_path
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        receipt_file.write_text('{"status":"succeeded"}\n', encoding="utf-8")
        receipt_hash = hashlib.sha256(receipt_file.read_bytes()).hexdigest()
        state.finish_rollout(
            fixture.root, run_id,
            outcome={
                "state": state_name, "databaseState": database_state,
                "steps": [
                    {"step": 1, "status": "verified", "receiptPath": receipt_path, "receiptSha256": receipt_hash},
                    {"step": 2, "status": "unknown" if state_name == "unknown" else "failed", "receiptPath": None, "receiptSha256": None},
                ],
                "checkpointVerificationIds": [verification_id],
                "budget": {"seconds": 120, "elapsedSeconds": 1.0, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )
        return run_id

    def _cleanup(self) -> dict:
        state = self.fixture.state_module()
        with patch.object(state.release_capsule, "load_capsule", return_value=self.fixture.capsule):
            return state.cleanup_capsule(self.fixture.root, self.fixture.payload_digest)

    def test_all_noop_and_mixed_production_runs_need_complete_evidence_before_cleanup(self) -> None:
        for modes in (("no-op", "no-op"), ("applied", "no-op")):
            with self.subTest(modes=modes):
                report, run_id, _plan = self.fixture.complete_rollout(environment="prod", modes=modes)
                self.assertEqual(report["completionProof"]["runId"], run_id)
                self.assertEqual(report["completionProof"]["budget"]["passed"], True)
                result = self._cleanup()
                self.assertTrue(result["eligible"], result["blockers"])
                self.assertTrue(self.fixture.capsule.root.exists())

    def test_partial_feature_write_followed_by_app_ords_data_verification_or_pause_failure_blocks_cleanup(self) -> None:
        cases = (
            ("app", "failed", "verified"),
            ("ords", "failed", "verified"),
            ("data", "failed", "verified"),
            ("verification", "unknown", "verified"),
            ("pause", "unknown", "verified"),
        )
        for label, run_state, database_state in cases:
            with self.subTest(step=label):
                fixture = ReleasePromotionTests()
                fixture.setUp()
                previous_fixture = self.fixture
                self.fixture = fixture
                try:
                    self._partial_run(state_name=run_state, database_state=database_state, label=label)
                    result = self._cleanup()
                    self.assertFalse(result["eligible"])
                    self.assertTrue(any("complete verified production rollout" in item for item in result["blockers"]))
                    self.assertTrue(self.fixture.capsule.root.exists())
                finally:
                    fixture.tearDown()
                    self.fixture = previous_fixture

    def test_missing_final_report_and_edited_step_receipt_block_cleanup(self) -> None:
        report, _run_id, _plan = self.fixture.complete_rollout(environment="prod")
        report_path = self.fixture.root / report["completionProof"]["finalReportPath"]
        report_path.unlink()
        missing_report = self._cleanup()
        self.assertFalse(missing_report["eligible"])

        report, _run_id, _plan = self.fixture.complete_rollout(environment="prod")
        report_path = self.fixture.root / report["completionProof"]["finalReportPath"]
        report_path.write_text(report_path.read_text(encoding="utf-8").replace('"status": "success"', '"status": "success "'), encoding="utf-8")
        edited_report = self._cleanup()
        self.assertFalse(edited_report["eligible"])

        report, _run_id, _plan = self.fixture.complete_rollout(environment="prod")
        receipt_path = self.fixture.root / report["completionProof"]["steps"][0]["receiptPath"]
        receipt_path.unlink()
        missing_receipt = self._cleanup()
        self.assertFalse(missing_receipt["eligible"])

        report, _run_id, _plan = self.fixture.complete_rollout(environment="prod")
        receipt_path = self.fixture.root / report["completionProof"]["steps"][0]["receiptPath"]
        receipt_path.write_bytes(b"edited receipt\n")
        edited_receipt = self._cleanup()
        self.assertFalse(edited_receipt["eligible"])
        self.assertTrue(self.fixture.capsule.root.exists())

    def test_missing_checkpoint_evidence_blocks_cleanup(self) -> None:
        report, _run_id, _plan = self.fixture.complete_rollout(environment="prod")
        evidence_path = self.fixture.root / report["completionProof"]["checkpointVerifications"][0]["evidence"][0]["path"]
        evidence_path.unlink()
        result = self._cleanup()
        self.assertFalse(result["eligible"])
        self.assertTrue(self.fixture.capsule.root.exists())

    def test_portable_stage_proof_rejects_missing_checkpoint_wrong_identity_and_budget_failure(self) -> None:
        state = self.fixture.state_module()
        report = self.fixture.stage_report()

        missing_checkpoint = copy.deepcopy(report)
        missing_checkpoint["completionProof"]["checkpointVerificationIds"].pop()
        with self.assertRaisesRegex(state.ReleaseStateError, "checkpoint verification"):
            state.validate_stage_proof(self.fixture.capsule, missing_checkpoint)

        for mutation in (
            lambda value: value["completionProof"].update(environment="prod"),
            lambda value: value["completionProof"].update(engineDigest="f" * 64),
            lambda value: value["completionProof"].update(planDigest="e" * 64),
        ):
            changed = copy.deepcopy(report)
            mutation(changed)
            with self.assertRaises(state.ReleaseStateError):
                state.validate_stage_proof(self.fixture.capsule, changed)

        over_budget = copy.deepcopy(report)
        over_budget["objectBudget"]["elapsedSeconds"] = 121.0
        with self.assertRaisesRegex(state.ReleaseStateError, "budget"):
            state.validate_stage_proof(self.fixture.capsule, over_budget)

    def test_later_unresolved_run_and_copy_only_report_cannot_authorize_cleanup(self) -> None:
        state = self.fixture.state_module()
        self.fixture.complete_rollout(environment="prod")
        plan = self.fixture.make_prod_plan()
        with patch.object(state.release_capsule, "load_capsule", return_value=self.fixture.capsule):
            later_run = state.start_rollout(self.fixture.root, self.fixture.capsule, plan)
        state.finish_rollout(
            self.fixture.root, later_run,
            outcome={
                "state": "unknown", "databaseState": "verified", "steps": [],
                "checkpointVerificationIds": [],
                "budget": {"seconds": 120, "elapsedSeconds": None, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )
        unresolved = self._cleanup()
        self.assertFalse(unresolved["eligible"])
        self.assertTrue(self.fixture.capsule.root.exists())

        stage_report, _stage_run, _stage_plan = self.fixture.complete_rollout(environment="staging")
        copied_path = self.fixture.root / "scratch" / "stage-copy.json"
        copied_path.write_text(json.dumps(stage_report) + "\n", encoding="utf-8")
        (self.fixture.root / "releases" / ".local-state.json").unlink()
        self.assertTrue(state.validate_stage_proof(self.fixture.capsule, stage_report) is None)
        copy_only = self._cleanup()
        self.assertFalse(copy_only["eligible"])

    def test_cleanup_and_rollout_start_serialize_around_the_capsule(self) -> None:
        fixture = self.fixture
        state = fixture.state_module()
        fixture.complete_rollout(environment="prod")
        plan = fixture.make_prod_plan()
        loader_entered = threading.Event()
        allow_start = threading.Event()
        cleanup_started = threading.Event()
        outcomes: dict[str, object] = {}

        def gated_load(_root, path):
            loader_entered.set()
            if not allow_start.wait(5):
                raise AssertionError("test did not release the rollout loader")
            if not Path(path).is_dir():
                raise state.release_capsule.ReleaseCapsuleError("capsule was removed before rollout start")
            return fixture.capsule

        def start():
            try:
                outcomes["runId"] = state.start_rollout(fixture.root, fixture.capsule, plan)
            except Exception as error:
                outcomes["startError"] = error

        def cleanup():
            cleanup_started.set()
            try:
                outcomes["cleanup"] = state.cleanup_capsule(fixture.root, fixture.payload_digest, apply=True)
            except Exception as error:
                outcomes["cleanupError"] = error

        with patch.object(state.release_capsule, "load_capsule", side_effect=gated_load):
            start_thread = threading.Thread(target=start)
            start_thread.start()
            self.assertTrue(loader_entered.wait(5))
            cleanup_thread = threading.Thread(target=cleanup)
            cleanup_thread.start()
            self.assertTrue(cleanup_started.wait(5))
            allow_start.set()
            start_thread.join(5)
            cleanup_thread.join(5)

        self.assertFalse(start_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertIn("runId", outcomes)
        self.assertIsInstance(outcomes.get("cleanupError"), state.ReleaseStateError)
        self.assertTrue(fixture.capsule.root.exists())

    def test_stale_start_after_cleanup_refuses_removed_capsule(self) -> None:
        fixture = self.fixture
        state = fixture.state_module()
        fixture.complete_rollout(environment="prod")
        with patch.object(state.release_capsule, "load_capsule", return_value=fixture.capsule):
            result = state.cleanup_capsule(fixture.root, fixture.payload_digest, apply=True)
        self.assertEqual(result["state"], "complete")

        with self.assertRaisesRegex(state.ReleaseStateError, "changed or is unavailable"):
            state.start_rollout(fixture.root, fixture.capsule, fixture.make_prod_plan())
        self.assertFalse(fixture.capsule.root.exists())

    def test_start_revalidates_capsule_identity_before_persisting_a_run(self) -> None:
        fixture = self.fixture
        state = fixture.state_module()
        changed_capsule = copy.copy(fixture.capsule)
        changed_capsule.engine_digest = "f" * 64
        with patch.object(state.release_capsule, "load_capsule", return_value=changed_capsule):
            with self.assertRaisesRegex(state.ReleaseStateError, "identity changed"):
                state.start_rollout(fixture.root, fixture.capsule, fixture.make_prod_plan())
        self.assertEqual(state._load_index(fixture.root)["rollouts"], [])


if __name__ == "__main__":
    unittest.main()
