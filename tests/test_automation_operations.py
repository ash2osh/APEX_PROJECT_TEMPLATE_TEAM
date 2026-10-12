from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import automation_operations as operations
from scripts.db_targets import Target
from scripts.sqlcl_session import SqlclError, SqlclResult


def snapshot(*, selected_status="DISABLED", other_status="DISABLED", app_id=940269):
    return {
        "schemaVersion": 1,
        "identity": {
            "session_user": "DEMO2",
            "current_schema": "DEMO2",
            "db_unique_name": "FREE",
            "container_id": "3",
            "container_name": "FREEPDB1",
            "edition": "ORA$BASE",
            "server_host": "b72393fa20fa",
            "service_name": "freepdb1",
        },
        "release": "26.2.0",
        "workspace": "DEMO",
        "workspaceId": "1000000000000000",
        "appId": app_id,
        "applicationPresent": True,
        "startedAt": "2026-10-12T07:00:00.000000Z",
        "completedAt": "2026-10-12T07:00:01.000000Z",
        "automations": [
            {"staticId": "TEMPLATE_AUTOMATION_SELECTED", "status": selected_status},
            {"staticId": "TEMPLATE_AUTOMATION_UNSELECTED", "status": other_status},
        ],
        "workflows": [],
        "tasks": [],
        "coverage": {
            "application": True,
            "workspace": True,
            "automations": True,
            "workflows": True,
            "tasks": True,
        },
    }


class AutomationOperationTests(unittest.TestCase):
    def setUp(self):
        self.target = Target("dev", "docker-demo2", "DEMO2", "DEMO2", "development")
        self.before = snapshot()

    def test_sql_adapter_emits_null_run_log_when_no_run_log_exists(self):
        adapter = Path(__file__).resolve().parents[1] / "scripts" / "automation_operations.sql"
        normalized = "".join(adapter.read_text(encoding="utf-8").split()).casefold()
        self.assertIn(
            "apex_json.write('runlog',v_log_status,p_write_null=>true);",
            normalized,
        )

    def action_output(self, action, *, run_log=None):
        after_status = {"enable": "ACTIVE", "disable": "DISABLED", "run-once": "DISABLED"}[action]
        payload = {
            "schemaVersion": 1,
            "action": action.upper(),
            "appId": 940269,
            "workspace": "DEMO",
            "workspaceId": "1000000000000000",
            "staticId": "TEMPLATE_AUTOMATION_SELECTED",
            "beforeStatus": "DISABLED",
            "afterStatus": after_status,
            "apiReturned": True,
            "sessionDeleted": True,
            "startedAt": "2026-10-12T07:00:02.000000Z",
            "runLogCount": 1 if run_log else 0,
            "runLog": run_log,
        }
        return "\n".join(
            (
                "APEX_AUTOMATION_ACTION_PAYLOAD_BEGIN",
                json.dumps(payload, indent=2),
                "APEX_AUTOMATION_ACTION_PAYLOAD_END",
                "APEX_AUTOMATION_ACTION_VERIFIED",
            )
        )

    def test_action_parser_accepts_apex_omitted_null_run_log(self):
        output = self.action_output("disable")
        lines = output.splitlines()
        begin = lines.index("APEX_AUTOMATION_ACTION_PAYLOAD_BEGIN")
        end = lines.index("APEX_AUTOMATION_ACTION_PAYLOAD_END")
        payload = json.loads("".join(lines[begin + 1 : end]))
        payload.pop("runLog")
        output = "\n".join(
            [
                *lines[: begin + 1],
                *json.dumps(payload, indent=2).splitlines(),
                *lines[end:],
            ]
        )
        parsed = operations._parse_action_output(
            output, "disable", 940269, "TEMPLATE_AUTOMATION_SELECTED"
        )
        self.assertIsNone(parsed["runLog"])

    def execute(self, action, after, *, run_log=None, runner_error=None):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            captures = [self.before, after]
            with (
                patch.object(operations, "capture_lifecycle", side_effect=captures) as capture,
                patch.object(
                    operations,
                    "run_sqlcl",
                    side_effect=runner_error
                    if runner_error is not None
                    else lambda target, driver, directory: SqlclResult(
                        0, self.action_output(action, run_log=run_log), directory
                    ),
                ) as runner,
            ):
                result = operations.execute_automation_action(
                    self.target,
                    "DEMO",
                    940269,
                    "TEMPLATE_AUTOMATION_SELECTED",
                    action,
                    self.before,
                    run_dir,
                    workspace_user="DEMO",
                )
        return result, capture, runner

    def test_list_automations_accepts_only_known_schedule_statuses(self):
        self.assertEqual(
            operations.list_automations(self.before),
            (
                {"staticId": "TEMPLATE_AUTOMATION_SELECTED", "status": "DISABLED"},
                {"staticId": "TEMPLATE_AUTOMATION_UNSELECTED", "status": "DISABLED"},
            ),
        )
        with self.assertRaises(operations.AutomationOperationError):
            operations.list_automations(snapshot(selected_status="RUNNING"))

    def test_selection_is_exact_and_rejects_duplicates_and_absent_ids(self):
        rows = operations.list_automations(self.before)
        self.assertEqual(
            operations.select_automations(rows, ["TEMPLATE_AUTOMATION_UNSELECTED"]),
            ({"staticId": "TEMPLATE_AUTOMATION_UNSELECTED", "status": "DISABLED"},),
        )
        for ids in ([], ["missing"], ["TEMPLATE_AUTOMATION_SELECTED"] * 2):
            with self.subTest(ids=ids), self.assertRaises(operations.AutomationOperationError):
                operations.select_automations(rows, ids)

    def test_enable_requires_exact_confirmation_and_readback(self):
        after = snapshot(selected_status="ACTIVE")
        result, capture, runner = self.execute("enable", after)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["before"]["status"], "DISABLED")
        self.assertEqual(result["after"]["status"], "ACTIVE")
        self.assertEqual(result["after"]["unselected"]["TEMPLATE_AUTOMATION_UNSELECTED"], "DISABLED")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(capture.call_count, 2)

    def test_status_drift_after_confirmation_refuses_before_sqlcl(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(operations, "capture_lifecycle", return_value=snapshot(selected_status="ACTIVE")),
                patch.object(operations, "run_sqlcl") as runner,
            ):
                result = operations.execute_automation_action(
                    self.target,
                    "DEMO",
                    940269,
                    "TEMPLATE_AUTOMATION_SELECTED",
                    "enable",
                    self.before,
                    Path(temporary),
                    workspace_user="DEMO",
                )
        self.assertEqual(result["status"], "refused")
        runner.assert_not_called()

    def test_changed_target_or_application_after_confirmation_refuses(self):
        for fresh in (
            snapshot(app_id=940270),
            {**snapshot(), "identity": {**self.before["identity"], "db_unique_name": "OTHER"}},
        ):
            with tempfile.TemporaryDirectory() as temporary:
                with (
                    patch.object(operations, "capture_lifecycle", return_value=fresh),
                    patch.object(operations, "run_sqlcl") as runner,
                ):
                    result = operations.execute_automation_action(
                        self.target,
                        "DEMO",
                        940269,
                        "TEMPLATE_AUTOMATION_SELECTED",
                        "disable",
                        self.before,
                        Path(temporary),
                        workspace_user="DEMO",
                    )
            self.assertEqual(result["status"], "refused")
            runner.assert_not_called()

    def test_native_lock_refusal_does_not_run_an_inverse_action(self):
        error = SqlclError(
            "automation action refused",
            Path("/private/run"),
            "APEX_AUTOMATION_ACTION_REFUSED:APPLICATION_LOCKED",
        )
        result, _capture, runner = self.execute("enable", self.before, runner_error=error)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "application is locked by another Builder operation")
        self.assertEqual(runner.call_count, 1)

    def test_api_failure_is_unknown_and_never_replayed(self):
        error = SqlclError("action outcome is unknown", Path("/private/run"), "ORA-00001")
        result, _capture, runner = self.execute("disable", self.before, runner_error=error)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(runner.call_count, 1)

    def test_run_once_requires_a_completed_success_log_record(self):
        log = {
            "id": "501",
            "applicationId": 940269,
            "workspaceId": "1000000000000000",
            "workspace": "DEMO",
            "staticId": "TEMPLATE_AUTOMATION_SELECTED",
            "statusCode": "SUCCESS",
            "status": "Successful",
            "startedAt": "2026-10-12T07:00:02.000000Z",
            "endedAt": "2026-10-12T07:00:03.000000Z",
            "successfulRows": 0,
            "errorRows": 0,
        }
        result, _capture, _runner = self.execute("run-once", self.before, run_log=log)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["runLog"]["statusCode"], "SUCCESS")
        self.assertEqual(result["after"]["status"], "DISABLED")

    def test_run_once_without_completion_evidence_is_unknown(self):
        in_progress = {
            "id": "501",
            "applicationId": 940269,
            "workspaceId": "1000000000000000",
            "workspace": "DEMO",
            "staticId": "TEMPLATE_AUTOMATION_SELECTED",
            "statusCode": "IN_PROGRESS",
            "status": "In Progress",
            "startedAt": "2026-10-12T07:00:02.000000Z",
            "endedAt": None,
            "successfulRows": 0,
            "errorRows": 0,
        }
        result, _capture, _runner = self.execute("run-once", self.before, run_log=in_progress)
        self.assertEqual(result["status"], "unknown")

    def test_run_once_with_no_log_frame_is_unknown(self):
        after = snapshot()
        result, _capture, _runner = self.execute("run-once", after)
        self.assertEqual(result["status"], "unknown")

    def test_run_once_execution_log_identity_must_match_selected_scope(self):
        log = {
            "id": "501",
            "statusCode": "SUCCESS",
            "status": "Success",
            "startedAt": "2026-10-12T07:00:02.000000Z",
            "endedAt": "2026-10-12T07:00:03.000000Z",
            "successfulRows": 0,
            "errorRows": 0,
            "applicationId": 940270,
            "workspaceId": "1000000000000000",
            "workspace": "DEMO",
            "staticId": "TEMPLATE_AUTOMATION_SELECTED",
        }
        result, _capture, _runner = self.execute("run-once", self.before, run_log=log)
        self.assertEqual(result["status"], "unknown")

    def test_cli_negative_confirmation_makes_no_write_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with (
                patch.object(
                    operations,
                    "_validate_scope",
                    return_value=(940269, self.before, run_dir, {
                        "environment": "dev", "connection": "docker-demo2", "sessionUser": "DEMO2",
                        "currentSchema": "DEMO2", "service": "freepdb1", "workspace": "DEMO",
                        "appId": 940269, "release": "26.2.0",
                    }),
                ),
                patch.object(operations, "read_project_env", return_value={"APEX_WORKSPACE_USERNAME": "DEMO"}),
                patch.object(operations, "execute_automation_action") as action,
                patch("sys.stdin", io.StringIO("no\n")),
                patch("sys.stdout", new_callable=io.StringIO) as output,
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                code = operations.main([
                    "enable", "940269", "--env", "dev", "--schema", "DEMO2",
                    "--automation", "TEMPLATE_AUTOMATION_SELECTED", "--repo-root", temporary,
                    "--env-file", str(run_dir / ".env"), "--format", "json",
                ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "refused")
        action.assert_not_called()

    def test_cli_displays_exact_action_and_selected_state_before_confirmation(self):
        expected = {"schemaVersion": 1, "action": "enable", "status": "verified", "scope": {},
                    "before": {"staticId": "TEMPLATE_AUTOMATION_SELECTED", "status": "DISABLED"},
                    "after": {"staticId": "TEMPLATE_AUTOMATION_SELECTED", "status": "ACTIVE"}}
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with (
                patch.object(
                    operations,
                    "_validate_scope",
                    return_value=(940269, self.before, run_dir, {
                        "environment": "dev", "connection": "docker-demo2", "sessionUser": "DEMO2",
                        "currentSchema": "DEMO2", "service": "freepdb1", "workspace": "DEMO",
                        "appId": 940269, "release": "26.2.0",
                    }),
                ),
                patch.object(operations, "read_project_env", return_value={"APEX_WORKSPACE_USERNAME": "DEMO"}),
                patch.object(operations, "_target", return_value=self.target),
                patch.object(operations, "execute_automation_action", return_value=expected) as action,
                patch("sys.stdin", io.StringIO("ENABLE 940269 TEMPLATE_AUTOMATION_SELECTED\n")),
                patch("sys.stdout", new_callable=io.StringIO) as output,
                patch("sys.stderr", new_callable=io.StringIO) as prompt,
            ):
                code = operations.main([
                    "enable", "940269", "--env", "dev", "--schema", "DEMO2",
                    "--automation", "TEMPLATE_AUTOMATION_SELECTED", "--repo-root", temporary,
                    "--env-file", str(run_dir / ".env"), "--format", "json",
                ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "verified")
        self.assertIn("environment=dev", prompt.getvalue())
        self.assertIn("schema=DEMO2", prompt.getvalue())
        self.assertIn("current status=DISABLED", prompt.getvalue())
        self.assertIn("ENABLE 940269 TEMPLATE_AUTOMATION_SELECTED", prompt.getvalue())
        action.assert_called_once()

    def test_action_driver_uses_current_session_public_apis_and_no_schedule_restore(self):
        renderer = operations.render_action_driver
        with tempfile.TemporaryDirectory() as temporary:
            driver = renderer(
                self.target,
                "DEMO",
                940269,
                "TEMPLATE_AUTOMATION_SELECTED",
                "run-once",
                "DISABLED",
                "DEMO",
                self.before["identity"],
                "1000000000000000",
                Path(temporary),
            )
            source = driver.read_text(encoding="utf-8").upper()
        adapter = Path(operations.__file__).with_suffix(".sql").read_text(encoding="utf-8").upper()
        source = source + adapter
        self.assertIn("APEX_SESSION.CREATE_SESSION", source)
        self.assertIn("APEX_AUTOMATION.EXECUTE", source)
        self.assertIn("P_RUN_IN_BACKGROUND => FALSE", source)
        self.assertIn("IF C_ACTION = 'ENABLE' THEN", source)
        self.assertIn("ELSIF C_ACTION = 'DISABLE' THEN", source)
        self.assertIn("ELSE\n    APEX_AUTOMATION.EXECUTE", source)
        self.assertNotIn("PUBLISH_APP", source)
        self.assertNotIn("APEX_WORKFLOW", source)
        self.assertNotIn("APEX_HUMAN_TASK", source)


if __name__ == "__main__":
    unittest.main()
