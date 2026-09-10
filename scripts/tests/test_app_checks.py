from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import shutil
import tempfile
import unittest

from teamlib.app_checks import AppCheckError, verify_candidate_apps
from teamlib.qualification import QualificationError, _flow_runner, _require_flow_adapter, _select_runner


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
    def test_candidate_checks_require_persistent_target_and_pass(self):
        seen = []

        def select_runner(alias, check):
            seen.append(("select", alias, check["id"]))
            return {"status": "PASS", "observed_objects": ["DEMO.EMP"]}

        def flow_runner(alias, check):
            seen.append(("flow", alias, check["id"]))
            return {"status": "PASS", "url": "/ords/employee/"}

        report = verify_candidate_apps(
            {"commit": "abc123", "apps": {"employee": {"app_id": 11}}},
            {"target_kind": "persistent", "role": "integration", "environment": "staging", "instance_id": "ci-1", "workspace_id": 22, "app_ids": {"employee": 11}, "select_runner": select_runner, "flow_runner": flow_runner},
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
                {"target_kind": "persistent", "role": "integration", "environment": "staging", "instance_id": "ci-1", "workspace_id": 22, "app_ids": {"employee": 11}},
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
                {"target_kind": "persistent", "role": "integration", "environment": "staging", "instance_id": "ci-1", "app_ids": {"employee": 11}},
                {"employee": broken},
            )


class SelectRunnerTests(unittest.TestCase):
    def test_all_pass_rows_produce_a_pass(self):
        recorded = {}

        def fake_run_sqlcl(target, operation, driver, work, **kwargs):
            recorded["operation"] = operation
            recorded["driver"] = Path(driver).read_text(encoding="utf-8")

            class Result:
                stdout = "TEAM_ASSERT|employee_table_exists|PASS\nTEAM_ASSERT|dept_fk|PASS\n"

            return Result()

        root = Path(tempfile.mkdtemp(prefix="team-select-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        checks = root / "ci" / "app-checks" / "employee"
        checks.mkdir(parents=True)
        (checks.parent / "employee" / "employee-table.verify.sql").write_text(
            "SELECT 'employee_table_exists' assertion_name, 'PASS' status FROM dual;\n",
            encoding="utf-8",
            newline="\n",
        )
        runner = _select_runner(
            profile=object(), repo=root, work=root / "work", run_sqlcl=fake_run_sqlcl
        )
        observed = runner("employee", {"id": "t1", "verify_sql": "employee/employee-table.verify.sql"})
        self.assertEqual(observed["status"], "PASS")
        self.assertEqual(recorded["operation"], "read")

    def test_a_fail_row_produces_a_fail_with_the_assertion_named(self):
        def fake_run_sqlcl(target, operation, driver, work, **kwargs):
            class Result:
                stdout = "TEAM_ASSERT|dept_fk|FAIL\n"

            return Result()

        root = Path(tempfile.mkdtemp(prefix="team-select-fail-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        checks = root / "ci" / "app-checks" / "employee"
        checks.mkdir(parents=True)
        (checks / "dept.verify.sql").write_text(
            "SELECT 'dept_fk' assertion_name, 'FAIL' status FROM dual;\n",
            encoding="utf-8",
            newline="\n",
        )
        runner = _select_runner(
            profile=object(), repo=root, work=root / "work", run_sqlcl=fake_run_sqlcl
        )
        observed = runner("employee", {"id": "t2", "verify_sql": "employee/dept.verify.sql"})
        self.assertEqual(observed["status"], "FAIL")
        self.assertIn("dept_fk", observed["diagnostic"])

    def test_a_missing_verify_member_is_a_fail_not_an_unknown(self):
        root = Path(tempfile.mkdtemp(prefix="team-select-missing-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "ci" / "app-checks").mkdir(parents=True)
        runner = _select_runner(
            profile=object(), repo=root, work=root / "work", run_sqlcl=lambda *a, **k: None
        )
        observed = runner("employee", {"id": "t3", "verify_sql": "employee/absent.verify.sql"})
        self.assertEqual(observed["status"], "FAIL")
        self.assertIn("absent.verify.sql", observed["diagnostic"])


class FlowRunnerTests(unittest.TestCase):
    def test_absent_flow_adapter_is_named_in_the_refusal(self):
        with self.assertRaises(QualificationError) as raised:
            _require_flow_adapter(
                {"employee": {"checks": [{"id": "smoke", "kind": "flow"}]}}, None
            )
        message = str(raised.exception)
        self.assertIn("TEAM_FLOW_RUNNER", message)
        self.assertIn("employee/smoke", message)

    def test_no_flow_checks_needs_no_adapter(self):
        _require_flow_adapter(
            {"employee": {"checks": [{"id": "t1", "kind": "select"}]}}, None
        )

    def test_flow_runner_parses_the_adapter_result(self):
        root = Path(tempfile.mkdtemp(prefix="team-flow-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        adapter = root / "flow.sh"
        adapter.write_text(
            '#!/usr/bin/env bash\necho \'{"status": "PASS", "diagnostic": ""}\'\n',
            encoding="utf-8",
            newline="\n",
        )
        adapter.chmod(0o755)
        runner = _flow_runner(str(adapter), root)
        self.assertEqual(runner("employee", {"id": "smoke", "kind": "flow"})["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
