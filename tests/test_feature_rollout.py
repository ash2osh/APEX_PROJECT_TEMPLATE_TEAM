from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import rollout
from scripts.feature_manifest import FeatureCheckpoint
from scripts.release_capsule import ReleaseCapsule


class FeatureRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.manifest_path = self.root / "releases" / "current.json"
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_raw = b'{"schemaVersion":2,"databaseObjectBudgetSeconds":120,"steps":[]}\n'
        self.manifest_path.write_bytes(self.manifest_raw)
        self.manifest_sha = hashlib.sha256(self.manifest_raw).hexdigest()
        self.payload = hashlib.sha256(b"payload").hexdigest()
        self.engine = hashlib.sha256(b"engine").hexdigest()
        self.source_commit = "abcdef1234567890abcdef1234567890abcdef12"
        capsule_root = self.root / "scratch" / "release-capsules" / f"{self.payload}-{self.source_commit[:12]}"
        capsule_root.mkdir(parents=True)
        (capsule_root / "capsule.json").write_text("{}\n", encoding="utf-8")
        self.checkpoints = [
            FeatureCheckpoint("APP_CODE", (), (), (), ())
            for _ in range(2)
        ]
        self.capsule = ReleaseCapsule(
            capsule_root,
            hashlib.sha256(b"capsule").hexdigest(),
            self.payload,
            self.engine,
            self.source_commit,
            tuple(self.checkpoints),
            (
                {"path": "payload/features/one/APP_CODE/ddl.sql", "sha256": hashlib.sha256(b"ddl").hexdigest()},
                {"path": "drivers/checkpoint-001-input-001.sql", "sha256": hashlib.sha256(b"driver1").hexdigest()},
                {"path": "drivers/checkpoint-002-input-001.sql", "sha256": hashlib.sha256(b"driver2").hexdigest()},
            ),
            120,
        )
        self.capsule_path = capsule_root
        self.capsule_document = {
            "payloadDefinition": {
                "rolloutManifest": {"path": "releases/current.json", "sha256": self.manifest_sha},
            },
            "checkpoints": [
                {"inputs": [{"path": f"features/feature-{i}/APP_CODE/ddl.sql", "sha256": hashlib.sha256(f"ddl{i}".encode()).hexdigest()}],
                 "checks": [{"path": f"features/feature-{i}/APP_CODE/checks.json", "sha256": hashlib.sha256(f"check{i}".encode()).hexdigest()}]}
                for i in (1, 2)
            ],
            "sourceInputs": [
                {"step": i, "input": 1, "sourcePath": f"payload/features/feature-{i}/APP_CODE/ddl.sql",
                 "sourceSha256": hashlib.sha256(f"ddl{i}".encode()).hexdigest(),
                 "driverPath": f"drivers/checkpoint-{i:03d}-input-001.sql",
                 "driverSha256": hashlib.sha256(f"driver{i}".encode()).hexdigest(),
                 "unit": None, "category": "ddl"}
                for i in (1, 2)
            ],
        }
        self.steps = [
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": [f"features/feature-{i}/APP_CODE/ddl.sql"]}
            for i in (1, 2)
        ]
        self.capsule_document["payloadDefinition"]["steps"] = self.steps
        self.manifest = rollout.RolloutManifest(
            self.manifest_path, self.root, self.manifest_sha, tuple(self.steps), 2, 120,
        )
        self.prepared_target_plan = self._target_plan("staging", 2, "1" * 64)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(self, steps=None) -> list[dict]:
        actual_steps = self.steps if steps is None else steps
        result = []
        for number, step in enumerate(actual_steps, 1):
            item = {
                "number": number,
                "type": step["type"],
                "step": step,
                "description": f"feature-migrate: {step.get('schema', 'APP_CODE')}",
                "sha256": hashlib.sha256(f"step{number}".encode()).hexdigest(),
                "inputHashes": [],
                "targetBinding": None,
                "appDeployConfig": None,
                "appDeployConfigSha256": None,
                "appDeploySourceSchemaSelector": None,
            }
            if step["type"] == "migrate":
                item["phase"] = "uncovered-object"
                item["classificationReason"] = rollout._auxiliary_classification_reason("migrate", "uncovered-object")
            result.append(item)
        return result

    def _target_plan(self, environment: str, checkpoint_count: int, plan_digest: str):
        database = environment.upper()
        identity = {
            "session_user": "APP_CODE", "current_schema": "APP_CODE",
            "db_name": database, "db_unique_name": f"{database}_UNIQUE",
            "service_name": f"{environment}.example.invalid", "container_id": "3",
            "container_name": f"{database}PDB", "edition": "ORA$BASE",
            "database_version": "19.0.0.0",
        }
        target = {
            "environment": environment, "connection": f"{environment}-conn",
            "expectedUser": "APP_CODE", "schema": "APP_CODE", "classification": "service",
            "observed": identity,
        }
        guards = tuple({
            "checkpoint": number, "schema": "APP_CODE", "targetKey": "schema:APP_CODE",
            "targetIdentity": identity,
        } for number in range(1, checkpoint_count + 1))
        return SimpleNamespace(
            environment=environment, payload_digest=self.payload, plan_digest=plan_digest,
            targets={"schema:APP_CODE": target}, checkpoint_guards=guards,
            recovery_attempt=None, recovery_binding=None,
        )

    def _patches(self, *, environment="staging", steps=None):
        from scripts import feature_release, release_capsule, release_state

        manifest = self.manifest
        if environment == "prod":
            manifest = rollout.RolloutManifest(
                self.manifest_path, self.root, self.manifest_sha, tuple(steps or self.steps), 2, 120,
            )
        actual_steps = self.steps if steps is None else steps
        self.capsule_document["payloadDefinition"]["steps"] = actual_steps
        plan = self._plan(actual_steps)
        target_plan = self._target_plan(
            environment, len(self.capsule.checkpoints),
            "1" * 64 if environment == "staging" else "2" * 64,
        )
        return (
            patch.object(release_capsule, "load_capsule", return_value=self.capsule),
            patch.object(release_capsule, "_read_capsule_json", return_value=(self.capsule_document, b"capsule")),
            patch.object(rollout, "load_manifest", return_value=manifest),
            patch.object(rollout, "_prepare_plan", return_value=plan),
            patch.object(feature_release, "load_target_plan", return_value=target_plan),
            patch.object(feature_release, "prepare_target", return_value=target_plan),
            patch.object(rollout, "_capsule_live_source_check"),
            patch.object(release_state, "_matching_capsules", return_value=[{
                "path": self.capsule.root.relative_to(self.root).as_posix(),
                "engineDigest": self.engine, "sourceCommit": self.source_commit,
            }]),
            patch.object(
                release_state, "_validate_completion_proof",
                side_effect=lambda _capsule, report, environment: report["completionProof"],
            ),
            patch.object(rollout, "_validate_capsule_app_targets"),
        )

    def _fake_checkpoint_result(
        self, args, kwargs, *, object_seconds: float, session_count: int = 2, message: str = "checkpoint verified",
    ):
        from scripts import release_ownership, release_state

        root, capsule, target_plan, checkpoint = args[:4]
        run_id = kwargs["run_id"]
        scope = release_state.target_scope_from_identity(
            target_plan.checkpoint_guards[checkpoint - 1]["targetIdentity"],
        )
        identity = {
            "runId": run_id, "environment": target_plan.environment,
            "payloadDigest": capsule.payload_digest, "engineDigest": capsule.engine_digest,
            "planDigest": target_plan.plan_digest, "checkpoint": checkpoint,
            "recoveryAttempt": target_plan.recovery_attempt,
        }
        evidence_path = root / "scratch" / "fake-checkpoint-evidence" / f"{run_id}-{checkpoint}.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps({"checkpoint": checkpoint, "verified": True}) + "\n", encoding="utf-8")
        evidence = [evidence_path.relative_to(root).as_posix()]
        with release_ownership.target_writer_lease(root, scope, identity) as lease:
            verification_id = release_state.record_checkpoint_verification(
                root, capsule, target_plan, checkpoint,
                run_id=run_id, attempt_id=None, lease=lease, evidence=evidence,
                metrics={"objectSeconds": object_seconds, "sessionCount": session_count},
            )
        return SimpleNamespace(
            exit_code=0, database_state="verified", object_seconds=object_seconds,
            session_count=session_count, evidence_paths=tuple(evidence), message=message,
            verification_id=verification_id,
        )

    def _enter(self, patches):
        self._patch_mocks = []
        for item in patches:
            self._patch_mocks.append(item.start())
            self.addCleanup(item.stop)

    def test_prepare_only_writes_schema_two_plan_report_without_confirmation_or_rollout(self) -> None:
        from scripts import feature_release

        report_base = self.root / "scratch" / "prepare-report"
        patches = self._patches()
        self._enter(patches)
        with patch.object(feature_release, "execute_checkpoint") as execute:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                prepare_only=True, report_path=report_base,
                confirm=lambda _prompt: self.fail("prepare-only must not request rollout confirmation"),
            )

        self.assertEqual(status, 0)
        execute.assert_not_called()
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["schemaVersion"], 2)
        self.assertEqual(report["status"], "prepared")
        self.assertEqual(report["targetPlanDigest"], self.prepared_target_plan.plan_digest)
        self.assertEqual(report["payloadDigest"], self.payload)

    def test_schema_two_feature_steps_do_not_require_legacy_target_binding(self) -> None:
        plan = rollout._prepare_plan(self.manifest, "staging")

        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["type"], "feature-migrate")
        self.assertEqual(plan[0]["description"], "feature-migrate: APP_CODE (1 frozen inputs)")
        self.assertIsNone(plan[0]["targetBinding"])
        self.assertEqual(plan[0]["inputHashes"], [])

    def test_schema_two_auxiliary_target_uses_migration_profile_binding(self) -> None:
        from unittest.mock import patch

        step = {"type": "sql-script", "file": "scripts/example.sql", "schema": "APP_CODE"}
        env = {
            "STAGING_MIGRATION_SCHEMA": "APP_CODE",
            "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-migration",
            "STAGING_MIGRATION_EXPECTED_USER": "APP_CODE",
        }
        with patch.dict("os.environ", env):
            binding = rollout._target_binding(step, self.root, "staging")

        self.assertEqual(binding, {
            "environment": "staging", "operation": "migration", "logicalSchema": "APP_CODE",
            "connection": "stage-migration", "expectedUser": "APP_CODE", "schema": "APP_CODE",
            "classification": "staging",
        })

    def test_capsule_dry_run_is_offline_and_does_not_require_a_prepared_target_plan(self) -> None:
        from scripts import feature_release

        patches = self._patches()
        self._enter(patches)
        report_base = self.root / "scratch" / "offline-dry-run"
        with patch.object(feature_release, "prepare_target") as prepare, \
             patch.object(feature_release, "load_target_plan") as load_plan:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                dry_run=True, report_path=report_base,
                confirm=lambda _prompt: self.fail("dry-run must not request confirmation"),
            )

        self.assertEqual(status, 0)
        prepare.assert_not_called()
        load_plan.assert_not_called()
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "planned")
        self.assertIsNone(report["targetPlanDigest"])
        self.assertEqual(report["databaseState"], "not-started")

    def test_rollout_cli_refuses_invalid_feature_flag_combinations_before_dispatch(self) -> None:
        with patch.object(rollout, "run_rollout") as dispatch:
            for argv in (
                ["releases/current.json", "--env", "staging", "--prepare"],
                ["releases/current.json", "--env", "staging", "--capsule", "scratch/capsule", "--recover", "attempt"],
                ["releases/current.json", "--env", "staging", "--capsule", "scratch/capsule", "--stage-report", "stage.json"],
            ):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    rollout.main(argv)
            dispatch.assert_not_called()

    def test_recovery_prepare_displays_reconciliation_prompt_and_binds_exact_ack(self) -> None:
        from scripts import release_state

        self._enter(self._patches())
        attempt_id = "12345678-1234-4234-8234-123456789abc"
        prompt_text = "scope and saved evidence; confirm the original SQLcl/database operation has ended"
        acknowledgments = []
        with patch.object(release_state, "recovery_prompt", return_value=prompt_text) as show:
            status = rollout.run_rollout(
                self.manifest_path,
                "staging",
                repo_root=self.root,
                capsule_path=self.capsule_path,
                prepare_only=True,
                recovery_attempt=attempt_id,
                recovery_confirm=lambda prompt: acknowledgments.append(prompt) or f"RECONCILE {attempt_id}",
            )

        self.assertEqual(status, 0)
        show.assert_called_once_with(self.root, self.capsule, "staging", attempt_id)
        self.assertEqual(acknowledgments, [prompt_text])
        prepare = self._patch_mocks[5]
        self.assertEqual(prepare.call_args.kwargs["terminal_ack"], f"RECONCILE {attempt_id}")

    def test_cli_recovery_adapter_reads_typed_acknowledgment(self) -> None:
        attempt_id = "12345678-1234-4234-8234-123456789abc"
        with patch("builtins.input", return_value=f"RECONCILE {attempt_id}") as read:
            value = rollout._recovery_ack_from_terminal("show scope/evidence and terminal-state assertion")

        self.assertEqual(value, f"RECONCILE {attempt_id}")
        read.assert_called_once_with("Reconciliation acknowledgment: ")

    def test_recovery_refuses_to_replay_verified_prior_write_steps(self) -> None:
        plan = self._plan([
            {"type": "sql-script", "schema": "APP_DATA"},
            self.steps[0],
            self.steps[1],
        ])
        target_plan = SimpleNamespace(recovery_binding={"checkpoint": 2})
        parent_run = {"steps": [
            {"step": 1, "status": "verified"},
            {"step": 2, "status": "verified"},
            {"step": 3, "status": "failed"},
        ]}

        with self.assertRaisesRegex(rollout.RolloutError, r"would replay previously verified writing step\(s\): 1"):
            rollout._assert_recovery_will_not_replay_prior_writes(plan, target_plan, parent_run)

    def test_recovery_refuses_to_replay_verified_prior_feature_checkpoint(self) -> None:
        plan = self._plan([self.steps[0], self.steps[1]])
        target_plan = SimpleNamespace(recovery_binding={"checkpoint": 2})
        parent_run = {"steps": [
            {"step": 1, "status": "verified"},
            {"step": 2, "status": "failed"},
        ]}

        with self.assertRaisesRegex(rollout.RolloutError, r"previously verified writing step\(s\): 1"):
            rollout._assert_recovery_will_not_replay_prior_writes(plan, target_plan, parent_run)

    def test_recovery_allows_prior_read_only_steps_and_requires_verified_writes(self) -> None:
        read_only_plan = self._plan([
            {"type": "verify", "phase": "pre"},
            self.steps[0],
        ])
        read_only_target_plan = SimpleNamespace(recovery_binding={"checkpoint": 1})
        read_only_parent_run = {"steps": [
            {"step": 1, "status": "verified"},
            {"step": 2, "status": "failed"},
        ]}
        rollout._assert_recovery_will_not_replay_prior_writes(
            read_only_plan, read_only_target_plan, read_only_parent_run,
        )

        write_plan = self._plan([
            {"type": "ords-import", "schema": "REST_API"},
            self.steps[0],
            self.steps[1],
        ])
        write_target_plan = SimpleNamespace(recovery_binding={"checkpoint": 2})
        write_parent_run = {"steps": [
            {"step": 1, "status": "unknown"},
            {"step": 2, "status": "verified"},
            {"step": 3, "status": "failed"},
        ]}
        with self.assertRaisesRegex(rollout.RolloutError, "does not have a verified receipt"):
            rollout._assert_recovery_will_not_replay_prior_writes(
                write_plan, write_target_plan, write_parent_run,
            )

    def test_recovery_accepts_a_parent_killed_before_recording_its_checkpoint_or_later_steps(self) -> None:
        # A rollout killed mid-step records no outcome for that step or later ones. Nothing
        # before the checkpoint ran, so there is nothing to replay and recovery may proceed.
        plan = self._plan([self.steps[0], self.steps[1]])
        target_plan = SimpleNamespace(recovery_binding={"checkpoint": 1})
        rollout._assert_recovery_will_not_replay_prior_writes(plan, target_plan, {"steps": []})
        rollout._assert_recovery_will_not_replay_prior_writes(
            plan, target_plan, {"steps": [{"step": 1, "status": "unknown"}]},
        )

    def test_recovery_refuses_an_earlier_write_step_with_no_recorded_outcome(self) -> None:
        plan = self._plan([
            {"type": "sql-script", "schema": "APP_DATA"},
            self.steps[0],
            self.steps[1],
        ])
        with self.assertRaisesRegex(rollout.RolloutError, "no recorded outcome for earlier writing step 1"):
            rollout._assert_recovery_will_not_replay_prior_writes(
                plan, SimpleNamespace(recovery_binding={"checkpoint": 2}), {"steps": []},
            )

    def test_recovery_refuses_a_parent_with_more_outcomes_than_the_frozen_plan(self) -> None:
        plan = self._plan([self.steps[0]])
        with self.assertRaisesRegex(rollout.RolloutError, "more step outcomes than the frozen plan"):
            rollout._assert_recovery_will_not_replay_prior_writes(
                plan, SimpleNamespace(recovery_binding={"checkpoint": 1}),
                {"steps": [{"step": 1, "status": "unknown"}, {"step": 2, "status": "unknown"}]},
            )

    def test_capsule_recovery_refuses_prior_write_before_confirmation(self) -> None:
        from scripts import release_state

        steps = [
            {"type": "sql-script", "schema": "APP_DATA"},
            self.steps[0],
            self.steps[1],
        ]
        self._enter(self._patches(steps=steps))
        attempt_id = "12345678-1234-4234-8234-123456789abc"
        recovery_plan = self._target_plan("staging", 2, "2" * 64)
        recovery_plan.recovery_attempt = attempt_id
        recovery_plan.recovery_binding = {"checkpoint": 2}
        self._patch_mocks[4].return_value = recovery_plan
        parent_run = {"steps": [
            {"step": 1, "status": "verified"},
            {"step": 2, "status": "verified"},
            {"step": 3, "status": "failed"},
        ]}
        confirmations = []

        with patch.object(release_state, "recovery_parent_rollout", return_value=parent_run) as parent, \
             patch.object(rollout, "_validate_capsule_auxiliary_targets"):
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                confirm=lambda prompt: confirmations.append(prompt) or True,
            )

        self.assertEqual(status, 2)
        parent.assert_called_once()
        self.assertEqual(confirmations, [])

    def test_prod_refuses_without_stage_proof_before_checkpoint_execution(self) -> None:
        from scripts import feature_release

        patches = self._patches(environment="prod")
        self._enter(patches)
        with patch.object(feature_release, "execute_checkpoint") as execute:
            status = rollout.run_rollout(
                self.manifest_path, "prod", repo_root=self.root, capsule_path=self.capsule_path,
                confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        execute.assert_not_called()

    def test_rollout_binds_every_checkpoint_to_the_durable_run_id(self) -> None:
        from scripts import feature_release

        self._enter(self._patches())
        result = SimpleNamespace(
            exit_code=2, database_state="not-started", object_seconds=0.0,
            session_count=0, evidence_paths=(), message="stop after proving run binding",
        )
        with patch.object(feature_release, "execute_checkpoint", return_value=result) as execute:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=self.root / "scratch" / "run-binding-report", confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        self.assertEqual(execute.call_count, 1)
        self.assertIn("run_id", execute.call_args.kwargs)
        run_id = execute.call_args.kwargs["run_id"]
        report = json.loads((self.root / "scratch" / "run-binding-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["localRunId"], run_id)

    def test_staging_and_production_run_feature_data_and_ords_phases_on_prepared_targets(self) -> None:
        from scripts import feature_release, release_state

        data_source = (
            b"INSERT INTO APP_CODE.T (ID) VALUES (1);\n"
            b"UPDATE APP_CODE.T SET ID = 2;\n"
            b"DELETE FROM APP_CODE.T WHERE ID = 2;\n"
            b"MERGE INTO APP_CODE.T d USING APP_CODE.S s ON (d.ID = s.ID) "
            b"WHEN MATCHED THEN UPDATE SET d.ID = s.ID;\nCOMMIT;\n"
        )
        ords_source = (
            b"-- Generated by SQLcl\n-- Schema: REST_OWNER\n"
            b"BEGIN\n  ORDS.ENABLE_SCHEMA(p_schema => 'REST_OWNER');\n  COMMIT;\nEND;\n/\n"
        )
        data_path = self.root / "scripts" / "data.sql"
        ords_path = self.root / "database" / "REST_OWNER" / "ords" / "schema.sql"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        ords_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_bytes(data_source)
        ords_path.write_bytes(ords_source)
        steps = [
            dict(self.steps[0]),
            {"type": "sql-script", "file": "scripts/data.sql", "schema": "APP_CODE"},
            {"type": "ords-import", "file": "database/REST_OWNER/ords/schema.sql", "schema": "REST_OWNER", "excludeModules": []},
            dict(self.steps[1]),
        ]
        data_hash = hashlib.sha256(data_source).hexdigest()
        ords_hash = hashlib.sha256(ords_source).hexdigest()
        identity_by_connection = {}

        def target_record(environment, number, schema, connection, expected_user):
            db_name = "STAGE" if environment == "staging" else "LIVE"
            identity = {
                "session_user": expected_user, "current_schema": schema,
                "db_name": db_name, "db_unique_name": f"{db_name}_UNIQUE",
                "service_name": f"{environment}.example.invalid",
                "container_id": "3" if environment == "staging" else "7",
                "container_name": f"{db_name}PDB", "edition": "ORA$BASE",
                "database_version": "19.0.0.0",
            }
            identity_by_connection[connection] = identity
            return {
                "environment": environment, "operation": "migration",
                "logicalSchema": schema, "connection": connection,
                "expectedUser": expected_user, "schema": schema,
                "classification": "staging" if environment == "staging" else "production",
                "observed": identity,
            }

        for environment in ("staging", "prod"):
            profile_prefix = "STAGING" if environment == "staging" else "PROD"
            data_alias = f"{environment}-data-alias"
            ords_alias = f"{environment}-ords-alias"
            data_user = "STAGE_WRITER" if environment == "staging" else "PROD_WRITER"
            environ = {
                f"{profile_prefix}_MIGRATION_SCHEMA": "APP_CODE,REST_OWNER",
                f"{profile_prefix}_MIGRATION_SQLCL_CONNECTION": f"{data_alias},{ords_alias}",
                f"{profile_prefix}_MIGRATION_EXPECTED_USER": f"{data_user},REST_OWNER",
            }
            manifest = rollout.RolloutManifest(
                self.manifest_path, self.root, self.manifest_sha, tuple(steps), 2, 120,
            )
            report_base = self.root / "scratch" / f"{environment}-phase-report"
            if environment == "prod":
                stage_report_path = self.root / "scratch" / "staging-phase-report.json"
            else:
                stage_report_path = None

            patchers = list(self._patches(environment=environment, steps=steps))
            patchers.extend((
                patch.dict("os.environ", environ, clear=True),
                patch.object(rollout, "_snapshot"),
                patch.object(rollout, "_observe_target_identity", side_effect=lambda target, _dir: identity_by_connection[target.connection]),
                patch.object(release_state, "validate_stage_proof"),
            ))
            with ExitStack() as stack:
                mocks = [stack.enter_context(patcher) for patcher in patchers]
                mocks[2].return_value = manifest
                plan = mocks[3].return_value
                for item in plan:
                    if item["number"] == 2:
                        item["phase"] = rollout.classify_auxiliary_step(steps[1], data_source)
                        item["classificationReason"] = rollout._auxiliary_classification_reason("sql-script", item["phase"])
                        item["inputHashes"] = [{"path": "scripts/data.sql", "sha256": data_hash}]
                        item["sha256"] = rollout._canonical_hash({"step": item["step"], "inputHashes": item["inputHashes"]})
                    elif item["number"] == 3:
                        item["phase"] = rollout.classify_auxiliary_step(steps[2], ords_source)
                        item["classificationReason"] = rollout._auxiliary_classification_reason("ords-import", item["phase"])
                        item["inputHashes"] = [{"path": "database/REST_OWNER/ords/schema.sql", "sha256": ords_hash}]
                        item["sha256"] = rollout._canonical_hash({"step": item["step"], "inputHashes": item["inputHashes"]})
                target_plan = mocks[4].return_value
                target_plan.targets["step:2"] = target_record(environment, 2, "APP_CODE", data_alias, data_user)
                target_plan.targets["step:3"] = target_record(environment, 3, "REST_OWNER", ords_alias, "REST_OWNER")
                mocks[-1].return_value = None
                execute_mock = stack.enter_context(
                    patch.object(rollout, "execute_step", return_value=rollout.StepResult(0, message="verified")),
                )
                checkpoint = stack.enter_context(patch.object(
                    feature_release, "execute_checkpoint",
                    side_effect=lambda *args, **kwargs: self._fake_checkpoint_result(
                        args, kwargs, object_seconds=0.25, session_count=2,
                    ),
                ))
                status = rollout.run_rollout(
                    self.manifest_path, environment, repo_root=self.root,
                    capsule_path=self.capsule_path, report_path=report_base,
                    stage_report_path=stage_report_path,
                    confirm=lambda _prompt: True,
                )

            self.assertEqual(status, 0)
            report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["objectBudget"]["includedCheckpoints"], [1, 2])
            self.assertEqual(report["objectBudget"]["excludedObjectSteps"], [])
            self.assertTrue(report["objectBudget"]["coverageComplete"])
            self.assertEqual([row["phase"] for row in (report["steps"][1], report["steps"][2])], ["data", "ords"])
            self.assertAlmostEqual(report["dataSeconds"], report["steps"][1]["phaseSeconds"], places=3)
            self.assertAlmostEqual(report["ordsSeconds"], report["steps"][2]["phaseSeconds"], places=3)
            self.assertEqual(
                [call.args[0]["connection"] for call in execute_mock.call_args_list],
                [data_alias, ords_alias],
            )
            self.assertEqual(checkpoint.call_count, 2)

    def test_budget_boundary_stops_before_second_feature_checkpoint(self) -> None:
        from scripts import feature_release

        patches = self._patches()
        self._enter(patches)
        clock = [0.0]

        def fake_checkpoint(*_args, **_kwargs):
            started = clock[0]
            clock[0] += 120.0
            return self._fake_checkpoint_result(
                _args, _kwargs, object_seconds=clock[0] - started, session_count=2,
                message="verified at the budget boundary",
            )

        report_base = self.root / "scratch" / "budget-report"
        with patch.object(rollout.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(feature_release, "execute_checkpoint", side_effect=fake_checkpoint) as execute:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        self.assertEqual(execute.call_count, 1)
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["databaseState"], "verified")
        self.assertEqual(report["objectBudget"]["elapsedSeconds"], 120.0)
        self.assertEqual(report["objectBudget"]["includedCheckpoints"], [1])
        self.assertEqual(report["checkpointSqlclSessions"], {"1": 2})
        self.assertEqual(report["steps"][1]["status"], "not-run")

    def test_verified_300_second_overrun_is_reported_without_losing_verified_state(self) -> None:
        from scripts import feature_release

        self.capsule = ReleaseCapsule(
            self.capsule.root, self.capsule.manifest_sha256, self.capsule.payload_digest,
            self.capsule.engine_digest, self.capsule.source_commit, (self.checkpoints[0],),
            self.capsule.input_hashes[:2], 300,
        )
        self.steps = [self.steps[0]]
        patches = self._patches(steps=self.steps)
        self._enter(patches)
        report_base = self.root / "scratch" / "overrun-report"
        clock = [0.0]

        def fake_checkpoint(*_args, **_kwargs):
            started = clock[0]
            clock[0] += 301.0
            return self._fake_checkpoint_result(
                _args, _kwargs, object_seconds=clock[0] - started, session_count=2,
                message="database state verified",
            )

        with patch.object(rollout.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(feature_release, "execute_checkpoint", side_effect=fake_checkpoint) as execute, \
             patch.object(rollout, "_report_outputs_do_not_overlap"), \
             patch.object(rollout, "_snapshot"):
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        execute.assert_called_once()
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["databaseState"], "verified")
        self.assertEqual(report["objectBudget"]["seconds"], 300)
        self.assertEqual(report["objectBudget"]["elapsedSeconds"], 301.0)
        self.assertFalse(report["objectBudget"]["withinBudget"])
        self.assertEqual(report["applicationSeconds"], 0.0)

    def test_app_import_duration_is_reported_outside_database_object_budget(self) -> None:
        from scripts import feature_release

        self.capsule = ReleaseCapsule(
            self.capsule.root, self.capsule.manifest_sha256, self.capsule.payload_digest,
            self.capsule.engine_digest, self.capsule.source_commit, (self.checkpoints[0],),
            self.capsule.input_hashes[:2], 120,
        )
        self.steps = [self.steps[0], {"type": "app-deploy", "appId": 100}]
        patches = self._patches(steps=self.steps)
        self._enter(patches)
        report_base = self.root / "scratch" / "app-duration-report"
        clock = [0.0]

        def fake_checkpoint(*_args, **_kwargs):
            started = clock[0]
            clock[0] += 100.0
            return self._fake_checkpoint_result(
                _args, _kwargs, object_seconds=clock[0] - started, session_count=2,
                message="database state verified",
            )

        def fake_app(*_args, **_kwargs):
            clock[0] += 37.0
            return rollout.StepResult(0, (), "app imported")

        with patch.object(rollout.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(feature_release, "execute_checkpoint", side_effect=fake_checkpoint), \
             patch.object(rollout, "_report_outputs_do_not_overlap"), \
             patch.object(rollout, "_snapshot"), \
             patch.object(rollout, "execute_step", side_effect=fake_app) as app:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 0)
        app.assert_called_once()
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["objectBudget"]["elapsedSeconds"], 100.0)
        self.assertTrue(report["objectBudget"]["withinBudget"])
        self.assertEqual(report["applicationSeconds"], 37.0)
        app_receipt = json.loads((self.root / report["steps"][1]["receiptPath"]).read_text(encoding="utf-8"))
        self.assertEqual(app_receipt["schemaVersion"], 1)
        self.assertNotIn("targetBinding", app_receipt)
        self.assertNotIn("observedIdentity", app_receipt)

    def test_legacy_database_step_is_reported_outside_checkpoint_coverage(self) -> None:
        from scripts import feature_release

        self.capsule = ReleaseCapsule(
            self.capsule.root, self.capsule.manifest_sha256, self.capsule.payload_digest,
            self.capsule.engine_digest, self.capsule.source_commit, (self.checkpoints[0],),
            self.capsule.input_hashes[:2], 120,
        )
        self.steps = [self.steps[0], {"type": "migrate", "folders": ["migrations/APP_CODE/2026-01-01_legacy-r001"]}]
        patches = self._patches(steps=self.steps)
        self._enter(patches)
        clock = [0.0]

        def fake_checkpoint(*_args, **_kwargs):
            started = clock[0]
            clock[0] += 10.0
            return self._fake_checkpoint_result(
                _args, _kwargs, object_seconds=clock[0] - started, session_count=3,
                message="checkpoint verified",
            )

        report_base = self.root / "scratch" / "legacy-step-report"
        with patch.object(rollout.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(feature_release, "execute_checkpoint", side_effect=fake_checkpoint), \
             patch.object(rollout, "_snapshot"), \
             patch.object(rollout, "execute_step", return_value=rollout.StepResult(0, (), "legacy script complete")):
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True,
            )

        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(status, 2, report.get("error"))
        self.assertFalse(report["objectBudget"]["coverageComplete"])
        self.assertEqual(report["objectBudget"]["excludedObjectSteps"], ["step-002:migrate"])

    def test_capsule_tampering_after_confirmation_stops_before_checkpoint_write(self) -> None:
        from scripts import feature_release, release_capsule

        changed = SimpleNamespace(**vars(self.capsule))
        object.__setattr__(changed, "engine_digest", "e" * 64)
        patches = list(self._patches())
        patches[0] = patch.object(
            release_capsule, "load_capsule", side_effect=[self.capsule, self.capsule, changed],
        )
        self._enter(patches)
        with patch.object(feature_release, "execute_checkpoint") as execute:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        execute.assert_not_called()

    def test_capsule_tampering_after_checkpoint_blocks_success_receipt(self) -> None:
        from scripts import feature_release, release_capsule

        changed = SimpleNamespace(**vars(self.capsule))
        object.__setattr__(changed, "payload_digest", "f" * 64)
        patches = list(self._patches())
        patches[0] = patch.object(
            release_capsule, "load_capsule",
            side_effect=[self.capsule, self.capsule, self.capsule, self.capsule, changed],
        )
        self._enter(patches)
        report_base = self.root / "scratch" / "capsule-tamper-report"
        success = SimpleNamespace(
            exit_code=0, database_state="verified", object_seconds=2.0, session_count=3,
            evidence_paths=(), message="checkpoint verified",
        )
        with patch.object(feature_release, "execute_checkpoint", return_value=success), \
             patch.object(rollout, "_write_receipt") as write_receipt:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        write_receipt.assert_not_called()
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")

    def test_failed_checkpoint_never_writes_success_receipt(self) -> None:
        from scripts import feature_release

        patches = self._patches()
        self._enter(patches)
        failure = SimpleNamespace(
            exit_code=2, database_state="unknown", object_seconds=2.0, session_count=3,
            evidence_paths=(), message="postcondition failed",
        )
        report_base = self.root / "scratch" / "failed-checkpoint-report"
        with patch.object(feature_release, "execute_checkpoint", return_value=failure), \
             patch.object(rollout, "_write_receipt") as write_receipt:
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        write_receipt.assert_not_called()
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["databaseState"], "unknown")
        self.assertEqual(report["steps"][0]["status"], "failed")

    def test_verified_staging_proof_is_required_and_production_uses_prepared_prod_plan(self) -> None:
        from scripts import feature_release, release_state

        stage_path = self.root / "scratch" / "staging.json"
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text('{"status":"success"}\n', encoding="utf-8")
        patches = self._patches(environment="prod")
        self._enter(patches)
        def fake_checkpoint(*args, **kwargs):
            return self._fake_checkpoint_result(
                args, kwargs, object_seconds=1.0, session_count=3,
                message="production checkpoint verified",
            )
        with patch.object(release_state, "validate_stage_proof") as validate, \
             patch.object(feature_release, "execute_checkpoint", side_effect=fake_checkpoint) as execute:
            status = rollout.run_rollout(
                self.manifest_path, "prod", repo_root=self.root, capsule_path=self.capsule_path,
                stage_report_path=stage_path,
                confirm=lambda prompt: "Capsule payload SHA-256" in prompt and "Target plan SHA-256" in prompt,
            )

        self.assertEqual(status, 0)
        validate.assert_called_once_with(self.capsule, {"status": "success"})
        self.assertEqual(execute.call_args.args[2].environment, "prod")
        self.assertEqual(self._patch_mocks[-1].call_args.args[3], "prod")


if __name__ == "__main__":
    unittest.main()
