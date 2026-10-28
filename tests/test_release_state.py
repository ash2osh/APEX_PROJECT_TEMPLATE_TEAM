from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


IDENTITY = {
    "session_user": "APP_CODE",
    "current_schema": "APP_CODE",
    "db_name": "STAGE",
    "db_unique_name": "STAGE_UNIQUE",
    "service_name": "stage.example.invalid",
    "container_id": "3",
    "container_name": "STAGEPDB",
    "edition": "ORA$BASE",
    "database_version": "19.0.0.0",
}
PAYLOAD = hashlib.sha256(b"payload").hexdigest()
ENGINE = hashlib.sha256(b"engine").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"plan").hexdigest()
SOURCE_COMMIT = "a" * 40


class ReleaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.feature_dir = self.root / "features" / "dispatch" / "APP_CODE"
        self.feature_dir.mkdir(parents=True)
        (self.feature_dir / "ddl.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8")
        self.capsule = self.make_capsule()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_capsule(self, *, payload: str = PAYLOAD, engine: str = ENGINE):
        inputs = ({"path": "features/dispatch/APP_CODE/ddl.sql", "sha256": hashlib.sha256(b"source").hexdigest()},)
        return SimpleNamespace(
            root=self.root / "scratch" / "release-capsules" / f"{payload}-{SOURCE_COMMIT[:12]}",
            manifest_sha256=hashlib.sha256(b"capsule").hexdigest(),
            payload_digest=payload,
            engine_digest=engine,
            source_commit=SOURCE_COMMIT,
            checkpoints=(SimpleNamespace(schema="APP_CODE", inputs=()), SimpleNamespace(schema="APP_CODE", inputs=())),
            input_hashes=inputs,
            database_object_budget_seconds=120,
        )

    def make_plan(
        self,
        *,
        environment: str = "staging",
        connection: str = "stage-alias-one",
        recovery_attempt: str | None = None,
        recovery_binding: dict | None = None,
        plan_digest: str = PLAN_DIGEST,
        identity: dict | None = None,
    ):
        observed = dict(identity or IDENTITY)
        target = {
            "environment": environment,
            "connection": connection,
            "expectedUser": "APP_CODE",
            "schema": "APP_CODE",
            "classification": "service",
            "observed": observed,
        }
        guards = tuple({
            "checkpoint": number,
            "schema": "APP_CODE",
            "targetKey": "schema:APP_CODE",
            "targetIdentity": observed,
        } for number in (1, 2))
        return SimpleNamespace(
            environment=environment,
            payload_digest=self.capsule.payload_digest,
            plan_digest=plan_digest,
            targets={"schema:APP_CODE": target},
            checkpoint_guards=guards,
            recovery_attempt=recovery_attempt,
            recovery_binding=recovery_binding,
        )

    def state_module(self):
        try:
            from scripts import release_state
        except ImportError as error:
            self.fail(f"release attempt state is not implemented: {error}")
        return release_state

    def start_rollout(self, capsule=None, plan=None):
        state = self.state_module()
        selected_capsule = capsule or self.capsule
        selected_plan = plan or self.make_plan()
        with patch.object(state.release_capsule, "load_capsule", return_value=selected_capsule):
            return state.start_rollout(self.root, selected_capsule, selected_plan)

    def index(self) -> dict:
        return json.loads((self.root / "releases" / ".local-state.json").read_text(encoding="utf-8"))

    def record(self, *, plan=None, capsule=None, checkpoint: int = 1) -> str:
        from scripts import release_ownership

        state = self.state_module()
        selected_capsule = capsule or self.capsule
        selected_plan = plan or self.make_plan()
        run_id = self.start_rollout(selected_capsule, selected_plan)
        scope = state.target_scope_from_identity(selected_plan.checkpoint_guards[checkpoint - 1]["targetIdentity"])
        execution_identity = {
            "runId": run_id, "environment": selected_plan.environment,
            "payloadDigest": selected_capsule.payload_digest, "engineDigest": selected_capsule.engine_digest,
            "planDigest": selected_plan.plan_digest, "checkpoint": checkpoint,
            "recoveryAttempt": selected_plan.recovery_attempt,
        }
        with release_ownership.target_writer_lease(self.root, scope, execution_identity) as lease:
            attempt_id = state.record_attempt(
                self.root, selected_capsule, selected_plan, checkpoint,
                run_id=run_id, lease=lease,
            )
        state.finish_rollout(
            self.root, run_id,
            outcome={
                "state": "unknown", "databaseState": "unknown", "steps": [],
                "checkpointVerificationIds": [],
                "budget": {"seconds": getattr(selected_capsule, "database_object_budget_seconds", None), "elapsedSeconds": None, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )
        return attempt_id

    def verified_evidence(self, attempt_id: str) -> list[str]:
        path = self.root / "scratch" / "reports" / f"{attempt_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"databaseState":"verified"}\n', encoding="utf-8")
        return [path.relative_to(self.root).as_posix()]

    def test_public_attempt_and_cleanup_entrypoints_exist(self) -> None:
        state = self.state_module()
        for name in ("record_attempt", "finish_attempt", "validate_stage_proof", "cleanup_capsule"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(state, name, None)), f"missing public operation {name}")

    def test_attempt_records_physical_target_and_atomic_private_index(self) -> None:
        attempt_id = self.record()

        row = self.index()["attempts"][0]
        self.assertEqual(row["attemptId"], attempt_id)
        self.assertEqual(row["targetScope"], {
            "dbUniqueName": "STAGE_UNIQUE", "containerId": "3", "schema": "APP_CODE", "edition": "ORA$BASE",
        })
        self.assertEqual(row["state"], "attempting")
        self.assertTrue(row["writePossible"])
        self.assertEqual(row["checkpoint"], 1)
        self.assertEqual(row["payloadDigest"], PAYLOAD)
        index_path = self.root / "releases" / ".local-state.json"
        self.assertEqual(index_path.read_bytes(), (json.dumps(self.index(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode())
        if os.name != "nt":
            self.assertEqual(index_path.stat().st_mode & 0o077, 0)
        self.assertFalse(list(index_path.parent.glob("*.tmp")))
        self.assertFalse(list(index_path.parent.glob("*.tmp-*")))

    def test_existing_releases_directory_permissions_are_preserved(self) -> None:
        releases = self.root / "releases"
        releases.mkdir(mode=0o755)
        releases.chmod(0o755)
        before = releases.stat().st_mode & 0o777

        self.record()

        self.assertEqual(releases.stat().st_mode & 0o777, before)

    def test_attempt_plan_checkpoint_schema_must_match_capsule(self) -> None:
        state = self.state_module()
        other_identity = dict(IDENTITY, session_user="OTHER", current_schema="OTHER")
        other_plan = self.make_plan(identity=other_identity)
        other_plan.targets = {"schema:OTHER": {
            "environment": "staging", "connection": "stage-alias-one", "expectedUser": "OTHER",
            "schema": "OTHER", "classification": "service", "observed": other_identity,
        }}
        other_plan.checkpoint_guards = ({
            "checkpoint": 1, "schema": "OTHER", "targetKey": "schema:OTHER", "targetIdentity": other_identity,
        },)

        with self.assertRaisesRegex(state.ReleaseStateError, "checkpoint|schema"):
            self.record(plan=other_plan)

    def test_orphaned_writer_lock_refuses_without_changing_index(self) -> None:
        state = self.state_module()
        self.record()
        index_path = self.root / "releases" / ".local-state.json"
        before = index_path.read_bytes()
        (index_path.parent / ".local-state.lock").write_text('{"pid":999999}\n', encoding="utf-8")

        with self.assertRaisesRegex(state.ReleaseStateError, "lock|writer|reconcil"):
            self.record(checkpoint=2)

        self.assertEqual(index_path.read_bytes(), before)
        self.assertTrue((index_path.parent / ".local-state.lock").exists())

    def test_corrupt_duplicate_key_index_refuses_before_mutation(self) -> None:
        state = self.state_module()
        releases = self.root / "releases"
        releases.mkdir()
        index_path = releases / ".local-state.json"
        before = b'{"schemaVersion":1,"schemaVersion":1,"attempts":[],"cleanupHistory":[]}\n'
        index_path.write_bytes(before)

        with self.assertRaisesRegex(state.ReleaseStateError, "duplicate"):
            self.record()

        self.assertEqual(index_path.read_bytes(), before)

    def test_schema_one_index_migrates_atomically_and_retains_original_bytes(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        prior = self.index()
        legacy = {
            "schemaVersion": 1,
            "attempts": prior["attempts"],
            "cleanupHistory": prior["cleanupHistory"],
        }
        raw = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        index_path = self.root / "releases" / ".local-state.json"
        index_path.write_bytes(raw)
        evidence = self.root / "scratch" / "sqlcl.log"
        evidence.write_text("ORA-03113: end-of-file on communication channel\n", encoding="utf-8")

        state.finish_attempt(self.root, attempt_id, state="unknown", evidence=["scratch/sqlcl.log"], metrics={})

        upgraded = self.index()
        self.assertEqual(upgraded["schemaVersion"], 2)
        self.assertEqual(upgraded["checkpointVerifications"], [])
        self.assertEqual(upgraded["recoveryLinks"], [])
        provenance = upgraded["migrationProvenance"]
        self.assertEqual(provenance["schema1Sha256"], hashlib.sha256(raw).hexdigest())
        retained = self.root / provenance["retainedPath"]
        self.assertEqual(retained.read_bytes(), raw)
        self.assertEqual(upgraded["attempts"][0]["state"], "unknown")

    def test_schema_two_rollout_success_refuses_without_checkpoint_and_step_evidence(self) -> None:
        state = self.state_module()
        run_id = self.start_rollout(self.capsule, self.make_plan())
        run = self.index()["rollouts"][0]
        self.assertEqual(run["runId"], run_id)
        self.assertEqual(run["state"], "running")

        with self.assertRaisesRegex(state.ReleaseStateError, "complete checkpoint|rollout outcome|verified rollout"):
            state.finish_rollout(self.root, run_id, outcome={
                "state": "verified", "databaseState": "verified", "steps": [],
                "checkpointVerificationIds": [],
                "budget": {"seconds": 120, "elapsedSeconds": 1, "coverageComplete": True, "passed": True},
                "finalReportPath": None, "finalReportSha256": hashlib.sha256(b"report").hexdigest(),
            })
        self.assertEqual(self.index()["rollouts"][0]["state"], "running")

    def test_unknown_rollout_keeps_its_target_occupied_until_reconciliation(self) -> None:
        state = self.state_module()
        plan = self.make_plan()
        run_id = self.start_rollout(self.capsule, plan)
        state.finish_rollout(
            self.root, run_id,
            outcome={
                "state": "unknown", "databaseState": "unknown", "steps": [],
                "checkpointVerificationIds": [],
                "budget": {"seconds": 120, "elapsedSeconds": None, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )

        with self.assertRaisesRegex(state.ReleaseStateError, "unresolved rollout.*reconcile"):
            self.start_rollout(self.capsule, plan)

    def test_mixed_applied_and_noop_checkpoints_have_complete_durable_receipts(self) -> None:
        from scripts import release_ownership

        state = self.state_module()
        plan = self.make_plan()
        run_id = self.start_rollout(self.capsule, plan)
        verification_ids = []
        for checkpoint in (1, 2):
            scope = state.target_scope_from_identity(plan.checkpoint_guards[checkpoint - 1]["targetIdentity"])
            lease_identity = {
                "runId": run_id, "environment": plan.environment,
                "payloadDigest": self.capsule.payload_digest, "engineDigest": self.capsule.engine_digest,
                "planDigest": plan.plan_digest, "checkpoint": checkpoint,
                "recoveryAttempt": plan.recovery_attempt,
            }
            evidence_path = self.root / "scratch" / "reports" / f"checkpoint-{checkpoint}.json"
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps({"checkpoint": checkpoint, "verified": True}) + "\n", encoding="utf-8")
            wrong_evidence_path = evidence_path.with_name(f"other-{checkpoint}.json")
            wrong_evidence_path.write_text("different evidence\n", encoding="utf-8")
            attempt_id = None
            with release_ownership.target_writer_lease(self.root, scope, lease_identity) as lease:
                state.assert_checkpoint_allowed(
                    self.root, self.capsule, plan, checkpoint, run_id=run_id, lease=lease,
                )
                if checkpoint == 1:
                    attempt_id = state.record_attempt(
                        self.root, self.capsule, plan, checkpoint, run_id=run_id, lease=lease,
                    )
                    state.finish_attempt(
                        self.root, attempt_id, state="verified",
                        evidence=[evidence_path.relative_to(self.root).as_posix()], metrics={},
                    )
                    with self.assertRaisesRegex(state.ReleaseStateError, "evidence differs"):
                        state.record_checkpoint_verification(
                            self.root, self.capsule, plan, checkpoint,
                            run_id=run_id, attempt_id=attempt_id, lease=lease,
                            evidence=[wrong_evidence_path.relative_to(self.root).as_posix()],
                            metrics={"objectSeconds": 1.25, "sessionCount": 2},
                        )
                verification_ids.append(state.record_checkpoint_verification(
                    self.root, self.capsule, plan, checkpoint,
                    run_id=run_id, attempt_id=attempt_id, lease=lease,
                    evidence=[evidence_path.relative_to(self.root).as_posix()],
                    metrics={"objectSeconds": 1.25, "sessionCount": 2},
                ))

        index = self.index()
        rows = sorted(index["checkpointVerifications"], key=lambda row: row["checkpoint"])
        self.assertEqual([row["mode"] for row in rows], ["applied", "no-op"])
        self.assertEqual([row["attemptId"] is not None for row in rows], [True, False])
        self.assertEqual(index["rollouts"][0]["state"], "running")
        self.assertEqual(index["rollouts"][0]["checkpointVerificationIds"], verification_ids)

    def test_attempt_verification_and_terminal_state_commit_atomically(self) -> None:
        from scripts import release_ownership

        state = self.state_module()
        plan = self.make_plan()
        run_id = self.start_rollout(self.capsule, plan)
        scope = state.target_scope_from_identity(plan.checkpoint_guards[0]["targetIdentity"])
        lease_identity = {
            "runId": run_id, "environment": plan.environment,
            "payloadDigest": self.capsule.payload_digest, "engineDigest": self.capsule.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": 1,
            "recoveryAttempt": plan.recovery_attempt,
        }
        evidence_path = self.verified_evidence("applied-checkpoint")
        evidence = evidence_path[0]
        with release_ownership.target_writer_lease(self.root, scope, lease_identity) as lease:
            attempt_id = state.record_attempt(
                self.root, self.capsule, plan, 1, run_id=run_id, lease=lease,
            )
            with patch.object(state, "_write_index", side_effect=state.ReleaseStateError("simulated state write refusal")):
                with self.assertRaisesRegex(state.ReleaseStateError, "simulated state write refusal"):
                    state.record_checkpoint_verification(
                        self.root, self.capsule, plan, 1,
                        run_id=run_id, attempt_id=attempt_id, lease=lease,
                        evidence=[evidence], metrics={"objectSeconds": 1.0, "sessionCount": 1},
                        finish_attempt_metrics={"objectSeconds": 1.0, "budgetCoverageComplete": True},
                    )

            failed_update = self.index()
            self.assertEqual(failed_update["attempts"][0]["state"], "attempting")
            self.assertEqual(failed_update["checkpointVerifications"], [])
            state.record_checkpoint_verification(
                self.root, self.capsule, plan, 1,
                run_id=run_id, attempt_id=attempt_id, lease=lease,
                evidence=[evidence], metrics={"objectSeconds": 1.0, "sessionCount": 1},
                finish_attempt_metrics={"objectSeconds": 1.0, "budgetCoverageComplete": True},
            )

        committed = self.index()
        self.assertEqual(committed["attempts"][0]["state"], "verified")
        self.assertEqual(len(committed["checkpointVerifications"]), 1)

    def test_corrupt_entry_types_raise_a_release_state_error(self) -> None:
        state = self.state_module()
        self.record()
        index_path = self.root / "releases" / ".local-state.json"
        document = self.index()
        document["attempts"][0]["environment"] = []
        index_path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        before = index_path.read_bytes()

        with self.assertRaises(state.ReleaseStateError):
            self.record(checkpoint=2)

        self.assertEqual(index_path.read_bytes(), before)

    def test_scratch_deletion_does_not_unlock_unknown_physical_target_or_alias(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        log = self.root / "scratch" / "sqlcl.log"
        log.write_text("ORA-03113: end-of-file on communication channel\n", encoding="utf-8")
        state.finish_attempt(self.root, attempt_id, state="unknown", evidence=["scratch/sqlcl.log"], metrics={})
        shutil.rmtree(self.scratch)
        changed_alias = self.make_plan(connection="renamed-stage-connection")

        with self.assertRaisesRegex(state.ReleaseStateError, "unresolved|reconcil|attempt"):
            self.record(plan=changed_alias, checkpoint=2)

        self.assertEqual(self.index()["attempts"][0]["state"], "unknown")
        self.assertFalse((self.feature_dir / ".release-lock").exists())

    def test_verified_database_over_budget_remains_verified_and_ineligible(self) -> None:
        state = self.state_module()
        attempt_id = self.record(checkpoint=2)
        state.finish_attempt(
            self.root,
            attempt_id,
            state="verified",
            evidence=self.verified_evidence(attempt_id),
            metrics={
                "objectSeconds": 130.0,
                "budgetSeconds": 120,
                "budgetCoverageComplete": True,
                "budgetPassed": False,
                "includedCheckpoints": [1, 2],
                "excludedObjectSteps": [],
            },
        )

        row = self.index()["attempts"][0]
        self.assertEqual(row["state"], "verified")
        self.assertEqual(row["budget"], {
            "seconds": 120, "elapsedSeconds": 130.0, "coverageComplete": True, "passed": False,
        })
        with self.assertRaisesRegex(state.ReleaseStateError, "already|terminal|transition"):
            state.finish_attempt(self.root, attempt_id, state="unknown", evidence=[], metrics={})

    def test_unmeasured_metrics_cannot_claim_that_object_budget_passed(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        state.finish_attempt(
            self.root,
            attempt_id,
            state="verified",
            evidence=self.verified_evidence(attempt_id),
            metrics={"budgetPassed": True},
        )

        row = self.index()["attempts"][0]
        self.assertIsNone(row["budget"]["passed"])
        self.assertFalse(row["metrics"]["budgetPassed"])

    def test_only_a_proven_pre_payload_connection_failure_can_be_not_started(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        log = self.root / "scratch" / "sqlcl.log"
        log.write_text("ORA-12154: TNS could not resolve the connect identifier\n", encoding="utf-8")
        state.finish_attempt(self.root, attempt_id, state="not-started", evidence=["scratch/sqlcl.log"], metrics={})
        row = self.index()["attempts"][0]
        self.assertEqual(row["state"], "not-started")
        self.assertFalse(row["writePossible"])
        self.assertEqual(row["metrics"]["noWriteClassification"], "connection-failed-before-payload")

    def test_sqlcl_start_refusal_is_also_a_narrow_no_write_classification(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        path = self.root / "scratch" / "sqlcl.log"
        path.write_text("unable to start SQLcl: executable was not found\n", encoding="utf-8")

        state.finish_attempt(self.root, attempt_id, state="not-started", evidence=["scratch/sqlcl.log"], metrics={})

        row = self.index()["attempts"][0]
        self.assertEqual(row["state"], "not-started")
        self.assertEqual(row["metrics"]["noWriteClassification"], "sqlcl-start-failed")

    def test_payload_marker_or_missing_log_cannot_be_marked_not_started(self) -> None:
        state = self.state_module()
        first = self.record()
        log = self.root / "scratch" / "sqlcl.log"
        log.write_text(
            "ORA-12154: TNS could not resolve the connect identifier\nMIGRATION_PAYLOAD_STARTED:abc\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(state.ReleaseStateError, "no.write|payload|evidence"):
            state.finish_attempt(self.root, first, state="not-started", evidence=["scratch/sqlcl.log"], metrics={})
        self.assertEqual(self.index()["attempts"][0]["state"], "attempting")

    def test_missing_log_cannot_be_marked_not_started(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        with self.assertRaisesRegex(state.ReleaseStateError, "log|evidence|no.write"):
            state.finish_attempt(self.root, attempt_id, state="not-started", evidence=[], metrics={})
        self.assertEqual(self.index()["attempts"][0]["state"], "attempting")

    def test_timeout_and_lost_connection_errors_are_not_no_write_proof(self) -> None:
        state = self.state_module()
        attempt_id = self.record()
        path = self.root / "scratch" / "sqlcl.log"
        for output in (
            "ORA-03113: end-of-file on communication channel\n",
            "operation timed out\n",
            "operation cancelled by user\n",
        ):
            path.write_text(output, encoding="utf-8")
            with self.subTest(output=output.strip()), self.assertRaisesRegex(state.ReleaseStateError, "no.write|pre.payload"):
                state.finish_attempt(self.root, attempt_id, state="not-started", evidence=["scratch/sqlcl.log"], metrics={})
            self.assertEqual(self.index()["attempts"][0]["state"], "attempting")

    def test_unknown_attempt_recovery_links_without_rewriting_old_evidence(self) -> None:
        state = self.state_module()
        old_id = self.record()
        log = self.root / "scratch" / "sqlcl.log"
        log.write_text("ORA-03113: end-of-file on communication channel\n", encoding="utf-8")
        state.finish_attempt(self.root, old_id, state="unknown", evidence=["scratch/sqlcl.log"], metrics={})
        old_before = dict(self.index()["attempts"][0])
        scope = state.recovery_attempt_details(self.root, self.capsule, "staging", old_id)["targetScope"]
        recovery_binding = {
            "attemptId": old_id, "attemptState": "unknown", "checkpoint": 1,
            "targetScope": scope, "terminalAcknowledgment": f"RECONCILE {old_id}",
            "acknowledgedAt": state._utc_now(), "oldEvidence": old_before["evidence"],
            "observedStateSha256": hashlib.sha256(b"partial state").hexdigest(),
        }
        recovery_plan = self.make_plan(
            recovery_attempt=old_id, recovery_binding=recovery_binding,
            plan_digest=hashlib.sha256(b"recovery").hexdigest(),
        )
        recovery_id = self.record(plan=recovery_plan, checkpoint=1)
        state.finish_attempt(
            self.root, recovery_id, state="verified", evidence=self.verified_evidence(recovery_id), metrics={},
        )

        rows = {row["attemptId"]: row for row in self.index()["attempts"]}
        self.assertEqual(rows[old_id]["state"], "unknown")
        self.assertEqual(rows[old_id]["evidence"], old_before["evidence"])
        self.assertEqual(rows[old_id]["recoveredBy"], recovery_id)
        self.assertEqual(rows[recovery_id]["recoveryOf"], old_id)

    def test_recovery_reconciles_only_the_bound_unknown_rollout(self) -> None:
        from scripts import release_ownership

        state = self.state_module()
        old_id = self.record()
        log = self.root / "scratch" / "sqlcl.log"
        log.write_text("ORA-03113: end-of-file on communication channel\n", encoding="utf-8")
        state.finish_attempt(self.root, old_id, state="unknown", evidence=["scratch/sqlcl.log"], metrics={})
        old_attempt = next(row for row in self.index()["attempts"] if row["attemptId"] == old_id)
        old_run = next(row for row in self.index()["rollouts"] if row["planDigest"] == old_attempt["planDigest"])
        with self.assertRaisesRegex(state.ReleaseStateError, "unresolved rollout.*reconcile"):
            self.start_rollout(self.capsule, self.make_plan())

        scope = state.recovery_attempt_details(self.root, self.capsule, "staging", old_id)["targetScope"]
        recovery_binding = {
            "attemptId": old_id, "attemptState": "unknown", "checkpoint": 1,
            "targetScope": scope, "terminalAcknowledgment": f"RECONCILE {old_id}",
            "acknowledgedAt": state._utc_now(), "oldEvidence": old_attempt["evidence"],
            "observedStateSha256": hashlib.sha256(b"reconciled partial state").hexdigest(),
        }
        recovery_plan = self.make_plan(
            recovery_attempt=old_id, recovery_binding=recovery_binding,
            plan_digest=hashlib.sha256(b"bound recovery plan").hexdigest(),
        )
        self.assertEqual(
            state.recovery_parent_rollout(self.root, self.capsule, recovery_plan)["runId"],
            old_run["runId"],
        )
        recovery_run = self.start_rollout(self.capsule, recovery_plan)

        evidence_path = self.root / "scratch" / "recovery-checkpoint.json"
        evidence_path.write_text('{"reconciled":true}\n', encoding="utf-8")
        identity = {
            "runId": recovery_run, "environment": "staging",
            "payloadDigest": self.capsule.payload_digest, "engineDigest": self.capsule.engine_digest,
            "planDigest": recovery_plan.plan_digest, "checkpoint": 1,
            "recoveryAttempt": old_id,
        }
        with release_ownership.target_writer_lease(self.root, scope, identity) as lease:
            verification_id = state.record_checkpoint_verification(
                self.root, self.capsule, recovery_plan, 1,
                run_id=recovery_run, attempt_id=None, lease=lease,
                evidence=["scratch/recovery-checkpoint.json"],
                metrics={"objectSeconds": 1.0, "sessionCount": 0},
            )

        state.finish_rollout(
            self.root, recovery_run,
            outcome={
                "state": "failed", "databaseState": "verified", "steps": [],
                "checkpointVerificationIds": [verification_id],
                "budget": {"seconds": 120, "elapsedSeconds": 1.0, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )
        next_run = self.start_rollout(self.capsule, self.make_plan())

        attempts = {row["attemptId"]: row for row in self.index()["attempts"]}
        runs = {row["runId"]: row for row in self.index()["rollouts"]}
        self.assertEqual(attempts[old_id]["state"], "unknown")
        self.assertEqual(attempts[old_id]["evidence"], old_attempt["evidence"])
        self.assertEqual(runs[old_run["runId"]]["state"], "unknown")
        self.assertEqual(runs[next_run]["state"], "running")

    def test_recovery_cannot_bypass_an_unknown_noncheckpoint_step(self) -> None:
        state = self.state_module()
        old_id = self.record()
        state.finish_attempt(self.root, old_id, state="unknown", evidence=[], metrics={})
        old_attempt = next(row for row in self.index()["attempts"] if row["attemptId"] == old_id)
        scope = old_attempt["targetScope"]
        recovery_plan = self.make_plan(
            recovery_attempt=old_id,
            recovery_binding={
                "attemptId": old_id, "attemptState": "unknown", "checkpoint": 1,
                "targetScope": scope, "terminalAcknowledgment": f"RECONCILE {old_id}",
                "acknowledgedAt": state._utc_now(), "oldEvidence": old_attempt["evidence"],
                "observedStateSha256": hashlib.sha256(b"reconciled partial state").hexdigest(),
            },
            plan_digest=hashlib.sha256(b"recovery cannot hide auxiliary uncertainty").hexdigest(),
        )

        def add_unknown_step(index: dict) -> dict:
            run = next(row for row in index["rollouts"] if row["planDigest"] == old_attempt["planDigest"])
            run["steps"] = [{"step": 1, "status": "unknown", "receiptPath": None, "receiptSha256": None}]
            return index

        state._atomic_update(self.root, add_unknown_step)
        with self.assertRaisesRegex(state.ReleaseStateError, "unresolved rollout.*reconcile"):
            self.start_rollout(self.capsule, recovery_plan)
        self.assertEqual(len(self.index()["rollouts"]), 1)

    def test_recovery_must_bind_to_an_unresolved_same_payload_target_attempt(self) -> None:
        state = self.state_module()
        old_id = self.record()
        state.finish_attempt(self.root, old_id, state="unknown", evidence=[], metrics={})
        wrong_payload = self.make_capsule(payload=hashlib.sha256(b"different").hexdigest())
        wrong_plan = self.make_plan(recovery_attempt=old_id)
        wrong_plan.payload_digest = wrong_payload.payload_digest
        wrong_plan.plan_digest = hashlib.sha256(b"different recovery plan").hexdigest()
        with self.assertRaisesRegex(state.ReleaseStateError, "payload|recovery|attempt"):
            self.record(plan=wrong_plan, capsule=wrong_payload, checkpoint=1)

    def test_recovery_attempt_must_cover_the_original_checkpoint(self) -> None:
        state = self.state_module()
        old_id = self.record(checkpoint=1)
        state.finish_attempt(self.root, old_id, state="unknown", evidence=[], metrics={})
        recovery_plan = self.make_plan(
            recovery_attempt=old_id, plan_digest=hashlib.sha256(b"recovery checkpoint").hexdigest(),
        )

        with self.assertRaisesRegex(state.ReleaseStateError, "checkpoint|recovery"):
            self.record(plan=recovery_plan, checkpoint=2)
