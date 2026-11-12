from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import rollout, rollout_capsule
from scripts.db_targets import Target
from scripts.feature_manifest import FeatureCheckpoint
from scripts.release_capsule import ReleaseCapsule

MIGRATION_FOLDER = "migrations/APP_CODE/2026-01-01_legacy-r001"
MIGRATION_PAYLOAD = hashlib.sha256(b"legacy migration payload").hexdigest()
MIGRATION_CHECKS = hashlib.sha256(b"legacy migration checks").hexdigest()


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
                item["phase"] = "migration"
                item["classificationReason"] = rollout._auxiliary_classification_reason("migrate", "migration")
            result.append(item)
        return result

    def _migration_target(self, environment: str, *, connection: str | None = None, payload: str = MIGRATION_PAYLOAD):
        """What the live migration target resolution returns for a schema-scoped migration folder."""
        target = Target(
            environment, connection or f"{environment}-conn", "APP_CODE", "APP_CODE",
            "production" if environment == "prod" else "staging",
        )
        migration = SimpleNamespace(payload_digest=payload, checks_sha256=MIGRATION_CHECKS, schema="APP_CODE")
        return ("APP_CODE", target, (migration,))

    def _migration_binding(self, target_plan, folder: str = MIGRATION_FOLDER) -> dict:
        """The prepared binding a migrate step carries in the target plan."""
        identity = target_plan.targets["schema:APP_CODE"]["observed"]
        return {
            "environment": target_plan.environment,
            "operation": "migration",
            "logicalSchema": "APP_CODE",
            "connection": f"{target_plan.environment}-conn",
            "expectedUser": "APP_CODE",
            "schema": "APP_CODE",
            "classification": "production" if target_plan.environment == "prod" else "staging",
            "observed": identity,
            "folders": [{"path": folder, "payloadDigest": MIGRATION_PAYLOAD, "checksSha256": MIGRATION_CHECKS}],
            "preflight": {"state": "clear", "exitCode": 0, "conflicts": [], "errors": [], "reportSha256": "3" * 64},
        }

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
        for number, step in enumerate(actual_steps, start=1):
            if step["type"] == "migrate":
                target_plan.targets[f"step:{number}"] = self._migration_binding(target_plan, step["folders"][0])
        return (
            patch.object(release_capsule, "load_capsule", return_value=self.capsule),
            patch.object(release_capsule, "_read_capsule_json", return_value=(self.capsule_document, b"capsule")),
            patch.object(rollout, "load_manifest", return_value=manifest),
            patch.object(rollout, "_prepare_plan", return_value=plan),
            patch.object(feature_release, "load_target_plan", return_value=target_plan),
            patch.object(feature_release, "prepare_target", return_value=target_plan),
            patch.object(rollout_capsule, "_capsule_live_source_check"),
            patch.object(release_state, "_matching_capsules", return_value=[{
                "path": self.capsule.root.relative_to(self.root).as_posix(),
                "engineDigest": self.engine, "sourceCommit": self.source_commit,
            }]),
            patch.object(
                release_state, "_validate_completion_proof",
                side_effect=lambda _capsule, report, environment: report["completionProof"],
            ),
            patch.object(rollout_capsule, "_validate_capsule_app_targets"),
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

    def test_a_version_one_migrate_binding_keeps_its_shape_and_only_a_capsule_adds_the_logical_schema(self) -> None:
        from unittest.mock import patch

        # A schema-version 1 rollout hashes this binding into its step digest and receipts, so it must not change.
        step = {"type": "migrate", "folders": ["migrations/2026-05-10_example-r001"]}
        target = rollout.Target("staging", "stage-migration", "APP_CODE", "APP_CODE", "staging")
        with patch.object(rollout, "_target_for_step", return_value=target), \
             patch.object(rollout, "_migration_step_target", return_value=("APP_CODE", target, ())):
            legacy = rollout._target_binding(step, self.root, "staging")
            capsule = rollout._target_binding(step, self.root, "staging", capsule=True)

        self.assertEqual(legacy, {
            "environment": "staging", "connection": "stage-migration", "expectedUser": "APP_CODE",
            "schema": "APP_CODE", "classification": "staging",
        })
        self.assertEqual(capsule, {**legacy, "operation": "migration", "logicalSchema": "APP_CODE"})

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
        with patch("scripts.terminal_confirm.stdin_is_terminal", return_value=True), patch(
            "builtins.input", return_value=f"RECONCILE {attempt_id}"
        ) as read:
            value = rollout._recovery_ack_from_terminal("show scope/evidence and terminal-state assertion")

        self.assertEqual(value, f"RECONCILE {attempt_id}")
        read.assert_called_once_with("Reconciliation acknowledgment: ")

    def test_cli_recovery_adapter_refuses_redirected_input_unless_the_test_switch_is_set(self) -> None:
        attempt_id = "12345678-1234-4234-8234-123456789abc"
        stderr = io.StringIO()
        with patch("scripts.terminal_confirm.stdin_is_terminal", return_value=False), patch(
            "builtins.input", side_effect=AssertionError("a redirected acknowledgment must not be read")
        ), patch.dict(os.environ, {}, clear=False), contextlib.redirect_stderr(stderr):
            os.environ.pop("TEAM_ACK_FROM_STDIN", None)
            value = rollout._recovery_ack_from_terminal("show scope/evidence and terminal-state assertion")
        self.assertEqual(value, "")
        self.assertIn("redirected input is not accepted", stderr.getvalue())

        with patch("scripts.terminal_confirm.stdin_is_terminal", return_value=False), patch.dict(os.environ, {"TEAM_ACK_FROM_STDIN": "1"}), patch(
            "builtins.input", return_value=f"RECONCILE {attempt_id}"
        ), contextlib.redirect_stderr(io.StringIO()) as notice:
            value = rollout._recovery_ack_from_terminal("show scope/evidence and terminal-state assertion")
        self.assertEqual(value, f"RECONCILE {attempt_id}")
        self.assertIn("acknowledgment read from redirected input (TEAM_ACK_FROM_STDIN)", notice.getvalue())

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
             patch.object(rollout_capsule, "_validate_capsule_auxiliary_targets"):
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

    def test_capsule_yes_alone_is_refused_for_production_before_any_checkpoint(self) -> None:
        from scripts import feature_release

        stderr = io.StringIO()
        with patch.object(feature_release, "execute_checkpoint") as execute, contextlib.redirect_stderr(stderr):
            status = rollout.run_rollout(
                self.manifest_path, "prod", repo_root=self.root, capsule_path=self.capsule_path,
                yes=True, confirm=lambda _prompt: self.fail("no prompt"),
            )

        self.assertEqual(status, 2)
        execute.assert_not_called()
        self.assertIn("production also needs --confirm-target <db_unique_name>", stderr.getvalue())

    def test_capsule_staging_yes_runs_without_a_prompt_and_records_it(self) -> None:
        from scripts import feature_release

        self._enter(self._patches(environment="staging"))
        report_base = self.root / "scratch" / "staging-yes"
        output = io.StringIO()
        with patch.object(feature_release, "execute_checkpoint", side_effect=RuntimeError("stop before the first checkpoint")) as execute, \
                contextlib.redirect_stdout(output), patch("builtins.input", side_effect=AssertionError("-y must not read stdin")):
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, yes=True, confirm=lambda _prompt: self.fail("no prompt"),
            )

        self.assertEqual(status, 2)
        self.assertTrue(execute.called, "the flag must lead to the first checkpoint")
        self.assertIn("Confirmed by --yes; the interactive prompt was skipped.", output.getvalue())
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["confirmedBy"], "--yes")

    def test_capsule_production_name_mismatch_refuses_before_any_checkpoint(self) -> None:
        from scripts import feature_release, release_state

        self._enter(self._patches(environment="prod"))
        stage_report = self.root / "scratch" / "stage-report.json"
        stage_report.parent.mkdir(parents=True, exist_ok=True)
        stage_report.write_text("{}\n", encoding="utf-8")
        observed = {field: "observed" for field in rollout.OBSERVED_IDENTITY_FIELDS} | {"db_unique_name": "OTHER_UNIQUE"}
        stderr = io.StringIO()
        with patch.object(release_state, "validate_stage_proof"), patch.object(rollout, "_observe_target_identity", return_value=observed), \
                patch.object(feature_release, "execute_checkpoint") as execute, contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            status = rollout.run_rollout(
                self.manifest_path, "prod", repo_root=self.root, capsule_path=self.capsule_path, stage_report_path=stage_report,
                yes=True, confirm_target="PRODDB_UNIQUE", confirm=lambda _prompt: self.fail("no prompt"),
            )

        self.assertEqual(status, 2)
        execute.assert_not_called()
        self.assertIn("does not match the observed db_unique_name 'OTHER_UNIQUE'", stderr.getvalue())

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

    def _observed_identity(self, environment: str = "staging") -> dict:
        return self._target_plan(environment, 1, "1" * 64).targets["schema:APP_CODE"]["observed"]

    def _migration_release_patches(self, *, target=None, observed=None, environment: str = "staging") -> list:
        return [
            patch.object(rollout, "_migration_step_target", return_value=target or self._migration_target(environment)),
            patch.object(rollout, "_observe_target_identity", return_value=observed or self._observed_identity(environment)),
        ]

    def _run_migration_release(
        self, extra_patches, *, migrate_seconds: float = 25.0, budget_seconds: int = 120, label: str = "release",
        migration_only: bool = False, environment: str = "staging", stage_report_path=None, migrate_effect=None,
    ):
        from scripts import feature_release

        (self.root / MIGRATION_FOLDER).mkdir(parents=True, exist_ok=True)

        if migration_only:
            checkpoints = ()
            self.steps = [{"type": "migrate", "folders": [MIGRATION_FOLDER]}]
        else:
            checkpoints = (self.checkpoints[0],)
            self.steps = [self.steps[0], {"type": "migrate", "folders": [MIGRATION_FOLDER]}]
        self.capsule = ReleaseCapsule(
            self.capsule.root, self.capsule.manifest_sha256, self.capsule.payload_digest,
            self.capsule.engine_digest, self.capsule.source_commit, checkpoints,
            self.capsule.input_hashes[:2], budget_seconds,
        )
        self._enter(list(self._patches(environment=environment, steps=self.steps)) + list(extra_patches))
        clock = [0.0]

        def fake_checkpoint(*args, **kwargs):
            started = clock[0]
            clock[0] += 10.0
            return self._fake_checkpoint_result(
                args, kwargs, object_seconds=clock[0] - started, session_count=3, message="checkpoint verified",
            )

        def fake_migrate(*args, **kwargs):
            clock[0] += migrate_seconds
            if migrate_effect is not None:
                return migrate_effect(*args, **kwargs)
            return rollout.StepResult(0, (), "migration applied")

        report_base = self.root / "scratch" / f"{label}-report"
        with patch.object(rollout.time, "perf_counter", side_effect=lambda: clock[0]), \
             patch.object(feature_release, "execute_checkpoint", side_effect=fake_checkpoint), \
             patch.object(rollout, "_snapshot"), \
             patch.object(rollout, "execute_step", side_effect=fake_migrate) as migrate:
            status = rollout.run_rollout(
                self.manifest_path, environment, repo_root=self.root, capsule_path=self.capsule_path,
                report_path=report_base, confirm=lambda _prompt: True, stage_report_path=stage_report_path,
            )
        # A refusal before confirmation raises before any report file is written.
        report_path = report_base.with_suffix(".json")
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
        return status, report, migrate

    def test_prepared_migration_step_is_a_covered_checkpoint_in_a_verified_release(self) -> None:
        status, report, migrate = self._run_migration_release(self._migration_release_patches(), label="covered-migration")

        self.assertEqual(status, 0, report.get("error"))
        migrate.assert_called_once()
        self.assertNotIn("connection", migrate.call_args.args[0])
        budget = report["objectBudget"]
        self.assertTrue(budget["coverageComplete"])
        self.assertEqual(budget["includedCheckpoints"], [1])
        self.assertEqual(budget["includedMigrationSteps"], [2])
        self.assertEqual(budget["excludedObjectSteps"], [])
        self.assertEqual(report["objectPhaseSeconds"], 35.0)
        self.assertEqual(report["steps"][1]["phase"], "migration")
        self.assertEqual(report["steps"][1]["objectSeconds"], 25.0)

    def test_migration_step_stops_before_the_write_when_profile_identity_or_folders_changed(self) -> None:
        cases = (
            (
                "profile", "migration profile changed after target preparation",
                self._migration_release_patches(target=self._migration_target("staging", connection="other-conn")),
            ),
            (
                "identity", "target identity changed after preparation",
                self._migration_release_patches(observed=dict(self._observed_identity(), db_unique_name="OTHER_UNIQUE")),
            ),
            (
                "folder digest", "migration folders changed after target preparation",
                self._migration_release_patches(target=self._migration_target("staging", payload="f" * 64)),
            ),
        )
        for label, expected, patches in cases:
            with self.subTest(label=label):
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured):
                    status, report, migrate = self._run_migration_release(patches, label=f"changed-{label.replace(' ', '-')}")
                self.assertEqual(status, 2)
                migrate.assert_not_called()
                refusal = captured.getvalue() + (report or {}).get("error", "")
                self.assertIn(expected, refusal)

    def test_migration_step_over_budget_fails_with_verified_database_state(self) -> None:
        status, report, migrate = self._run_migration_release(
            self._migration_release_patches(), migrate_seconds=200.0, label="migration-over-budget",
        )

        self.assertEqual(status, 2)
        migrate.assert_called_once()
        self.assertEqual(report["databaseState"], "verified")
        self.assertIn("after a verified feature or migration checkpoint", report["error"])
        self.assertEqual(report["objectBudget"]["includedMigrationSteps"], [2])
        self.assertFalse(report["objectBudget"]["withinBudget"])

    def test_migration_only_release_runs_staging_as_covered_migration_checkpoints(self) -> None:
        status, report, migrate = self._run_migration_release(
            self._migration_release_patches(), migration_only=True, label="migration-only-staging",
        )

        self.assertEqual(status, 0, report.get("error"))
        migrate.assert_called_once()
        budget = report["objectBudget"]
        self.assertTrue(budget["coverageComplete"])
        self.assertEqual(budget["includedCheckpoints"], [])
        self.assertEqual(budget["includedMigrationSteps"], [1])
        self.assertEqual(report["objectPhaseSeconds"], 25.0)

    def test_migration_only_release_reaches_production_only_with_a_stage_proof(self) -> None:
        from scripts import release_state

        stage_path = self.root / "scratch" / "migration-only-staging.json"
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text('{"status":"success"}\n', encoding="utf-8")
        status, report, _migrate = self._run_migration_release(
            self._migration_release_patches(environment="prod") + [patch.object(release_state, "validate_stage_proof")],
            migration_only=True, environment="prod", stage_report_path=stage_path, label="migration-only-prod",
        )
        self.assertEqual(status, 0, report.get("error"))
        self.assertEqual(report["objectBudget"]["includedMigrationSteps"], [1])

        status, _report, migrate = self._run_migration_release(
            self._migration_release_patches(environment="prod"), migration_only=True, environment="prod",
            stage_report_path=None, label="migration-only-prod-unstaged",
        )
        self.assertEqual(status, 2)
        migrate.assert_not_called()

    def test_recovery_is_refused_for_a_capsule_with_no_feature_checkpoint(self) -> None:
        from scripts import feature_release

        self.capsule = ReleaseCapsule(
            self.capsule.root, self.capsule.manifest_sha256, self.capsule.payload_digest,
            self.capsule.engine_digest, self.capsule.source_commit, (), self.capsule.input_hashes[:2], 120,
        )
        self.steps = [{"type": "migrate", "folders": [MIGRATION_FOLDER]}]
        self._enter(list(self._patches(steps=self.steps)) + self._migration_release_patches())
        captured = io.StringIO()
        with patch.object(feature_release, "prepare_target") as prepare, contextlib.redirect_stderr(captured):
            status = rollout.run_rollout(
                self.manifest_path, "staging", repo_root=self.root, capsule_path=self.capsule_path,
                prepare_only=True, recovery_attempt="attempt-1", recovery_confirm=lambda _prompt: "RECONCILE attempt-1",
            )

        self.assertEqual(status, 2)
        prepare.assert_not_called()
        self.assertIn("--recover is refused", captured.getvalue())
        self.assertIn("docs/migration-rules.md", captured.getvalue())

    def _attempt_event(self, state: str, attempt_id: str, predecessor: str | None = None, evidence=None) -> dict:
        """A schema-valid attempt event for the migration folder, as migrate writes it."""
        import uuid
        from datetime import datetime, timezone

        return {
            "schemaVersion": 1,
            "eventId": str(uuid.uuid4()),
            "attemptId": attempt_id,
            "runId": "migration-attempt-rollout-test",
            "predecessorEventId": predecessor,
            "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "migrationPath": MIGRATION_FOLDER,
            "environment": "staging",
            "target": {
                "environment": "staging", "connection": "stage-data-alias",
                "expected_user": "APP_CODE", "current_schema": "APP_CODE",
            },
            "identity": None,
            "files": [{"name": "001-create-legacy.sql", "sequence": 1, "sha256": "a" * 64}],
            "checksSha256": "b" * 64,
            "payloadDigest": MIGRATION_PAYLOAD,
            "state": state,
            "writeAttempted": state != "apply-not-started",
            "evidence": evidence or [],
        }

    def _install_attempt_event(self, event: dict) -> None:
        path = self.root / MIGRATION_FOLDER / f"attempt-event.{event['eventId']}.json"
        path.write_text(json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def _start_again_on_the_same_target(self) -> str:
        """Start a later migration-only run on the same physical target, as a new checkout run would."""
        from scripts import release_state

        plan = self._target_plan("staging", 0, "3" * 64)
        plan.targets["step:1"] = self._migration_binding(plan)
        return release_state.start_rollout(self.root, self.capsule, plan)

    def _finished_run(self) -> dict:
        return json.loads((self.root / "releases" / ".local-state.json").read_text(encoding="utf-8"))["rollouts"][-1]

    def test_migration_refused_before_its_write_leaves_the_target_free_for_a_later_run(self) -> None:
        mismatched = dict(self._observed_identity(), db_unique_name="OTHER_UNIQUE")
        status, report, migrate = self._run_migration_release(
            self._migration_release_patches(observed=mismatched), migration_only=True, label="refused-before-write",
        )

        self.assertEqual(status, 2)
        migrate.assert_not_called()
        self.assertNotEqual(report["databaseState"], "unknown")
        run = self._finished_run()
        self.assertEqual(run["state"], "failed")
        self.assertNotEqual(run["databaseState"], "unknown")
        self.assertIsInstance(self._start_again_on_the_same_target(), str)

    def test_migration_that_left_a_pending_attempt_finishes_unknown_and_still_blocks(self) -> None:
        from scripts import release_state

        def pending_then_fail(*_args, **_kwargs):
            self._install_attempt_event(self._attempt_event("pending", attempt_id="a" * 8 + "-0000-4000-8000-" + "0" * 12))
            return rollout.StepResult(2, (), "apply failed after its pending event")

        status, report, _migrate = self._run_migration_release(
            self._migration_release_patches(), migration_only=True, label="pending-only", migrate_effect=pending_then_fail,
        )

        self.assertEqual(status, 2)
        self.assertEqual(report["steps"][0]["writeState"], "unknown")
        self.assertEqual(report["databaseState"], "unknown")
        self.assertEqual(self._finished_run()["state"], "unknown")
        with self.assertRaisesRegex(release_state.ReleaseStateError, "owns one of these target scopes"):
            self._start_again_on_the_same_target()

    def test_migration_write_state_is_read_from_the_attempt_events_that_appeared(self) -> None:
        from scripts import rollout

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            folder = repo / MIGRATION_FOLDER
            folder.mkdir(parents=True)
            folders = [MIGRATION_FOLDER]

            self.assertEqual(rollout._migration_write_state(set(), folders, repo), "no-write-attempted")
            self.assertEqual(rollout._migration_write_state(None, folders, repo), "unknown")

            pending = self._attempt_event("pending", attempt_id="1" * 8 + "-0000-4000-8000-" + "0" * 12)
            self._install_attempt_event_at(repo, pending)
            self.assertEqual(rollout._migration_write_state(set(), folders, repo), "unknown")
            baseline = {pending["eventId"]}
            self.assertEqual(rollout._migration_write_state(baseline, folders, repo), "no-write-attempted")

            verified = self._attempt_event("verified", attempt_id=pending["attemptId"], predecessor=pending["eventId"])
            self._install_attempt_event_at(repo, verified)
            self.assertEqual(rollout._migration_write_state(baseline, folders, repo), "partially-verified")

            other = self._attempt_event("pending", attempt_id="2" * 8 + "-0000-4000-8000-" + "0" * 12)
            self._install_attempt_event_at(repo, other)
            self.assertEqual(rollout._migration_write_state(baseline, folders, repo), "unknown")

    def test_a_resolved_attempt_is_an_unknown_write_that_recovery_will_not_replay(self) -> None:
        from scripts import migration_attempts, rollout

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            (repo / MIGRATION_FOLDER).mkdir(parents=True)
            folders = [MIGRATION_FOLDER]
            attempt_id = "4" * 8 + "-0000-4000-8000-" + "0" * 12
            pending = self._attempt_event("pending", attempt_id=attempt_id)
            self._install_attempt_event_at(repo, pending)
            resolved = self._attempt_event(
                "failed-no-target-effect", attempt_id=attempt_id, predecessor=pending["eventId"],
                evidence=[{"kind": "owner-resolution", "path": "evidence/owner-check.txt", "sha256": "e" * 64}],
            )
            self._install_attempt_event_at(repo, resolved)
            try:
                read_back = migration_attempts.read_attempt_events(repo / MIGRATION_FOLDER)
            except migration_attempts.MigrationAttemptError as error:
                self.fail(f"resolved attempt events are not readable yet: {error}")
            self.assertEqual({event["state"] for event in read_back}, {"pending", "failed-no-target-effect"})

            self.assertEqual(rollout._migration_write_state(set(), folders, repo), "unknown")
            self.assertEqual(rollout._migration_write_state({pending["eventId"]}, folders, repo), "unknown")

    def _install_attempt_event_at(self, repo: Path, event: dict) -> None:
        path = repo / MIGRATION_FOLDER / f"attempt-event.{event['eventId']}.json"
        path.write_text(json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def test_recovery_replays_nothing_that_a_migration_step_already_verified(self) -> None:
        steps = [self.steps[0], {"type": "migrate", "folders": [MIGRATION_FOLDER]}, self.steps[0]]
        plan = self._plan(steps)
        target_plan = SimpleNamespace(recovery_binding={"checkpoint": 2})
        verified = [{"step": 1, "status": "verified"}, {"step": 2, "status": "verified"}]
        with self.assertRaisesRegex(rollout.RolloutError, "previously verified writing step"):
            rollout._assert_recovery_will_not_replay_prior_writes(plan, target_plan, {"steps": verified})
        unknown_migration = [{"step": 1, "status": "verified"}]
        with self.assertRaisesRegex(rollout.RolloutError, "no recorded outcome for earlier writing step 2"):
            rollout._assert_recovery_will_not_replay_prior_writes(plan, target_plan, {"steps": unknown_migration})
        # A parent killed before the migration ran has no migration outcome; recovering the first checkpoint replays nothing.
        rollout._assert_recovery_will_not_replay_prior_writes(
            plan, SimpleNamespace(recovery_binding={"checkpoint": 1}), {"steps": []},
        )

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
