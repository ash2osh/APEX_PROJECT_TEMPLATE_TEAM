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


ROOT = Path(__file__).resolve().parents[1]


class RolloutManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.manifest_path = self.root / "rollout.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

        with patch("scripts.rollout.migrate.main", side_effect=fake_migrate_main):
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
        with patch.dict(os.environ, {
            "APEX_PARSING_SCHEMA": "DEMO",
            "STAGING_SCHEMA": "DEMO",
            "STAGING_SQLCL_CONNECTION": "TEAM_STAGE",
            "STAGING_EXPECTED_USER": "APP_SCHEMA",
        }, clear=True), patch(
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

        def deploy_frozen_source(command, **_kwargs):
            staged = Path(command[command.index("--app-source-dir") + 1])
            original = (app_dir / "application.apx").read_bytes()
            (app_dir / "application.apx").write_text("app 100 {\n  changed: true\n}\n", encoding="utf-8")
            self.assertEqual((staged / "application.apx").read_bytes(), original)
            (app_dir / "application.apx").write_bytes(original)
            return completed

        with patch.dict(os.environ, {"APEX_PARSING_SCHEMA": "DEMO"}, clear=True), patch(
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
