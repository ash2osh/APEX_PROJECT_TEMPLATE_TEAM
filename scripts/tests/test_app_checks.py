from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import shutil
import json
import tempfile
import unittest

from teamlib.app_checks import (
    AppCheckBundle,
    AppCheckError,
    build_app_check_bundle,
    verify_candidate_apps,
)
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
                "flow": "employee/flows/home.json",
                "steps": [
                    {"action": "navigate", "path": "/ords/employee/"},
                    {"action": "click", "selector": "[data-test=employees]", "expected_visible_text": "Employees"},
                ],
            },
        ],
    }


def valid_check_members():
    return {
        "employee.json": json.dumps(declaration(), sort_keys=True).encode("utf-8"),
        "employee/employee-table.verify.sql": (
            b"SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status "
            b"FROM (SELECT 'employee_table_exists' assertion_name, "
            b"CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END status "
            b"FROM user_tables WHERE table_name = 'EMPLOYEE');\n"
        ),
        "employee/flows/home.json": b"{}\n",
    }


class AppCheckBundleTests(unittest.TestCase):
    def test_bundle_closes_aliases_and_referenced_members(self):
        members = valid_check_members()
        bundle = build_app_check_bundle(members, ("employee",))

        self.assertEqual(set(bundle.declarations), {"employee"})
        self.assertTrue(
            bundle.member_bytes("employee/employee-table.verify.sql").startswith(b"SELECT")
        )
        self.assertEqual(len(bundle.checks_digest), 64)
        self.assertEqual(len(bundle.artifact_digest), 64)

        members["employee.json"] = b"{}"
        self.assertEqual(bundle.declarations["employee"]["alias"], "employee")

    def test_bundle_rejects_missing_extra_and_unsafe_members(self):
        missing = valid_check_members()
        missing.pop("employee/employee-table.verify.sql")
        with self.assertRaisesRegex(AppCheckError, "referenced check member is missing"):
            build_app_check_bundle(missing, ("employee",))

        extra = {**valid_check_members(), "other.json": b"{}"}
        with self.assertRaisesRegex(AppCheckError, "unknown application"):
            build_app_check_bundle(extra, ("employee",))

        with self.assertRaisesRegex(AppCheckError, "safe relative path"):
            build_app_check_bundle({"../employee.json": b"{}"}, ("employee",))

    def test_bundle_rejects_case_collisions_non_bytes_and_bad_json(self):
        collision = {
            **valid_check_members(),
            "Employee/employee-table.verify.sql": b"SELECT 1 FROM dual;\n",
        }
        with self.assertRaisesRegex(AppCheckError, "case-colliding"):
            build_app_check_bundle(collision, ("employee",))

        non_bytes = valid_check_members()
        non_bytes["employee/flows/home.json"] = "{}"  # type: ignore[assignment]
        with self.assertRaisesRegex(AppCheckError, "bytes"):
            build_app_check_bundle(non_bytes, ("employee",))

        bad_declaration = valid_check_members()
        bad_declaration["employee.json"] = b"\xff"
        with self.assertRaisesRegex(AppCheckError, "unreadable"):
            build_app_check_bundle(bad_declaration, ("employee",))

        bad_flow = valid_check_members()
        bad_flow["employee/flows/home.json"] = b"[]\n"
        with self.assertRaisesRegex(AppCheckError, "JSON object"):
            build_app_check_bundle(bad_flow, ("employee",))

    def test_bundle_rejects_alias_and_check_coverage_mismatches(self):
        mismatched = valid_check_members()
        value = json.loads(mismatched["employee.json"])
        value["alias"] = "other"
        mismatched["employee.json"] = json.dumps(value).encode("utf-8")
        with self.assertRaisesRegex(AppCheckError, "alias does not match"):
            build_app_check_bundle(mismatched, ("employee",))

        for kind in ("select", "flow"):
            with self.subTest(missing_kind=kind):
                incomplete = valid_check_members()
                value = json.loads(incomplete["employee.json"])
                value["checks"] = [
                    check for check in value["checks"] if check["kind"] != kind
                ]
                incomplete["employee.json"] = json.dumps(value).encode("utf-8")
                with self.assertRaisesRegex(AppCheckError, f"no {kind.upper() if kind == 'select' else 'page smoke flow'}"):
                    build_app_check_bundle(incomplete, ("employee",))

        wrong_owner = valid_check_members()
        value = json.loads(wrong_owner["employee.json"])
        value["checks"][0]["verify_sql"] = "other/check.verify.sql"
        wrong_owner["employee.json"] = json.dumps(value).encode("utf-8")
        wrong_owner["other/check.verify.sql"] = wrong_owner.pop(
            "employee/employee-table.verify.sql"
        )
        with self.assertRaisesRegex(AppCheckError, "must belong to employee"):
            build_app_check_bundle(wrong_owner, ("employee",))

    def test_bundle_digests_are_stable_and_cover_referenced_bytes(self):
        members = valid_check_members()
        reversed_members = dict(reversed(tuple(members.items())))
        original = build_app_check_bundle(members, ("employee",))
        reordered = build_app_check_bundle(reversed_members, ("employee",))
        self.assertEqual(original.checks_digest, reordered.checks_digest)
        self.assertEqual(original.artifact_digest, reordered.artifact_digest)

        changed_sql = valid_check_members()
        changed_sql["employee/employee-table.verify.sql"] += b"\n"
        changed_flow = valid_check_members()
        changed_flow["employee/flows/home.json"] = b'{"version":1}\n'
        self.assertNotEqual(
            original.artifact_digest,
            build_app_check_bundle(changed_sql, ("employee",)).artifact_digest,
        )
        self.assertNotEqual(
            original.artifact_digest,
            build_app_check_bundle(changed_flow, ("employee",)).artifact_digest,
        )


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
        members = valid_check_members()
        members["employee/dept.verify.sql"] = members[
            "employee/employee-table.verify.sql"
        ]
        runner = _select_runner(
            profile=object(),
            bundle=build_app_check_bundle(members, ("employee",)),
            work=root / "work",
            run_sqlcl=fake_run_sqlcl,
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
        members = valid_check_members()
        members["employee/dept.verify.sql"] = members[
            "employee/employee-table.verify.sql"
        ]
        runner = _select_runner(
            profile=object(),
            bundle=build_app_check_bundle(members, ("employee",)),
            work=root / "work",
            run_sqlcl=fake_run_sqlcl,
        )
        observed = runner("employee", {"id": "t2", "verify_sql": "employee/dept.verify.sql"})
        self.assertEqual(observed["status"], "FAIL")
        self.assertIn("dept_fk", observed["diagnostic"])

    def test_duplicate_assertion_rows_are_rejected(self):
        def fake_run_sqlcl(target, operation, driver, work, **kwargs):
            class Result:
                stdout = (
                    "TEAM_ASSERT|dept_fk|PASS\n"
                    "TEAM_ASSERT|dept_fk|PASS\n"
                )

            return Result()

        root = Path(tempfile.mkdtemp(prefix="team-select-duplicate-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        checks = root / "ci" / "app-checks" / "employee"
        checks.mkdir(parents=True)
        (checks / "dept.verify.sql").write_text(
            "SELECT 'TEAM_ASSERT|dept_fk|PASS' FROM dual;\n",
            encoding="utf-8",
            newline="\n",
        )
        members = valid_check_members()
        members["employee/dept.verify.sql"] = members[
            "employee/employee-table.verify.sql"
        ]
        runner = _select_runner(
            profile=object(),
            bundle=build_app_check_bundle(members, ("employee",)),
            work=root / "work",
            run_sqlcl=fake_run_sqlcl,
        )

        observed = runner(
            "employee", {"id": "t2", "verify_sql": "employee/dept.verify.sql"}
        )

        self.assertEqual(observed["status"], "FAIL")
        self.assertIn("duplicate assertion", observed["diagnostic"])

    def test_a_missing_verify_member_is_a_fail_not_an_unknown(self):
        root = Path(tempfile.mkdtemp(prefix="team-select-missing-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "ci" / "app-checks").mkdir(parents=True)
        bundle = build_app_check_bundle(valid_check_members(), ("employee",))
        missing_members = dict(bundle.members)
        missing_members.pop("employee/employee-table.verify.sql")
        runner = _select_runner(
            profile=object(),
            bundle=AppCheckBundle(
                bundle.declarations,
                missing_members,
                bundle.checks_digest,
                bundle.artifact_digest,
            ),
            work=root / "work",
            run_sqlcl=lambda *a, **k: None,
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
        bundle = build_app_check_bundle(valid_check_members(), ("employee",))
        runner = _flow_runner(str(adapter), root, bundle)
        self.assertEqual(
            runner(
                "employee",
                {
                    "id": "smoke",
                    "kind": "flow",
                    "flow": "employee/flows/home.json",
                },
            )["status"],
            "PASS",
        )

    def test_flow_runner_materializes_bundle_bytes_not_checkout_bytes(self):
        root = Path(tempfile.mkdtemp(prefix="team-flow-bundle-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        observed = root / "observed.json"
        adapter = root / "flow.py"
        adapter.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "payload = pathlib.Path(sys.argv[sys.argv.index('--check-json') + 1])\n"
            "check = json.loads(payload.read_text())\n"
            f"pathlib.Path({str(observed)!r}).write_bytes(pathlib.Path(check['flow']).read_bytes())\n"
            "print(json.dumps({'status': 'PASS', 'diagnostic': ''}))\n",
            encoding="utf-8",
            newline="\n",
        )
        adapter.chmod(0o755)
        members = valid_check_members()
        members["employee/flows/home.json"] = b'{"source":"bundle"}\n'
        bundle = build_app_check_bundle(members, ("employee",))
        checkout = root / "employee" / "flows"
        checkout.mkdir(parents=True)
        (checkout / "home.json").write_text('{"source":"checkout"}\n', encoding="utf-8")

        result = _flow_runner(str(adapter), root / "work", bundle)(
            "employee",
            {
                "id": "smoke",
                "kind": "flow",
                "flow": "employee/flows/home.json",
            },
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(observed.read_bytes(), b'{"source":"bundle"}\n')


if __name__ == "__main__":
    unittest.main()
