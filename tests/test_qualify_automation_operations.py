from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools import qualify_automation_operations as qualification
from tools.probe_apex_26_2 import ProbeTarget


class AutomationQualificationTests(unittest.TestCase):
    def setUp(self):
        self.target = ProbeTarget(
            "docker-demo2", "DEMO2", "DEMO2", "DEMO", 940269, "b72393fa20fa", "freepdb1"
        )

    def test_report_directory_must_be_new_and_under_checkout_scratch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scratch").mkdir()
            good = qualification._output_path(root / "scratch" / "run", root)
            self.assertEqual(good, root / "scratch" / "run")
            with self.assertRaises(ValueError):
                qualification._output_path(root / "outside", root)
            (root / "scratch" / "used").mkdir()
            with self.assertRaises(ValueError):
                qualification._output_path(root / "scratch" / "used", root)

    def test_fixture_adds_two_disabled_noop_automations_and_rewrites_app_alias(self):
        source = Path(__file__).parent / "fixtures" / "apex_26_2" / "canonical_26_2"
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "fixture"
            qualification.prepare_fixture_source(
                source,
                destination,
                app_alias="APEX262-PROBE-940269-0123456789AB",
                workspace="DEMO",
                app_id=940269,
                schema="DEMO2",
            )
            app = (destination / "application.apx").read_text(encoding="utf-8")
            self.assertIn("app APEX262-PROBE-940269-0123456789AB (", app)
            autos = sorted((destination / "shared-components" / "automations").glob("*.apx"))
            self.assertEqual(len(autos), 2)
            combined = "\n".join(path.read_text(encoding="utf-8") for path in autos)
            self.assertIn("TEMPLATE-AUTOMATION-SELECTED", combined)
            self.assertIn("TEMPLATE-AUTOMATION-UNSELECTED", combined)
            self.assertEqual(combined.count("scheduleStatus: disabled"), 2)
            self.assertEqual(combined.count("begin null; end;"), 2)
            self.assertIn("FREQ=YEARLY;BYMONTH=12;BYMONTHDAY=31", combined)
            descriptor = json.loads((destination / "deployments" / "automation-qualification.json").read_text())
            self.assertEqual(descriptor["app"]["id"], 940269)
            self.assertEqual(descriptor["app"]["databaseSession"]["parsingSchema"], "DEMO2")
            self.assertEqual(descriptor["workspace"]["name"], "DEMO")

    def test_occupied_app_id_refuses_before_validation_or_import(self):
        probe = Mock()
        probe.version_reader.return_value = "26.3.0.0"
        probe.identify.return_value = {
            "appCount": 1,
            "sessionUser": "DEMO2",
            "currentSchema": "DEMO2",
            "serverHost": "b72393fa20fa",
            "service": "freepdb1",
            "workspace": "DEMO",
            "workspaceId": 1000000000000000,
            "apexVersion": "26.2.0",
        }
        probe.report = {"checks": {}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scratch").mkdir()
            result = qualification.qualify(
                self.target,
                Path("scratch/run"),
                source=Path(__file__).parent / "fixtures" / "apex_26_2" / "canonical_26_2",
                developer="DEMO",
                repo_root=root,
                probe_factory=lambda *_args, **_kwargs: probe,
            )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(probe.execute.call_count, 0)
        probe.cleanup.assert_not_called()

    def test_missing_create_job_refuses_before_import(self):
        probe = Mock()
        probe.version_reader.return_value = "26.3.0.0"
        probe.identify.return_value = {
            "appCount": 0,
            "sessionUser": "DEMO2",
            "currentSchema": "DEMO2",
            "serverHost": "b72393fa20fa",
            "service": "freepdb1",
            "workspace": "DEMO",
            "workspaceId": 1000000000000000,
            "apexVersion": "26.2.0",
        }
        probe.execute.return_value = "APEX_AUTOMATION_CREATE_JOB:0"
        probe.report = {"checks": {}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scratch").mkdir()
            result = qualification.qualify(
                self.target,
                Path("scratch/run"),
                source=Path(__file__).parent / "fixtures" / "apex_26_2" / "canonical_26_2",
                developer="DEMO",
                repo_root=root,
                probe_factory=lambda *_args, **_kwargs: probe,
            )
        self.assertEqual(result["status"], "unavailable")
        probe.execute.assert_called_once()
        self.assertEqual(probe.save_report.call_count, 1)
        probe.cleanup.assert_not_called()

    def test_retained_log_for_unused_app_id_refuses_before_import(self):
        probe = Mock()
        probe.version_reader.return_value = "26.3.0.0"
        probe.identify.return_value = {
            "appCount": 0,
            "sessionUser": "DEMO2",
            "currentSchema": "DEMO2",
            "serverHost": "b72393fa20fa",
            "service": "freepdb1",
            "workspace": "DEMO",
            "workspaceId": 1000000000000000,
            "apexVersion": "26.2.0",
        }
        probe.execute.side_effect = [
            "APEX_AUTOMATION_CREATE_JOB:1",
            "APEX_AUTOMATION_LOG_COUNT:1",
        ]
        probe.report = {"checks": {}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scratch").mkdir()
            result = qualification.qualify(
                self.target,
                Path("scratch/run"),
                source=Path(__file__).parent / "fixtures" / "apex_26_2" / "canonical_26_2",
                developer="DEMO",
                repo_root=root,
                probe_factory=lambda *_args, **_kwargs: probe,
            )
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("retained APEX automation history", result["reason"])
        self.assertEqual(probe.execute.call_count, 2)
        self.assertEqual(probe.execute.call_args_list[1].args[0], "automation-log-preflight")
        probe.cleanup.assert_not_called()

    def test_ambiguous_action_retains_fixture_for_explicit_recovery(self):
        # The native integration runner owns the write boundary. This unit test
        # exercises the outcome policy through injected adapters.
        self.assertEqual(qualification.cleanup_policy("unknown"), "retain")
        self.assertEqual(qualification.cleanup_policy("refused"), "retain")
        self.assertEqual(qualification.cleanup_policy("verified"), "cleanup")

    def test_fixture_inventory_compares_normalized_ids_but_preserves_exact_live_case(self):
        live = {
            "schemaVersion": 1,
            "release": "26.2.0",
            "identity": {
                "session_user": "DEMO2", "current_schema": "DEMO2", "db_unique_name": "FREE",
                "container_id": "3", "container_name": "FREEPDB1", "edition": "ORA$BASE",
                "server_host": "b72393fa20fa", "service_name": "freepdb1",
            },
            "workspace": "DEMO", "workspaceId": "1000000000000000", "appId": 940269,
            "applicationPresent": True, "startedAt": "2026-10-12T07:00:00Z",
            "completedAt": "2026-10-12T07:00:01Z",
            "automations": [
                {"staticId": "template-automation-selected", "status": "DISABLED"},
                {"staticId": "template-automation-unselected", "status": "DISABLED"},
            ],
            "workflows": [], "tasks": [],
            "coverage": {"application": True, "workspace": True, "automations": True, "workflows": True, "tasks": True},
        }
        result = qualification._assert_snapshot(
            live, "DEMO", 940269, selected="DISABLED", unselected="DISABLED"
        )
        self.assertEqual(result["TEMPLATE-AUTOMATION-SELECTED"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
