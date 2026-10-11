from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from fake_sqlcl import environment as fake_environment, install as install_fake_sqlcl

from scripts import rollout
from scripts.migration_manifest import load_batch


ROOT = Path(__file__).resolve().parents[1]


class RolloutManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.manifest_path = self.root / "rollout.json"
        self.project_env_file = self.root / "test-project.env"
        self.project_env_values = {
            "PROJECT_NAME": "TEST_PROJECT",
            "DEVELOPER_NAME": "TESTDEV",
            "DB_ENVIRONMENT": "development",
            "APEX_APP_ID": "100",
            "TABLES_SCHEMA": "DEMO",
            "TABLES_PREFIXES": "*",
            "TABLES_SQLCL_CONNECTION": "DEV_ALIAS",
            "TABLES_EXPECTED_USER": "DEMO",
            "CODE_SCHEMA": "DEMO",
            "CODE_PREFIXES": "*",
            "CODE_SQLCL_CONNECTION": "DEV_ALIAS",
            "CODE_EXPECTED_USER": "DEMO",
            "APEX_PARSING_SCHEMA": "DEMO",
            "APEX_SQLCL_CONNECTION": "DEV_ALIAS",
            "APEX_EXPECTED_USER": "DEMO",
            "STAGING_SCHEMA": "DEMO",
            "STAGING_SQLCL_CONNECTION": "STAGE_ALIAS",
            "STAGING_EXPECTED_USER": "DEMO",
            "PROD_SCHEMA": "DEMO",
            "PROD_SQLCL_CONNECTION": "PROD_ALIAS",
            "PROD_EXPECTED_USER": "DEMO",
        }
        self.write_project_env(self.project_env_values)
        self.environment_patch = patch.dict(os.environ, {
            **self.project_env_values,
            "PROJECT_ENV_FILE": str(self.project_env_file),
            "PROJECT_SCHEMA": "",
        }, clear=False)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.identity_patch = patch(
            "scripts.rollout._observe_target_identity", side_effect=self.observe_target_identity,
        )
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_project_env(self, values: dict[str, str]) -> None:
        self.project_env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
            encoding="utf-8",
        )

    def write_manifest(self, steps: list[dict], **extra) -> Path:
        value = {"schemaVersion": 1, "steps": steps, **extra}
        self.manifest_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return self.manifest_path

    def pause_steps(self, count: int) -> list[dict]:
        return [{"type": "pause", "message": f"Pause {index}"} for index in range(1, count + 1)]

    def add_migration(self, folder_name: str = "2026-10-10_release-r001") -> str:
        folder = self.root / "migrations" / folder_name
        folder.mkdir(parents=True)
        (folder / "001-create-table.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8")
        checks = {
            "schemaVersion": 1,
            "preconditions": [],
            "postconditions": [{"id": "table-exists", "sql": "SELECT 1 FROM dual", "expected": 1}],
        }
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8")
        return f"migrations/{folder_name}"

    def add_app(self, app_id: int = 100, schema: str = "DEMO") -> Path:
        app_dir = self.root / "apps" / schema / str(app_id)
        (app_dir / "deployments").mkdir(parents=True)
        (app_dir / "application.apx").write_text(f"app {app_id} {{\n}}\n", encoding="utf-8")
        descriptor = {
            "workspace": {"name": "TEAM_WS"},
            "app": {"id": app_id, "databaseSession": {"parsingSchema": schema}},
        }
        (app_dir / "deployments" / "staging.json").write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
        return app_dir

    def fake_executor(self, calls: list[int], *, fail_at: int | None = None, mutate=None):
        def execute(step, **context):
            number = context["step_number"]
            calls.append(number)
            evidence = context["evidence_dir"] / f"step-{number:03d}.log"
            evidence.write_text(f"step {number}\n", encoding="utf-8")
            if mutate is not None:
                mutate(number)
            code = 2 if number == fail_at else 0
            return rollout.StepResult(code, (evidence.relative_to(self.root).as_posix(),), "simulated")

        return execute

    def observe_target_identity(self, target, run_dir):
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "session_user": target.expected_user,
            "current_schema": target.schema,
            "db_name": "TESTDB",
            "db_unique_name": "TESTDB_UNIQUE",
            "service_name": "test.service",
            "container_id": "3",
            "container_name": "TEST_PDB",
            "edition": "ORA$BASE",
            "database_version": "23.0.0.0.0",
        }

    def test_rollout_stops_at_first_failed_step_and_reports_times_and_evidence(self) -> None:
        calls: list[int] = []
        steps = self.pause_steps(3)
        manifest = self.write_manifest(steps)
        report_base = self.root / "scratch" / "report"
        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls, fail_at=2)), patch(
            "sys.stdout"
        ) as stdout, patch("sys.stderr") as stderr:
            status = rollout.run_rollout(
                manifest,
                "dev",
                repo_root=self.root,
                report_path=report_base,
                confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
            )

        self.assertEqual(status, 2)
        self.assertEqual(calls, [1, 2])
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertRegex(report["startedAt"], r"^\d{4}-\d\d-\d\dT")
        self.assertIsInstance(report["durationSeconds"], (int, float))
        self.assertTrue(report["steps"][0]["startedAt"])
        self.assertIsInstance(report["steps"][0]["durationSeconds"], (int, float))
        self.assertTrue(report["steps"][0]["evidencePaths"])
        self.assertEqual(report["steps"][2]["status"], "not-run")
        stdout_text = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        stderr_text = "".join(call.args[0] for call in stderr.write.call_args_list if call.args)
        self.assertIn("Rollout step 02 failed: simulated", stderr_text)
        self.assertIn("report.json", stdout_text)
        markdown = report_base.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("# Rollout report", markdown)
        self.assertIn("step-001.log", markdown)

    def test_staging_confirmation_shows_every_step_hash_once_before_execution(self) -> None:
        calls: list[int] = []
        manifest = self.write_manifest(self.pause_steps(2))
        report_base = self.root / "scratch" / "report"
        prompts: list[str] = []
        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls)):
            status = rollout.run_rollout(
                manifest,
                "staging",
                repo_root=self.root,
                report_path=report_base,
                confirm=lambda prompt: prompts.append(prompt) or True,
            )

        self.assertEqual(status, 0)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(prompts), 1)
        self.assertIn("Step 01", prompts[0])
        self.assertIn("Step 02", prompts[0])
        self.assertIn("SHA-256", prompts[0])

    def test_declined_manifest_confirmation_runs_no_steps(self) -> None:
        calls: list[int] = []
        manifest = self.write_manifest(self.pause_steps(1))
        report_base = self.root / "scratch" / "report"
        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls)):
            status = rollout.run_rollout(
                manifest,
                "prod",
                repo_root=self.root,
                report_path=report_base,
                confirm=lambda _prompt: False,
            )

        self.assertEqual(status, 1)
        self.assertEqual(calls, [])
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "declined")

    def test_resume_requires_matching_receipts_and_skips_only_prior_steps(self) -> None:
        calls: list[int] = []
        manifest = self.write_manifest(self.pause_steps(3))
        report_base = self.root / "scratch" / "first"
        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls)):
            first_status = rollout.run_rollout(
                manifest, "dev", repo_root=self.root, report_path=report_base,
                confirm=lambda _prompt: True,
            )
        self.assertEqual(first_status, 0)
        self.assertEqual(calls, [1, 2, 3])

        calls.clear()
        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls)):
            resumed_status = rollout.run_rollout(
                manifest, "dev", from_step=3, repo_root=self.root,
                report_path=self.root / "scratch" / "resumed", confirm=lambda _prompt: True,
            )
        self.assertEqual(resumed_status, 0)
        self.assertEqual(calls, [3])
        resumed = json.loads((self.root / "scratch" / "resumed.json").read_text(encoding="utf-8"))
        self.assertEqual([step["status"] for step in resumed["steps"]], ["resumed", "resumed", "succeeded"])

        receipt_path = self.root / resumed["steps"][0]["receiptPath"]
        tampered_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        tampered_receipt["evidencePaths"] = ["../../outside"]
        receipt_path.write_text(json.dumps(tampered_receipt) + "\n", encoding="utf-8")
        calls.clear()
        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls)):
            rejected_status = rollout.run_rollout(
                manifest, "dev", from_step=3, repo_root=self.root,
                report_path=self.root / "scratch" / "rejected", confirm=lambda _prompt: True,
            )
        self.assertEqual(rejected_status, 2)
        self.assertEqual(calls, [])

    def test_resume_revalidates_independent_database_identity_before_skipping(self) -> None:
        folder = self.add_migration()
        manifest = self.write_manifest([
            {"type": "verify", "folders": [folder], "phase": "post"},
            {"type": "pause", "message": "finish"},
        ])
        first_identity = {
            "session_user": "APP", "current_schema": "APP", "db_name": "DBA",
            "db_unique_name": "DBA_UNQ", "service_name": "APP_SERVICE", "container_id": "3",
            "container_name": "PDBA", "edition": "ORA$BASE", "database_version": "23.0.0.0.0",
        }
        repointed_identity = {**first_identity, "db_name": "DBB", "db_unique_name": "DBB_UNQ", "container_name": "PDBB"}
        configured = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "DEV_ALIAS",
            "CODE_EXPECTED_USER": "APP",
        }
        calls: list[int] = []

        def execute(step, **context):
            number = context["step_number"]
            calls.append(number)
            evidence = context["evidence_dir"] / f"step-{number:03d}.log"
            evidence.write_text("verified\n", encoding="utf-8")
            return rollout.StepResult(0, (evidence.relative_to(self.root).as_posix(),), "simulated")

        def observer(identity):
            def observe(_target, run_dir):
                run_dir.mkdir(parents=True, exist_ok=True)
                return identity
            return observe

        initial_observer = observer(first_identity)

        with patch.dict(os.environ, configured, clear=False), patch(
            "scripts.rollout.execute_step", side_effect=execute
        ), patch("scripts.rollout._observe_target_identity", side_effect=initial_observer):
            first_status = rollout.run_rollout(
                manifest, "dev", repo_root=self.root, report_path=self.root / "scratch" / "identity-first",
                confirm=lambda _prompt: True,
            )
        self.assertEqual(first_status, 0)

        calls.clear()
        with patch.dict(os.environ, configured, clear=False), patch(
            "scripts.rollout.execute_step", side_effect=execute
        ), patch("scripts.rollout._observe_target_identity", side_effect=observer(repointed_identity)):
            resumed_status = rollout.run_rollout(
                manifest, "dev", from_step=2, repo_root=self.root,
                report_path=self.root / "scratch" / "identity-resume",
                confirm=lambda _prompt: True,
            )

        self.assertEqual(resumed_status, 2)
        self.assertEqual(calls, [])
        resumed = json.loads((self.root / "scratch" / "identity-resume.json").read_text(encoding="utf-8"))
        self.assertIn("differs from its successful receipt", resumed["error"])

    def test_file_change_during_a_step_aborts_before_the_next_step(self) -> None:
        payload = self.root / "scripts" / "second.sql"
        payload.write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        steps = [
            {"type": "pause", "message": "first"},
            {"type": "sql-script", "file": "scripts/second.sql", "connection": "OPS", "expectedUser": "OPS", "schema": "APP"},
        ]
        manifest = self.write_manifest(steps)
        calls: list[int] = []

        def mutate(number: int) -> None:
            if number == 1:
                payload.write_text("DROP TABLE important_data;\n", encoding="utf-8")

        with patch("scripts.rollout.execute_step", side_effect=self.fake_executor(calls, mutate=mutate)):
            status = rollout.run_rollout(
                manifest, "dev", repo_root=self.root,
                report_path=self.root / "scratch" / "changed", confirm=lambda _prompt: True,
            )

        self.assertEqual(status, 2)
        self.assertEqual(calls, [1])
        report = json.loads((self.root / "scratch" / "changed.json").read_text(encoding="utf-8"))
        self.assertIn("changed after rollout started", report["error"])

    def test_dry_run_prints_hashed_plan_without_running_steps_or_writing_receipts(self) -> None:
        payload = self.root / "scripts" / "pre.sql"
        payload.write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        manifest = self.write_manifest([
            {"type": "sql-script", "file": "scripts/pre.sql", "connection": "OPS", "expectedUser": "OPS", "schema": "APP"}
        ])
        report_base = self.root / "scratch" / "dry-run"
        sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()
        with patch("scripts.rollout.execute_step") as execute, patch("sys.stdout") as stdout:
            status = rollout.run_rollout(
                manifest, "prod", dry_run=True, repo_root=self.root,
                report_path=report_base,
                confirm=lambda _prompt: self.fail("dry-run must not request confirmation"),
            )

        self.assertEqual(status, 0)
        execute.assert_not_called()
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("dry run", output.casefold())
        self.assertIn(sha256, output)
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "planned")
        self.assertFalse(list((self.root / "scratch").glob("rollout-receipts/**/step-*.json")))

    def test_manifest_schema_rejects_unknown_fields_duplicate_keys_and_unsafe_paths(self) -> None:
        self.manifest_path.write_text(
            '{"schemaVersion":1,"schemaVersion":1,"steps":[{"type":"pause","message":"x"}]}\n',
            encoding="utf-8",
        )
        with self.assertRaises(rollout.RolloutError):
            rollout.load_manifest(self.manifest_path, self.root)

        self.write_manifest([{"type": "pause", "message": "x", "typo": True}])
        with self.assertRaises(rollout.RolloutError):
            rollout.load_manifest(self.manifest_path, self.root)

        self.write_manifest([{"type": "sql-script", "file": "../outside.sql", "connection": "OPS", "expectedUser": "OPS", "schema": "APP"}])
        with self.assertRaises(rollout.RolloutError):
            rollout.load_manifest(self.manifest_path, self.root)

    def test_sql_script_refuses_marker_spoofing_client_directives_and_schema_changes(self) -> None:
        for source in (
            "PROMPT ROLLOUT_IDENTITY:OPS:APP\n",
            "PROMPT ROLLOUT_TARGET:DEVDB:DEVDB:DEV\n",
            "ALTER SESSION SET CURRENT_SCHEMA = OTHER;\n",
            "BEGIN\n  EXECUTE IMMEDIATE 'ALTER SESSION SET CURRENT_SCHEMA = OTHER';\nEND;\n/\n",
            "-- client commands can hide source files\n@@other.sql\n",
        ):
            with self.subTest(source=source), self.assertRaises(rollout.RolloutError):
                rollout.validate_sql_script(source)

    def test_sql_script_rejects_sqlcl_abbreviations_and_comment_reader_hazards(self) -> None:
        for source in (
            "conn OTHER\nCREATE TABLE T (ID NUMBER);\n",
            "sta other.sql\nCREATE TABLE T (ID NUMBER);\n",
            "GET other.sql /\nCREATE TABLE T (ID NUMBER);\n",
            "/* hidden reader hazard\n/\n*/\nCONNECT OTHER\n",
        ):
            with self.subTest(source=source), self.assertRaises(rollout.RolloutError):
                rollout.validate_sql_script(source)

    def test_plan_freezes_target_binding_and_snapshot_rejects_configuration_change(self) -> None:
        payload = self.root / "scripts" / "prerequisite.sql"
        payload.write_text("CREATE TABLE APP.PRECHECK (ID NUMBER);\n", encoding="utf-8")
        manifest = self.write_manifest([{
            "type": "sql-script", "file": "scripts/prerequisite.sql",
            "connection": "OPS", "expectedUser": "OPS", "schema": "APP",
        }])
        loaded = rollout.load_manifest(manifest, self.root)
        with patch.dict(os.environ, {"DB_ENVIRONMENT": "development"}, clear=False):
            plan = rollout._prepare_plan(loaded, "dev")
            binding = plan[0].get("targetBinding")
            self.assertEqual(binding, {
                "environment": "dev", "connection": "OPS", "expectedUser": "OPS",
                "schema": "APP", "classification": "development",
            })
            with patch.dict(os.environ, {"DB_ENVIRONMENT": "test"}, clear=False):
                with self.assertRaisesRegex(rollout.RolloutError, "target binding changed"):
                    rollout._snapshot(loaded, plan, "before step 1", "dev")

    def test_rollout_migration_resume_requires_exact_source_and_observed_target_receipts(self) -> None:
        folder = self.add_migration()
        migration = load_batch(self.root, [folder])[0]
        observed = {
            "session_user": "APP", "current_schema": "APP", "db_name": "DBA",
            "db_unique_name": "DBA_UNQ", "service_name": "APP_SERVICE", "container_id": "3",
            "container_name": "PDBA", "edition": "ORA$BASE", "database_version": "23.0.0.0.0",
        }
        target_identity = {
            "environment": "dev", "connection": "DEV_ALIAS", "expected_user": "APP", **observed,
        }
        receipt = {
            "schemaVersion": 1,
            "state": "verified",
            "environment": "dev",
            "migration": migration.folder.name,
            "createdDate": migration.date,
            "family": migration.family,
            "revision": migration.revision,
            "files": [{"name": item.name, "sequence": item.sequence, "sha256": item.sha256} for item in migration.files],
            "checksSha256": migration.checks_sha256,
            "payloadDigest": migration.payload_digest,
            "target": target_identity,
            "applyStartedAt": "2026-10-11T00:00:00Z",
            "applyCompletedAt": "2026-10-11T00:01:00Z",
            "verifiedAt": "2026-10-11T00:02:00Z",
            "checks": [{"id": check.id, "passed": True, "rows": 1, "value": 1} for check in migration.postconditions],
            "verifier": "template-migration-v1",
            "normalization": "normalization-v1",
        }
        status_path = migration.folder / "status.dev.json"
        status_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        step = {"type": "migrate", "folders": [folder]}
        binding = {
            "environment": "dev", "connection": "DEV_ALIAS", "expectedUser": "APP",
            "schema": "APP", "classification": "development",
        }

        rollout._verify_migration_receipts(self.root, step, binding, observed)

        wrong_target = {**receipt, "target": {**target_identity, "db_unique_name": "OTHER_DB"}}
        status_path.write_text(json.dumps(wrong_target) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(rollout.RolloutError, "exact source and observed target"):
            rollout._verify_migration_receipts(self.root, step, binding, observed)

        status_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        (migration.folder / migration.files[0].name).write_text("CREATE TABLE T (ID NUMBER, EXTRA NUMBER);\n", encoding="utf-8")
        with self.assertRaisesRegex(rollout.RolloutError, "exact source and observed target"):
            rollout._verify_migration_receipts(self.root, step, binding, observed)

    def test_sql_script_runs_through_fake_sqlcl_with_identity_and_failure_guards(self) -> None:
        payload = self.root / "scripts" / "prerequisite.sql"
        payload.write_text("CREATE TABLE APP.PRECHECK (ID NUMBER);\n", encoding="utf-8")
        captured_driver = self.root / "captured-driver.sql"
        fake_bin = self.root / "fake-bin"
        install_fake_sqlcl(
            fake_bin,
            'for arg in "$@"; do case "$arg" in @*) driver="${arg#@}";; esac; done\n'
            'cp "$driver" "$FAKE_CAPTURED_DRIVER"\n'
            'printf "ROLLOUT_IDENTITY:OPS:APP\\nROLLOUT_TARGET:DEVDB:DEVDB:DEVSERVICE\\nROLLOUT_SCRIPT_COMPLETED:0001\\n"\n',
        )
        step = {"type": "sql-script", "file": "scripts/prerequisite.sql", "connection": "OPS", "expectedUser": "OPS", "schema": "APP"}
        evidence = self.root / "scratch" / "run" / "step-001"
        manifest = self.write_manifest([step])
        loaded = rollout.load_manifest(manifest, self.root)
        plan = rollout._prepare_plan(loaded, "dev")
        with patch.dict(os.environ, fake_environment(fake_bin, FAKE_CAPTURED_DRIVER=str(captured_driver), DB_ENVIRONMENT="development")):
            result = rollout.execute_step(
                step,
                environment="dev",
                repo_root=self.root,
                evidence_dir=evidence,
                step_number=1,
                manifest=loaded,
                plan=plan,
                manifest_confirmed=False,
            )

        self.assertEqual(result.exit_code, 0, result.message)
        driver = captured_driver.read_text(encoding="utf-8")
        self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", driver)
        self.assertIn("DBMS_OUTPUT.PUT_LINE('ROLLOUT_IDENTITY:'", driver)
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = APP;", driver)
        self.assertIn("@@payload.sql", driver)
        self.assertLess(driver.index("IF UPPER(v_session_user) <> 'OPS'"), driver.index("ALTER SESSION SET CURRENT_SCHEMA"))
        self.assertLess(driver.index("ALTER SESSION SET CURRENT_SCHEMA"), driver.index("@@payload.sql"))
        self.assertIn("SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')", driver)
        self.assertIn("resembles_production(v_service_name)", driver)
        self.assertLess(driver.index("IF c_target_environment <> 'prod'"), driver.index("@@payload.sql"))
        self.assertTrue((evidence / "sqlcl-output.log").is_file())

    def test_sql_script_failure_report_includes_sqlcl_error_output(self) -> None:
        payload = self.root / "scripts" / "prerequisite.sql"
        payload.write_text("CREATE TABLE APP.PRECHECK (ID NUMBER);\n", encoding="utf-8")
        fake_bin = self.root / "fake-bin"
        install_fake_sqlcl(fake_bin, 'printf "ORA-20999: prerequisite check failed\\n"\nexit 1\n')
        manifest = self.write_manifest([{
            "type": "sql-script", "file": "scripts/prerequisite.sql",
            "connection": "OPS", "expectedUser": "OPS", "schema": "APP",
        }])
        report_base = self.root / "scratch" / "sql-failure"

        with patch.dict(
            os.environ,
            fake_environment(fake_bin, DB_ENVIRONMENT="development"),
        ):
            status = rollout.run_rollout(
                manifest, "dev", repo_root=self.root, report_path=report_base,
                confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
            )

        self.assertEqual(status, 2)
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIn("ORA-20999: prerequisite check failed", report["steps"][0]["message"])

    def test_failed_verify_report_names_failed_check_ids_and_errors(self) -> None:
        folder = self.add_migration()
        manifest = self.write_manifest([{"type": "verify", "folders": [folder], "phase": "post"}])
        report_base = self.root / "scratch" / "verify-failure"
        failed_report = {
            "schemaVersion": 1,
            "evidenceDir": "scratch/check-evidence",
            "checks": [{
                "folder": "2026-10-10_release-r001",
                "phase": "postconditions",
                "id": "receipt-table-exists",
                "expected": 1,
                "observed": 0,
                "status": "false",
                "error": None,
            }],
            "errors": [{
                "code": "CHECK_ERROR",
                "folder": "2026-10-10_release-r001",
                "phase": "postconditions",
                "id": "receipt-index-valid",
                "message": "ORA-00942: table or view does not exist",
            }],
        }

        def fake_verify_main(_arguments, *, environ, repo_root, expected_input_hashes):
            print(json.dumps(failed_report, indent=2))
            return 2

        with patch("scripts.rollout.verify_checks.main", side_effect=fake_verify_main):
            status = rollout.run_rollout(
                manifest, "dev", repo_root=self.root, report_path=report_base,
                confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
            )

        self.assertEqual(status, 2)
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        message = report["steps"][0]["message"]
        self.assertIn("receipt-table-exists", message)
        self.assertIn("false", message)
        self.assertIn("receipt-index-valid", message)
        self.assertIn("ORA-00942", message)

    def test_sql_script_cannot_execute_bytes_different_from_the_confirmed_hash(self) -> None:
        payload = self.root / "scripts" / "prerequisite.sql"
        original = b"CREATE TABLE APP.SAFE_STEP (ID NUMBER);\n"
        payload.write_bytes(original)
        captured_payload = self.root / "captured-payload.sql"
        fake_bin = self.root / "fake-bin"
        install_fake_sqlcl(
            fake_bin,
            'for arg in "$@"; do case "$arg" in @*) driver="${arg#@}";; esac; done\n'
            'cp "$(dirname "$driver")/payload.sql" "$FAKE_CAPTURED_PAYLOAD"\n'
            'printf "ROLLOUT_IDENTITY:OPS:APP\\nROLLOUT_TARGET:DEVDB:DEVDB:devservice\\nROLLOUT_SCRIPT_COMPLETED:0001\\n"\n',
        )
        manifest = self.write_manifest([{
            "type": "sql-script", "file": "scripts/prerequisite.sql",
            "connection": "OPS", "expectedUser": "OPS", "schema": "APP",
        }])
        report_base = self.root / "scratch" / "race"
        snapshot = rollout._snapshot
        calls = 0

        def change_after_pre_step(manifest_arg, plan_arg, boundary, environment):
            nonlocal calls
            calls += 1
            if calls == 1:
                snapshot(manifest_arg, plan_arg, boundary, environment)
                payload.write_bytes(b"DROP TABLE APP.IMPORTANT_DATA;\n")
                return
            if calls == 2:
                payload.write_bytes(original)
            snapshot(manifest_arg, plan_arg, boundary, environment)

        with patch.dict(
            os.environ,
            fake_environment(fake_bin, FAKE_CAPTURED_PAYLOAD=str(captured_payload), DB_ENVIRONMENT="development"),
        ), patch("scripts.rollout._snapshot", side_effect=change_after_pre_step):
            status = rollout.run_rollout(
                manifest, "dev", repo_root=self.root, report_path=report_base,
                confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
            )

        self.assertEqual(status, 2)
        self.assertFalse(captured_payload.exists(), "SQLcl must not receive unconfirmed source bytes")
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIn("does not match the confirmed SHA-256", report["error"])

    def test_sql_script_refuses_connection_alias_that_differs_from_manifest_user(self) -> None:
        payload = self.root / "scripts" / "prerequisite.sql"
        payload.write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        step = {"type": "sql-script", "file": "scripts/prerequisite.sql", "connection": "OPS_ALIAS", "expectedUser": "OPS", "schema": "APP"}
        with self.assertRaises(rollout.RolloutError):
            rollout.validate_step(step, 1, self.root)

    def test_migrate_step_uses_existing_runner_with_the_manifest_approval(self) -> None:
        folder = self.add_migration()
        manifest = self.write_manifest([{"type": "migrate", "folders": [folder]}])
        prompts: list[str] = []
        callback_approvals: list[bool] = []

        def fake_migrate_main(arguments, *, environ, repo_root, confirm, expected_input_hashes):
            self.assertEqual(arguments, [folder, "--env", "staging"])
            self.assertIs(environ, os.environ)
            self.assertEqual(repo_root, self.root)
            self.assertEqual(
                expected_input_hashes[f"{folder}/001-create-table.sql"],
                hashlib.sha256((self.root / folder / "001-create-table.sql").read_bytes()).hexdigest(),
            )
            callback_approvals.append(confirm("child migration prompt"))
            return 0

        with patch("scripts.rollout.migrate.main", side_effect=fake_migrate_main), patch(
            "scripts.rollout._verify_migration_receipts"
        ):
            status = rollout.run_rollout(
                manifest,
                "staging",
                repo_root=self.root,
                report_path=self.root / "scratch" / "migration-rollout",
                confirm=lambda prompt: prompts.append(prompt) or True,
            )

        self.assertEqual(status, 0)
        self.assertEqual(len(prompts), 1)
        self.assertIn("Step 01", prompts[0])
        self.assertEqual(callback_approvals, [True])

    def test_manifest_confirmation_names_the_application_workspace_and_schema(self) -> None:
        self.add_app()
        manifest = self.write_manifest([{"type": "app-deploy", "appId": 100}])
        prompts: list[str] = []
        self.write_project_env({
            **self.project_env_values,
            "STAGING_SQLCL_CONNECTION": "TEAM_STAGE",
            "STAGING_EXPECTED_USER": "APP_SCHEMA",
        })
        with patch.dict(os.environ, {
            "APEX_PARSING_SCHEMA": "DEMO",
            "STAGING_SCHEMA": "DEMO",
            "STAGING_SQLCL_CONNECTION": "TEAM_STAGE",
            "STAGING_EXPECTED_USER": "APP_SCHEMA",
        }, clear=False), patch(
            "scripts.rollout.subprocess.run",
            return_value=subprocess.CompletedProcess(["bash"], 0, "deployment complete\n"),
        ):
            status = rollout.run_rollout(
                manifest,
                "staging",
                repo_root=self.root,
                report_path=self.root / "scratch" / "app-target",
                confirm=lambda prompt: prompts.append(prompt) or True,
            )

        self.assertEqual(status, 0)
        self.assertEqual(len(prompts), 1)
        self.assertIn("workspace TEAM_WS", prompts[0])
        self.assertIn("parsing schema DEMO", prompts[0])
        self.assertIn("connection TEAM_STAGE", prompts[0])
        self.assertIn("expected user APP_SCHEMA", prompts[0])

    def test_migration_and_verify_runners_refuse_loaded_bytes_not_in_rollout_hashes(self) -> None:
        folder = self.add_migration()
        expected = {f"{folder}/001-create-table.sql": "0" * 64}
        cases = (
            (rollout.migrate.main, [folder, "--env", "dev"]),
            (rollout.verify_checks.main, [folder, "--env", "dev", "--phase", "post", "--format", "json"]),
        )
        for runner, arguments in cases:
            with self.subTest(runner=runner.__module__), patch("sys.stderr") as stderr, patch("sys.stdout") as stdout:
                status = runner(arguments, environ={}, repo_root=self.root, expected_input_hashes=expected)
            self.assertEqual(status, 2)
            message = "".join(call.args[0] for call in stderr.write.call_args_list if call.args)
            message += "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
            self.assertIn("does not match the confirmed SHA-256", message)

    def test_verify_step_uses_existing_read_only_runner_and_json_output(self) -> None:
        folder = self.add_migration()
        manifest = self.write_manifest([{"type": "verify", "folders": [folder], "phase": "post"}])
        captured: list[list[str]] = []

        def fake_verify_main(arguments, *, environ, repo_root, expected_input_hashes):
            captured.append(list(arguments))
            self.assertEqual(
                expected_input_hashes[f"{folder}/checks.json"],
                hashlib.sha256((self.root / folder / "checks.json").read_bytes()).hexdigest(),
            )
            (self.root / "scratch" / "check-evidence").mkdir(parents=True, exist_ok=True)
            print(json.dumps({"schemaVersion": 1, "evidenceDir": "scratch/check-evidence"}))
            return 0

        with patch("scripts.rollout.verify_checks.main", side_effect=fake_verify_main):
            status = rollout.run_rollout(
                manifest,
                "dev",
                repo_root=self.root,
                report_path=self.root / "scratch" / "verify-rollout",
                confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
            )

        self.assertEqual(status, 0)
        self.assertEqual(captured, [[folder, "--env", "dev", "--phase", "post", "--format", "json"]])
        report = json.loads((self.root / "scratch" / "verify-rollout.json").read_text(encoding="utf-8"))
        self.assertEqual(report["steps"][0]["evidencePaths"][1], "scratch/check-evidence")

    def test_app_deploy_uses_existing_deploy_script_with_manifest_approval(self) -> None:
        app_dir = self.add_app()
        manifest = self.write_manifest([{"type": "app-deploy", "appId": 100}])
        prompts: list[str] = []
        completed = subprocess.CompletedProcess(["bash"], 0, "deployment complete\n")
        self.write_project_env({
            **self.project_env_values,
            "STAGING_SQLCL_CONNECTION": "TEAM_STAGE",
            "STAGING_EXPECTED_USER": "APP_SCHEMA",
        })

        def deploy_frozen_source(command, **_kwargs):
            staged = Path(command[command.index("--app-source-dir") + 1])
            original = (app_dir / "application.apx").read_bytes()
            (app_dir / "application.apx").write_text("app 100 {\n  changed: true\n}\n", encoding="utf-8")
            self.assertEqual((staged / "application.apx").read_bytes(), original)
            (app_dir / "application.apx").write_bytes(original)
            return completed

        with patch.dict(os.environ, {
            "APEX_PARSING_SCHEMA": "DEMO",
            "STAGING_SCHEMA": "DEMO",
            "STAGING_SQLCL_CONNECTION": "TEAM_STAGE",
            "STAGING_EXPECTED_USER": "APP_SCHEMA",
        }, clear=False), patch(
            "scripts.rollout.subprocess.run", side_effect=deploy_frozen_source
        ) as run:
            status = rollout.run_rollout(
                manifest,
                "staging",
                repo_root=self.root,
                report_path=self.root / "scratch" / "app-rollout",
                confirm=lambda prompt: prompts.append(prompt) or True,
            )

        self.assertEqual(status, 0)
        self.assertEqual(len(prompts), 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-5:-2], ["100", "--env", "staging"])
        self.assertIn("--app-source-dir", command)
        staged_app = Path(command[command.index("--app-source-dir") + 1])
        self.assertEqual((staged_app / "application.apx").read_bytes(), b"app 100 {\n}\n")
        self.assertEqual((staged_app / "deployments" / "staging.json").read_bytes(), (app_dir / "deployments" / "staging.json").read_bytes())
        self.assertNotIn("--force", command)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(run.call_args.kwargs["input"], "y\n")
        self.assertFalse(any(key.startswith("TEAM_ROLLOUT_") for key in child_env))
        self.assertTrue((app_dir / "application.apx").is_file())
        report = json.loads((self.root / "scratch" / "app-rollout.json").read_text(encoding="utf-8"))
        confirmation = json.loads((self.root / report["confirmationPath"]).read_text(encoding="utf-8"))
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(confirmation["steps"][0]["sha256"], report["steps"][0]["sha256"])

    def test_app_deploy_uses_frozen_child_configuration_after_original_changes_after_confirmation(self) -> None:
        self.add_app()
        manifest = self.write_manifest([{"type": "app-deploy", "appId": 100}])
        deploy_script = self.root / "scripts" / "deploy.sh"
        loader = ROOT / "scripts" / "load_env.sh"
        deploy_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"source {loader.as_posix()!r} \"$PROJECT_ENV_FILE\"\n"
            "printf 'CHILD_TARGET:%s:%s\\n' \"$STAGING_SQLCL_CONNECTION\" \"$STAGING_EXPECTED_USER\"\n",
            encoding="utf-8",
        )
        deploy_script.chmod(0o700)
        prompts: list[str] = []

        def confirm_after_mutating_original(prompt: str) -> bool:
            prompts.append(prompt)
            changed = {
                **self.project_env_values,
                "STAGING_SQLCL_CONNECTION": "CHANGED_STAGE",
                "STAGING_EXPECTED_USER": "OTHER_APP",
            }
            self.write_project_env(changed)
            return True

        report_base = self.root / "scratch" / "frozen-app-config"
        status = rollout.run_rollout(
            manifest,
            "staging",
            repo_root=self.root,
            report_path=report_base,
            confirm=confirm_after_mutating_original,
        )

        self.assertEqual(status, 0)
        self.assertEqual(len(prompts), 1)
        self.assertIn("child configuration SHA-256", prompts[0])
        report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        confirmation = json.loads((self.root / report["confirmationPath"]).read_text(encoding="utf-8"))
        config_hash = report["steps"][0]["childConfigurationSha256"]
        self.assertEqual(confirmation["steps"][0]["childConfigurationSha256"], config_hash)
        deploy_log_path = next(
            self.root / path
            for path in report["steps"][0]["evidencePaths"]
            if path.endswith("deploy-output.log")
        )
        deploy_log = deploy_log_path.read_text(encoding="utf-8")
        self.assertIn("CHILD_TARGET:STAGE_ALIAS:DEMO", deploy_log)
        self.assertNotIn("CHANGED_STAGE", deploy_log)
        self.assertEqual(rollout.read_project_env(self.project_env_file)["STAGING_SQLCL_CONNECTION"], "CHANGED_STAGE")
        frozen_env_path = next(
            self.root / path
            for path in report["steps"][0]["evidencePaths"]
            if path.endswith("app-deploy.env")
        )
        self.assertNotEqual(frozen_env_path, self.project_env_file)
        self.assertEqual(rollout.read_project_env(frozen_env_path)["STAGING_SQLCL_CONNECTION"], "STAGE_ALIAS")

    def test_app_deploy_rejects_child_configuration_target_mismatch_before_confirmation(self) -> None:
        self.add_app()
        self.write_project_env({
            **self.project_env_values,
            "STAGING_SQLCL_CONNECTION": "DIFFERENT_STAGE",
        })
        manifest = self.write_manifest([{"type": "app-deploy", "appId": 100}])
        loaded = rollout.load_manifest(manifest, self.root)

        with self.assertRaisesRegex(rollout.RolloutError, "does not match the rollout target binding"):
            rollout._prepare_plan(loaded, "staging")

    def test_app_deploy_preserves_dev_selector_for_distinct_staging_and_prod_schemas(self) -> None:
        app_dir = self.add_app()
        app_id = 100
        for environment, schema in (("staging", "STAGE_OWNER"), ("prod", "PROD_OWNER")):
            descriptor = {
                "workspace": {"name": "TEAM_WS"},
                "app": {"id": app_id, "databaseSession": {"parsingSchema": schema}},
            }
            (app_dir / "deployments" / f"{environment}.json").write_text(
                json.dumps(descriptor) + "\n", encoding="utf-8",
            )

        config = {
            **self.project_env_values,
            "STAGING_SCHEMA": "STAGE_OWNER",
            "STAGING_SQLCL_CONNECTION": "STAGE_ALIAS",
            "STAGING_EXPECTED_USER": "STAGE_OWNER",
            "PROD_SCHEMA": "PROD_OWNER",
            "PROD_SQLCL_CONNECTION": "PROD_ALIAS",
            "PROD_EXPECTED_USER": "PROD_OWNER",
        }
        self.write_project_env(config)
        deploy_script = self.root / "scripts" / "deploy.sh"
        loader = ROOT / "scripts" / "load_env.sh"
        deploy_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"source {loader.as_posix()!r} \"$PROJECT_ENV_FILE\"\n"
            "printf 'SOURCE_SELECTOR:%s\\n' \"${PROJECT_SCHEMA:-<unset>}\"\n"
            "printf 'STAGING_PROFILE:%s:%s:%s\\n' \"${STAGING_SCHEMA:-}\" \"${STAGING_SQLCL_CONNECTION:-}\" \"${STAGING_EXPECTED_USER:-}\"\n"
            "printf 'PROD_PROFILE:%s:%s:%s\\n' \"${PROD_SCHEMA:-}\" \"${PROD_SQLCL_CONNECTION:-}\" \"${PROD_EXPECTED_USER:-}\"\n",
            encoding="utf-8",
        )
        deploy_script.chmod(0o700)
        manifest = self.write_manifest([{"type": "app-deploy", "appId": app_id}])

        for selector in (None, "DEMO"):
            for environment, schema, alias in (
                ("staging", "STAGE_OWNER", "STAGE_ALIAS"),
                ("prod", "PROD_OWNER", "PROD_ALIAS"),
            ):
                with self.subTest(selector=selector, environment=environment):
                    self.write_project_env(config)
                    configured = {
                        **config,
                        "PROJECT_ENV_FILE": str(self.project_env_file),
                        "PROJECT_SCHEMA": selector or "",
                    }
                    prompts: list[str] = []
                    report_base = self.root / "scratch" / f"selector-{selector or 'unset'}-{environment}"

                    def confirm_after_changing_process_selector(
                        prompt: str,
                        captured_prompts: list[str] = prompts,
                    ) -> bool:
                        captured_prompts.append(prompt)
                        os.environ["PROJECT_SCHEMA"] = "STAGE_OWNER"
                        return True

                    with patch.dict(os.environ, configured, clear=False):
                        if selector is None:
                            os.environ.pop("PROJECT_SCHEMA", None)
                        status = rollout.run_rollout(
                            manifest,
                            environment,
                            repo_root=self.root,
                            report_path=report_base,
                            confirm=confirm_after_changing_process_selector,
                        )

                    self.assertEqual(status, 0)
                    self.assertEqual(len(prompts), 1)
                    selector_label = selector if selector is not None else "<unset>"
                    self.assertIn(f"source PROJECT_SCHEMA selector: {selector_label}", prompts[0])
                    self.assertIn(f"parsing schema {schema}", prompts[0])
                    report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
                    self.assertEqual(report["steps"][0]["sourceSchemaSelector"], selector)
                    confirmation = json.loads((self.root / report["confirmationPath"]).read_text(encoding="utf-8"))
                    self.assertEqual(confirmation["steps"][0]["sourceSchemaSelector"], selector)
                    child_log = next(
                        self.root / path
                        for path in report["steps"][0]["evidencePaths"]
                        if path.endswith("deploy-output.log")
                    ).read_text(encoding="utf-8")
                    self.assertIn(f"SOURCE_SELECTOR:{selector_label}", child_log)
                    self.assertIn("STAGING_PROFILE:STAGE_OWNER:STAGE_ALIAS:STAGE_OWNER", child_log)
                    self.assertIn("PROD_PROFILE:PROD_OWNER:PROD_ALIAS:PROD_OWNER", child_log)
                    self.assertIn(f'"schema": "{schema}"', prompts[0])
                    self.assertIn(f'"connection": "{alias}"', prompts[0])

    def test_app_hashes_do_not_omit_unrelated_files_named_like_migration_receipts(self) -> None:
        app_dir = self.add_app()
        unusual = app_dir / "status.staging.json"
        unusual.write_text("app-owned source sidecar\n", encoding="utf-8")
        manifest = self.write_manifest([{"type": "app-deploy", "appId": 100}])
        loaded = rollout.load_manifest(manifest, self.root)
        plan = rollout._prepare_plan(loaded, "staging")
        self.assertIn(
            "apps/DEMO/100/status.staging.json",
            {item["path"] for item in plan[0]["inputHashes"]},
        )

    def test_report_path_cannot_overlap_app_deployment_inputs(self) -> None:
        app_dir = self.add_app()
        manifest = self.write_manifest([{"type": "app-deploy", "appId": 100}])
        status = rollout.run_rollout(
            manifest,
            "staging",
            repo_root=self.root,
            report_path=app_dir / "rollout.json",
            confirm=lambda _prompt: self.fail("preflight should reject an overlapping report path"),
        )

        self.assertEqual(status, 2)
        self.assertFalse((app_dir / "rollout.json").exists())

    def test_report_path_cannot_overwrite_a_rollout_receipt(self) -> None:
        manifest = self.write_manifest(self.pause_steps(1))
        manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        destinations = (
            self.root / "scratch" / "rollout-receipts" / manifest_hash / "dev" / "step-001.json",
            self.root / "scratch" / "migration-attempt-inflight" / "run-manifest.json",
        )
        for destination in destinations:
            with self.subTest(destination=destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("existing recovery evidence\n", encoding="utf-8")

                status = rollout.run_rollout(
                    manifest,
                    "dev",
                    repo_root=self.root,
                    report_path=destination,
                    confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
                )

                self.assertEqual(status, 2)
                self.assertEqual(destination.read_text(encoding="utf-8"), "existing recovery evidence\n")

    def test_report_path_cannot_overwrite_apexlang_upgrade_recovery_evidence(self) -> None:
        manifest = self.write_manifest(self.pause_steps(1))
        destination = self.root / "scratch" / "apexlang-upgrade.run123" / "conversion" / "conversion.json"
        destination.parent.mkdir(parents=True)
        destination.write_text("retained upgrade evidence\n", encoding="utf-8")

        status = rollout.run_rollout(
            manifest,
            "dev",
            repo_root=self.root,
            report_path=destination,
            confirm=lambda _prompt: self.fail("DEV must not ask for a rollout confirmation"),
        )

        self.assertEqual(status, 2)
        self.assertEqual(destination.read_text(encoding="utf-8"), "retained upgrade evidence\n")

    def test_ords_import_excludes_a_module_without_touching_other_modules(self) -> None:
        source = """-- Generated by SQLcl
-- Schema: REST_API
BEGIN
  ORDS.ENABLE_SCHEMA(p_schema => 'REST_API');
  ORDS.DEFINE_MODULE(p_module_name => 'legacy.api', p_base_path => '/legacy/');
  ORDS.DEFINE_TEMPLATE(p_module_name => 'legacy.api', p_pattern => 'old');
  ORDS.DEFINE_MODULE(p_module_name => 'current.api', p_base_path => '/current/');
  ORDS.DEFINE_TEMPLATE(p_module_name => 'current.api', p_pattern => 'new');
  COMMIT;
END;
/
"""
        filtered, removed = rollout.exclude_ords_modules(source, ["legacy.api"], "REST_API")
        self.assertEqual(removed, ["legacy.api"])
        self.assertNotIn("legacy.api", filtered)
        self.assertIn("current.api", filtered)
        self.assertIn("ORDS.ENABLE_SCHEMA", filtered)

    def test_ords_import_applies_exclusions_through_fake_sqlcl(self) -> None:
        payload_path = self.root / "scripts" / "ords-schema.sql"
        payload_path.write_text(
            """-- Generated by SQLcl
-- Schema: REST_API
BEGIN
  ORDS.ENABLE_SCHEMA(p_schema => 'REST_API');
  ORDS.DEFINE_MODULE(p_module_name => 'legacy.api', p_base_path => '/legacy/');
  ORDS.DEFINE_TEMPLATE(p_module_name => 'legacy.api', p_pattern => 'old');
  ORDS.DEFINE_MODULE(p_module_name => 'current.api', p_base_path => '/current/');
  ORDS.DEFINE_TEMPLATE(p_module_name => 'current.api', p_pattern => 'new');
  COMMIT;
END;
/
""",
            encoding="utf-8",
        )
        captured_payload = self.root / "captured-ords-payload.sql"
        fake_bin = self.root / "fake-bin"
        install_fake_sqlcl(
            fake_bin,
            'for arg in "$@"; do case "$arg" in @*) driver="${arg#@}";; esac; done\n'
            'cp "$(dirname "$driver")/payload.sql" "$FAKE_CAPTURED_PAYLOAD"\n'
            'printf "ROLLOUT_IDENTITY:REST_API:REST_API\\nROLLOUT_TARGET:DEVDB:DEVDB:DEVSERVICE\\nROLLOUT_SCRIPT_COMPLETED:0001\\n"\n',
        )
        step = {
            "type": "ords-import",
            "file": "scripts/ords-schema.sql",
            "connection": "REST_API",
            "expectedUser": "REST_API",
            "schema": "REST_API",
            "excludeModules": ["legacy.api"],
        }
        manifest = self.write_manifest([step])
        loaded = rollout.load_manifest(manifest, self.root)
        plan = rollout._prepare_plan(loaded, "dev")
        evidence = self.root / "scratch" / "ords-step"
        with patch.dict(
            os.environ,
            fake_environment(fake_bin, FAKE_CAPTURED_PAYLOAD=str(captured_payload), DB_ENVIRONMENT="development"),
        ):
            result = rollout.execute_step(
                step,
                environment="dev",
                repo_root=self.root,
                evidence_dir=evidence,
                step_number=1,
                manifest=loaded,
                plan=plan,
                manifest_confirmed=False,
            )

        self.assertEqual(result.exit_code, 0, result.message)
        installed_source = captured_payload.read_text(encoding="utf-8")
        self.assertNotIn("legacy.api", installed_source)
        self.assertIn("current.api", installed_source)
        self.assertIn("ORDS.ENABLE_SCHEMA", installed_source)


if __name__ == "__main__":
    unittest.main()
