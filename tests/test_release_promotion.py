from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ReleasePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.manifest_bytes = b'{"schemaVersion":2}\n'
        self.driver_bytes = b"-- generated driver\n"
        self.file_rows = [
            {"path": "drivers/apply.sql", "sha256": digest(self.driver_bytes), "kind": "driver"},
            {"path": "payload/releases/current.json", "sha256": digest(self.manifest_bytes), "kind": "source"},
        ]
        self.payload_definition = {
            "schemaVersion": 1,
            "rolloutManifest": {"path": "releases/current.json", "sha256": digest(self.manifest_bytes)},
            "databaseObjectBudgetSeconds": 120,
            "engineDigest": digest(b"engine"),
            "files": self.file_rows,
            "steps": [
                {"type": "feature-migrate", "schema": "APP_CODE"},
                {"type": "feature-migrate", "schema": "APP_CODE"},
            ],
            "sourceToDriver": [],
            "sourceInputs": [],
            "checkpoints": [{"schema": "APP_CODE"}, {"schema": "APP_CODE"}],
        }
        self.payload_digest = digest(canonical(self.payload_definition))
        self.engine_digest = self.payload_definition["engineDigest"]
        self.source_commit = "b" * 40
        self.test_run_id = None
        self.input_hashes = (
            {"path": "features/dispatch/APP_CODE/ddl.sql", "sha256": digest(b"input")},
        )
        self.capsule = SimpleNamespace(
            root=self.root / "scratch" / "release-capsules" / f"{self.payload_digest}-{self.source_commit[:12]}",
            manifest_sha256=digest(b"capsule manifest"),
            payload_digest=self.payload_digest,
            engine_digest=self.engine_digest,
            source_commit=self.source_commit,
            input_hashes=self.input_hashes,
            checkpoints=(SimpleNamespace(schema="APP_CODE", inputs=()), SimpleNamespace(schema="APP_CODE", inputs=())),
            database_object_budget_seconds=120,
        )
        self.write_capsule_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_capsule_manifest(self) -> None:
        document = {
            "schemaVersion": 1,
            "payloadDigest": self.payload_digest,
            "engineDigest": self.engine_digest,
            "engineFiles": ["scripts/release_state.py"],
            "sourceCommit": self.source_commit,
            "rolloutManifestPath": "releases/current.json",
            "rolloutManifestSha256": digest(self.manifest_bytes),
            "payloadDefinition": self.payload_definition,
            "files": self.file_rows,
            "sourceInputs": [],
            "checkpoints": self.payload_definition["checkpoints"],
        }
        self.capsule.root.mkdir(parents=True, exist_ok=True)
        capsule_json = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        (self.capsule.root / "capsule.json").write_text(capsule_json, encoding="utf-8", newline="\n")
        for record in self.file_rows:
            path = self.capsule.root / record["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if record["kind"] == "driver":
                path.write_bytes(self.driver_bytes)
            elif record["path"] == "payload/scripts/data.sql":
                path.write_bytes(self.data_script)
            else:
                path.write_bytes(self.manifest_bytes)

    def add_data_script_step(self) -> None:
        old_root = self.capsule.root
        self.data_script = b"INSERT INTO APP_CODE.T (ID) VALUES (1);\nCOMMIT;\n"
        row = {
            "path": "payload/scripts/data.sql", "sha256": digest(self.data_script), "kind": "source",
        }
        self.file_rows.append(row)
        self.file_rows.sort(key=lambda item: item["path"])
        self.payload_definition["files"] = self.file_rows
        self.payload_definition["steps"].insert(1, {
            "type": "sql-script", "file": "scripts/data.sql", "schema": "APP_CODE",
        })
        self.payload_digest = digest(canonical(self.payload_definition))
        self.input_hashes = (*self.input_hashes, {"path": "payload/scripts/data.sql", "sha256": row["sha256"]})
        self.capsule = SimpleNamespace(
            root=self.root / "scratch" / "release-capsules" / f"{self.payload_digest}-{self.source_commit[:12]}",
            manifest_sha256=digest(b"capsule manifest"),
            payload_digest=self.payload_digest,
            engine_digest=self.engine_digest,
            source_commit=self.source_commit,
            input_hashes=self.input_hashes,
            checkpoints=(SimpleNamespace(schema="APP_CODE", inputs=()), SimpleNamespace(schema="APP_CODE", inputs=())),
            database_object_budget_seconds=120,
        )
        shutil.rmtree(old_root, ignore_errors=True)
        self.write_capsule_manifest()

    def state_module(self):
        try:
            from scripts import release_state
        except ImportError as error:
            self.fail(f"release promotion state is not implemented: {error}")
        return release_state

    def stage_report(self) -> dict:
        report, _run_id, _plan = self.complete_rollout(environment="staging")
        return report

    def make_prod_plan(
        self, *, environment: str = "prod", checkpoint_count: int = 2, plan_digest: str | None = None,
    ):
        identity = {
            "session_user": "APP_CODE", "current_schema": "APP_CODE", "db_name": environment.upper(),
            "db_unique_name": f"{environment.upper()}_UNIQUE", "service_name": f"{environment}.example.invalid",
            "container_id": "7" if environment == "prod" else "3",
            "container_name": f"{environment.upper()}PDB", "edition": "ORA$BASE",
            "database_version": "19.0.0.0",
        }
        guards = tuple({
            "checkpoint": number,
            "schema": "APP_CODE",
            "targetKey": "schema:APP_CODE",
            "targetIdentity": identity,
        } for number in range(1, checkpoint_count + 1))
        targets = {"schema:APP_CODE": {
            "environment": environment, "connection": f"{environment}-alias", "expectedUser": "APP_CODE",
            "schema": "APP_CODE", "classification": "service", "observed": identity,
        }}
        for number, definition in enumerate(self.payload_definition["steps"], start=1):
            if definition.get("type") != "sql-script":
                continue
            targets[f"step:{number}"] = {
                "environment": environment, "operation": "migration",
                "logicalSchema": definition["schema"], "connection": f"{environment}-alias",
                "expectedUser": "APP_CODE", "schema": "APP_CODE", "classification": "staging" if environment == "staging" else "production",
                "observed": identity,
            }
        return SimpleNamespace(
            environment=environment,
            payload_digest=self.payload_digest,
            plan_digest=plan_digest or digest(f"{environment} plan".encode()),
            targets=targets,
            checkpoint_guards=guards,
            recovery_attempt=None,
        )

    def complete_rollout(
        self, *, environment: str = "prod", modes: tuple[str, ...] = ("applied", "no-op"),
    ) -> tuple[dict, str, SimpleNamespace]:
        from scripts import release_ownership

        state = self.state_module()
        plan = self.make_prod_plan(environment=environment)
        with patch.object(state.release_capsule, "load_capsule", return_value=self.capsule):
            run_id = state.start_rollout(self.root, self.capsule, plan)
        evidence_by_checkpoint = {}
        for checkpoint, mode in enumerate(modes, start=1):
            scope = state.target_scope_from_identity(plan.checkpoint_guards[checkpoint - 1]["targetIdentity"])
            identity = {
                "runId": run_id, "environment": environment,
                "payloadDigest": self.payload_digest, "engineDigest": self.engine_digest,
                "planDigest": plan.plan_digest, "checkpoint": checkpoint,
                "recoveryAttempt": plan.recovery_attempt,
            }
            evidence_path = self.root / "scratch" / "reports" / f"{environment}-{run_id}-checkpoint-{checkpoint}.json"
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps({"checkpoint": checkpoint, "verified": True}) + "\n", encoding="utf-8")
            evidence = [evidence_path.relative_to(self.root).as_posix()]
            evidence_by_checkpoint[checkpoint] = evidence
            with release_ownership.target_writer_lease(self.root, scope, identity) as lease:
                attempt_id = None
                if mode == "applied":
                    attempt_id = state.record_attempt(
                        self.root, self.capsule, plan, checkpoint, run_id=run_id, lease=lease,
                    )
                    state.finish_attempt(
                        self.root, attempt_id, state="verified", evidence=evidence,
                        metrics={"objectSeconds": 1.0, "budgetSeconds": 120, "budgetCoverageComplete": True, "sqlclSessions": 1},
                    )
                elif mode != "no-op":
                    raise AssertionError(f"unsupported fixture checkpoint mode: {mode}")
                state.record_checkpoint_verification(
                    self.root, self.capsule, plan, checkpoint,
                    run_id=run_id, attempt_id=attempt_id, lease=lease,
                    evidence=evidence, metrics={"objectSeconds": 1.0, "sessionCount": 1},
                )

        index = state._load_index(self.root)
        verifications = sorted(
            (row for row in index["checkpointVerifications"] if row["runId"] == run_id),
            key=lambda row: row["checkpoint"],
        )
        verification_ids = [row["verificationId"] for row in verifications]
        report_path = f"scratch/reports/{environment}-{run_id}-final.json"
        receipt_rows = []
        report_steps = []
        step_outcomes = []
        for number, definition in enumerate(self.payload_definition["steps"], start=1):
            step_hash = digest(f"step-{number}".encode())
            receipt_path = f"scratch/rollout-receipts/{environment}-{run_id}/step-{number:03d}.json"
            receipt = {
                "schemaVersion": 1, "status": "succeeded", "environment": environment,
                "manifestSha256": digest(self.manifest_bytes), "stepNumber": number,
                "stepType": definition["type"], "stepSha256": step_hash, "inputHashes": [],
                "startedAt": "2026-10-28T00:00:00Z", "endedAt": "2026-10-28T00:00:01Z",
                "durationSeconds": 1.0, "evidencePaths": [],
            }
            receipt_raw = canonical(receipt) + b"\n"
            path = self.root / receipt_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(receipt_raw)
            receipt_hash = digest(receipt_raw)
            receipt_rows.append({
                "step": number, "status": "verified", "receiptPath": receipt_path,
                "receiptSha256": receipt_hash, "receiptJson": receipt_raw.decode("utf-8"),
            })
            report_steps.append({
                "number": number, "type": definition["type"], "status": "succeeded",
                "sha256": step_hash, "inputHashes": [], "receiptPath": receipt_path,
            })
            if definition.get("type") in {"sql-script", "ords-import"}:
                from scripts import rollout

                source = (self.capsule.root / "payload" / definition["file"]).read_bytes()
                phase = rollout.classify_auxiliary_step(definition, source)
                report_steps[-1].update({
                    "phase": phase,
                    "classificationReason": rollout._auxiliary_classification_reason(definition["type"], phase),
                    "phaseSeconds": 1.0,
                })
            step_outcomes.append({
                "step": number, "status": "verified", "receiptPath": receipt_path,
                "receiptSha256": receipt_hash,
            })

        budget = {"seconds": 120, "elapsedSeconds": float(len(modes)), "coverageComplete": True, "passed": True}
        report = {
            "schemaVersion": 2, "environment": environment, "status": "success",
            "databaseState": "verified", "payloadDigest": self.payload_digest,
            "engineDigest": self.engine_digest, "sourceCommit": self.source_commit,
            "targetPlanDigest": plan.plan_digest, "inputHashes": list(self.input_hashes),
            "objectBudget": {
                "seconds": 120, "elapsedSeconds": budget["elapsedSeconds"],
                "coverageComplete": True, "withinBudget": True,
                "includedCheckpoints": list(range(1, len(modes) + 1)), "excludedObjectSteps": [],
            },
            "dataSeconds": sum(step.get("phaseSeconds", 0.0) for step in report_steps if step.get("phase") == "data"),
            "ordsSeconds": sum(step.get("phaseSeconds", 0.0) for step in report_steps if step.get("phase") == "ords"),
            "localRunId": run_id, "reportFiles": {"json": report_path, "markdown": report_path[:-5] + ".md"},
            "steps": report_steps,
        }
        proof = {
            "schemaVersion": 1, "runId": run_id, "environment": environment,
            "sourceCommit": self.source_commit, "payloadDigest": self.payload_digest,
            "engineDigest": self.engine_digest, "planDigest": plan.plan_digest,
            "targetScopes": index["rollouts"][-1]["targetScopes"],
            "state": "verified", "databaseState": "verified", "steps": receipt_rows,
            "checkpointVerificationIds": verification_ids, "checkpointVerifications": verifications,
            "recoveryLinks": [
                row for row in index["recoveryLinks"] if row["verificationId"] in verification_ids
            ],
            "budget": budget, "finalReportPath": report_path,
            "finalReportSha256": state._completion_core_hash(report),
        }
        report["completionProof"] = proof
        final_report_file = self.root / report_path
        final_report_file.parent.mkdir(parents=True, exist_ok=True)
        final_report_file.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with patch.object(state.release_capsule, "load_capsule", return_value=self.capsule):
            state.finish_rollout(
                self.root, run_id,
                outcome={
                    "state": "verified", "databaseState": "verified", "steps": step_outcomes,
                    "checkpointVerificationIds": verification_ids, "budget": budget,
                    "finalReportPath": report_path, "finalReportSha256": proof["finalReportSha256"],
                },
            )
        return report, run_id, plan

    def record_prod_checkpoint(self, checkpoint: int) -> str:
        from scripts import release_ownership

        state = self.state_module()
        plan = self.make_prod_plan()
        if self.test_run_id is None:
            with patch.object(state.release_capsule, "load_capsule", return_value=self.capsule):
                self.test_run_id = state.start_rollout(self.root, self.capsule, plan)
        scope = state.target_scope_from_identity(plan.checkpoint_guards[checkpoint - 1]["targetIdentity"])
        identity = {
            "runId": self.test_run_id, "environment": plan.environment,
            "payloadDigest": self.capsule.payload_digest, "engineDigest": self.capsule.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": checkpoint,
            "recoveryAttempt": plan.recovery_attempt,
        }
        with release_ownership.target_writer_lease(self.root, scope, identity) as lease:
            return state.record_attempt(
                self.root, self.capsule, plan, checkpoint,
                run_id=self.test_run_id, lease=lease,
            )

    def verified_evidence(self, attempt_id: str) -> list[str]:
        path = self.root / "scratch" / "reports" / f"{attempt_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"databaseState":"verified"}\n', encoding="utf-8")
        return [path.relative_to(self.root).as_posix()]

    def test_successful_stage_report_matches_exact_payload_and_budget(self) -> None:
        state = self.state_module()
        state.validate_stage_proof(self.capsule, self.stage_report())

    def test_stage_report_requires_success_and_matching_payload_engine_sources(self) -> None:
        state = self.state_module()
        mutations = (
            ("status", "failed"),
            ("databaseState", "unknown"),
            ("payloadDigest", digest(b"other payload")),
            ("engineDigest", digest(b"other engine")),
            ("sourceCommit", "c" * 40),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                report = self.stage_report()
                report[field] = value
                with self.assertRaises(state.ReleaseStateError):
                    state.validate_stage_proof(self.capsule, report)

        report = self.stage_report()
        report["inputHashes"] = [{"path": "features/dispatch/APP_CODE/ddl.sql", "sha256": digest(b"changed")}]
        with self.assertRaisesRegex(state.ReleaseStateError, "input|source|hash"):
            state.validate_stage_proof(self.capsule, report)

    def test_stage_report_requires_complete_in_budget_object_coverage(self) -> None:
        state = self.state_module()
        mutations = (
            ("coverageComplete", False),
            ("withinBudget", False),
            ("elapsedSeconds", 120.01),
            ("includedCheckpoints", [1]),
            ("excludedObjectSteps", ["legacy-sql"]),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                report = self.stage_report()
                report["objectBudget"][field] = value
                with self.assertRaises(state.ReleaseStateError):
                    state.validate_stage_proof(self.capsule, report)

        report = self.stage_report()
        report["objectBudget"]["seconds"] = 300
        with self.assertRaisesRegex(state.ReleaseStateError, "budget|seconds"):
            state.validate_stage_proof(self.capsule, report)

        report = self.stage_report()
        report["objectBudget"]["includedCheckpoints"] = [True, 2]
        with self.assertRaisesRegex(state.ReleaseStateError, "checkpoint|budget"):
            state.validate_stage_proof(self.capsule, report)

    def test_stage_proof_recomputes_auxiliary_phase_from_frozen_source(self) -> None:
        state = self.state_module()
        self.add_data_script_step()
        report = self.stage_report()
        state.validate_stage_proof(self.capsule, report)

        for field, value in (("phase", "ords"), ("classificationReason", "approved")):
            tampered = json.loads(json.dumps(report))
            tampered["steps"][1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(state.ReleaseStateError, "phase"):
                state.validate_stage_proof(self.capsule, tampered)

    def test_stage_and_production_use_distinct_valid_target_plan_digests(self) -> None:
        state = self.state_module()
        report = self.stage_report()
        production_plan = self.make_prod_plan()
        self.assertNotEqual(report["targetPlanDigest"], production_plan.plan_digest)
        state.validate_stage_proof(self.capsule, report)

    def test_cleanup_dry_run_and_unverified_state_preserve_everything(self) -> None:
        state = self.state_module()
        plan_path = self.root / "scratch" / "feature-release-plans" / "prod" / f"{self.payload_digest}.json"
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text("production plan\n", encoding="utf-8")
        review_path = self.root / "ai_generate" / "2026-10-27" / "feature-diffs" / "review.diff"
        review_path.parent.mkdir(parents=True)
        review_path.write_text("review survives cleanup\n", encoding="utf-8")
        before_capsule = (self.capsule.root / "capsule.json").read_bytes()
        report = state.cleanup_capsule(self.root, self.payload_digest)

        self.assertFalse(report["eligible"])
        self.assertTrue(report["blockers"])
        with self.assertRaisesRegex(state.ReleaseStateError, "cleanup refused"):
            state.cleanup_capsule(self.root, self.payload_digest, apply=True)
        self.assertEqual((self.capsule.root / "capsule.json").read_bytes(), before_capsule)
        self.assertTrue(plan_path.exists())
        self.assertTrue(review_path.exists())

    def test_cleanup_refuses_unresolved_production_attempt(self) -> None:
        state = self.state_module()
        attempt = self.record_prod_checkpoint(1)
        state.finish_attempt(self.root, attempt, state="unknown", evidence=[], metrics={})

        report = state.cleanup_capsule(self.root, self.payload_digest)

        self.assertFalse(report["eligible"])
        self.assertTrue(any("unresolved" in blocker.lower() for blocker in report["blockers"]))
        self.assertTrue((self.capsule.root / "capsule.json").exists())

    def test_cleanup_refuses_unlisted_files_inside_generated_capsule(self) -> None:
        state = self.state_module()
        (self.capsule.root / "operator-note.txt").write_text("not part of the capsule\n", encoding="utf-8")

        with self.assertRaisesRegex(state.ReleaseStateError, "inventory|unlisted|unexpected"):
            state.cleanup_capsule(self.root, self.payload_digest)

    def test_cleanup_after_all_production_checkpoints_preserves_receipts_and_review(self) -> None:
        state = self.state_module()
        report, _run_id, _plan = self.complete_rollout(environment="prod", modes=("applied", "no-op"))
        retained_report = self.root / report["completionProof"]["finalReportPath"]
        retained_receipt = self.root / report["completionProof"]["steps"][0]["receiptPath"]
        plans_dir = self.root / "scratch" / "feature-release-plans"
        staging_plan = plans_dir / "staging" / f"{self.payload_digest}.json"
        production_plan = plans_dir / "prod" / f"{self.payload_digest}.json"
        staging_plan.parent.mkdir(parents=True)
        production_plan.parent.mkdir(parents=True)
        staging_plan.write_text("staging plan\n", encoding="utf-8")
        production_plan.write_text("production plan\n", encoding="utf-8")
        review_path = self.root / "ai_generate" / "2026-10-27" / "feature-diffs" / "review.diff"
        review_path.parent.mkdir(parents=True)
        review_path.write_text("retained review\n", encoding="utf-8")
        unrelated = self.root / "scratch" / "release-capsules" / "different-payload" / "capsule.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("preserve unrelated capsule\n", encoding="utf-8")

        with patch.object(state.release_capsule, "load_capsule", return_value=self.capsule):
            dry_run = state.cleanup_capsule(self.root, self.payload_digest)
        self.assertTrue(dry_run["eligible"])
        self.assertTrue(self.capsule.root.exists())
        with patch.object(state.release_capsule, "load_capsule", return_value=self.capsule):
            result = state.cleanup_capsule(self.root, self.payload_digest, apply=True)

        self.assertEqual(result["state"], "complete")
        self.assertEqual(set(result["removedPaths"]), {
            self.capsule.root.relative_to(self.root).as_posix(),
            staging_plan.relative_to(self.root).as_posix(),
            production_plan.relative_to(self.root).as_posix(),
        })
        self.assertFalse(self.capsule.root.exists())
        self.assertFalse(staging_plan.exists())
        self.assertFalse(production_plan.exists())
        self.assertTrue((self.root / "releases" / ".local-state.json").exists())
        self.assertTrue(retained_report.exists())
        self.assertTrue(retained_receipt.exists())
        self.assertTrue(review_path.exists())
        self.assertTrue(unrelated.exists())
        history = json.loads((self.root / "releases" / ".local-state.json").read_text(encoding="utf-8"))["cleanupHistory"]
        self.assertEqual(history[-1]["state"], "complete")
        self.assertEqual(set(history[-1]["removedPaths"]), set(result["removedPaths"]))

    def test_cleanup_marks_partial_from_actual_removed_paths_only(self) -> None:
        state = self.state_module()
        self.complete_rollout(environment="prod")
        plans_dir = self.root / "scratch" / "feature-release-plans"
        stage = plans_dir / "staging" / f"{self.payload_digest}.json"
        prod = plans_dir / "prod" / f"{self.payload_digest}.json"
        stage.parent.mkdir(parents=True)
        prod.parent.mkdir(parents=True)
        stage.write_text("stage\n", encoding="utf-8")
        prod.write_text("prod\n", encoding="utf-8")

        original_unlink = Path.unlink
        def fail_on_prod(path: Path, *args, **kwargs):
            if path == prod:
                raise OSError("simulated removal failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_on_prod), \
             patch.object(state.release_capsule, "load_capsule", return_value=self.capsule):
            result = state.cleanup_capsule(self.root, self.payload_digest, apply=True)

        self.assertEqual(result["state"], "partial")
        self.assertIn(stage.relative_to(self.root).as_posix(), result["removedPaths"])
        self.assertNotIn(prod.relative_to(self.root).as_posix(), result["removedPaths"])
        self.assertFalse(stage.exists())
        self.assertTrue(prod.exists())
