from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.db_targets import Target
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


if __name__ == "__main__":
    unittest.main()
