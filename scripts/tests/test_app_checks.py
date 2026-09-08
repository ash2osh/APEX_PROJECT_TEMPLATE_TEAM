from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.app_checks import AppCheckError, verify_candidate_apps


def declaration():
    return {
        "version": 1,
        "alias": "employee",
        "page_ids": [1],
        "checks": [
            {
                "id": "employee-table",
                "page_id": 1,
                "kind": "select",
                "verify_sql": "employee/employee-table.verify.sql",
                "expected_objects": ["DEMO.EMP"],
                "sql": "SELECT 'employee-table' assertion_name, 'PASS' status FROM dual",
            },
            {
                "id": "employee-home",
                "page_id": 1,
                "kind": "flow",
                "flow": "flows/home.json",
                "steps": [
                    {"action": "navigate", "path": "/ords/employee/"},
                    {"action": "click", "selector": "[data-test=employees]", "expected_visible_text": "Employees"},
                ],
            },
        ],
    }


class AppCheckTests(unittest.TestCase):
    def test_candidate_checks_require_disposable_target_and_pass(self):
        seen = []

        def select_runner(alias, check):
            seen.append(("select", alias, check["id"]))
            return {"status": "PASS", "observed_objects": ["DEMO.EMP"]}

        def flow_runner(alias, check):
            seen.append(("flow", alias, check["id"]))
            return {"status": "PASS", "url": "/ords/employee/"}

        report = verify_candidate_apps(
            {"commit": "abc123", "apps": {"employee": {"app_id": 11}}},
            {"status": "disposable", "instance_id": "ci-1", "workspace_id": 22, "app_ids": {"employee": 11}, "select_runner": select_runner, "flow_runner": flow_runner},
            {"employee": declaration()},
        )
        self.assertEqual(report.status, "PASS")
        self.assertEqual(report.source_commit, "abc123")
        self.assertEqual(len(report.results), 2)
        self.assertEqual({item.status for item in report.results}, {"PASS"})
        self.assertEqual(len(seen), 2)

    def test_unknown_runner_fails_and_reports_unknown(self):
        with self.assertRaises(AppCheckError) as caught:
            verify_candidate_apps(
                {"commit": "abc123", "apps": {"employee": {}}},
                {"status": "disposable", "instance_id": "ci-1", "workspace_id": 22, "app_ids": {"employee": 11}},
                {"employee": declaration()},
            )
        self.assertIn("UNKNOWN", str(caught.exception))
        self.assertIsNotNone(caught.exception.report)

    def test_declarations_cannot_skip_select_or_flow(self):
        broken = declaration()
        broken["checks"] = [broken["checks"][0]]
        with self.assertRaises(AppCheckError):
            verify_candidate_apps(
                {"commit": "abc123", "apps": {"employee": {}}},
                {"status": "disposable"},
                {"employee": broken},
            )


if __name__ == "__main__":
    unittest.main()
