from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import feature_cli, feature_dev, feature_dev_views, feature_release, feature_sources, migration_checks
from scripts import release_capsule, release_state
from test_feature_runtime import IDENTITY, SETTINGS


class FeatureDevTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scope = self.root / "features/inventory/APP_CODE"
        self.scope.mkdir(parents=True)
        self.checks = self.scope / "checks.json"
        self.checks.write_text(json.dumps({
            "schemaVersion": 1, "preconditions": [],
            "postconditions": [{"id": "ready", "sql": "SELECT 1 FROM DUAL", "expected": 1}],
        }), encoding="utf-8")
        self.env = {
            "DB_ENVIRONMENT": "development", "CODE_SCHEMA": "APP_CODE",
            "CODE_SQLCL_CONNECTION": "dev-code", "CODE_EXPECTED_USER": "APP_CODE",
            "MIGRATION_SCHEMA": "APP_CODE", "MIGRATION_SQLCL_CONNECTION": "dev-writer",
            "MIGRATION_EXPECTED_USER": "DEV_WRITER",
        }
        self.identity = dict(
            IDENTITY, session_user="DEV_WRITER", db_name="DEVELOPMENT",
            db_unique_name="DEVELOPMENT", service_name="dev.example.invalid",
        )
        self.state = {
            "identity": self.identity, "schema": "APP_CODE",
            "effectiveViewCollation": "USING_NLS_COMP",
            "sourceUnits": {},
            "structural": {section: [] for section in feature_release.STRUCTURAL_SECTIONS},
            "coverage": {"ownerComplete": False, "selectedScopeComplete": True},
        }
        capture = patch.object(feature_release, "_capture_selected_state", return_value=self.state, create=True)
        self.capture_patch = capture
        self.capture = capture.start()
        self.addCleanup(capture.stop)
        checks = patch.object(migration_checks, "run_checks", side_effect=self.report)
        self.read_checks = checks.start()
        self.addCleanup(checks.stop)
        write = patch.object(feature_release.sqlcl_session, "run_sqlcl")
        self.apply = write.start()
        self.addCleanup(write.stop)
        engine = patch.object(release_capsule, "capsule_engine_digest", return_value="a" * 64)
        engine.start()
        self.addCleanup(engine.stop)

    def report(self, *_args, **_kwargs):
        return migration_checks.CheckReport(True, True, (), (), {
            "sessions": 1, "sessionIdentities": [self.identity],
        })

    def add_source(self, kind="PROCEDURE", name="RUN_JOB", body="BEGIN NULL; END;", *, settings=True):
        lines = [f"{kind} {name} AS\n", body + "\n"]
        prefix = feature_release._settings_prologue(name, kind, SETTINGS)[0] if settings else ""
        filename = feature_sources._unit_source_path(self.scope, name.strip('"'), kind)
        filename.parent.mkdir(exist_ok=True)
        prefix = prefix + "\n" if prefix and not prefix.endswith("\n") else prefix
        filename.write_text(f"{prefix}CREATE OR REPLACE {''.join(lines)}/\n", encoding="utf-8")
        return filename, lines

    def prepare(self, *extra, selector="PROCEDURE:run_job"):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = feature_cli.main([
                "apply", "inventory", "--schema", "APP_CODE", "--env", "dev",
                "--prepare", "--unit", selector, *extra,
            ], repo_root=self.root, environ=self.env)
        self.apply.assert_not_called()
        return result, stdout.getvalue() + stderr.getvalue()

    def plans(self):
        return sorted((self.root / "scratch/feature-dev").glob("*/plan.json"))

    def existing(self, lines, *, valid=True):
        self.state["sourceUnits"]["PROCEDURE:RUN_JOB"] = {
            "lines": lines, "settings": dict(SETTINGS), "valid": valid,
        }

    def baseline_digest(self, output):
        match = re.search(r"--accept-live-baseline ([a-f0-9]{64})", output)
        self.assertIsNotNone(match, output)
        return match.group(1)

    def test_prepare_refuses_invalid_selection_before_sqlcl(self):
        path, _ = self.add_source()
        cases = (
            ("PROCEDURE:../RUN_JOB", (), "identifier"),
            ("MATERIALIZED VIEW:RUN_JOB", (), "supported"),
            ("PROCEDURE:RUN_JOB", ("--checks", "../checks.json"), "inside the repository"),
            ("PROCEDURE:RUN_JOB", ("--checks", "features/inventory/OTHER/checks.json"), "APP_CODE"),
            ("PROCEDURE:RUN_JOB", ("--unit", "PROCEDURE:run_job"), "repeat"),
        )
        for unit, args, detail in cases:
            with self.subTest(unit=unit, args=args):
                code, output = self.prepare(*args, selector=unit)
                self.assertEqual(code, 2, output)
                self.assertIn(detail, output)
        duplicate = self.root / "features/other/APP_CODE/procedures/run_job.sql"
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(path.read_bytes())
        code, output = self.prepare()
        self.assertEqual(code, 2, output)
        self.assertIn("ownership", output)
        duplicate.unlink()
        path.write_text("CREATE TABLE RUN_JOB (ID NUMBER);\n", encoding="utf-8")
        self.assertEqual(self.prepare()[0], 2)
        self.capture.assert_not_called()
        self.read_checks.assert_not_called()

    def test_prepares_quoted_unit_from_hashed_stem_and_binds_exact_inputs(self):
        name = '"' + 'é' * 55 + '"'
        path, _ = self.add_source(name=name)
        self.assertRegex(path.name, r"^quoted-unit-[0-9a-f]{64}\.")
        result, output = self.prepare(selector=f"procedure:{name}")
        self.assertEqual(result, 0, output)
        plan, = self.plans()
        document = json.loads(plan.read_bytes())
        self.assertEqual(document["target"]["connection"], "dev-writer")
        self.assertEqual(document["units"][0]["sourcePath"], path.relative_to(self.root).as_posix())
        self.assertEqual(document["units"][0]["name"], name[1:-1])
        self.assertEqual(document["checks"][0]["path"], self.checks.relative_to(self.root).as_posix())
        self.assertTrue((plan.parent / document["units"][0]["driverPath"]).read_bytes())
        self.assertIn("Plan SHA-256", output)

    def test_prepare_refuses_changed_spec_without_live_body_selection(self):
        self.add_source("PACKAGE", "DISPATCH", "  PROCEDURE RUN; END DISPATCH;")
        self.state["sourceUnits"]["PACKAGE:DISPATCH"] = {
            "lines": ["PACKAGE DISPATCH AS\n", "  PROCEDURE OLD_RUN; END DISPATCH;\n"],
            "settings": dict(SETTINGS), "valid": True,
        }
        self.state["structural"]["stored-code"].append({"type": "PACKAGE BODY", "name": "DISPATCH"})
        result, output = self.prepare(selector="PACKAGE:DISPATCH")
        self.assertEqual(result, 1, output)
        self.assertIn("PACKAGE BODY", output)
        self.assertFalse(self.plans())

    def test_verified_failed_check_is_exit_one_and_unavailable_is_exit_two(self):
        self.add_source()
        for complete, expected in ((True, 1), (False, 2)):
            with self.subTest(complete=complete):
                self.read_checks.side_effect = None
                self.read_checks.return_value = migration_checks.CheckReport(
                    False, complete, (), ({"message": "check did not pass"},), self.report().coverage,
                )
                result, output = self.prepare()
                self.assertEqual(result, expected, output)
                self.assertFalse(self.plans())

    def test_explicit_scoped_checks_and_nonmatching_migration_profile(self):
        self.add_source()
        scoped = self.scope / "checks/run-job.json"
        scoped.parent.mkdir()
        self.checks.rename(scoped)
        arguments = ("--checks", scoped.relative_to(self.root).as_posix())
        self.env["MIGRATION_SCHEMA"] = "OTHER"
        self.assertEqual(self.prepare(*arguments)[0], 2)
        self.capture.assert_not_called()
        self.env["MIGRATION_SCHEMA"] = "APP_CODE"
        result, output = self.prepare(*arguments)
        self.assertEqual(result, 0, output)

    def test_adopts_only_fresh_reviewed_live_baseline_without_changing_feature_source(self):
        source, _ = self.add_source(body="BEGIN NULL; END;")
        self.existing(["PROCEDURE RUN_JOB AS\n", "BEGIN DBMS_OUTPUT.PUT_LINE('a  b'); END;\n"])
        desired = source.read_bytes()
        result, output = self.prepare()
        self.assertEqual(result, 1, output)
        old = self.baseline_digest(output)
        self.assertFalse(self.plans())
        self.assertFalse(list(self.root.glob("scratch/feature-dev/baselines/**/*.json")))
        self.existing(["PROCEDURE RUN_JOB AS\n", "BEGIN DBMS_OUTPUT.PUT_LINE('a b'); END;\n"])
        result, output = self.prepare("--accept-live-baseline", old)
        self.assertEqual(result, 1, output)
        fresh = self.baseline_digest(output)
        self.assertNotEqual(fresh, old)
        result, output = self.prepare("--accept-live-baseline", fresh)
        self.assertEqual(result, 0, output)
        self.assertEqual(source.read_bytes(), desired)
        self.assertEqual(len(list(self.root.glob("scratch/feature-dev/baselines/**/*.json"))), 1)
        self.existing(["PROCEDURE RUN_JOB AS\n", "BEGIN DBMS_OUTPUT.PUT_LINE('a  b'); END;\n"])
        result, output = self.prepare()
        self.assertEqual(result, 1, output)
        self.assertIn("changed since the retained baseline", output)
        result, output = self.prepare("--accept-live-baseline", self.baseline_digest(output))
        self.assertEqual(result, 0, output)
        self.assertEqual(source.read_bytes(), desired)
        baseline, = self.root.glob("scratch/feature-dev/baselines/**/*.json")
        damaged = json.loads(baseline.read_bytes())
        damaged["state"]["settings"] = None
        baseline.write_text(json.dumps(damaged), encoding="utf-8")
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("incomplete state", output)

    def test_capture_baseline_allows_different_logins_but_requires_review_for_another_scope(self):
        path, lines = self.add_source()
        self.existing(lines)
        observed = dict(self.identity, session_user="CAPTURE_USER", service_name="dev-other-service.example.invalid")
        with patch.object(feature_release, "_captured_dev_baseline", return_value=(path.read_text(), observed)):
            result, output = self.prepare()
            self.assertEqual(result, 0, output)
            plan = json.loads(self.plans()[0].read_bytes())
            self.assertTrue(plan["noOp"])
            for field, value in (
                ("db_unique_name", "DIFFERENT"),
                ("container_id", "8"),
                ("current_schema", "OTHER"),
                ("edition", "OTHER_EDITION"),
            ):
                with self.subTest(field=field), patch.dict(observed, {field: value}):
                    result, output = self.prepare()
                    self.assertEqual(result, 1, output)
                    self.baseline_digest(output)

    def test_retained_baselines_are_independent_on_reconfigured_targets(self):
        _, lines = self.add_source()
        self.existing(lines)
        result, output = self.prepare()
        self.assertEqual(result, 1, output)
        self.assertEqual(self.prepare("--accept-live-baseline", self.baseline_digest(output))[0], 0)
        with patch.dict(self.identity, {"container_id": "9"}):
            result, output = self.prepare()
            self.assertEqual(result, 1, output)
            self.assertEqual(self.prepare("--accept-live-baseline", self.baseline_digest(output))[0], 0)
        self.assertEqual(self.prepare()[0], 0)
        self.assertEqual(len(list(self.root.glob("scratch/feature-dev/baselines/**/*.json"))), 2)

    def test_prepare_requires_settings_for_new_units_and_refuses_disabled_trigger(self):
        path, _ = self.add_source(settings=False)
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("explicit compiler settings", output)
        path.write_bytes(b"ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\n" + path.read_bytes())
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("complete compiler settings", output)
        self.add_source("TRIGGER", "JOBS_T", "BEGIN NULL; END;")
        self.state["sourceUnits"]["TRIGGER:JOBS_T"] = {
            "lines": ["TRIGGER JOBS_T AS\n", "BEGIN RAISE NO_DATA_FOUND; END;\n"],
            "settings": dict(SETTINGS), "valid": True,
        }
        self.state["structural"]["triggers"].append({"name": "JOBS_T", "status": "DISABLED"})
        result, output = self.prepare(selector="TRIGGER:JOBS_T")
        self.assertEqual(result, 1, output)
        self.assertIn("DISABLED", output)
        self.assertFalse(self.plans())

    def test_invalid_unit_and_unresolved_or_unreadable_evidence_block_adoption(self):
        _, lines = self.add_source()
        self.existing(lines, valid=False)
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("invalid DEV unit", output)
        self.existing(lines)
        pending = {
            "attempts": [{"state": "unknown", "attemptId": "pending", "targetScope": release_state.target_scope_from_identity(self.identity)}],
            "rollouts": [], "recoveryLinks": [], "checkpointVerifications": [],
        }
        for evidence in ({"return_value": pending}, {"side_effect": release_state.ReleaseStateError("unreadable release evidence")}):
            with patch.object(release_state, "_load_index", **evidence):
                result, output = self.prepare()
                self.assertEqual(result, 2, output)
                self.assertRegex(output, r"unresolved release write evidence|unreadable release evidence")
        self.assertFalse(self.plans())

    def test_prepare_binds_effective_env_file_and_refuses_changed_inputs(self):
        source, _ = self.add_source()
        dotenv = self.root / "private.env"
        dotenv.write_text("CODE_SQLCL_CONNECTION=example\n", encoding="utf-8")
        self.env["PROJECT_ENV_FILE"] = str(dotenv)
        result, output = self.prepare()
        self.assertEqual(result, 0, output)
        plan = json.loads(self.plans()[0].read_bytes())
        self.assertEqual(plan["target"]["configurationFile"]["path"], "private.env")
        self.assertNotIn("CODE_SQLCL_CONNECTION=", json.dumps(plan))

        def changed(*args, **kwargs):
            source.write_bytes(source.read_bytes() + b"\n")
            return self.report(*args, **kwargs)

        self.read_checks.side_effect = changed
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("input changed during preparation", output)
        self.assertEqual(len(self.plans()), 1)

        def changed_config(*args, **kwargs):
            dotenv.write_bytes(dotenv.read_bytes() + b"# updated\n")
            return self.report(*args, **kwargs)

        self.read_checks.side_effect = changed_config
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("effective project configuration changed", output)
        self.assertEqual(len(self.plans()), 1)

    def test_prepare_refuses_a_different_observed_session_before_reading_checks(self):
        self.add_source()
        self.identity["session_user"] = "ANOTHER_LOGIN"
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("does not match DEV_WRITER", output)
        self.read_checks.assert_not_called()

    def test_prepare_uses_the_existing_scoped_catalog_reader_on_dev(self):
        self.add_source()
        self.capture_patch.stop()
        catalog = {
            "identity": self.identity,
            "sections": {section: [] for section in feature_release.SELECTED_SECTIONS},
            "coverage": {"sections": {section: {"complete": True} for section in feature_release.SELECTED_SECTIONS}},
        }
        with patch.object(feature_release.compare_schema, "capture_environment_catalog", return_value=catalog) as reader:
            result, output = self.prepare()
        self.assertEqual(result, 0, output)
        target, environment, sections, _ = reader.call_args.args
        self.assertEqual((target.connection, target.expected_user, target.schema, environment),
                         ("dev-writer", "DEV_WRITER", "APP_CODE", "dev"))
        self.assertIn("baseline-source", sections)
        self.assertEqual(reader.call_args.kwargs["baseline_names"], ["RUN_JOB"])
        self.assertEqual(reader.call_args.kwargs["scope_names"], ["RUN_JOB"])
        self.assertFalse(reader.call_args.kwargs["dba_capture"])

    def test_precondition_read_back_must_use_the_prepared_target(self):
        self.add_source()
        self.read_checks.side_effect = None
        self.read_checks.return_value = migration_checks.CheckReport(True, True, (), (), {
            "sessions": 1, "sessionIdentities": [dict(self.identity, edition="OTHER_EDITION")],
        })
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("differs from the prepared target", output)
        self.assertFalse(self.plans())

    def test_adoption_refuses_unresolved_archived_migration(self):
        _, lines = self.add_source()
        self.existing(lines)
        migration_dir = self.root / "migrations/APP_CODE"
        migration_dir.mkdir(parents=True)
        (migration_dir / ".archived.json").write_text(json.dumps({
            "schemaVersion": 1,
            "archived": [{
                "folder": "2026-01-01_legacy-r001", "family": "legacy", "revision": 1,
                "payloadDigest": "a" * 64, "archivedAt": "2026-01-01",
                "destination": "migrations-archive/APP_CODE/2026-01-01_legacy-r001",
                "reason": "adopt legacy archive", "evidence": ["attempt-event:pending:dev", "unresolved (preserved)"],
            }],
        }), encoding="utf-8")
        result, output = self.prepare()
        self.assertEqual(result, 2, output)
        self.assertIn("has unresolved evidence", output)

    def execute(self, plan):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = feature_cli.main([
                "apply", "inventory", "--schema", "APP_CODE", "--env", "dev",
                "--execute-plan", plan.relative_to(self.root).as_posix(),
            ], repo_root=self.root, environ=self.env)
        return result, output.getvalue()

    def test_execute_refuses_changed_inputs_before_connecting(self):
        source, _ = self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        original_plan = plan.read_bytes()
        driver = plan.parent / "drivers/001.sql"
        for path in (source, self.checks, driver, plan):
            original = path.read_bytes()
            path.write_bytes(original + b"tampered")
            self.capture.reset_mock()
            result, output = self.execute(plan)
            self.assertEqual(result, 2, output)
            self.capture.assert_not_called()
            self.apply.assert_not_called()
            path.write_bytes(original)
        self.assertEqual(plan.read_bytes(), original_plan)
        with patch.dict(self.env, {"MIGRATION_SQLCL_CONNECTION": "other-dev-writer"}):
            result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertIn("configuration changed", output)
        self.capture.assert_not_called()
        self.apply.assert_not_called()

    def test_unreadable_attempt_directory_refuses_status_and_write(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        directory = self.root / feature_dev.ATTEMPT_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        iterdir = Path.iterdir

        def inaccessible(path):
            if path == directory:
                raise PermissionError("cannot enumerate attempt markers")
            return iterdir(path)

        with patch.object(Path, "iterdir", inaccessible):
            self.assertEqual(self.status()[0], 2)
            self.capture.reset_mock()
            self.assertEqual(self.execute(plan)[0], 2)
            self.capture.assert_not_called()
            self.apply.assert_not_called()

    def test_execute_writes_once_and_retains_verified_baseline(self):
        source, lines = self.add_source()
        result, output = self.prepare()
        self.assertEqual(result, 0, output)
        plan, = self.plans()

        def fake_apply(target, driver, run_dir, **kwargs):
            driver_text = driver.read_text(encoding="utf-8")
            self.assertLess(driver_text.index("MIGRATION_IDENTITY_GUARD_REFUSED"), driver_text.index("MIGRATION_PAYLOAD_STARTED"))
            self.assertTrue(list(self.root.glob("releases/.dev-apply-attempts/*.json")))
            self.existing(lines)
            transcript = (
                "MIGRATION_IDENTITY_BEGIN\n" + json.dumps(self.identity) + "\nMIGRATION_IDENTITY_END\n"
                "MIGRATION_IDENTITY_VERIFIED\nMIGRATION_PAYLOAD_STARTED:001.sql\n"
                "MIGRATION_APPLY_COMPLETED\nFEATURE_CHECKPOINT_APPLY_COMPLETED\n"
            )
            (run_dir / "sqlcl-output.log").write_text(transcript)
            return SimpleNamespace(returncode=0, output=transcript)

        self.apply.side_effect = fake_apply
        original = source.read_bytes()
        result, output = self.execute(plan)
        self.assertEqual(result, 0, output)
        marker, = self.root.glob("releases/.dev-apply-attempts/*.json")
        self.assertEqual(json.loads(marker.read_bytes())["state"], "verified")
        self.assertTrue(list(self.root.glob("scratch/feature-dev/baselines/**/*.json")))
        self.assertEqual(original, source.read_bytes())
        daily, = self.root.glob("ai_generate/*/feature-diffs/inventory/APP_CODE/dev-apply/*.diff")
        self.assertEqual(daily.name, f"{plan.parent.name}.diff")
        self.assertEqual(daily.read_bytes(), (plan.parent / "source-review.diff").read_bytes())
        self.capture.reset_mock()
        result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertIn("consumed", output)
        self.capture.assert_not_called()
        self.apply.assert_called_once()

    def status(self):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = feature_cli.main([
                "apply", "another-feature", "--schema", "APP_CODE", "--env", "dev", "--status",
            ], repo_root=self.root, environ={})
        return result, output.getvalue()

    def test_ambiguous_write_survives_scratch_cleanup_and_blocks_other_plans(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        original_plan, = self.plans()
        self.assertEqual(self.prepare()[0], 0)
        other_plan, = (path for path in self.plans() if path != original_plan)
        self.apply.side_effect = OSError("connection lost during apply")
        result, output = self.execute(original_plan)
        self.assertEqual(result, 2, output)
        self.assertIn("requires reconciliation", output)
        self.assertFalse((self.root / "ai_generate").exists())
        shutil.rmtree(original_plan.parent)
        result, output = self.status()
        self.assertEqual(result, 1, output)
        self.assertIn("Missing evidence", output)
        self.assertIn(original_plan.parent.name, output)
        self.capture.reset_mock()
        result, output = self.execute(other_plan)
        self.assertEqual(result, 2, output)
        self.assertIn("unresolved DEV apply", output)
        self.capture.assert_not_called()
        self.apply.assert_called_once()

    def test_pending_in_another_container_allows_prepare_and_malformed_attempt_refuses_status(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        self.apply.side_effect = OSError("connection lost during apply")
        self.assertEqual(self.execute(plan)[0], 2)
        marker = self.root / feature_dev.ATTEMPT_DIRECTORY / f"{plan.parent.name}.json"
        record = json.loads(marker.read_bytes())
        self.apply.reset_mock()
        with patch.dict(self.identity, {"container_id": "9", "container_name": "OTHER_DEV"}):
            code, output = self.prepare()
            self.assertEqual(code, 0, output)
        self.assertEqual(self.status()[0], 1)
        marker.write_text(json.dumps(dict(record, planId="wrong-plan")))
        result, output = self.status()
        self.assertEqual(result, 2, output)
        self.assertIn("unreadable DEV attempt marker", output)

    def test_reconcile_reobserves_reviewed_before_state_and_records_acknowledgment(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        self.apply.side_effect = OSError("connection lost during apply")
        self.assertEqual(self.execute(plan)[0], 2)
        marker, = self.root.glob("releases/.dev-apply-attempts/*.json")
        record = json.loads(marker.read_bytes())
        evidence = self.root / "reviewed.json"
        row = {
            "schemaVersion": 1, "planId": record["planId"], "planSha256": record["planSha256"],
            "targetScope": record["targetScope"], "conclusion": "before",
            "establishedBy": "operator", "notes": "Reviewed the original target and ended operation.",
        }

        def reconcile(document, *extra, acknowledgment=None):
            evidence.write_text(json.dumps(document), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                result = feature_cli.main([
                    "reconcile-run", record["planId"], "--env", "dev", "--evidence", evidence.name, *extra,
                ], repo_root=self.root, environ=self.env, read_acknowledgment=acknowledgment)
            return result, output.getvalue()

        result, output = reconcile(dict(row, targetScope=dict(row["targetScope"], edition="WRONG")), "--yes")
        self.assertEqual(result, 2, output)
        self.assertIn("same physical target", output)
        result, output = reconcile(dict(row, conclusion="intended"), "--yes")
        self.assertEqual(result, 2, output)
        self.assertIn("could not prove the intended", output)
        self.assertEqual(reconcile(row, acknowledgment=lambda _: "")[0], 1)
        result, output = reconcile(row, "--yes")
        self.assertEqual(result, 0, output)
        closed = json.loads(marker.read_bytes())
        self.assertEqual((closed["state"], closed["acknowledgedBy"]), ("reconciled", "--yes"))
        self.assertEqual(self.status()[0], 0)
        self.apply.assert_called_once()

    def test_no_op_is_verified_and_consumed_without_a_write_session(self):
        path, lines = self.add_source()
        self.existing(lines)
        with patch.object(feature_release, "_captured_dev_baseline", return_value=(path.read_text(), self.identity)):
            self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        result, output = self.execute(plan)
        self.assertEqual(result, 0, output)
        self.assertIn("Verified DEV no-op", output)
        self.assertFalse((self.root / "ai_generate").exists())
        self.assertEqual(self.execute(plan)[0], 2)
        self.apply.assert_not_called()

    def test_target_writer_in_another_process_excludes_same_and_other_plans_before_capture(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        self.assertEqual(self.prepare()[0], 0)
        other, = (candidate for candidate in self.plans() if candidate != plan)
        scope = release_state.target_scope_from_identity(self.identity)
        child_code = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from scripts.release_ownership import target_writer_lease\n"
            "with target_writer_lease(Path(sys.argv[1]), json.loads(sys.argv[2]), {'planId': sys.argv[3]}):\n"
            "    print('READY', flush=True)\n"
            "    sys.stdin.read()\n"
        )
        with subprocess.Popen(
            [sys.executable, "-B", "-c", child_code, str(self.root), json.dumps(scope), plan.parent.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        ) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), "READY")
                self.capture.reset_mock()
                for candidate in (plan, other):
                    with self.subTest(plan=candidate):
                        result, output = self.execute(candidate)
                        self.assertEqual(result, 2, output)
                        self.assertIn("owns this target scope", output)
                        self.capture.assert_not_called()
                        self.apply.assert_not_called()
            finally:
                child.communicate(input="", timeout=10)

    def test_apply_failure_or_receipt_failure_retains_pending_attempt(self):
        _, lines = self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()

        def invalid(_target, _driver, run_dir, **_kwargs):
            output = "MIGRATION_PAYLOAD_STARTED:001.sql\nWarning: Procedure created with compilation errors.\n"
            (run_dir / "sqlcl-output.log").write_text(output)
            return SimpleNamespace(returncode=0, output=output)

        self.apply.side_effect = invalid
        result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertIn("PROCEDURE RUN_JOB", output)
        self.assertIn("compiled with errors", output)
        self.assertEqual(self.status()[0], 1)
        marker, = self.root.glob("releases/.dev-apply-attempts/*.json")
        self.assertEqual(json.loads(marker.read_bytes())["state"], "pending")
        # Use an independent disposable run to exercise receipt-write failure after verification.
        marker.unlink()
        self.apply.side_effect = None

        def simulate_verified(*_args, **kwargs):
            kwargs["before_run"]()
            self.existing(lines)
            return self.identity, 1

        with patch.object(feature_release, "_run_apply_session", side_effect=simulate_verified), patch.object(
            feature_dev, "_finish_attempt", side_effect=OSError("receipt could not be finalized"),
        ):
            result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertIn("receipt could not be finalized", output)
        self.assertEqual(self.status()[0], 1)

    def test_verified_apply_preserves_working_edits_and_receipt_on_baseline_refresh_attention(self):
        path, lines = self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        changed = path.read_bytes() + b"-- working edit after apply started\n"

        def simulate_verified(*_args, **kwargs):
            kwargs["before_run"]()
            self.existing(lines)
            path.write_bytes(changed)
            return self.identity, 1

        with patch.object(feature_release, "_run_apply_session", side_effect=simulate_verified), patch.object(
            feature_dev, "_refresh_baselines", side_effect=OSError("baseline directory unavailable"),
        ):
            result, output = self.execute(plan)
        self.assertEqual(result, 0, output)
        self.assertIn("Refresh retained DEV baselines separately", output)
        self.assertEqual(path.read_bytes(), changed)
        marker, = self.root.glob("releases/.dev-apply-attempts/*.json")
        self.assertEqual(json.loads(marker.read_bytes())["state"], "verified")

    def test_interruption_before_write_is_130_but_unknown_write_is_2(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        self.capture.side_effect = KeyboardInterrupt
        self.assertEqual(self.execute(plan)[0], 130)
        self.assertEqual(self.status()[0], 0)
        self.capture.side_effect = None
        self.apply.side_effect = KeyboardInterrupt
        result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertEqual(self.status()[0], 1)

    def test_interruption_after_persisting_pending_marker_is_unknown(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        with patch.object(feature_dev, "_sync_attempt_directory", side_effect=KeyboardInterrupt):
            self.assertEqual(self.execute(plan)[0], 2)
        self.assertEqual(self.status()[0], 1)
        self.apply.assert_not_called()

    def test_process_loss_after_payload_preserves_attempt_and_releases_writer_lease(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        child_code = """
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from test_feature_dev import FeatureDevTests, feature_release

test = FeatureDevTests()
tempfile.tempdir = sys.argv[1]
test.setUp()
test.root = Path(sys.argv[1])

def disconnect(checkpoint, target, identity, checks, run_dir, guards, drivers, *, before_run, exact_before):
    before_run()
    (run_dir / 'sqlcl-output.log').write_text('MIGRATION_PAYLOAD_STARTED:001.sql\\n', encoding='utf-8')
    os._exit(77)

with patch.object(feature_release, '_run_apply_session', side_effect=disconnect):
    sys.exit(test.execute(Path(sys.argv[2]))[0])
"""
        tests = Path(__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, "-B", "-c", child_code, str(self.root), str(plan)],
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(tests), str(tests.parent)))},
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertEqual(self.status()[0], 1)
        marker = self.root / feature_dev.ATTEMPT_DIRECTORY / f"{plan.parent.name}.json"
        evidence = json.loads(marker.read_bytes())["evidencePaths"]
        log = next(path for path in evidence if path.endswith("/sqlcl-output.log"))
        self.assertIn("MIGRATION_PAYLOAD_STARTED:", (self.root / log).read_text())
        code, output = self.prepare()
        self.assertEqual(code, 2, output)
        self.assertIn("unresolved DEV apply", output)

    def test_timeout_after_payload_keeps_pending_marker(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()

        def timeout(_target, _driver, run_dir, **_kwargs):
            output = "MIGRATION_PAYLOAD_STARTED:001.sql\n"
            (run_dir / "sqlcl-output.log").write_text(output)
            raise feature_release.sqlcl_session.SqlclTimeout(
                run_dir, phase="apply", timeout_seconds=1, elapsed_seconds=1, output=output,
            )

        self.apply.side_effect = timeout
        result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertIn("timed out", output)
        self.assertEqual(self.status()[0], 1)

    def test_successful_write_without_fresh_source_match_keeps_pending_marker(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()

        def applied_without_proof(*_args, **kwargs):
            kwargs["before_run"]()
            return self.identity, 1

        with patch.object(feature_release, "_run_apply_session", side_effect=applied_without_proof):
            result, output = self.execute(plan)
        self.assertEqual(result, 2, output)
        self.assertIn("read-back did not prove", output)
        self.assertEqual(self.status()[0], 1)

    def test_fresh_identity_and_before_state_are_checked_before_the_payload(self):
        self.add_source()
        self.assertEqual(self.prepare()[0], 0)
        plan, = self.plans()
        with patch.dict(self.identity, {"edition": "ANOTHER_EDITION"}):
            result, output = self.execute(plan)
            self.assertEqual(result, 2, output)
            self.assertIn("observed DEV identity changed", output)
        self.assertIn("consumed", self.execute(plan)[1])
        self.assertEqual(self.prepare()[0], 0)
        new_plan, = (candidate for candidate in self.plans() if candidate != plan)
        self.existing(["PROCEDURE RUN_JOB AS\n", "BEGIN NULL; END;\n"])
        result, output = self.execute(new_plan)
        self.assertEqual(result, 1, output)
        self.assertIn("before-state changed", output)
        self.assertIn("consumed", self.execute(new_plan)[1])
        self.apply.assert_not_called()

    def view_source(self, statement="CREATE OR REPLACE VIEW CUSTOMER_LOOKUP AS SELECT 1 AS ID FROM DUAL;\n"):
        path = self.scope / "views/customer_lookup.sql"
        path.parent.mkdir(exist_ok=True)
        path.write_text(statement, encoding="utf-8")
        return path

    def existing_view(self, query="SELECT 1 AS ID FROM DUAL", *, aliases="ID", suffix=""):
        self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"] = {
            "text": query, "ddl": f'CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_CODE"."CUSTOMER_LOOKUP" ({aliases}) AS {query}{suffix}',
            "valid": True,
        }
        self.state["structural"]["views"] = [{"name": "CUSTOMER_LOOKUP", "status": "VALID"}]

    def test_view_replacement_with_a_same_named_public_synonym(self):
        self.view_source()
        self.existing_view()
        self.state["structural"]["synonyms"] = [{"owner": "PUBLIC", "name": "CUSTOMER_LOOKUP"}]
        result, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(result, 1, output)
        result, output = self.prepare("--accept-live-baseline", self.baseline_digest(output), selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(result, 0, output)

    def test_verified_view_baseline_allows_repair_after_validity_only_changes(self):
        self.view_source()
        self.existing_view()
        result, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(result, 1, output)
        self.assertEqual(self.prepare("--accept-live-baseline", self.baseline_digest(output), selector="VIEW:CUSTOMER_LOOKUP")[0], 0)
        old_plan, = self.plans()
        self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]["valid"] = False
        self.state["structural"]["views"][0]["status"] = "INVALID"
        result, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(result, 0, output)
        plan, = (candidate for candidate in self.plans() if candidate != old_plan)
        self.assertFalse(json.loads(plan.read_bytes())["noOp"])

    def test_view_apply_accepts_one_dedicated_definition(self):
        path = self.view_source()
        (self.scope / "ddl.sql").write_text("CREATE TABLE CUSTOMER_EXTRA (ID NUMBER);\n", encoding="utf-8")
        code, output = self.prepare(selector="VIEW:customer_lookup")
        self.assertEqual(code, 0, output)
        plan, = self.plans()
        saved = json.loads(plan.read_bytes())
        self.assertEqual([row["sourcePath"] for row in saved["units"]], [path.relative_to(self.root).as_posix()])
        self.assertNotIn("CUSTOMER_EXTRA", (plan.parent / "sources/001.sql").read_text())
        def apply(*_args, **kwargs):
            kwargs["before_run"]()
            self.existing_view()
            return self.identity, 1
        with patch.object(feature_release, "_run_apply_session", side_effect=apply):
            code, output = self.execute(plan)
        self.assertEqual(code, 0, output)
        path.write_text(path.read_text() + "ALTER TABLE CUSTOMER_EXTRA ADD LABEL VARCHAR2(10);\n")
        self.assertEqual(self.prepare(selector="VIEW:customer_lookup")[0], 2)

    def test_view_selection_refuses_duplicate_unsafe_or_changed_sources(self):
        source = self.view_source()
        other = self.root / "features/other/APP_CODE/views/customer_lookup.sql"
        other.parent.mkdir(parents=True)
        other.write_bytes(source.read_bytes())
        self.assertEqual(self.prepare(selector="VIEW:CUSTOMER_LOOKUP")[0], 2)
        self.capture.assert_not_called()
        other.unlink()
        self.view_source("CREATE OR REPLACE VIEW CUSTOMER_LOOKUP AS SELECT ID FROM ROWS_TABLE@REMOTE_DB;\n")
        self.assertEqual(self.prepare(selector="VIEW:CUSTOMER_LOOKUP")[0], 2)
        self.capture.assert_not_called()
        self.view_source("CREATE OR REPLACE VIEW CUSTOMER_LOOKUP (ID) AS SELECT a.* FROM ROWS_TABLE a;\n")
        code, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(code, 2, output)
        self.assertIn("list the projected columns explicitly", output)
        self.capture.assert_not_called()
        self.view_source()
        self.assertEqual(self.prepare(selector="VIEW:CUSTOMER_LOOKUP")[0], 0)
        plan, = self.plans()
        self.view_source("CREATE OR REPLACE VIEW CUSTOMER_LOOKUP AS SELECT 2 AS ID FROM DUAL;\n")
        self.capture.reset_mock()
        code, output = self.execute(plan)
        self.assertEqual(code, 2, output)
        self.assertIn("source or driver changed", output)
        self.capture.assert_not_called()

    def test_view_prepare_refuses_incomplete_or_inconsistent_catalog(self):
        self.view_source()
        self.existing_view()
        view = self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]
        for replacement in ("", "SELECT 2 AS ID FROM DUAL"):
            with self.subTest(text=replacement), patch.dict(view, {"text": replacement}):
                code, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
                self.assertEqual(code, 2, output)
                self.assertRegex(output, "catalog text or validity is unavailable|catalog text and DDL disagree")
                self.assertFalse(self.plans())

    def test_view_definition_comparison_preserves_aliases_properties_and_literal_spaces(self):
        statement = "CREATE OR REPLACE VIEW CUSTOMER_LOOKUP AS SELECT 'a b' AS ID FROM DUAL;\n"
        path = self.view_source(statement)
        item = feature_dev_views.selected_view(path.relative_to(self.root).as_posix(), path.read_bytes(), ("APP_CODE", "VIEW", "CUSTOMER_LOOKUP"))
        self.existing_view("SELECT 'a b' AS ID FROM DUAL")
        matching = self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]
        self.assertTrue(feature_dev_views.matches(item, matching))
        for query, alias, suffix in (
            ("SELECT 'a  b' AS ID FROM DUAL", "ID", ""),
            ("SELECT 'a b' AS ID FROM DUAL", "NEW_NAME", ""),
            ("SELECT 'a b' AS ID FROM DUAL", "ID", " WITH READ ONLY"),
            ("SELECT 'a b' AS ID FROM DUAL", "ID", " WITH CHECK OPTION"),
        ):
            with self.subTest(query=query, alias=alias, suffix=suffix):
                self.existing_view(query, aliases=alias, suffix=suffix)
                self.assertFalse(feature_dev_views.matches(item, self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]))
        for attribute in ("NONEDITIONABLE VIEW", "EDITIONABLE EDITIONING VIEW", "EDITIONABLE VIEW"):
            ddl = matching["ddl"].replace("EDITIONABLE VIEW", attribute)
            if ddl == matching["ddl"]:
                ddl = ddl.replace(") AS ", ") BEQUEATH CURRENT_USER AS ")
            self.assertFalse(feature_dev_views.matches(item, dict(matching, ddl=ddl)))

    def test_view_comparison_distinguishes_numeric_literals_from_implicit_aliases(self):
        for literal, alias_form in (("1e1", "1 e1"), ("1f", "1 f"), (".5d", ".5 d")):
            with self.subTest(literal=literal):
                path = self.view_source(f"CREATE OR REPLACE VIEW CUSTOMER_LOOKUP (ID) AS SELECT {literal} FROM DUAL;\n")
                item = feature_dev_views.selected_view(path.relative_to(self.root).as_posix(), path.read_bytes(), ("APP_CODE", "VIEW", "CUSTOMER_LOOKUP"))
                self.existing_view(f"SELECT {literal.upper()} FROM DUAL")
                self.assertTrue(feature_dev_views.matches(item, self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]))
                self.existing_view(f"SELECT {alias_form} FROM DUAL")
                self.assertFalse(feature_dev_views.matches(item, self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]))

    def test_view_comparison_requires_explicit_proof_for_reported_default_collation(self):
        path = self.view_source()
        name = ("APP_CODE", "VIEW", "CUSTOMER_LOOKUP")
        self.existing_view()
        observed = self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]
        observed["ddl"] = observed["ddl"].replace(") AS ", ") DEFAULT COLLATION BINARY_CI AS ")
        implicit = feature_dev_views.selected_view(path.relative_to(self.root).as_posix(), path.read_bytes(), name)
        with self.assertRaisesRegex(ValueError, "declare DEFAULT COLLATION explicitly"):
            feature_dev_views.matches(implicit, observed)
        self.assertTrue(feature_dev_views.matches(implicit, observed, default_collation="BINARY_CI"))
        self.assertFalse(feature_dev_views.matches(implicit, observed, default_collation="BINARY"))
        for collation in ("BINARY_CI", "BINARY"):
            with self.subTest(collation=collation):
                path = self.view_source(f"CREATE OR REPLACE VIEW CUSTOMER_LOOKUP DEFAULT COLLATION {collation} AS SELECT 1 AS ID FROM DUAL;\n")
                item = feature_dev_views.selected_view(path.relative_to(self.root).as_posix(), path.read_bytes(), name)
                self.assertEqual(feature_dev_views.matches(item, observed), collation == "BINARY_CI")

    def test_new_view_binds_and_verifies_the_observed_implicit_collation(self):
        self.view_source()
        self.state["effectiveViewCollation"] = "BINARY_CI"
        result, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(result, 0, output)
        plan, = self.plans()
        self.assertEqual(json.loads(plan.read_bytes())["sourceGuards"][0]["defaultCollation"], "BINARY_CI")

        def apply(*_args, **kwargs):
            guard = "\n".join(kwargs["exact_before"])
            self.assertIn("SESSION_DEFAULT_COLLATION", guard)
            self.assertIn("'BINARY_CI'", guard)
            kwargs["before_run"]()
            self.existing_view()
            observed = self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]
            observed["ddl"] = observed["ddl"].replace(") AS ", ") DEFAULT COLLATION BINARY_CI AS ")
            self.state["viewCollations"] = {"CUSTOMER_LOOKUP": "BINARY_CI"}
            return self.identity, 1

        with patch.object(feature_release, "_run_apply_session", side_effect=apply):
            code, output = self.execute(plan)
        self.assertEqual(code, 0, output)

    def test_long_view_driver_and_before_guard_round_trip_supplementary_unicode(self):
        projections = ",\n".join(f"'{chr(0x10400) * 800}' AS C{index}" for index in range(12))
        query = f"SELECT {projections} FROM DUAL"
        path = self.view_source(f"CREATE OR REPLACE VIEW CUSTOMER_LOOKUP AS {query};\n")
        self.assertGreater(len(path.read_bytes()), 32768)
        self.existing_view(query, aliases=", ".join(f"C{i}" for i in range(12)))
        code, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(code, 1, output)
        code, output = self.prepare("--accept-live-baseline", self.baseline_digest(output), selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(code, 0, output)
        plan, = self.plans()
        driver = (plan.parent / "drivers/001.sql").read_text(encoding="utf-8")
        checkpoint = self.capture.call_args.args[1]
        before = "\n".join(feature_dev_views.before_guards(checkpoint, self.state))
        for generated, expected in (
            (driver, path.read_text(encoding="utf-8").strip().removesuffix(";")),
            (before, self.state["sourceUnits"]["VIEW:CUSTOMER_LOOKUP"]["ddl"]),
        ):
            chunks = re.findall(r"HEXTORAW\('([0-9A-Fa-f]+)'\)", generated)
            self.assertGreater(len(chunks), 2)
            self.assertEqual(chunks[::2], chunks[1::2])
            self.assertEqual("".join(bytes.fromhex(chunk).decode("utf-8") for chunk in chunks[::2]), expected)

    def test_view_apply_refuses_late_definition_drift_in_write_session(self):
        self.view_source("CREATE OR REPLACE VIEW CUSTOMER_LOOKUP AS SELECT 2 AS ID FROM DUAL;\n")
        self.existing_view()
        code, output = self.prepare(selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(code, 1, output)
        code, output = self.prepare("--accept-live-baseline", self.baseline_digest(output), selector="VIEW:CUSTOMER_LOOKUP")
        self.assertEqual(code, 0, output)
        plan, = self.plans()

        def apply(_target, driver, run_dir, **_kwargs):
            sql = driver.read_text(encoding="utf-8")
            self.assertLess(sql.index("FEATURE_VIEW_GUARD_REFUSED"), sql.index("MIGRATION_PAYLOAD_STARTED"))
            self.assertIn("DBMS_LOB.COMPARE", sql)
            self.existing_view(aliases="CHANGED")
            output = (
                "MIGRATION_IDENTITY_VERIFIED\n"
                "ORA-20984: FEATURE_VIEW_GUARD_REFUSED: CUSTOMER_LOOKUP\n"
            )
            (run_dir / "sqlcl-output.log").write_text(output, encoding="utf-8")
            return SimpleNamespace(returncode=1, output=output)

        self.apply.side_effect = apply
        code, output = self.execute(plan)
        self.assertEqual(code, 1, output)
        self.assertEqual(self.status()[0], 0)

    def test_view_reconciliation_requires_matching_definition_and_keeps_working_tree(self):
        original = self.view_source()
        self.assertEqual(self.prepare(selector="VIEW:CUSTOMER_LOOKUP")[0], 0)
        plan, = self.plans()

        def lost_reply(*_args, **_kwargs):
            self.existing_view(aliases="CHANGED")
            raise OSError("reply lost after view write")

        self.apply.side_effect = lost_reply
        self.assertEqual(self.execute(plan)[0], 2)
        pending, = self.root.glob("releases/.dev-apply-attempts/*.json")
        record = json.loads(pending.read_bytes())
        evidence = self.root / "review.json"
        evidence.write_text(json.dumps({
            "schemaVersion": 1, "planId": record["planId"], "planSha256": record["planSha256"],
            "targetScope": record["targetScope"], "conclusion": "intended",
            "establishedBy": "operator", "notes": "Reviewed installed view after connection terminated.",
        }), encoding="utf-8")
        later_edit = original.read_bytes() + b"-- unrelated later edit\n"
        original.write_bytes(later_edit)
        args = ["reconcile-run", record["planId"], "--env", "dev", "--evidence", evidence.name]
        def reconcile():
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return feature_cli.main(args, repo_root=self.root, environ=self.env, read_acknowledgment=lambda expected: expected)
        self.assertEqual(reconcile(), 2)
        self.assertEqual(json.loads(pending.read_bytes())["state"], "pending")
        self.existing_view()
        self.assertEqual(reconcile(), 0)
        self.assertEqual(json.loads(pending.read_bytes())["acknowledgedBy"], "terminal")
        self.assertEqual(original.read_bytes(), later_edit)
        daily, = self.root.glob("ai_generate/*/feature-diffs/inventory/APP_CODE/dev-apply/*.diff")
        self.assertEqual(daily.read_bytes(), (plan.parent / "source-review.diff").read_bytes())


if __name__ == "__main__":
    unittest.main()
