from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.feature_manifest import FeatureCheckpoint, FeatureInput
from scripts.migration_checks import CheckReport
from scripts.migration_manifest import QueryCheck
from scripts.release_capsule import ReleaseCapsule


IDENTITY = {
    "session_user": "APP_CODE",
    "current_schema": "APP_CODE",
    "db_name": "STAGING",
    "db_unique_name": "STAGING_UNIQUE",
    "service_name": "stage.example.invalid",
    "container_id": "3",
    "container_name": "STAGEPDB",
    "edition": "ORA$BASE",
    "database_version": "19.0.0.0",
}
SETTINGS = {
    "plsql_optimize_level": "2",
    "plsql_code_type": "INTERPRETED",
    "plsql_debug": "FALSE",
    "plsql_warnings": "ENABLE:ALL",
    "plsql_ccflags": "",
    "nls_length_semantics": "BYTE",
    "plscope_settings": "IDENTIFIERS:NONE",
}


class FeatureRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = (
            b"ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\n"
            b"ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\n"
            b"ALTER SESSION SET PLSQL_DEBUG = FALSE;\n"
            b"ALTER SESSION SET PLSQL_WARNINGS = 'ENABLE:ALL';\n"
            b"ALTER SESSION SET PLSQL_CCFLAGS = '';\n"
            b"ALTER SESSION SET NLS_LENGTH_SEMANTICS = BYTE;\n"
            b"ALTER SESSION SET PLSCOPE_SETTINGS = 'IDENTIFIERS:NONE';\n"
            b"CREATE OR REPLACE PACKAGE APP_CODE.DISPATCH AS\n"
            b"  PROCEDURE RUN;\n"
            b"END DISPATCH;\n/\n"
        )
        self.item = FeatureInput(
            "features/dispatch/APP_CODE/packages/dispatch.pks.sql",
            self.source,
            hashlib.sha256(self.source).hexdigest(),
            ("APP_CODE", "PACKAGE", "DISPATCH"),
            "stored-source",
        )
        self.checkpoint = FeatureCheckpoint(
            "APP_CODE", (self.item,),
            (QueryCheck("before-ready", "SELECT 1 FROM dual", 1),),
            (QueryCheck("after-ready", "SELECT 1 FROM dual", 1),),
            (("APP_CODE", "DISPATCH"),),
        )
        self.payload = hashlib.sha256(b"payload").hexdigest()
        self.engine = hashlib.sha256(b"engine").hexdigest()
        capsule_dir = self.root / "scratch" / "release-capsules" / f"{self.payload}-abcdef123456"
        capsule_dir.mkdir(parents=True)
        self.capsule = ReleaseCapsule(
            capsule_dir, hashlib.sha256(b"capsule").hexdigest(), self.payload, self.engine,
            "abcdef1234567890abcdef1234567890abcdef12", (self.checkpoint,), (), 120,
        )
        self.state = self._state()
        self.driver_path = "drivers/checkpoint-001-input-001.sql"
        self.driver_bytes = self._driver_bytes(self.item)
        driver = capsule_dir / self.driver_path
        driver.parent.mkdir(parents=True)
        driver.write_bytes(self.driver_bytes)
        self.capsule = replace(self.capsule, input_hashes=(
            {"path": self.driver_path, "sha256": hashlib.sha256(self.driver_bytes).hexdigest()},
        ))
        source_file = self.root / self.item.path
        source_file.parent.mkdir(parents=True)
        source_file.write_bytes(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _driver_bytes(self, item: FeatureInput) -> bytes:
        from scripts import release_capsule

        return release_capsule._stored_source_driver(item)[0]

    def _state(self, *, lines: list[str] | None = None) -> dict:
        identity = dict(IDENTITY)
        return {
            "schema": "APP_CODE",
            "identity": identity,
            "sourceUnits": {
                "PACKAGE:DISPATCH": {
                    "lines": lines or ["PACKAGE DISPATCH AS\n", "  PROCEDURE OLD_RUN;\n", "END DISPATCH;\n"],
                    "settings": dict(SETTINGS),
                    "valid": True,
                },
            },
            "structural": {
                "tables": [], "columns": [], "constraints": [], "indexes": [], "triggers": [],
                "sequences": [], "synonyms": [], "views": [], "stored-code": [],
                "invalid-objects": [], "identity-columns": [], "object-grants": [], "feature-dependencies": [],
                "feature-synonym-access": [],
            },
            "coverage": {"ownerComplete": False, "selectedScopeComplete": True},
        }

    def _environ(self) -> dict[str, str]:
        return {
            "STAGING_SQLCL_CONNECTION": "staging-conn",
            "STAGING_EXPECTED_USER": "APP_CODE",
            "STAGING_SCHEMA": "APP_CODE",
        }

    def _prepared(self, *, state: dict | None = None):
        from scripts import feature_release

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=state or self.state):
            return feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environ(),
            )

    def _check_report(self, identity: dict | None = None, *, passed: bool = True) -> CheckReport:
        observed = dict(identity or IDENTITY)
        return CheckReport(passed, True, (), (), {
            "sessions": 1,
            "identity": observed,
            "sessionIdentities": [observed],
            "complete": True,
        })

    def _apply_output(self, identity: dict | None = None, *, marker: bool = True) -> str:
        observed = dict(identity or IDENTITY)
        lines = [
            "MIGRATION_IDENTITY_BEGIN", json.dumps(observed, sort_keys=True),
            "MIGRATION_IDENTITY_END", "MIGRATION_IDENTITY_VERIFIED",
            "MIGRATION_PAYLOAD_STARTED:checkpoint-001",
        ]
        if marker:
            lines.extend(("MIGRATION_APPLY_COMPLETED", "FEATURE_CHECKPOINT_APPLY_COMPLETED"))
        return "\n".join(lines) + "\n"

    def _fake_runner(self, *, output: str | None = None, inspect=None):
        def run(target, driver, run_dir, **_kwargs):
            if inspect is not None:
                inspect(driver, run_dir)
            result = self._apply_output() if output is None else output
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "sqlcl-output.log").write_text(result, encoding="utf-8")
            return SimpleNamespace(returncode=0, output=result, run_dir=run_dir)

        return run

    @staticmethod
    def _apply_session_settings(text: str, state: dict[str, str]) -> None:
        for name, raw in re.findall(r"(?im)^ALTER SESSION SET ([A-Z_]+)\s*=\s*(.*?);\s*$", text):
            value = raw.strip()
            if value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            state[name.lower()] = value

    def _start_run(self, capsule, plan):
        from scripts import release_state
        from unittest.mock import patch

        with patch.object(release_state.release_capsule, "load_capsule", return_value=capsule):
            return release_state.start_rollout(self.root, capsule, plan)

    def _record_attempt(self, capsule, plan, checkpoint=1):
        from scripts import release_ownership, release_state

        run_id = self._start_run(capsule, plan)
        guard = plan.checkpoint_guards[checkpoint - 1]
        scope = release_state.target_scope_from_identity(guard["targetIdentity"])
        identity = {
            "runId": run_id, "environment": plan.environment,
            "payloadDigest": capsule.payload_digest, "engineDigest": capsule.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": checkpoint,
            "recoveryAttempt": plan.recovery_attempt,
        }
        with release_ownership.target_writer_lease(self.root, scope, identity) as lease:
            attempt_id = release_state.record_attempt(
                self.root, capsule, plan, checkpoint, run_id=run_id, lease=lease,
            )
        release_state.finish_rollout(
            self.root, run_id,
            outcome={
                "state": "unknown", "databaseState": "unknown", "steps": [],
                "checkpointVerificationIds": [],
                "budget": {"seconds": capsule.database_object_budget_seconds, "elapsedSeconds": None, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )
        return attempt_id

    def _leave_attempt_in_run(self, capsule, plan, *, state):
        from scripts import release_ownership, release_state

        run_id = self._start_run(capsule, plan)
        scope = release_state.target_scope_from_identity(plan.checkpoint_guards[0]["targetIdentity"])
        identity = {
            "runId": run_id, "environment": plan.environment,
            "payloadDigest": capsule.payload_digest, "engineDigest": capsule.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": 1,
            "recoveryAttempt": plan.recovery_attempt,
        }
        with release_ownership.target_writer_lease(self.root, scope, identity) as lease:
            attempt_id = release_state.record_attempt(
                self.root, capsule, plan, 1, run_id=run_id, lease=lease,
            )
        if state != "attempting":
            release_state.finish_attempt(self.root, attempt_id, state=state, evidence=[], metrics={})
        return run_id, attempt_id

    def _assert_noop_refuses_occupied_target(self, attempt_state):
        from scripts import feature_release, release_state

        expected_unit, _settings = feature_release._stored_source(self.item)
        observed = self._state(lines=expected_unit["lines"])
        plan = self._prepared(state=observed)
        run_id, attempt_id = self._leave_attempt_in_run(self.capsule, plan, state=attempt_state)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=observed), \
             patch.object(feature_release.migration_checks, "run_checks", return_value=self._check_report()), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "unresolved attempt|occupies"):
                feature_release.execute_checkpoint(
                    self.root, self.capsule, plan, 1, run_id=run_id, environ=self._environ(),
                )
        apply.assert_not_called()
        index = release_state._load_index(self.root)
        self.assertEqual(index["attempts"][0]["attemptId"], attempt_id)
        self.assertEqual(index["attempts"][0]["state"], attempt_state)
        self.assertEqual(index["checkpointVerifications"], [])

    def _runtime(self, plan, *, check_side_effect=None, runner=None, state=None, run_id=None):
        from scripts import feature_release

        run_id = run_id or self._start_run(self.capsule, plan)
        self.last_run_id = run_id
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=state or self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=check_side_effect or [self._check_report(), self._check_report()]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=runner or self._fake_runner()) as apply:
            result = feature_release.execute_checkpoint(
                self.root, self.capsule, plan, 1, run_id=run_id, environ=self._environ(),
            )
        return result, apply

    def test_execute_checkpoint_rejects_missing_prepared_plan_before_capture_or_write(self) -> None:
        from scripts import feature_release

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state") as capture, \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "plan|missing"):
                feature_release.execute_checkpoint(
                    self.root, self.capsule, SimpleNamespace(environment="staging", payload_digest=self.payload, plan_digest="a" * 64),
                    1, run_id="00000000-0000-4000-8000-000000000000", environ=self._environ(),
                )

        capture.assert_not_called()
        apply.assert_not_called()

    def test_prepare_and_apply_use_the_separate_migration_profile_and_reject_profile_drift(self) -> None:
        from scripts import db_targets, feature_release

        environ = {
            **self._environ(),
            "STAGING_MIGRATION_SCHEMA": "APP_CODE",
            "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-migration",
            "STAGING_MIGRATION_EXPECTED_USER": "APP_CODE",
        }
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state) as prepare_capture, \
             patch.object(feature_release, "resolve_target", wraps=db_targets.resolve_target) as resolve:
            plan = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=environ,
            )

        self.assertEqual(plan.targets["schema:APP_CODE"]["connection"], "stage-migration")
        self.assertEqual(prepare_capture.call_args.args[0].connection, "stage-migration")
        self.assertEqual(resolve.call_args.args[2], "migration")

        runner = self._fake_runner()
        run_id = self._start_run(self.capsule, plan)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state) as execution_capture, \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[
                 self._check_report(), self._check_report(),
             ]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=runner) as apply, \
             patch.object(feature_release, "resolve_target", wraps=db_targets.resolve_target) as execution_resolve:
            result = feature_release.execute_checkpoint(
                self.root, self.capsule, plan, 1, run_id=run_id, environ=environ,
            )

        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(execution_capture.call_args.args[0].connection, "stage-migration")
        self.assertEqual(apply.call_args.args[0].connection, "stage-migration")
        self.assertEqual(execution_resolve.call_args.args[2], "migration")

        changed = {**environ, "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-migration-repointed"}
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state") as drift_capture, \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as drift_apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "differs from the prepared target descriptor"):
                feature_release.execute_checkpoint(
                    self.root, self.capsule, plan, 1, run_id=run_id, environ=changed,
                )
        drift_capture.assert_not_called()
        drift_apply.assert_not_called()

    def test_live_selected_source_drift_after_prepare_refuses_before_apply(self) -> None:
        from scripts import feature_release

        plan = self._prepared()
        changed = self._state(lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE OTHER_RUN;\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=changed), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "changed|drift|source"):
                feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        apply.assert_not_called()

    def test_stale_saved_plan_refuses_before_live_capture_or_apply(self) -> None:
        from scripts import feature_release

        plan = self._prepared()
        path = self.root / "scratch" / "feature-release-plans" / "staging" / f"{self.payload}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["targets"] = {}
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state") as capture, \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "digest|plan"):
                feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        capture.assert_not_called()
        apply.assert_not_called()

    def test_one_apply_session_preserves_frozen_source_and_accepts_ordinary_warning(self) -> None:
        from scripts import feature_release

        source = (
            b"ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\n"
            b"ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\n"
            b"ALTER SESSION SET PLSQL_DEBUG = FALSE;\n"
            b"ALTER SESSION SET PLSQL_WARNINGS = 'ENABLE:ALL';\n"
            b"ALTER SESSION SET PLSQL_CCFLAGS = '';\n"
            b"ALTER SESSION SET NLS_LENGTH_SEMANTICS = BYTE;\n"
            b"ALTER SESSION SET PLSCOPE_SETTINGS = 'IDENTIFIERS:NONE';\n"
            b"CREATE OR REPLACE PACKAGE APP_CODE.DISPATCH AS\n"
            b"  -- caf\xc3\xa9 and a quoted 'value'\n"
            b"\n \t\n"
            b"  PROCEDURE RUN; -- trailing-hyphen-\n"
            b"END DISPATCH;\n/\n"
        )
        item = replace(self.item, source=source, sha256=hashlib.sha256(source).hexdigest())
        checkpoint = replace(self.checkpoint, inputs=(item,))
        capsule = replace(self.capsule, checkpoints=(checkpoint,))
        expected_driver = self._driver_bytes(item)
        (capsule.root / self.driver_path).write_bytes(expected_driver)
        capsule = replace(capsule, input_hashes=({
            "path": self.driver_path, "sha256": hashlib.sha256(expected_driver).hexdigest(),
        },))
        self.capsule = capsule
        self.item = item
        self.checkpoint = checkpoint
        plan = self._prepared()

        def inspect(driver, run_dir):
            self.assertEqual((run_dir / "payload" / "checkpoint-001-input-001.sql").read_bytes(), expected_driver)
            text = driver.read_text(encoding="utf-8")
            self.assertEqual(text.count("@@payload/checkpoint-001-input-001.sql"), 1)
            self.assertIn("MIGRATION_IDENTITY_GUARD_REFUSED", text)

        def successful_warning(_target, _checks, run_dir, *, phase, **_kwargs):
            return self._check_report()

        runner = self._fake_runner(output="Warning: session option preserved\n" + self._apply_output(), inspect=inspect)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=successful_warning), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=runner) as apply:
            result = feature_release.execute_checkpoint(self.root, capsule, plan, 1, run_id=self._start_run(capsule, plan), environ=self._environ())

        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(result.database_state, "verified")
        self.assertEqual(apply.call_count, 1)

    def test_structural_checks_run_in_apply_session_before_first_payload(self) -> None:
        from scripts import feature_preflight, feature_release
        from scripts.db_targets import Target

        source = b"CREATE TABLE APP_CODE.NEW_TABLE (ID NUMBER);\n"
        item = FeatureInput(
            "features/new-table/APP_CODE/new-table.sql",
            source,
            hashlib.sha256(source).hexdigest(),
            None,
            "ddl",
        )
        checkpoint = FeatureCheckpoint(
            "APP_CODE", (item,), (), (QueryCheck("ready", "SELECT 1 FROM dual", 1),),
            (("APP_CODE", "NEW_TABLE"),),
        )
        capsule = replace(self.capsule, checkpoints=(checkpoint,))
        structural = feature_preflight.prepare_checkpoint(checkpoint, self.state, ())
        run_dir = self.root / "scratch" / "structural-apply"
        target = Target("staging", "staging-conn", "APP_CODE", "APP_CODE", "staging")
        expected_identity = dict(IDENTITY)

        def copy_driver(_root, _capsule, destination, checkpoint_number, input_number):
            (destination / "payload").mkdir(parents=True, exist_ok=True)
            (destination / "payload" / "ddl.sql").write_bytes(source)
            return "payload/ddl.sql", item.path

        def fake_apply(_target, driver, _run_dir, *, phase):
            text = driver.read_text(encoding="utf-8")
            self.assertLess(text.index("CHECK_PAYLOAD_BEGIN:preconditions"), text.index("MIGRATION_PAYLOAD_STARTED:ddl.sql"))
            self.assertIn("structural-before-0001", text)
            return SimpleNamespace(returncode=0, output="MIGRATION_APPLY_COMPLETED\nFEATURE_CHECKPOINT_APPLY_COMPLETED\n")

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(feature_release, "_copy_capsule_driver", side_effect=copy_driver), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=fake_apply), \
             patch.object(feature_release.migrate, "_apply_identity_from_output", return_value=expected_identity):
            identity, sessions = feature_release._apply_checkpoint_session(
                self.root, capsule, checkpoint,
                {"structuralPlan": feature_preflight.plan_to_dict(structural)},
                target, expected_identity, structural.preconditions, run_dir, 1, {},
            )

        self.assertEqual(identity, expected_identity)
        self.assertEqual(sessions, 1)

    def test_two_package_inputs_share_one_apply_session_and_each_final_source_runs_once(self) -> None:
        from scripts import feature_release

        sources = (
            (
                "features/dispatch/APP_CODE/packages/dispatch.pks.sql",
                b"CREATE OR REPLACE PACKAGE APP_CODE.DISPATCH AS\n  PROCEDURE RUN;\n  PROCEDURE NEW_RUN;\nEND DISPATCH;\n/\n",
                ("APP_CODE", "PACKAGE", "DISPATCH"),
            ),
            (
                "features/dispatch/APP_CODE/packages/dispatch.pkb.sql",
                b"CREATE OR REPLACE PACKAGE BODY APP_CODE.DISPATCH AS\n"
                b"  PROCEDURE RUN IS BEGIN NULL; END RUN;\n"
                b"  PROCEDURE NEW_RUN IS BEGIN NULL; END NEW_RUN;\n"
                b"END DISPATCH;\n/\n",
                ("APP_CODE", "PACKAGE BODY", "DISPATCH"),
            ),
        )
        inputs = tuple(FeatureInput(path, raw, hashlib.sha256(raw).hexdigest(), unit, "stored-source") for path, raw, unit in sources)
        checkpoint = FeatureCheckpoint("APP_CODE", inputs, self.checkpoint.preconditions, self.checkpoint.postconditions, (("APP_CODE", "DISPATCH"),))
        capsule = replace(self.capsule, checkpoints=(checkpoint,))
        hashes = []
        expected_drivers = []
        for index, item in enumerate(inputs, start=1):
            path = self.root / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.source)
            driver_bytes = self._driver_bytes(item)
            driver_path = f"drivers/checkpoint-001-input-{index:03d}.sql"
            target = capsule.root / driver_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(driver_bytes)
            hashes.append({"path": driver_path, "sha256": hashlib.sha256(driver_bytes).hexdigest()})
            expected_drivers.append(driver_path)
        capsule = replace(capsule, input_hashes=tuple(hashes))
        self.capsule = capsule
        state = self._state()
        state["sourceUnits"]["PACKAGE BODY:DISPATCH"] = {
            "lines": ["PACKAGE BODY DISPATCH AS\n", "  PROCEDURE RUN IS BEGIN NULL; END RUN;\n", "END DISPATCH;\n"],
            "settings": {**SETTINGS, "plsql_code_type": "NATIVE", "plsql_ccflags": "body:TRUE"}, "valid": True,
        }
        state["sourceUnits"]["PACKAGE:DISPATCH"]["settings"] = {
            **SETTINGS, "plsql_code_type": "INTERPRETED", "plsql_ccflags": "spec:TRUE",
        }
        self.state = state
        plan = self._prepared(state=state)

        def inspect(driver, run_dir):
            driver_text = driver.read_text(encoding="utf-8")
            current_settings: dict[str, str] = {}
            cursor = 0
            for unit_index, relative in enumerate(expected_drivers):
                staged = f"payload/{Path(relative).name.replace('checkpoint-001-', 'checkpoint-001-')}"
                self.assertEqual(driver_text.count(f"@@{staged}"), 1)
                include = driver_text.index(f"@@{staged}")
                self._apply_session_settings(driver_text[cursor:include], current_settings)
                self._apply_session_settings((run_dir / staged).read_text(encoding="utf-8"), current_settings)
                expected = plan.checkpoint_guards[0]["sourceGuards"][unit_index]["applySettings"]
                self.assertEqual(current_settings, expected)
                cursor = include + len(f"@@{staged}")
            self.assertEqual(driver_text.count("MIGRATION_PAYLOAD_STARTED:"), 2)
            self.assertIn("check_unit('APP_CODE', 'PACKAGE', 'DISPATCH', TRUE)", driver_text)
            self.assertIn("check_unit('APP_CODE', 'PACKAGE BODY', 'DISPATCH', TRUE)", driver_text)
            self.assertNotIn("OTHER_SCHEMA", driver_text)

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), self._check_report()]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=self._fake_runner(inspect=inspect)) as apply:
            result = feature_release.execute_checkpoint(self.root, capsule, plan, 1, run_id=self._start_run(capsule, plan), environ=self._environ())

        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(apply.call_count, 1)

    def test_partial_source_override_uses_complete_prepared_target_settings(self) -> None:
        from scripts import baseline

        source = (
            b"ALTER SESSION SET PLSQL_WARNINGS = 'DISABLE:ALL';\n"
            b"CREATE OR REPLACE PACKAGE APP_CODE.DISPATCH AS\n"
            b"  PROCEDURE RUN;\n"
            b"END DISPATCH;\n/\n"
        )
        item = replace(self.item, source=source, sha256=hashlib.sha256(source).hexdigest())
        checkpoint = replace(self.checkpoint, inputs=(item,))
        driver_bytes = self._driver_bytes(item)
        (self.root / item.path).write_bytes(source)
        (self.capsule.root / self.driver_path).write_bytes(driver_bytes)
        capsule = replace(
            self.capsule, checkpoints=(checkpoint,),
            input_hashes=({"path": self.driver_path, "sha256": hashlib.sha256(driver_bytes).hexdigest()},),
        )
        self.item, self.checkpoint, self.capsule = item, checkpoint, capsule
        target_settings = {**SETTINGS, "plsql_code_type": "NATIVE", "plsql_ccflags": "branch:TRUE"}
        state = self._state()
        state["sourceUnits"]["PACKAGE:DISPATCH"]["settings"] = target_settings
        self.state = state
        plan = self._prepared(state=state)
        source_guard = plan.checkpoint_guards[0]["sourceGuards"][0]
        expected_settings = {**target_settings, "plsql_warnings": "DISABLE:ALL"}
        self.assertEqual(source_guard["applySettings"], expected_settings)
        expected_prologue = baseline._session_settings_sql(expected_settings) + "\n"
        self.assertEqual(source_guard["settingsPrologue"], expected_prologue)
        self.assertEqual(
            source_guard["settingsPrologueSha256"],
            hashlib.sha256(expected_prologue.encode("utf-8")).hexdigest(),
        )

        def inspect(driver, run_dir):
            staged = run_dir / "payload" / "checkpoint-001-input-001.sql"
            effective: dict[str, str] = {}
            self._apply_session_settings(driver.read_text(encoding="utf-8"), effective)
            self._apply_session_settings(staged.read_text(encoding="utf-8"), effective)
            self.assertEqual(effective, expected_settings)

        result, apply = self._runtime(plan, runner=self._fake_runner(inspect=inspect), state=state)
        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(apply.call_count, 1)
        report_path = next(path for path in result.evidence_paths if path.endswith("checkpoint-report.json"))
        report = json.loads((self.root / report_path).read_text(encoding="utf-8"))
        self.assertEqual(report["compilerSettingsPrologues"], [{
            "unit": source_guard["unit"], "sha256": source_guard["settingsPrologueSha256"],
        }])

    def test_recovery_execution_rechecks_partial_state_and_preserves_old_attempt(self) -> None:
        from scripts import feature_release, release_state

        initial = self._state()
        partial = self._state(lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE PARTIAL;\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=initial):
            original = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environ(),
            )
        attempt_id = self._record_attempt(self.capsule, original, 1)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=partial):
            recovery = feature_release.prepare_target(
                self.root,
                self.capsule,
                "staging",
                environ=self._environ(),
                recovery_attempt=attempt_id,
                terminal_ack=f"RECONCILE {attempt_id}",
            )

        intervening = self._state(lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE OTHER;\n", "END DISPATCH;\n"])
        run_id = self._start_run(self.capsule, recovery)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=recovery), \
             patch.object(feature_release, "capture_selected_state", return_value=intervening), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "changed|drift|source"):
                feature_release.execute_checkpoint(
                    self.root, self.capsule, recovery, 1, run_id=run_id, environ=self._environ(),
                )
        apply.assert_not_called()
        self.assertEqual(release_state._load_index(self.root)["attempts"][0]["state"], "attempting")

        result, apply = self._runtime(recovery, runner=self._fake_runner(), state=partial, run_id=run_id)
        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(apply.call_count, 1)
        attempts = release_state._load_index(self.root)["attempts"]
        self.assertEqual(attempts[0]["state"], "attempting")
        self.assertEqual(attempts[1]["state"], "verified")
        self.assertEqual(attempts[1]["recoveryOf"], attempt_id)

    def test_recovery_checkpoint_runs_while_the_killed_parent_rollout_is_still_recorded_running(self) -> None:
        # A killed rollout keeps its run record "running". Starting the recovery already treats
        # that parent as retired, so the checkpoint occupancy check must treat it the same way.
        from scripts import feature_release, release_state

        initial = self._state()
        partial = self._state(lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE PARTIAL;\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=initial):
            original = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environ())
        parent_run_id, attempt_id = self._leave_attempt_in_run(self.capsule, original, state="attempting")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=partial):
            recovery = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environ(),
                recovery_attempt=attempt_id, terminal_ack=f"RECONCILE {attempt_id}",
            )

        run_id = self._start_run(self.capsule, recovery)
        result, _apply = self._runtime(recovery, runner=self._fake_runner(), state=partial, run_id=run_id)
        self.assertEqual(result.exit_code, 0, result.message)
        parent = next(row for row in release_state._load_index(self.root)["rollouts"] if row["runId"] == parent_run_id)
        self.assertEqual(parent["state"], "running")

    def test_a_later_run_is_not_blocked_by_a_killed_parent_that_a_recovery_reconciled(self) -> None:
        # After a recovery verifies the interrupted checkpoint, the killed parent still reads
        # "running". Later runs on the same scope must not be refused on its account.
        from scripts import feature_release, release_state

        initial = self._state()
        partial = self._state(lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE PARTIAL;\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=initial):
            original = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environ())
        _parent_run_id, attempt_id = self._leave_attempt_in_run(self.capsule, original, state="attempting")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=partial):
            recovery = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environ(),
                recovery_attempt=attempt_id, terminal_ack=f"RECONCILE {attempt_id}",
            )
        recovery_run_id = self._start_run(self.capsule, recovery)
        result, _apply = self._runtime(recovery, runner=self._fake_runner(), state=partial, run_id=recovery_run_id)
        self.assertEqual(result.exit_code, 0, result.message)
        verification_ids = [
            row["verificationId"] for row in release_state._load_index(self.root)["checkpointVerifications"]
            if row["runId"] == recovery_run_id
        ]
        release_state.finish_rollout(self.root, recovery_run_id, outcome={
            "state": "failed", "databaseState": "verified", "steps": [],
            "checkpointVerificationIds": verification_ids,
            "budget": {"seconds": self.capsule.database_object_budget_seconds, "elapsedSeconds": None,
                       "coverageComplete": False, "passed": False},
            "finalReportPath": None, "finalReportSha256": None,
        })

        # The later run starts from the frozen result, so its plan differs from the killed one.
        expected_unit, _settings = feature_release._stored_source(self.item)
        expected = self._state(lines=expected_unit["lines"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=expected):
            later = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environ())
        later_run_id = self._start_run(self.capsule, later)
        result, _apply = self._runtime(later, runner=self._fake_runner(), state=expected, run_id=later_run_id)
        self.assertEqual(result.exit_code, 0, result.message)

    def test_wrong_apply_identity_and_compilation_warning_never_verify_attempt(self) -> None:
        from scripts import feature_release

        plan = self._prepared()
        wrong = dict(IDENTITY, db_unique_name="OTHER_UNIQUE")
        output = self._apply_output(wrong)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), self._check_report()]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=self._fake_runner(output=output)):
            result = feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        self.assertNotEqual(result.exit_code, 0)
        index = json.loads((self.root / "releases/.local-state.json").read_text(encoding="utf-8"))
        self.assertEqual(index["attempts"][-1]["state"], "unknown")

        # Use a new target identity scope so the unresolved prior attempt does not
        # accidentally become the reason the compilation-warning case refuses.
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        (self.root / "scratch/release-capsules").mkdir(parents=True)
        self.capsule = replace(self.capsule, root=self.root / "scratch/release-capsules" / f"{self.payload}-abcdef123456")
        self.capsule.root.mkdir()
        (self.capsule.root / self.driver_path).parent.mkdir(parents=True, exist_ok=True)
        (self.capsule.root / self.driver_path).write_bytes(self.driver_bytes)
        self.state = self._state()
        plan = self._prepared()
        warning = "Warning: Package created with compilation errors.\n" + self._apply_output()
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), self._check_report()]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=self._fake_runner(output=warning)):
            result = feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        self.assertNotEqual(result.exit_code, 0)
        index = json.loads((self.root / "releases/.local-state.json").read_text(encoding="utf-8"))
        self.assertNotEqual(index["attempts"][-1]["state"], "verified")

    def test_wrong_precondition_session_identity_refuses_before_creating_attempt(self) -> None:
        from scripts import feature_release, release_state

        plan = self._prepared()
        wrong = dict(IDENTITY, db_unique_name="OTHER_UNIQUE")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", return_value=self._check_report(wrong)), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "precondition.*identity"):
                feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        apply.assert_not_called()
        self.assertEqual(release_state._load_index(self.root)["checkpointVerifications"], [])

    def test_wrong_postcondition_session_identity_leaves_unknown_attempt(self) -> None:
        from scripts import feature_release

        plan = self._prepared()
        wrong = dict(IDENTITY, service_name="other.example.invalid")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), self._check_report(wrong)]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=self._fake_runner()):
            result = feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.database_state, "unknown")
        index = json.loads((self.root / "releases/.local-state.json").read_text(encoding="utf-8"))
        self.assertEqual(index["attempts"][-1]["state"], "unknown")

    def test_missing_apply_completion_markers_leave_unknown_attempt_without_receipt(self) -> None:
        from scripts import feature_release

        plan = self._prepared()
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), self._check_report()]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=self._fake_runner(output=self._apply_output(marker=False))):
            result = feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.database_state, "unknown")
        self.assertTrue(result.evidence_paths)
        index = json.loads((self.root / "releases/.local-state.json").read_text(encoding="utf-8"))
        self.assertEqual(index["attempts"][-1]["state"], "unknown")

    def test_stored_source_header_blanks_the_owner_as_oracle_stores_all_source(self) -> None:
        # ALL_SOURCE keeps the columns of an owner qualifier as blanks, so the expected
        # first line must have that padding or every source postcondition fails live.
        from scripts import feature_release

        source = (
            b'CREATE OR REPLACE PACKAGE BODY "APP_CODE"."DISPATCH" as\n'
            b"  PROCEDURE RUN is begin null; end;\n"
            b'end "DISPATCH";\n/\n'
        )
        item = replace(
            self.item, path="features/dispatch/APP_CODE/packages/dispatch.pkb.sql", source=source,
            sha256=hashlib.sha256(source).hexdigest(), unit=("APP_CODE", "PACKAGE BODY", "DISPATCH"),
        )
        expected_unit, _settings = feature_release._stored_source(item)
        self.assertEqual(expected_unit["lines"][0], "PACKAGE BODY" + " " * 12 + '"DISPATCH" as\n')

    def test_validity_check_ids_are_kebab_case_for_underscored_unit_names(self) -> None:
        # Oracle names often contain underscores, but the check runner accepts only
        # lowercase kebab-case ids. An underscore id failed every feature postcondition.
        from scripts import feature_release, migration_manifest

        for object_type, name in (("PACKAGE BODY", "ITEM_LOOKUP"), ("PACKAGE", "APP_DISPATCH_2"), ("TRIGGER", "T$AUDIT")):
            with self.subTest(name=name):
                identifier = feature_release._validity_check_id(object_type, name)
                self.assertIsNotNone(migration_manifest.CHECK_ID_RE.fullmatch(identifier), identifier)

    def test_noop_unit_has_no_apply_session_or_write_attempt(self) -> None:
        from scripts import feature_release

        expected_unit, _settings = feature_release._stored_source(self.item)
        expected_lines = expected_unit["lines"]
        state = self._state(lines=expected_lines)
        plan = self._prepared(state=state)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), self._check_report()]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            result = feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(result.database_state, "verified")
        apply.assert_not_called()
        index = json.loads((self.root / "releases/.local-state.json").read_text(encoding="utf-8"))
        verification = index["checkpointVerifications"][0]
        self.assertEqual(verification["mode"], "no-op")
        self.assertIsNone(verification["attemptId"])
        self.assertEqual(result.verification_id, verification["verificationId"])
        self.assertTrue(verification["evidence"])
        self.assertTrue(all(row["sha256"] for row in verification["evidence"]))

    def test_noop_cannot_bypass_unknown_or_attempting_occupancy(self) -> None:
        self._assert_noop_refuses_occupied_target("unknown")

    def test_noop_cannot_bypass_attempting_occupancy(self) -> None:
        self._assert_noop_refuses_occupied_target("attempting")

    def test_noop_recovery_records_linked_verified_observation_without_write(self) -> None:
        from scripts import feature_release, release_state

        initial = self._state()
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=initial):
            original = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environ(),
            )
        old_attempt = self._record_attempt(self.capsule, original, 1)
        expected_unit, _settings = feature_release._stored_source(self.item)
        completed_state = self._state(lines=expected_unit["lines"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=completed_state):
            recovery = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environ(),
                recovery_attempt=old_attempt, terminal_ack=f"RECONCILE {old_attempt}",
            )

        result, apply = self._runtime(recovery, state=completed_state)

        self.assertEqual(result.exit_code, 0, result.message)
        self.assertEqual(result.database_state, "verified")
        apply.assert_not_called()
        index = release_state._load_index(self.root)
        self.assertEqual(len(index["attempts"]), 1)
        self.assertEqual(index["attempts"][0]["attemptId"], old_attempt)
        self.assertEqual(index["attempts"][0]["state"], "attempting")
        verification = index["checkpointVerifications"][0]
        self.assertEqual(verification["verificationId"], result.verification_id)
        self.assertEqual(verification["mode"], "recovery")
        self.assertIsNone(verification["attemptId"])
        link = index["recoveryLinks"][0]
        self.assertEqual(link["oldAttemptId"], old_attempt)
        self.assertEqual(link["verificationId"], verification["verificationId"])
        self.assertIn(link["reconciliationEvidence"], {row["path"] for row in verification["evidence"]})
        self.assertFalse(release_state._is_unresolved(
            index["attempts"][0], index["attempts"], index["recoveryLinks"], index["checkpointVerifications"],
        ))

    def test_postcondition_failure_leaves_unknown_attempt_without_success_receipt(self) -> None:
        from scripts import feature_release

        plan = self._prepared()
        failed = CheckReport(False, True, (), ({"code": "CHECK_FAILED", "message": "postcondition false"},), {
            "sessions": 1, "identity": dict(IDENTITY), "sessionIdentities": [dict(IDENTITY)], "complete": True,
        })
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "load_target_plan", return_value=plan), \
             patch.object(feature_release, "capture_selected_state", return_value=self.state), \
             patch.object(feature_release.migration_checks, "run_checks", side_effect=[self._check_report(), failed]), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=self._fake_runner()):
            result = feature_release.execute_checkpoint(self.root, self.capsule, plan, 1, run_id=self._start_run(self.capsule, plan), environ=self._environ())

        self.assertEqual(result.database_state, "unknown")
        index = json.loads((self.root / "releases/.local-state.json").read_text(encoding="utf-8"))
        self.assertEqual(index["attempts"][-1]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
