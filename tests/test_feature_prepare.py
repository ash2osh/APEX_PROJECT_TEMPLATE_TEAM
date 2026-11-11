from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from dataclasses import replace
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.db_targets import Target
from scripts.migration_checks import PreflightReport
from scripts.feature_manifest import FeatureCheckpoint, FeatureInput
from scripts.migration_manifest import QueryCheck
from scripts.release_capsule import ReleaseCapsule


IDENTITY_FIELDS = {
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
SETTINGS = {
    "plsql_optimize_level": "2",
    "plsql_code_type": "INTERPRETED",
    "plsql_debug": "FALSE",
    "plsql_warnings": "ENABLE:ALL",
    "plsql_ccflags": "",
    "nls_length_semantics": "BYTE",
    "plscope_settings": "IDENTIFIERS:NONE",
}


class FeaturePrepareTests(unittest.TestCase):
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
            "APP_CODE",
            (self.item,),
            (QueryCheck("before-ready", "SELECT 1 FROM dual", 1),),
            (QueryCheck("after-ready", "SELECT 1 FROM dual", 1),),
            (("APP_CODE", "DISPATCH"),),
        )
        self.payload_digest = hashlib.sha256(b"payload").hexdigest()
        self.engine_digest = hashlib.sha256(b"engine").hexdigest()
        capsule_dir = self.root / "scratch" / "release-capsules" / f"{self.payload_digest}-abcdef123456"
        capsule_dir.mkdir(parents=True)
        self.capsule = ReleaseCapsule(
            capsule_dir,
            hashlib.sha256(b"capsule manifest").hexdigest(),
            self.payload_digest,
            self.engine_digest,
            "abcdef1234567890abcdef1234567890abcdef12",
            (self.checkpoint,),
            (),
        )
        source_path = self.root / self.item.path
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(self.source)
        self._write_dev_capture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_dev_capture(self) -> None:
        captured = (
            b"CREATE OR REPLACE PACKAGE APP_CODE.DISPATCH AS\n"
            b"  PROCEDURE RUN;\n"
            b"  PROCEDURE OLD_DEV_ONLY;\n"
            b"END DISPATCH;\n/\n"
        )
        captured_path = self.root / "scratch/feature-source/dispatch/APP_CODE/captured/dispatch.pks.sql"
        captured_path.parent.mkdir(parents=True)
        captured_path.write_bytes(captured)
        metadata = {
            "schemaVersion": 1,
            "feature": "dispatch",
            "schema": "APP_CODE",
            "package": "DISPATCH",
            "identity": {**IDENTITY_FIELDS, "db_name": "DEV"},
            "units": [{
                "type": "PACKAGE", "name": "DISPATCH", "path": self.item.path,
                "capturedPath": "scratch/feature-source/dispatch/APP_CODE/captured/dispatch.pks.sql",
                "sha256": hashlib.sha256(captured).hexdigest(),
            }],
        }
        metadata_path = self.root / "scratch/feature-source/dispatch/APP_CODE/capture-dispatch.json"
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    def _target(self, environment: str, *, user: str = "APP_CODE", schema: str = "APP_CODE") -> Target:
        return Target(environment, f"{environment}-conn", user, schema, environment)

    def _environment(self, environment: str, *, database: str | None = None) -> dict[str, str]:
        prefix = "STAGING" if environment == "staging" else "PROD"
        return {
            f"{prefix}_SQLCL_CONNECTION": f"{environment}-conn",
            f"{prefix}_EXPECTED_USER": "APP_CODE",
            f"{prefix}_SCHEMA": "APP_CODE",
        }

    def _record_attempt(self, plan, checkpoint: int = 1) -> str:
        from scripts import release_ownership, release_state

        with patch.object(release_state.release_capsule, "load_capsule", return_value=self.capsule):
            run_id = release_state.start_rollout(self.root, self.capsule, plan)
        guard = plan.checkpoint_guards[checkpoint - 1]
        scope = release_state.target_scope_from_identity(guard["targetIdentity"])
        identity = {
            "runId": run_id, "environment": plan.environment,
            "payloadDigest": self.capsule.payload_digest, "engineDigest": self.capsule.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": checkpoint,
            "recoveryAttempt": plan.recovery_attempt,
        }
        with release_ownership.target_writer_lease(self.root, scope, identity) as lease:
            attempt_id = release_state.record_attempt(
                self.root, self.capsule, plan, checkpoint, run_id=run_id, lease=lease,
            )
        release_state.finish_rollout(
            self.root, run_id,
            outcome={
                "state": "unknown", "databaseState": "unknown", "steps": [],
                "checkpointVerificationIds": [],
                "budget": {"seconds": getattr(self.capsule, "database_object_budget_seconds", None), "elapsedSeconds": None, "coverageComplete": False, "passed": False},
                "finalReportPath": None, "finalReportSha256": None,
            },
        )
        return attempt_id

    def _ddl_checkpoint(self, schema: str, name: str) -> FeatureCheckpoint:
        source = f"CREATE TABLE {schema}.{name} (ID NUMBER);\n".encode("ascii")
        item = FeatureInput(
            f"features/{schema.lower()}/{schema}/ddl.sql",
            source,
            hashlib.sha256(source).hexdigest(),
            None,
            "ddl",
        )
        return FeatureCheckpoint(
            schema,
            (item,),
            (),
            (QueryCheck(f"{name.lower()}-ready", "SELECT 1 FROM dual", 1),),
            ((schema, name),),
        )

    def _state_for_target(self, target: Target) -> dict:
        identity = dict(IDENTITY_FIELDS)
        identity.update(
            session_user=target.expected_user,
            current_schema=target.schema,
            db_name=target.environment.upper(),
            db_unique_name="SHARED_TARGET",
            service_name=f"{target.environment}.service",
        )
        return {
            "schema": target.schema,
            "identity": identity,
            "sourceUnits": {},
            "structural": {
                "tables": [], "columns": [], "constraints": [], "indexes": [], "triggers": [],
                "sequences": [], "synonyms": [], "views": [], "stored-code": [],
                "invalid-objects": [], "identity-columns": [], "object-grants": [], "feature-dependencies": [],
                "feature-synonym-access": [],
            },
            "coverage": {"ownerComplete": False, "selectedScopeComplete": True},
        }

    def _state(self, *, environment: str, lines: list[str] | None = None, valid: bool = True) -> dict:
        identity = dict(IDENTITY_FIELDS)
        identity.update(db_name=environment.upper(), db_unique_name=f"{environment.upper()}_UNIQUE")
        return {
            "schema": "APP_CODE",
            "identity": identity,
            "sourceUnits": {
                "PACKAGE:DISPATCH": {
                    "lines": lines if lines is not None else ["PACKAGE DISPATCH AS\n", "  PROCEDURE RUN;\n", "END DISPATCH;\n"],
                    "settings": dict(SETTINGS),
                    "valid": valid,
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

    def test_selected_state_uses_scoped_read_only_catalog_capture(self) -> None:
        from scripts import feature_release

        catalog = {
            "identity": IDENTITY_FIELDS,
            "coverage": {"sections": {name: {"complete": True} for name in feature_release.SELECTED_SECTIONS}},
            "sections": {
                "baseline-source": [
                    {"name": "DISPATCH", "type": "PACKAGE", "line": 1, "text": "PACKAGE DISPATCH AS\n"},
                    {"name": "DISPATCH", "type": "PACKAGE", "line": 2, "text": "  PROCEDURE RUN;\n"},
                    {"name": "DISPATCH", "type": "PACKAGE", "line": 3, "text": "END DISPATCH;\n"},
                ],
                "baseline-settings": [{"name": "DISPATCH", "type": "PACKAGE", **SETTINGS}],
                **{section: [] for section in feature_release.STRUCTURAL_SECTIONS},
                "stored-code": [{"name": "DISPATCH", "type": "PACKAGE", "status": "VALID"}],
            },
        }
        with patch.object(feature_release.compare_schema, "capture_environment_catalog", return_value=catalog) as capture:
            state = feature_release.capture_selected_state(self._target("staging"), self.checkpoint, self.root / "scratch/run")

        kwargs = capture.call_args.kwargs
        self.assertEqual(kwargs["scope_names"], ["DISPATCH"])
        self.assertEqual(kwargs["baseline_names"], ["DISPATCH"])
        self.assertFalse(kwargs["dba_capture"])
        self.assertTrue(kwargs["baseline_capture"])
        self.assertEqual(state["sourceUnits"]["PACKAGE:DISPATCH"]["lines"][0], "PACKAGE DISPATCH AS\n")
        self.assertIn("object-grants", state["structural"])
        self.assertFalse(state["coverage"]["ownerComplete"])
        self.assertTrue(state["coverage"]["selectedScopeComplete"])

    def test_selected_trigger_validity_uses_database_object_status(self) -> None:
        # ASHARIF 2026-10-28: validity comes from ALL_OBJECTS status, independently of trigger enablement.
        from scripts import feature_release

        owner, name = "APP_CODE", "PROBE_TRIGGER"
        source = "TRIGGER PROBE_TRIGGER\nBEFORE INSERT ON PROBE_TABLE\nFOR EACH ROW\nBEGIN NULL; END;\n"
        sql = f"CREATE OR REPLACE {source}/\n".encode()
        item = FeatureInput(
            "features/probe/APP_CODE/triggers/probe_trigger.sql", sql,
            hashlib.sha256(sql).hexdigest(), (owner, "TRIGGER", name), "stored-source",
        )
        checkpoint = FeatureCheckpoint(
            owner, (item,), (), (QueryCheck("after-trigger", "SELECT 1 FROM DUAL", 1),), ((owner, name),),
        )
        source_lines = source.splitlines(keepends=True)

        def catalog(*, enabled: str, object_status: str | None, present: bool = True) -> dict:
            sections = {section: [] for section in feature_release.STRUCTURAL_SECTIONS}
            if present:
                sections["baseline-source"] = [
                    {"name": name, "type": "TRIGGER", "line": number, "text": line}
                    for number, line in enumerate(source_lines, 1)
                ]
                sections["baseline-settings"] = [{"name": name, "type": "TRIGGER", **SETTINGS}]
                trigger = {"name": name, "status": enabled, "table_name": "PROBE_TABLE"}
                if object_status is not None:
                    trigger["object_status"] = object_status
                sections["triggers"] = [trigger]
            return {
                "identity": IDENTITY_FIELDS,
                "coverage": {"sections": {section: {"complete": True} for section in feature_release.SELECTED_SECTIONS}},
                "sections": sections,
            }

        for enabled, object_status, expected_valid in (
            ("DISABLED", "VALID", True),
            ("ENABLED", "INVALID", False),
        ):
            with self.subTest(enabled=enabled, object_status=object_status), patch.object(
                feature_release.compare_schema, "capture_environment_catalog",
                return_value=catalog(enabled=enabled, object_status=object_status),
            ):
                state = feature_release.capture_selected_state(
                    self._target("staging"), checkpoint, self.root / "scratch/trigger-run",
                )
                self.assertIs(state["sourceUnits"]["TRIGGER:PROBE_TRIGGER"]["valid"], expected_valid)
                self.assertEqual(state["structural"]["triggers"][0]["status"], enabled)

        with patch.object(
            feature_release.compare_schema, "capture_environment_catalog",
            return_value=catalog(enabled="ENABLED", object_status=None),
        ), self.assertRaisesRegex(feature_release.FeatureReleaseError, "validity state is unavailable"):
            feature_release.capture_selected_state(
                self._target("staging"), checkpoint, self.root / "scratch/trigger-run-missing-status",
            )

        with patch.object(
            feature_release.compare_schema, "capture_environment_catalog",
            return_value=catalog(enabled="DISABLED", object_status=None, present=False),
        ):
            absent = feature_release.capture_selected_state(
                self._target("staging"), checkpoint, self.root / "scratch/trigger-run-new-trigger",
            )
        self.assertEqual(absent["sourceUnits"], {})

    def test_prepare_writes_target_and_dev_diffs_without_apply_or_receipts(self) -> None:
        from scripts import feature_release

        staging_state = self._state(environment="stage", lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE RUN;\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=staging_state) as capture, \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as runner:
            plan = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environment("staging"),
            )

        self.assertEqual(plan.payload_digest, self.payload_digest)
        self.assertEqual(plan.environment, "staging")
        self.assertFalse(runner.called)
        self.assertEqual(capture.call_count, 1)
        self.assertTrue(any("dev-capture" in path for path in plan.review_paths))
        self.assertTrue(any("staging-target" in path for path in plan.review_paths))
        reviews = [json.loads((self.root / path).read_text(encoding="utf-8")) for path in plan.review_paths if path.endswith("review.json")]
        by_kind = {row["baselineKind"]: row for row in reviews}
        self.assertNotEqual(by_kind["dev-capture"]["beforeSha256"], by_kind["staging-target"]["beforeSha256"])
        self.assertTrue(all(row["targetPlanDigest"] == plan.plan_digest for row in reviews))
        guard = plan.checkpoint_guards[0]["sourceGuards"][0]
        self.assertTrue(guard["preconditions"])
        self.assertTrue(all(isinstance(query, dict) for query in guard["preconditions"]))
        self.assertTrue((self.root / "scratch/feature-release-plans/staging" / f"{self.payload_digest}.json").is_file())
        self.assertFalse((self.root / "releases/.local-state.json").exists())
        loaded = feature_release.load_target_plan(self.root, self.capsule, "staging")
        self.assertEqual(loaded.plan_digest, plan.plan_digest)

    def test_later_missing_index_table_refuses_before_first_create(self) -> None:
        from scripts import feature_release

        early = self._ddl_checkpoint("APP_CODE", "EARLY")
        index_source = b"CREATE INDEX APP_CODE.IX_MISSING ON APP_CODE.MISSING_TABLE (ID);\n"
        index_item = FeatureInput(
            "features/demo/APP_CODE/ix-missing.sql",
            index_source,
            hashlib.sha256(index_source).hexdigest(),
            None,
            "ddl",
        )
        checkpoint = replace(early, inputs=(early.inputs[0], index_item))
        capsule = replace(self.capsule, checkpoints=(checkpoint,))

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=self._state(environment="staging")), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "MISSING_TABLE"):
                feature_release.prepare_target(
                    self.root, capsule, "staging", environ=self._environment("staging"),
                )

        apply.assert_not_called()
        self.assertFalse((self.root / "releases/.local-state.json").exists())

    def test_prepare_multiple_schemas_on_one_database_uses_each_staging_migration_profile(self) -> None:
        from scripts import db_targets, feature_release

        capsule = replace(
            self.capsule,
            checkpoints=(
                self._ddl_checkpoint("APP_DATA", "DATA_TABLE"),
                self._ddl_checkpoint("APP_CODE", "CODE_TABLE"),
            ),
        )
        environ = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP_DATA,APP_CODE",
            "STAGING_SCHEMA": "APP_CODE",
            "STAGING_SQLCL_CONNECTION": "stage-read",
            "STAGING_EXPECTED_USER": "APP_CODE",
            "STAGING_MIGRATION_SCHEMA": "APP_DATA,APP_CODE",
            "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-data-migrate,stage-code-migrate",
            "STAGING_MIGRATION_EXPECTED_USER": "APP_DATA,STAGE_LOGIN",
        }
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(
                 feature_release, "capture_selected_state",
                 side_effect=lambda target, *_args: self._state_for_target(target),
             ) as capture, \
             patch.object(feature_release, "resolve_target", wraps=db_targets.resolve_target) as resolve, \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as sqlcl:
            plan = feature_release.prepare_target(self.root, capsule, "staging", environ=environ)

        self.assertEqual(
            [(call.kwargs["schema"], call.args[2]) for call in resolve.call_args_list],
            [("APP_DATA", "migration"), ("APP_CODE", "migration")],
        )
        self.assertEqual([call.args[0].connection for call in capture.call_args_list], [
            "stage-data-migrate", "stage-code-migrate",
        ])
        self.assertEqual(
            (plan.targets["schema:APP_DATA"]["connection"], plan.targets["schema:APP_DATA"]["expectedUser"]),
            ("stage-data-migrate", "APP_DATA"),
        )
        self.assertEqual(
            (plan.targets["schema:APP_CODE"]["connection"], plan.targets["schema:APP_CODE"]["expectedUser"]),
            ("stage-code-migrate", "STAGE_LOGIN"),
        )
        self.assertEqual(plan.checkpoint_guards[0]["targetIdentity"]["db_unique_name"], "SHARED_TARGET")
        self.assertEqual(plan.checkpoint_guards[1]["targetIdentity"]["db_unique_name"], "SHARED_TARGET")
        sqlcl.assert_not_called()

    def test_prod_preparation_uses_the_migration_owner_login_profile(self) -> None:
        from scripts import db_targets, feature_release

        capsule = replace(self.capsule, checkpoints=(self._ddl_checkpoint("APP_CODE", "CODE_TABLE"),))
        environ = {
            "PROD_SCHEMA": "APP_CODE",
            "PROD_SQLCL_CONNECTION": "prod-read",
            "PROD_EXPECTED_USER": "APP_CODE",
            "PROD_MIGRATION_SCHEMA": "APP_CODE",
            "PROD_MIGRATION_SQLCL_CONNECTION": "prod-migration",
            "PROD_MIGRATION_EXPECTED_USER": "PROD_DEPLOYER",
        }
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(
                 feature_release, "capture_selected_state",
                 side_effect=lambda target, *_args: self._state_for_target(target),
             ) as capture, \
             patch.object(feature_release, "resolve_target", wraps=db_targets.resolve_target) as resolve:
            plan = feature_release.prepare_target(self.root, capsule, "prod", environ=environ)

        self.assertEqual(resolve.call_args.args[2], "migration")
        self.assertEqual(capture.call_args.args[0].connection, "prod-migration")
        self.assertEqual(plan.targets["schema:APP_CODE"]["expectedUser"], "PROD_DEPLOYER")

    def test_incomplete_or_missing_migration_profile_refuses_without_read_fallback(self) -> None:
        from scripts import feature_release

        capsule = replace(self.capsule, checkpoints=(self._ddl_checkpoint("APP_DATA", "DATA_TABLE"),))
        regular_data_profile = {
            **self._environment("staging"),
            "STAGING_SCHEMA": "APP_DATA",
            "STAGING_SQLCL_CONNECTION": "stage-read-data",
            "STAGING_EXPECTED_USER": "APP_DATA",
        }
        cases = (
            {
                **regular_data_profile,
                "STAGING_MIGRATION_SCHEMA": "APP_DATA",
                "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-data",
            },
            {
                **regular_data_profile,
                "STAGING_MIGRATION_SCHEMA": "APP_CODE",
                "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-code",
                "STAGING_MIGRATION_EXPECTED_USER": "APP_CODE",
            },
        )
        for environ in cases:
            with self.subTest(environ=environ):
                with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
                     patch.object(feature_release, "capture_selected_state") as capture:
                    with self.assertRaises(feature_release.FeatureReleaseError):
                        feature_release.prepare_target(self.root, capsule, "staging", environ=environ)
                capture.assert_not_called()

    def test_target_plan_rejects_review_diff_tampering(self) -> None:
        from scripts import feature_release

        state = self._state(environment="stage")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=state):
            plan = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))
        diff_path = next(self.root / path for path in plan.review_paths if path.endswith("source.diff"))
        diff_path.write_bytes(diff_path.read_bytes() + b"tampered\n")
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "diff"):
            feature_release.load_target_plan(self.root, self.capsule, "staging")

    def test_target_preparation_handles_long_quoted_unit_names_without_long_artifact_paths(self) -> None:
        from scripts import feature_release, new_migration

        name = "Å" * 60
        lines = [f'PACKAGE "{name}" AS\n', "  PROCEDURE RUN;\n", "END;\n"]
        source = new_migration.step_sql("APP_CODE", "PACKAGE", name, lines, settings=SETTINGS).encode("utf-8")
        item = FeatureInput(
            "features/dispatch/APP_CODE/packages/quoted-unit.pks.sql",
            source,
            hashlib.sha256(source).hexdigest(),
            ("APP_CODE", "PACKAGE", name),
            "stored-source",
        )
        checkpoint = FeatureCheckpoint(
            "APP_CODE", (item,), (), (QueryCheck("ready", "SELECT 1 FROM dual", 1),), (("APP_CODE", name),),
        )
        capsule = replace(self.capsule, checkpoints=(checkpoint,))
        state = self._state(environment="staging")
        state["sourceUnits"] = {
            f"PACKAGE:{name}": {"lines": lines, "settings": dict(SETTINGS), "valid": True},
        }

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=state):
            plan = feature_release.prepare_target(
                self.root, capsule, "staging", environ=self._environment("staging"),
            )

        artifact_dirs = { (self.root / path).parent for path in plan.review_paths }
        self.assertEqual(len(artifact_dirs), 1)
        for directory in artifact_dirs:
            self.assertTrue(directory.is_dir())
            self.assertTrue(directory.name.isascii())
            self.assertLessEqual(len(directory.name.encode("ascii")), 96)
        review_path = next(self.root / path for path in plan.review_paths if path.endswith("review.json"))
        review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertEqual(review["unitName"], name)
        self.assertEqual(review["afterSha256"], hashlib.sha256(source).hexdigest())
        loaded = feature_release.load_target_plan(self.root, capsule, "staging")
        self.assertEqual(loaded.plan_digest, plan.plan_digest)

    def test_recovery_preparation_requires_an_existing_unknown_attempt(self) -> None:
        from scripts import feature_release

        state = self._state(environment="staging")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=state):
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "recovery|attempt"):
                feature_release.prepare_target(
                    self.root,
                    self.capsule,
                    "staging",
                    environ=self._environment("staging"),
                    recovery_attempt=str(uuid.uuid4()),
                )

    def test_recovery_plan_binds_observed_partial_state_to_original_attempt(self) -> None:
        from scripts import feature_release, release_state

        initial = self._state(environment="staging", lines=["PACKAGE DISPATCH AS\n", "END DISPATCH;\n"])
        partial = self._state(environment="staging", lines=["PACKAGE PARTIAL AS\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", side_effect=(initial, partial)):
            original = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environment("staging"),
            )
            attempt_id = self._record_attempt(original, 1)
            log = self.root / "scratch" / "sqlcl.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("ORA-03113: end-of-file on communication channel\n", encoding="utf-8")
            release_state.finish_attempt(
                self.root, attempt_id, state="unknown", evidence=["scratch/sqlcl.log"], metrics={},
            )
            recovery = feature_release.prepare_target(
                self.root,
                self.capsule,
                "staging",
                environ=self._environment("staging"),
                recovery_attempt=attempt_id,
                terminal_ack=f"RECONCILE {attempt_id}",
            )

        self.assertEqual(recovery.recovery_attempt, attempt_id)
        self.assertNotEqual(recovery.plan_digest, original.plan_digest)
        self.assertNotEqual(recovery.checkpoint_guards[0]["sourceGuards"], original.checkpoint_guards[0]["sourceGuards"])

    def test_recovery_preparation_accepts_reconciled_attempting_after_writer_terminated(self) -> None:
        from scripts import feature_release, release_state

        initial = self._state(environment="staging")
        partial = self._state(environment="staging", lines=["PACKAGE PARTIAL AS\n", "END DISPATCH;\n"])
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", side_effect=(initial, partial)):
            original = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environment("staging"),
            )
            attempt_id = self._record_attempt(original, 1)
            recovery = feature_release.prepare_target(
                self.root,
                self.capsule,
                "staging",
                environ=self._environment("staging"),
                recovery_attempt=attempt_id,
                terminal_ack=f"RECONCILE {attempt_id}",
            )

        self.assertEqual(recovery.recovery_attempt, attempt_id)
        self.assertEqual(recovery.recovery_binding["attemptState"], "attempting")
        self.assertEqual(recovery.recovery_binding["terminalAcknowledgment"], f"RECONCILE {attempt_id}")
        index = release_state._load_index(self.root)
        self.assertEqual(index["attempts"][0]["state"], "attempting")
        self.assertFalse(index["attempts"][0]["evidence"])

    def test_recovery_requires_ack_and_refuses_an_active_target_lease_before_capture(self) -> None:
        from scripts import feature_release, release_ownership, release_state

        initial = self._state(environment="staging")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=initial):
            original = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environment("staging"),
            )
        attempt_id = self._record_attempt(original, 1)
        old = release_state.recovery_attempt_details(self.root, self.capsule, "staging", attempt_id)
        stale_path = release_ownership.target_writer_lock_path(self.root, old["targetScope"])
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text('{"pid":99999999,"token":"stale"}\n', encoding="utf-8")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state") as capture:
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "exact terminal reconciliation"):
                feature_release.prepare_target(
                    self.root, self.capsule, "staging", environ=self._environment("staging"),
                    recovery_attempt=attempt_id,
                )
            capture.assert_not_called()

            with release_ownership.target_writer_lease(
                self.root, old["targetScope"], {"purpose": "active writer test"},
            ):
                with self.assertRaisesRegex(feature_release.FeatureReleaseError, "target writer is active"):
                    feature_release.prepare_target(
                        self.root, self.capsule, "staging", environ=self._environment("staging"),
                        recovery_attempt=attempt_id, terminal_ack=f"RECONCILE {attempt_id}",
                    )
            capture.assert_not_called()

    def test_target_plans_bind_environment_identity_before_state_and_descriptor(self) -> None:
        from scripts import feature_release

        app_dir = self.root / "apps" / "DEMO" / "100"
        deployments = app_dir / "deployments"
        deployments.mkdir(parents=True)
        (app_dir / "application.apx").write_text("app 100 {\n}\n", encoding="utf-8")
        descriptor = {"workspace": {"name": "STAGE_WS"}, "app": {"id": 100, "databaseSession": {"parsingSchema": "APP_CODE"}}}
        (deployments / "staging.json").write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
        (deployments / "prod.json").write_text(json.dumps({**descriptor, "workspace": {"name": "PROD_WS"}}) + "\n", encoding="utf-8")
        capsule_manifest = {
            "schemaVersion": 1,
            "payloadDefinition": {"steps": [{"type": "feature-migrate", "schema": "APP_CODE", "inputs": [self.item.path]}, {"type": "app-deploy", "appId": 100}]},
        }
        (self.capsule.root / "capsule.json").write_text(json.dumps(capsule_manifest), encoding="utf-8")
        staging_states = [
            self._state(environment="staging", lines=["PACKAGE STAGING AS\n", "END DISPATCH;\n"]),
            self._state(environment="staging", lines=["PACKAGE STAGING_CHANGED AS\n", "END DISPATCH;\n"]),
            self._state(environment="staging", lines=["PACKAGE STAGING AS\n", "END DISPATCH;\n"]),
        ]
        production_state = self._state(environment="prod", lines=["PACKAGE PROD AS\n", "END DISPATCH;\n"])

        def capture_state(target, _checkpoint, _run_dir):
            return staging_states.pop(0) if target.environment == "staging" else production_state

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", side_effect=capture_state):
            staging = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))
            changed_before_state = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))
            production = feature_release.prepare_target(self.root, self.capsule, "prod", environ=self._environment("prod"))

            (deployments / "staging.json").write_text(json.dumps({**descriptor, "workspace": {"name": "OTHER_WS"}}) + "\n", encoding="utf-8")
            changed_descriptor = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))

        self.assertEqual(staging.payload_digest, production.payload_digest)
        self.assertNotEqual(staging.plan_digest, production.plan_digest)
        self.assertNotEqual(staging.plan_digest, changed_before_state.plan_digest)
        self.assertNotEqual(staging.plan_digest, changed_descriptor.plan_digest)
        self.assertIn("app:100", staging.targets)
        self.assertNotEqual(staging.targets["app:100"]["descriptorSha256"], changed_descriptor.targets["app:100"]["descriptorSha256"])

    def test_auxiliary_target_bindings_use_independent_environment_profiles(self) -> None:
        from scripts import feature_release

        steps = [{"type": "sql-script", "file": "scripts/data.sql", "schema": "APP_CODE"}]
        profiles = {
            "staging": {
                "STAGING_MIGRATION_SCHEMA": "APP_CODE",
                "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-data-alias",
                "STAGING_MIGRATION_EXPECTED_USER": "STAGE_WRITER",
            },
            "prod": {
                "PROD_MIGRATION_SCHEMA": "APP_CODE",
                "PROD_MIGRATION_SQLCL_CONNECTION": "prod-data-alias",
                "PROD_MIGRATION_EXPECTED_USER": "PROD_WRITER",
            },
        }

        def observed(target, _run_dir):
            name = "STAGE" if target.environment == "staging" else "LIVE"
            return {
                "session_user": target.expected_user,
                "current_schema": target.schema,
                "db_name": name,
                "db_unique_name": f"{name}_UNIQUE",
                "service_name": f"{target.environment}.example.invalid",
                "container_id": "3" if target.environment == "staging" else "7",
                "container_name": f"{name}PDB",
                "edition": "ORA$BASE",
                "database_version": "19.0.0.0",
            }

        with patch("scripts.feature_release.rollout._observe_target_identity", side_effect=observed):
            staging = feature_release._auxiliary_targets(self.root, steps, "staging", profiles["staging"])
            production = feature_release._auxiliary_targets(self.root, steps, "prod", profiles["prod"])

        self.assertEqual(staging["step:1"]["connection"], "stage-data-alias")
        self.assertEqual(staging["step:1"]["expectedUser"], "STAGE_WRITER")
        self.assertEqual(production["step:1"]["connection"], "prod-data-alias")
        self.assertEqual(production["step:1"]["expectedUser"], "PROD_WRITER")
        stage_plan = feature_release._plan_data("staging", self.capsule, staging, (), None, None)
        prod_plan = feature_release._plan_data("prod", self.capsule, production, (), None, None)
        self.assertEqual(stage_plan["payloadDigest"], prod_plan["payloadDigest"])
        self.assertNotEqual(stage_plan["planDigest"], prod_plan["planDigest"])

    MIGRATION_FOLDER = "migrations/APP_CODE/2026-01-01_legacy-r001"
    MIGRATION_PAYLOAD = "a" * 64
    MIGRATION_CHECKS = "b" * 64
    PROFILE = {
        "STAGING_MIGRATION_SCHEMA": "APP_CODE",
        "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-data-alias",
        "STAGING_MIGRATION_EXPECTED_USER": "STAGE_WRITER",
    }
    TWO_SCHEMAS = {
        "STAGING_MIGRATION_SCHEMA": "APP_CODE,APP_OTHER",
        "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-data-alias,stage-other-alias",
        "STAGING_MIGRATION_EXPECTED_USER": "STAGE_WRITER,OTHER_WRITER",
    }
    ONE_DEV_SCHEMA_MAPPED = {
        "CODE_SCHEMA": "APP_CODE",
        "CODE_SQLCL_CONNECTION": "dev-alias",
        "CODE_EXPECTED_USER": "APP_CODE",
        "DB_ENVIRONMENT": "development",
        "STAGING_SCHEMA": "APP_STAGE",
        "STAGING_SQLCL_CONNECTION": "stage-data-alias",
        "STAGING_EXPECTED_USER": "STAGE_WRITER",
    }

    def _auxiliary_migration(
        self, steps, report, *, environ=None, logical="APP_CODE", resolved="APP_CODE", folder=None, identity=None,
    ):
        """Prepare the bindings of migrate steps with the live preflight outcome supplied, reading no database."""
        from scripts import feature_release, migration_checks, rollout

        migrations = (SimpleNamespace(
            folder=self.root / (folder or self.MIGRATION_FOLDER), schema=logical,
            payload_digest=self.MIGRATION_PAYLOAD, checks_sha256=self.MIGRATION_CHECKS,
        ),)
        target = Target("staging", "stage-data-alias", "STAGE_WRITER", resolved, "staging")
        observed = (
            dict(IDENTITY_FIELDS, session_user="STAGE_WRITER", current_schema=resolved) if identity is None else identity
        )
        values = self.PROFILE if environ is None else environ
        with patch.object(rollout, "_migration_step_target", return_value=(logical, target, migrations)), \
                patch.object(rollout, "_observe_target_identity", return_value=observed), \
                patch.object(migration_checks, "_live_report", return_value=report):
            return feature_release._auxiliary_targets(self.root, steps, "staging", values)

    def test_migrate_preflight_clear_is_recorded_with_its_folder_digests(self) -> None:
        steps = [{"type": "migrate", "folders": [self.MIGRATION_FOLDER]}]
        record = self._auxiliary_migration(steps, PreflightReport(0, (), (), {}))["step:1"]

        self.assertEqual(record["preflight"]["state"], "clear")
        self.assertEqual(record["preflight"]["exitCode"], 0)
        self.assertEqual(record["logicalSchema"], "APP_CODE")
        self.assertEqual(record["operation"], "migration")
        self.assertEqual(record["folders"], [{
            "path": self.MIGRATION_FOLDER, "payloadDigest": self.MIGRATION_PAYLOAD, "checksSha256": self.MIGRATION_CHECKS,
        }])

    def test_migrate_missing_prerequisite_created_by_an_earlier_step_is_deferred_and_recorded(self) -> None:
        conflict = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V_ORDERS"}
        steps = [{"type": "feature-migrate", "schema": "APP_CODE"}, {"type": "migrate", "folders": [self.MIGRATION_FOLDER]}]
        preflight = self._auxiliary_migration(steps, PreflightReport(1, (conflict,), (), {}))["step:2"]["preflight"]

        self.assertEqual(preflight["state"], "deferred")
        self.assertEqual(preflight["exitCode"], 1)
        self.assertEqual(preflight["conflicts"], [conflict])
        self.assertEqual(preflight["errors"], [])

    def test_migrate_preflight_refusals_stop_the_prepare_and_name_the_conflict(self) -> None:
        from scripts import feature_release

        missing = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}
        occupied = {"code": "LIVE_NAMESPACE_OCCUPIED", "name": "V", "namespace": "COMMON", "existing_types": ["TABLE"]}
        feature = {"type": "feature-migrate", "schema": "APP_CODE"}
        migrate = {"type": "migrate", "folders": [self.MIGRATION_FOLDER]}
        cases = (
            ("occupied name", [feature, migrate], PreflightReport(1, (occupied,), (), {}), "LIVE_NAMESPACE_OCCUPIED"),
            ("missing prerequisite with no earlier step", [migrate], PreflightReport(1, (missing,), (), {}), "MISSING_PREREQUISITE"),
            (
                "preflight error", [feature, migrate],
                PreflightReport(2, (), ({"code": "LIVE_PREFLIGHT_UNAVAILABLE", "message": "unreachable"},), {}),
                "LIVE_PREFLIGHT_UNAVAILABLE",
            ),
        )
        for label, steps, report, expected in cases:
            with self.subTest(label=label), self.assertRaisesRegex(feature_release.FeatureReleaseError, f"migration preflight refused.*{expected}"):
                self._auxiliary_migration(steps, report)

    def test_migrate_preflight_that_observes_another_identity_refuses(self) -> None:
        from scripts import feature_release

        report = PreflightReport(0, (), (), {"observedIdentity": dict(IDENTITY_FIELDS, session_user="STAGE_WRITER", db_unique_name="OTHER_UNIQUE")})
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "different target identity"):
            self._auxiliary_migration([{"type": "migrate", "folders": [self.MIGRATION_FOLDER]}], report)

    def test_plan_digest_changes_when_the_migrate_preflight_outcome_changes(self) -> None:
        from scripts import feature_release

        steps = [{"type": "feature-migrate", "schema": "APP_CODE"}, {"type": "migrate", "folders": [self.MIGRATION_FOLDER]}]
        missing = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}
        clear = self._auxiliary_migration(steps, PreflightReport(0, (), (), {}))
        deferred = self._auxiliary_migration(steps, PreflightReport(1, (missing,), (), {}))

        clear_digest = feature_release._plan_data("staging", self.capsule, clear, (), None, None)["planDigest"]
        deferred_digest = feature_release._plan_data("staging", self.capsule, deferred, (), None, None)["planDigest"]
        self.assertNotEqual(clear_digest, deferred_digest)

    def test_migrate_binding_is_validated_strictly_when_the_plan_is_loaded(self) -> None:
        from scripts import feature_release

        step = {"type": "migrate", "folders": [self.MIGRATION_FOLDER]}
        steps = [{"type": "feature-migrate", "schema": "APP_CODE"}, step]
        missing = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}
        clear = self._auxiliary_migration([step], PreflightReport(0, (), (), {}))["step:1"]
        deferred = self._auxiliary_migration(steps, PreflightReport(1, (missing,), (), {}))["step:2"]
        migrations = [SimpleNamespace(payload_digest=self.MIGRATION_PAYLOAD, checks_sha256=self.MIGRATION_CHECKS)]

        def validate(candidate, earlier=()):
            with patch.object(feature_release, "load_batch", return_value=migrations):
                feature_release._validate_migration_binding(self.root, 1, step, candidate, list(earlier))

        validate(clear)
        validate(deferred, earlier=["APP_CODE"])
        invalid = {
            "folder digest changed": dict(clear, folders=[dict(clear["folders"][0], payloadDigest="c" * 64)]),
            "extra preflight field": dict(clear, preflight=dict(clear["preflight"], extra=1)),
            "clear with a conflict": dict(clear, preflight=dict(clear["preflight"], conflicts=[missing])),
            "malformed report digest": dict(clear, preflight=dict(clear["preflight"], reportSha256="not-a-digest")),
            "deferred without an earlier step": deferred,
        }
        for label, candidate in invalid.items():
            with self.subTest(label=label), self.assertRaises(feature_release.FeatureReleaseError):
                validate(candidate)

    def test_plan_schema_version_three_is_refused(self) -> None:
        from scripts import feature_release

        document = {
            "schemaVersion": 3, "environment": "staging", "payloadDigest": self.payload_digest,
            "engineDigest": self.engine_digest, "sourceCommit": self.capsule.source_commit, "recoveryAttempt": None,
            "recoveryBinding": None, "targets": {}, "checkpointGuards": [], "reviewPaths": [], "planDigest": "0" * 64,
        }
        path = self.root / "scratch" / "feature-release-plans" / "staging" / f"{self.payload_digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "schemaVersion is unsupported"):
            feature_release.load_target_plan(self.root, self.capsule, "staging")

    def _stored_plan(self, steps, targets):
        """Write a canonical stored plan for these steps and bindings, then load it through load_target_plan."""
        from scripts import feature_release

        guards = [{"checkpoint": 1, "schema": "APP_CODE", "targetKey": "schema:APP_CODE", "reviews": []}]
        plan = feature_release._plan_data("staging", self.capsule, targets, guards, None, None)
        path = self.root / "scratch" / "feature-release-plans" / "staging" / f"{self.payload_digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        migrations = [SimpleNamespace(payload_digest=self.MIGRATION_PAYLOAD, checks_sha256=self.MIGRATION_CHECKS)]
        with patch.object(feature_release, "_manifest_steps", return_value=steps), \
                patch.object(feature_release, "_checkpoint_guard", return_value={}), \
                patch.object(feature_release, "load_batch", return_value=migrations):
            return feature_release.load_target_plan(self.root, self.capsule, "staging")

    def _stored_targets(self, *, feature_schema="APP_CODE", migrate_schema="APP_CODE", state="clear"):
        """Bindings for one feature checkpoint and one migrate step, as prepare records them."""
        identity = dict(IDENTITY_FIELDS, session_user="STAGE_WRITER", current_schema=migrate_schema)
        feature_binding = {
            "environment": "staging", "connection": "stage-data-alias", "expectedUser": "STAGE_WRITER",
            "schema": feature_schema, "classification": "staging", "observed": dict(identity, current_schema=feature_schema),
        }
        preflight = {"state": state, "exitCode": 0, "conflicts": [], "errors": [], "reportSha256": "d" * 64}
        if state == "deferred":
            preflight.update(exitCode=1, conflicts=[{"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}])
        migrate_binding = {
            "environment": "staging", "operation": "migration", "logicalSchema": "APP_CODE",
            "connection": "stage-data-alias", "expectedUser": "STAGE_WRITER", "schema": migrate_schema,
            "classification": "staging", "observed": identity,
            "folders": [{"path": self.MIGRATION_FOLDER, "payloadDigest": self.MIGRATION_PAYLOAD, "checksSha256": self.MIGRATION_CHECKS}],
            "preflight": preflight,
        }
        return {f"schema:{feature_schema}": feature_binding, "step:2": migrate_binding}

    def test_stored_plan_re_derives_a_deferred_state_from_its_own_bindings(self) -> None:
        from scripts import feature_release

        migrate = {"type": "migrate", "folders": [self.MIGRATION_FOLDER]}
        earlier_same = [{"type": "feature-migrate", "schema": "APP_CODE"}, migrate]
        loaded = self._stored_plan(earlier_same, self._stored_targets(state="deferred"))
        self.assertEqual(loaded.targets["step:2"]["preflight"]["state"], "deferred")

        earlier_other = [{"type": "feature-migrate", "schema": "APP_OTHER"}, migrate]
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "inconsistent with its recorded outcome"):
            self._stored_plan(earlier_other, self._stored_targets(feature_schema="APP_OTHER", state="deferred"))

        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "no resolved schema binding for feature step 1"):
            self._stored_plan(earlier_other, self._stored_targets(feature_schema="APP_CODE", state="deferred"))

    def test_stored_migrate_binding_is_refused_when_incomplete_or_inexact(self) -> None:
        from scripts import feature_release

        steps = [{"type": "feature-migrate", "schema": "APP_CODE"}, {"type": "migrate", "folders": [self.MIGRATION_FOLDER]}]
        base = self._stored_targets()
        self._stored_plan(steps, copy.deepcopy(base))

        def mutate(change):
            targets = copy.deepcopy(base)
            change(targets["step:2"])
            return targets

        cases = (
            ("missing folders", lambda r: r.pop("folders")),
            ("missing preflight", lambda r: r.pop("preflight")),
            ("extra key", lambda r: r.update(extra=1)),
            ("folder row with a wrong digest", lambda r: r["folders"][0].update(payloadDigest="c" * 64)),
            ("preflight with an extra key", lambda r: r["preflight"].update(extra=1)),
            ("preflight missing reportSha256", lambda r: r["preflight"].pop("reportSha256")),
            ("non-hex reportSha256", lambda r: r["preflight"].update(reportSha256="G" * 64)),
            ("uppercase reportSha256", lambda r: r["preflight"].update(reportSha256="A" * 64)),
            ("unknown state", lambda r: r["preflight"].update(state="bogus")),
        )
        for label, change in cases:
            with self.subTest(label=label), self.assertRaises(feature_release.FeatureReleaseError):
                self._stored_plan(steps, mutate(change))

    def test_flat_migration_folder_defers_to_an_earlier_checkpoint_on_the_same_resolved_schema(self) -> None:
        flat = "migrations/2026-01-01_x-r001"
        steps = [{"type": "feature-migrate", "schema": "APP_CODE"}, {"type": "migrate", "folders": [flat]}]
        missing = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}
        record = self._auxiliary_migration(
            steps, PreflightReport(1, (missing,), (), {}), logical=None, folder=flat,
        )["step:2"]

        self.assertIsNone(record["logicalSchema"])
        self.assertEqual(record["schema"], "APP_CODE")
        self.assertEqual(record["preflight"]["state"], "deferred")

    def test_missing_prerequisite_from_an_earlier_checkpoint_on_another_resolved_schema_is_refused(self) -> None:
        from scripts import feature_release

        missing = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}
        steps = [
            {"type": "feature-migrate", "schema": "APP_OTHER"},
            {"type": "migrate", "folders": [self.MIGRATION_FOLDER]},
        ]
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "migration preflight refused.*MISSING_PREREQUISITE"):
            self._auxiliary_migration(steps, PreflightReport(1, (missing,), (), {}), environ=self.TWO_SCHEMAS)

    def test_one_dev_schema_mapped_to_a_differently_named_staging_schema_defers_on_the_resolved_name(self) -> None:
        missing = {"code": "MISSING_PREREQUISITE", "name": "ORDERS", "required_by": "V"}
        steps = [
            {"type": "feature-migrate", "schema": "APP_CODE"},
            {"type": "migrate", "folders": [self.MIGRATION_FOLDER]},
        ]
        targets = self._auxiliary_migration(
            steps, PreflightReport(1, (missing,), (), {}), environ=self.ONE_DEV_SCHEMA_MAPPED,
            logical="APP_CODE", resolved="APP_STAGE",
        )

        self.assertEqual(targets["step:2"]["logicalSchema"], "APP_CODE")
        self.assertEqual(targets["step:2"]["schema"], "APP_STAGE")
        self.assertEqual(targets["step:2"]["preflight"]["state"], "deferred")

    def test_migration_only_capsule_without_its_step_list_is_refused_not_truncated(self) -> None:
        from scripts import feature_release

        capsule = replace(self.capsule, checkpoints=())
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "step list is unavailable"):
            feature_release._manifest_steps(capsule)

    def test_migration_only_capsule_prepares_and_loads_a_plan_with_no_checkpoint_guards(self) -> None:
        from scripts import feature_release, migration_checks, rollout

        capsule = replace(self.capsule, checkpoints=())
        steps = [{"type": "migrate", "folders": [self.MIGRATION_FOLDER]}]
        migrations = (SimpleNamespace(
            folder=self.root / self.MIGRATION_FOLDER, schema="APP_CODE",
            payload_digest=self.MIGRATION_PAYLOAD, checks_sha256=self.MIGRATION_CHECKS,
        ),)
        target = Target("staging", "stage-data-alias", "STAGE_WRITER", "APP_CODE", "staging")
        observed = dict(IDENTITY_FIELDS, session_user="STAGE_WRITER")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
                patch.object(feature_release, "_manifest_steps", return_value=steps), \
                patch.object(rollout, "_migration_step_target", return_value=("APP_CODE", target, migrations)), \
                patch.object(rollout, "_observe_target_identity", return_value=observed), \
                patch.object(migration_checks, "_live_report", return_value=PreflightReport(0, (), (), {})):
            plan = feature_release.prepare_target(self.root, capsule, "staging", environ=self.PROFILE)

        self.assertEqual(plan.checkpoint_guards, ())
        self.assertEqual(set(plan.targets), {"step:1"})
        self.assertEqual(plan.targets["step:1"]["preflight"]["state"], "clear")
        with patch.object(feature_release, "_manifest_steps", return_value=steps), \
                patch.object(feature_release, "load_batch", return_value=list(migrations)):
            loaded = feature_release.load_target_plan(self.root, capsule, "staging")
        self.assertEqual(loaded.plan_digest, plan.plan_digest)

        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
                self.assertRaisesRegex(feature_release.FeatureReleaseError, "no feature checkpoint"):
            feature_release.prepare_target(
                self.root, capsule, "staging", environ=self.PROFILE,
                recovery_attempt="attempt-1", terminal_ack="RECONCILE attempt-1",
            )

    def test_incomplete_or_conflicting_target_identity_refuses_preparation(self) -> None:
        from scripts import feature_release

        incomplete = self._state(environment="stage")
        incomplete["identity"].pop("container_id")
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=incomplete):
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "identity"):
                feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))

        wrong_schema = self._state(environment="stage")
        wrong_schema["identity"]["current_schema"] = "OTHER_SCHEMA"
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=wrong_schema):
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "schema"):
                feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))

    def test_checkpoints_with_conflicting_editions_refuse_a_shared_target_plan(self) -> None:
        from scripts import feature_release

        two_checkpoints = replace(self.capsule, checkpoints=(self.checkpoint, self.checkpoint))
        first = self._state(environment="staging")
        second = self._state(environment="staging")
        second["identity"]["edition"] = "OTHER_EDITION"
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=two_checkpoints), \
             patch.object(feature_release, "capture_selected_state", side_effect=(first, second)):
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "edition"):
                feature_release.prepare_target(self.root, two_checkpoints, "staging", environ=self._environment("staging"))

    def test_no_op_comparison_ignores_header_case_quoting_and_setting_types(self) -> None:
        from scripts import feature_release

        # A captured header is rebuilt with upper-case keywords, a quoted name and blanks where the
        # owner qualifier was. The live text keeps what the developer wrote. Only the header may differ.
        frozen = ["PACKAGE" + " " * 10 + '"DISPATCH" AS\n', "  procedure run;\n", "END DISPATCH;\n"]
        self.assertTrue(feature_release._same_stored_source(
            ["package DISPATCH as\n", "  procedure run;\n", "END DISPATCH;\n"], frozen,
        ))
        self.assertFalse(feature_release._same_stored_source(
            ["package DISPATCH as\n", "  procedure stop;\n", "END DISPATCH;\n"], frozen,
        ))
        self.assertFalse(feature_release._same_stored_source(
            ["package OTHER as\n", "  procedure run;\n", "END DISPATCH;\n"], frozen,
        ))
        self.assertFalse(feature_release._same_stored_source(
            ["package body DISPATCH as\n", "  procedure run;\n", "END DISPATCH;\n"], frozen,
        ))

        self.assertTrue(feature_release._same_settings(
            {"plsql_optimize_level": "2", "plsql_ccflags": ""},
            {"plsql_optimize_level": 2, "plsql_ccflags": None},
        ))
        self.assertFalse(feature_release._same_settings(
            {"plsql_optimize_level": "3", "plsql_ccflags": ""},
            {"plsql_optimize_level": 2, "plsql_ccflags": None},
        ))

    def test_only_identical_valid_stored_unit_is_marked_noop(self) -> None:
        from scripts import feature_release

        # ALL_SOURCE blanks the APP_CODE. owner qualifier of the header and keeps its columns.
        exact_lines = ["PACKAGE" + " " * 10 + "DISPATCH AS\n", "  PROCEDURE RUN;\n", "END DISPATCH;\n"]
        exact = self._state(environment="stage", lines=exact_lines, valid=True)
        exact["sourceUnits"]["PACKAGE:DISPATCH"]["settings"] = dict(SETTINGS)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=exact):
            no_op = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))
        self.assertTrue(no_op.checkpoint_guards[0]["sourceGuards"][0]["noOp"])

        invalid = self._state(environment="stage", lines=exact_lines, valid=False)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=self.capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=invalid):
            needs_apply = feature_release.prepare_target(self.root, self.capsule, "staging", environ=self._environment("staging"))
        self.assertFalse(needs_apply.checkpoint_guards[0]["sourceGuards"][0]["noOp"])

    def test_new_stored_unit_requires_complete_compiler_settings(self) -> None:
        from scripts import feature_release

        source = (
            b"ALTER SESSION SET PLSQL_CCFLAGS = 'branch:TRUE';\n"
            b"CREATE OR REPLACE PACKAGE APP_CODE.NEW_PACKAGE AS\n"
            b"  PROCEDURE RUN;\n"
            b"END NEW_PACKAGE;\n/\n"
        )
        item = FeatureInput(
            "features/new-package/APP_CODE/packages/new_package.pks.sql",
            source,
            hashlib.sha256(source).hexdigest(),
            ("APP_CODE", "PACKAGE", "NEW_PACKAGE"),
            "stored-source",
        )
        checkpoint = replace(self.checkpoint, inputs=(item,), object_keys=(("APP_CODE", "NEW_PACKAGE"),))
        capsule = replace(self.capsule, checkpoints=(checkpoint,))
        state = self._state(environment="staging")
        state["sourceUnits"] = {}

        with patch("scripts.feature_release.release_capsule.load_capsule", return_value=capsule), \
             patch("scripts.feature_release.capture_selected_state", return_value=state):
            with self.assertRaisesRegex(feature_release.FeatureReleaseError, "needs complete compiler settings"):
                feature_release.prepare_target(
                    self.root, capsule, "staging", environ=self._environment("staging"),
                )

    def test_prepared_source_guard_rejects_a_self_consistent_but_unbound_settings_prologue(self) -> None:
        from scripts import feature_release

        state = self._state(environment="staging")
        with patch("scripts.feature_release.release_capsule.load_capsule", return_value=self.capsule), \
             patch("scripts.feature_release.capture_selected_state", return_value=state):
            plan = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environment("staging"),
            )
        guards = [dict(row) for row in plan.checkpoint_guards]
        source_guards = [dict(row) for row in guards[0]["sourceGuards"]]
        source_guards[0]["settingsPrologue"] = "ALTER SESSION SET PLSQL_CCFLAGS = 'tampered';\n"
        source_guards[0]["settingsPrologueSha256"] = hashlib.sha256(
            source_guards[0]["settingsPrologue"].encode("utf-8")
        ).hexdigest()
        guards[0]["sourceGuards"] = source_guards
        tampered = replace(plan, checkpoint_guards=tuple(guards))

        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "settings prologue changed"):
            feature_release._checkpoint_guard(tampered, 1, self.checkpoint)

    def test_new_unit_guard_has_no_before_digest_and_still_validates(self) -> None:
        # A unit that does not exist yet has no before-state, so its guard has no
        # beforeSourceSha256. Validation must accept that shape when it is reproduced.
        from scripts import feature_release

        state = self._state(environment="staging")
        state["sourceUnits"] = {}
        with patch("scripts.feature_release.release_capsule.load_capsule", return_value=self.capsule), \
             patch("scripts.feature_release.capture_selected_state", return_value=state):
            plan = feature_release.prepare_target(
                self.root, self.capsule, "staging", environ=self._environment("staging"),
            )
        self.assertFalse(plan.checkpoint_guards[0]["sourceGuards"][0]["targetExists"])
        guard = feature_release._checkpoint_guard(plan, 1, self.checkpoint)
        self.assertIsNone(guard["sourceGuards"][0]["beforeSourceSha256"])

    def test_no_op_comparison_ignores_a_missing_final_newline_and_a_trigger_owner_qualifier(self) -> None:
        from scripts import feature_release

        # ALL_SOURCE may leave the last line of a type specification without its newline, and a captured
        # file always ends its lines with one. The exact SQL check of that line already accepts both.
        frozen = ['TYPE          "ORDER_T" as object (id number)\n', "            \n"]
        self.assertTrue(feature_release._same_stored_source(["type ORDER_T as object (id number)\n", "            "], frozen))
        self.assertFalse(feature_release._same_stored_source(["type ORDER_T as object (id number)\n", "            x"], frozen))
        # ALL_SOURCE keeps the owner of a trigger header as written, but DEV's unqualified text has none.
        qualified = ['TRIGGER "APP_CODE"."ORDERS_TRG" before insert on ORDERS for each row\n', "            begin null; end;\n"]
        self.assertTrue(feature_release._same_stored_source(
            ["trigger ORDERS_TRG before insert on ORDERS for each row\n", "            begin null; end;\n"], qualified,
        ))
        self.assertTrue(feature_release._same_stored_source(qualified, qualified))
        self.assertFalse(feature_release._same_stored_source(
            ["trigger OTHER_TRG before insert on ORDERS for each row\n", "            begin null; end;\n"], qualified,
        ))
        # Only the unit's own owner is ignored: the owner of the trigger's table still has to match.
        self.assertFalse(feature_release._same_stored_source(
            ['TRIGGER "ORDERS_TRG" before insert on "OTHER"."ORDERS" for each row\n', "begin null; end;\n"],
            ['TRIGGER "ORDERS_TRG" before insert on "APP_CODE"."ORDERS" for each row\n', "begin null; end;\n"],
        ))
        self.assertEqual(feature_release._header_tokens('TYPE BODY "APP_CODE"."ORDER_T" as'), ["TYPE", "BODY", "ORDER_T", "AS"])

    def test_stored_source_keeps_a_trigger_owner_and_blanks_a_function_owner(self) -> None:
        import hashlib
        from scripts import feature_release
        from scripts.feature_manifest import FeatureInput

        def unit_input(path: str, sql: str, unit: tuple[str, str, str]) -> FeatureInput:
            source = sql.encode("ascii")
            return FeatureInput(path, source, hashlib.sha256(source).hexdigest(), unit, "stored-source")

        trigger, _ = feature_release._stored_source(unit_input(
            "features/probe/APP_CODE/triggers/orders_trg.trg.sql",
            'CREATE OR REPLACE TRIGGER "APP_CODE"."ORDERS_TRG"\nBEFORE INSERT ON ORDERS\nFOR EACH ROW\nBEGIN NULL; END;\n/\n',
            ("APP_CODE", "TRIGGER", "ORDERS_TRG"),
        ))
        self.assertEqual(trigger["lines"][0], 'TRIGGER "APP_CODE"."ORDERS_TRG"\n')
        function, _ = feature_release._stored_source(unit_input(
            "features/probe/APP_CODE/functions/get_one.fnc.sql",
            'CREATE OR REPLACE FUNCTION "APP_CODE"."GET_ONE" return number is begin return 1; end;\n/\n',
            ("APP_CODE", "FUNCTION", "GET_ONE"),
        ))
        self.assertNotIn('"APP_CODE"', function["lines"][0])
        self.assertIn('"GET_ONE"', function["lines"][0])

    def test_a_type_body_is_recreated_when_its_specification_is_recreated(self) -> None:
        from scripts import feature_release

        # Creating a specification invalidates its body even when the body text is unchanged.
        header = "".join(f"ALTER SESSION SET {name} = {value};\n" for name, value in (
            ("PLSQL_OPTIMIZE_LEVEL", "2"), ("PLSQL_CODE_TYPE", "INTERPRETED"), ("PLSQL_DEBUG", "FALSE"),
            ("PLSQL_WARNINGS", "'ENABLE:ALL'"), ("PLSQL_CCFLAGS", "''"), ("NLS_LENGTH_SEMANTICS", "BYTE"),
            ("PLSCOPE_SETTINGS", "'IDENTIFIERS:NONE'"),
        ))
        spec_sql = header + "CREATE OR REPLACE TYPE APP_CODE.ORDER_T AS OBJECT (id NUMBER, MEMBER FUNCTION total RETURN NUMBER)\n/\n"
        body_sql = (header + "CREATE OR REPLACE TYPE BODY APP_CODE.ORDER_T AS\n"
                    "  MEMBER FUNCTION total RETURN NUMBER IS BEGIN RETURN 1; END;\nEND;\n/\n")
        spec = FeatureInput("features/orders/APP_CODE/types/order_t.tps.sql", spec_sql.encode("ascii"), "0" * 64,
                            ("APP_CODE", "TYPE", "ORDER_T"), "stored-source")
        body = FeatureInput("features/orders/APP_CODE/types/order_t.tpb.sql", body_sql.encode("ascii"), "0" * 64,
                            ("APP_CODE", "TYPE BODY", "ORDER_T"), "stored-source")
        checkpoint = FeatureCheckpoint("APP_CODE", (spec, body), (), (QueryCheck("after-type", "SELECT 1 FROM dual", 1),),
                                       (("APP_CODE", "ORDER_T"),))
        spec_lines, _ = feature_release._stored_source(spec)
        body_lines, _ = feature_release._stored_source(body)

        def guards(live_spec: list[str]) -> dict:
            state = self._state(environment="staging")
            state["sourceUnits"] = {
                "TYPE:ORDER_T": {"lines": live_spec, "settings": dict(SETTINGS), "valid": True},
                "TYPE BODY:ORDER_T": {"lines": list(body_lines["lines"]), "settings": dict(SETTINGS), "valid": True},
            }
            return {guard["unit"]["type"]: guard for guard in feature_release._source_guards(checkpoint, state, 1)}

        unchanged = guards(list(spec_lines["lines"]))
        self.assertTrue(unchanged["TYPE"]["noOp"])
        self.assertTrue(unchanged["TYPE BODY"]["noOp"])
        replaced = guards(["TYPE ORDER_T AS OBJECT (id NUMBER)\n"])
        self.assertFalse(replaced["TYPE"]["noOp"])
        self.assertFalse(replaced["TYPE BODY"]["noOp"])

    def test_a_package_body_is_recreated_when_its_specification_is_recreated(self) -> None:
        from scripts import feature_release

        # Oracle invalidates a package body when its specification is replaced, as it does for a type body.
        header = "".join(f"ALTER SESSION SET {name} = {value};\n" for name, value in (
            ("PLSQL_OPTIMIZE_LEVEL", "2"), ("PLSQL_CODE_TYPE", "INTERPRETED"), ("PLSQL_DEBUG", "FALSE"),
            ("PLSQL_WARNINGS", "'ENABLE:ALL'"), ("PLSQL_CCFLAGS", "''"), ("NLS_LENGTH_SEMANTICS", "BYTE"),
            ("PLSCOPE_SETTINGS", "'IDENTIFIERS:NONE'"),
        ))
        spec_sql = header + "CREATE OR REPLACE PACKAGE APP_CODE.ORDER_API AS\n  FUNCTION total RETURN NUMBER;\nEND;\n/\n"
        body_sql = (header + "CREATE OR REPLACE PACKAGE BODY APP_CODE.ORDER_API AS\n"
                    "  FUNCTION total RETURN NUMBER IS BEGIN RETURN 1; END;\nEND;\n/\n")
        spec = FeatureInput("features/orders/APP_CODE/packages/order_api.pks.sql", spec_sql.encode("ascii"), "0" * 64,
                            ("APP_CODE", "PACKAGE", "ORDER_API"), "stored-source")
        body = FeatureInput("features/orders/APP_CODE/packages/order_api.pkb.sql", body_sql.encode("ascii"), "0" * 64,
                            ("APP_CODE", "PACKAGE BODY", "ORDER_API"), "stored-source")
        checkpoint = FeatureCheckpoint("APP_CODE", (spec, body), (), (QueryCheck("after-package", "SELECT 1 FROM dual", 1),),
                                       (("APP_CODE", "ORDER_API"),))
        spec_lines, _ = feature_release._stored_source(spec)
        body_lines, _ = feature_release._stored_source(body)

        def guards(live_spec: list[str]) -> dict:
            state = self._state(environment="staging")
            state["sourceUnits"] = {
                "PACKAGE:ORDER_API": {"lines": live_spec, "settings": dict(SETTINGS), "valid": True},
                "PACKAGE BODY:ORDER_API": {"lines": list(body_lines["lines"]), "settings": dict(SETTINGS), "valid": True},
            }
            return {guard["unit"]["type"]: guard for guard in feature_release._source_guards(checkpoint, state, 1)}

        unchanged = guards(list(spec_lines["lines"]))
        self.assertTrue(unchanged["PACKAGE"]["noOp"])
        self.assertTrue(unchanged["PACKAGE BODY"]["noOp"])
        replaced = guards(["PACKAGE ORDER_API AS\n", "END;\n"])
        self.assertFalse(replaced["PACKAGE"]["noOp"])
        self.assertFalse(replaced["PACKAGE BODY"]["noOp"])

    # A release that re-creates a trigger leaves it enabled, so prepare refuses a DISABLED one it would change.
    TRIGGER_SQL = (
        "ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\n"
        "ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\n"
        "ALTER SESSION SET PLSQL_DEBUG = FALSE;\n"
        "ALTER SESSION SET PLSQL_WARNINGS = 'ENABLE:ALL';\n"
        "ALTER SESSION SET PLSQL_CCFLAGS = '';\n"
        "ALTER SESSION SET NLS_LENGTH_SEMANTICS = BYTE;\n"
        "ALTER SESSION SET PLSCOPE_SETTINGS = 'IDENTIFIERS:NONE';\n"
        "CREATE OR REPLACE TRIGGER APP_CODE.PROBE_TRIGGER\nBEFORE INSERT ON PROBE_TABLE\nFOR EACH ROW\nBEGIN NULL; END;\n/\n"
    )

    def _trigger_checkpoint(self) -> FeatureCheckpoint:
        from scripts.migration_manifest import QueryCheck

        source = self.TRIGGER_SQL.encode("ascii")
        item = FeatureInput(
            "features/probe/APP_CODE/triggers/probe_trigger.trg.sql", source,
            hashlib.sha256(source).hexdigest(), ("APP_CODE", "TRIGGER", "PROBE_TRIGGER"), "stored-source",
        )
        return FeatureCheckpoint(
            "APP_CODE", (item,), (), (QueryCheck("after-trigger", "SELECT 1 FROM dual", 1),), (("APP_CODE", "PROBE_TRIGGER"),),
        )

    def _trigger_release(self, checkpoint: FeatureCheckpoint):
        return ReleaseCapsule(
            self.capsule.root, self.capsule.manifest_sha256, self.payload_digest, self.engine_digest,
            self.capsule.source_commit, (checkpoint,), (),
        )

    def _trigger_state(self, checkpoint: FeatureCheckpoint, *, status: str | None, same_source: bool, present: bool = True) -> dict:
        from scripts import feature_release

        state = self._state(environment="stage")
        state["sourceUnits"] = {}
        state["structural"]["triggers"] = []
        if present:
            expected, _overrides = feature_release._stored_source(checkpoint.inputs[0])
            lines = list(expected["lines"]) if same_source else ["TRIGGER PROBE_TRIGGER\n", "BEFORE INSERT ON PROBE_TABLE\n", "FOR EACH ROW\n", "BEGIN NULL; NULL; END;\n"]
            state["sourceUnits"]["TRIGGER:PROBE_TRIGGER"] = {"lines": lines, "settings": dict(SETTINGS), "valid": True}
            if status is not None:
                state["structural"]["triggers"] = [{"name": "PROBE_TRIGGER", "table_name": "PROBE_TABLE", "status": status, "object_status": "VALID"}]
        return state

    def _prepare(self, checkpoint: FeatureCheckpoint, state: dict):
        from scripts import feature_release

        capsule = self._trigger_release(checkpoint)
        with patch.object(feature_release.release_capsule, "load_capsule", return_value=capsule), \
             patch.object(feature_release, "capture_selected_state", return_value=state):
            return feature_release.prepare_target(self.root, capsule, "staging", environ=self._environment("staging"))

    def test_a_disabled_trigger_the_release_would_replace_refuses_prepare_and_names_it(self) -> None:
        from scripts import feature_release

        checkpoint = self._trigger_checkpoint()
        state = self._trigger_state(checkpoint, status="DISABLED", same_source=False)
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "trigger PROBE_TRIGGER is DISABLED.*reviewed migration"):
            self._prepare(checkpoint, state)
        self.assertFalse((self.root / "scratch/feature-release-plans/staging" / f"{self.payload_digest}.json").exists())

    def test_an_enabled_trigger_is_allowed_and_its_enablement_is_checked_again_before_the_write(self) -> None:
        checkpoint = self._trigger_checkpoint()
        plan = self._prepare(checkpoint, self._trigger_state(checkpoint, status="ENABLED", same_source=False))

        guard = plan.checkpoint_guards[0]["sourceGuards"][0]
        self.assertFalse(guard["noOp"])
        enabled = [query for query in guard["preconditions"] if query["id"].startswith("enabled-")]
        self.assertEqual(len(enabled), 1)
        self.assertIn("ALL_TRIGGERS", enabled[0]["sql"])
        self.assertIn("STATUS = 'ENABLED'", enabled[0]["sql"])
        self.assertEqual(enabled[0]["expected"], 1)

    def test_a_trigger_that_is_absent_on_the_target_is_allowed(self) -> None:
        checkpoint = self._trigger_checkpoint()
        plan = self._prepare(checkpoint, self._trigger_state(checkpoint, status=None, same_source=False, present=False))
        self.assertFalse(plan.checkpoint_guards[0]["sourceGuards"][0]["targetExists"])

    def test_a_disabled_trigger_that_is_already_current_is_allowed_because_nothing_is_replaced(self) -> None:
        checkpoint = self._trigger_checkpoint()
        plan = self._prepare(checkpoint, self._trigger_state(checkpoint, status="DISABLED", same_source=True))

        guard = plan.checkpoint_guards[0]["sourceGuards"][0]
        self.assertTrue(guard["noOp"])
        self.assertFalse([query for query in guard["preconditions"] if query["id"].startswith("enabled-")])

    def test_a_captured_trigger_keeps_its_dev_baseline_for_the_prepare_review(self) -> None:
        from scripts import baseline, feature_release, feature_sources

        dev_lines = ["TRIGGER PROBE_TRIGGER\n", "BEFORE INSERT ON PROBE_TABLE\n", "FOR EACH ROW\n", "BEGIN NULL; END;\n"]
        catalog = {
            "identity": {**IDENTITY_FIELDS, "db_name": "DEV"},
            "coverage": {"sections": {"baseline-source": {"complete": True}, "baseline-settings": {"complete": True}}},
            "unavailable": {},
            "sections": {
                "baseline-source": [
                    {"name": "PROBE_TRIGGER", "type": "TRIGGER", "line": number, "text": line} for number, line in enumerate(dev_lines, start=1)
                ],
                "baseline-settings": [{"name": "PROBE_TRIGGER", "type": "TRIGGER", **SETTINGS}],
            },
        }
        environ = {"CODE_SQLCL_CONNECTION": "dev_code", "CODE_EXPECTED_USER": "APP_CODE", "CODE_SCHEMA": "APP_CODE"}
        with patch.object(baseline, "_capture", return_value=catalog):
            (path,) = feature_sources.capture_unit(self.root, "probe", "APP_CODE", "trigger", "PROBE_TRIGGER", environ=environ)
        source = path.read_bytes()
        item = FeatureInput(
            path.relative_to(self.root).as_posix(), source, hashlib.sha256(source).hexdigest(),
            ("APP_CODE", "TRIGGER", "PROBE_TRIGGER"), "stored-source",
        )
        from scripts.migration_manifest import QueryCheck

        checkpoint = FeatureCheckpoint(
            "APP_CODE", (item,), (), (QueryCheck("after-trigger", "SELECT 1 FROM dual", 1),), (("APP_CODE", "PROBE_TRIGGER"),),
        )
        state = self._trigger_state(checkpoint, status="ENABLED", same_source=False)

        plan = self._prepare(checkpoint, state)

        self.assertTrue(any("/dev-capture/" in review for review in plan.review_paths), plan.review_paths)
        self.assertTrue(any("/staging-target/" in review for review in plan.review_paths), plan.review_paths)
        reviews = [json.loads((self.root / review).read_text(encoding="utf-8")) for review in plan.review_paths if review.endswith("review.json")]
        self.assertEqual(sorted(row["baselineKind"] for row in reviews), ["dev-capture", "staging-target"])
        self.assertTrue(all((row["unitType"], row["unitName"]) == ("TRIGGER", "PROBE_TRIGGER") for row in reviews))
        self.assertIsNotNone(feature_release.load_target_plan(self.root, self._trigger_release(checkpoint), "staging"))

    def test_an_existing_trigger_whose_enablement_state_is_not_observed_refuses_prepare(self) -> None:
        from scripts import feature_release

        checkpoint = self._trigger_checkpoint()
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "enablement state is unavailable for selected TRIGGER PROBE_TRIGGER"):
            self._prepare(checkpoint, self._trigger_state(checkpoint, status=None, same_source=False))


    # A replaced specification invalidates its body. The release must carry a body that the target has.
    def _dispatch_body_item(self) -> FeatureInput:
        header = self.source[: self.source.index(b"CREATE OR REPLACE")]
        sql = header + b"CREATE OR REPLACE PACKAGE BODY APP_CODE.DISPATCH AS\n  PROCEDURE RUN IS BEGIN NULL; END;\nEND DISPATCH;\n/\n"
        return FeatureInput(
            "features/dispatch/APP_CODE/packages/dispatch.pkb.sql", sql, hashlib.sha256(sql).hexdigest(),
            ("APP_CODE", "PACKAGE BODY", "DISPATCH"), "stored-source",
        )

    def _dispatch_stored_code(self, *, body: bool) -> list[dict]:
        rows = [{"name": "DISPATCH", "type": "PACKAGE", "status": "VALID"}]
        if body:
            rows.append({"name": "DISPATCH", "type": "PACKAGE BODY", "status": "VALID"})
        return rows

    def test_a_replaced_specification_whose_body_is_on_the_target_but_not_in_the_release_is_refused(self) -> None:
        from scripts import feature_release

        state = self._state(environment="stage", lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE OLD;\n", "END DISPATCH;\n"])
        state["structural"]["stored-code"] = self._dispatch_stored_code(body=True)

        with self.assertRaisesRegex(
            feature_release.FeatureReleaseError,
            r"package DISPATCH is replaced by this release, but its PACKAGE BODY exists on staging.*"
            r"--package DISPATCH --schema APP_CODE --env dev",
        ):
            self._prepare(self.checkpoint, state)
        self.assertFalse((self.root / "scratch/feature-release-plans/staging" / f"{self.payload_digest}.json").exists())

    def test_a_replaced_specification_is_allowed_when_its_body_is_in_the_same_release(self) -> None:
        from scripts.migration_manifest import QueryCheck

        body = self._dispatch_body_item()
        checkpoint = FeatureCheckpoint(
            "APP_CODE", (self.item, body), (), (QueryCheck("after-package", "SELECT 1 FROM dual", 1),), (("APP_CODE", "DISPATCH"),),
        )
        state = self._state(environment="stage", lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE OLD;\n", "END DISPATCH;\n"])
        state["sourceUnits"]["PACKAGE BODY:DISPATCH"] = {
            "lines": ["PACKAGE BODY DISPATCH AS\n", "  PROCEDURE RUN IS BEGIN NULL; NULL; END;\n", "END DISPATCH;\n"],
            "settings": dict(SETTINGS), "valid": True,
        }
        state["structural"]["stored-code"] = self._dispatch_stored_code(body=True)

        plan = self._prepare(checkpoint, state)

        guards = {guard["unit"]["type"]: guard for guard in plan.checkpoint_guards[0]["sourceGuards"]}
        self.assertFalse(guards["PACKAGE"]["noOp"])
        self.assertFalse(guards["PACKAGE BODY"]["noOp"])

    def test_a_replaced_specification_is_allowed_when_the_target_has_no_body(self) -> None:
        state = self._state(environment="stage", lines=["PACKAGE DISPATCH AS\n", "  PROCEDURE OLD;\n", "END DISPATCH;\n"])
        state["structural"]["stored-code"] = self._dispatch_stored_code(body=False)

        plan = self._prepare(self.checkpoint, state)

        guard = plan.checkpoint_guards[0]["sourceGuards"][0]
        self.assertTrue(guard["targetExists"])
        self.assertFalse(guard["noOp"])

    def test_an_unchanged_specification_is_allowed_while_its_body_is_on_the_target(self) -> None:
        # The no-op comparison of the same identical, valid unit (see test_only_identical_valid_stored_unit_is_marked_noop).
        exact_lines = ["PACKAGE" + " " * 10 + "DISPATCH AS\n", "  PROCEDURE RUN;\n", "END DISPATCH;\n"]
        state = self._state(environment="stage", lines=exact_lines, valid=True)
        state["sourceUnits"]["PACKAGE:DISPATCH"]["settings"] = dict(SETTINGS)
        state["structural"]["stored-code"] = self._dispatch_stored_code(body=True)

        plan = self._prepare(self.checkpoint, state)

        self.assertTrue(plan.checkpoint_guards[0]["sourceGuards"][0]["noOp"])


if __name__ == "__main__":
    unittest.main()
