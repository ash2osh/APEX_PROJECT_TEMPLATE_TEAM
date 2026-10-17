"""Public DEV test CLI contracts. All target operations are patched or forbidden."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import _no_real_sqlcl  # noqa: F401
from scripts import dev_test
from scripts.dev_test_core import BUILTIN_SCENARIO_IDS, builtin_suite
from scripts.dev_test_evidence import RunEvidence, append_event, create_run
from scripts.dev_test_manifest import Suite
from scripts.dev_test_report import new_report, save_report


IDENTITY = {
    "session_user": "APP",
    "current_schema": "APP",
    "db_name": "DEV",
    "db_unique_name": "DEV",
    "service_name": "DEV_SERVICE",
    "container_id": "1",
    "container_name": "DEV_PDB",
    "edition": "ORA$BASE",
    "database_version": "23.0",
}


def completed_report(run: RunEvidence, action: str, scenario_ids: list[str]) -> dict:
    report = new_report(run, action, "builtin", scenario_ids, "builtin")
    report["finishedAt"] = "2026-10-16T12:00:00Z"
    report["target"]["observed"] = dict(IDENTITY)
    report["coverage"].update({"complete": True, "eligible": True})
    report["scenarios"] = [
        {
            "id": scenario_id,
            "status": "PASSED",
            "eligible": True,
            "steps": [],
            "checks": [],
            "observations": [],
            "effects": {},
        }
        for scenario_id in scenario_ids
    ]
    return report


class DevTestCliTests(unittest.TestCase):
    def test_actions_are_mutually_exclusive_and_formats_are_supported(self) -> None:
        parser = dev_test.build_parser()
        examples = (
            ["--plan", "--env", "dev"],
            ["--run", "--env", "dev", "--format", "json"],
            ["--report", "20261016T120000Z-" + "a" * 32, "--format", "markdown"],
            ["--cleanup", "20261016T120000Z-" + "b" * 32],
        )
        for example in examples:
            with self.subTest(example=example):
                self.assertIsNotNone(parser.parse_args(example))
        with self.assertRaises(SystemExit) as error:
            parser.parse_args(["--plan", "--run", "--env", "dev"])
        self.assertEqual(2, error.exception.code)
        with self.assertRaises(SystemExit) as error:
            parser.parse_args(["--plan", "--env", "dev", "--format", "yaml"])
        self.assertEqual(2, error.exception.code)

    def test_no_action_prints_usage_without_loading_configuration(self) -> None:
        output = io.StringIO()
        with mock.patch.object(dev_test, "read_project_env", side_effect=AssertionError("must stay offline")):
            with contextlib.redirect_stdout(output):
                result = dev_test.main([])
        self.assertEqual(0, result)
        self.assertIn("usage:", output.getvalue())

    def test_no_action_with_arguments_is_an_error_when_using_process_argv(self) -> None:
        with mock.patch.object(dev_test.sys, "argv", ["dev_test.py", "--env", "dev"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    dev_test.main()
        self.assertEqual(2, error.exception.code)

    def test_plan_and_run_require_explicit_dev_before_loading_configuration(self) -> None:
        for argv in (["--plan"], ["--run", "--env", "prod"]):
            with self.subTest(argv=argv):
                with mock.patch.object(dev_test, "read_project_env", side_effect=AssertionError("must reject first")):
                    with self.assertRaises(SystemExit) as error:
                        dev_test.main(list(argv))
                self.assertEqual(2, error.exception.code)

    def test_schema_and_scenario_selection_are_repeatable_and_ordered(self) -> None:
        args = dev_test.build_parser().parse_args([
            "--plan", "--env", "dev", "--schema", "APP", "--suite", "suite.json",
            "--scenario", "migration-success", "--scenario", "datatype-roundtrip",
        ])
        self.assertEqual("APP", args.schema)
        self.assertEqual(["migration-success", "datatype-roundtrip"], args.scenario)
        selected = dev_test.select_scenarios(builtin_suite(), args.scenario)
        self.assertEqual(["datatype-roundtrip", "migration-success"], [item.id for item in selected])

    def test_cleanup_rejects_target_and_schema_overrides(self) -> None:
        run_id = "20261016T120000Z-" + "c" * 32
        for extra in (["--schema", "APP"], ["--env", "dev"], ["--suite", "suite.json"]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit) as error:
                    dev_test.main(["--cleanup", run_id, *extra])
                self.assertEqual(2, error.exception.code)

    def test_report_needs_no_env_or_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = create_run(root)
            report = completed_report(run, "plan", [])
            save_report(run, report)
            output = io.StringIO()
            with mock.patch.object(dev_test, "ROOT", root), \
                    mock.patch.object(dev_test, "read_project_env", side_effect=AssertionError("report must not read .env")), \
                    mock.patch.object(dev_test, "run_sqlcl", side_effect=AssertionError("report must not run SQLcl")), \
                    mock.patch.object(dev_test, "inspect_suite", side_effect=AssertionError("report must not inspect target")), \
                    contextlib.redirect_stdout(output):
                result = dev_test.main(["--report", run.run_id, "--format", "json"])
            self.assertEqual(0, result)
            rendered = json.loads(output.getvalue())
            self.assertEqual(run.run_id, rendered["runId"])

    def test_project_plan_does_not_execute_any_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suite_file = root / "suite.json"
            suite_file.write_text("{}\n", encoding="utf-8")
            run = RunEvidence("20261016T120000Z-" + "d" * 32, root)
            report = completed_report(run, "plan", list(BUILTIN_SCENARIO_IDS))
            output = io.StringIO()
            with mock.patch.object(dev_test, "ROOT", root), \
                    mock.patch.object(dev_test, "create_run", return_value=run), \
                    mock.patch.object(dev_test, "read_project_env", return_value={}), \
                    mock.patch.object(dev_test, "resolve_target", return_value=mock.Mock(schema="APP")), \
                    mock.patch.object(dev_test, "load_suite", return_value=builtin_suite()), \
                    mock.patch.object(dev_test, "inspect_suite", return_value=report) as inspect, \
                    mock.patch.object(dev_test, "execute_suite", side_effect=AssertionError("plan must not execute")), \
                    mock.patch.object(dev_test, "run_builtin", side_effect=AssertionError("plan must not execute")), \
                    mock.patch.object(dev_test, "execute_project_scenario", side_effect=AssertionError("plan must not execute")), \
                    mock.patch.object(dev_test, "save_report"), \
                    contextlib.redirect_stdout(output):
                result = dev_test.main(["--suite", str(suite_file), "--env", "dev", "--plan", "--format", "json"])
            self.assertEqual(0, result)
            inspect.assert_called_once()

    def test_default_run_keeps_fixture_results_and_never_calls_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = RunEvidence("20261016T120000Z-" + "e" * 32, root)
            suite = builtin_suite()
            plan = completed_report(run, "plan", list(BUILTIN_SCENARIO_IDS))
            plan["selection"]["scenarioIds"] = list(BUILTIN_SCENARIO_IDS)
            executed = completed_report(run, "run", list(BUILTIN_SCENARIO_IDS))
            executed["cleanup"]["status"] = "NOT_REQUESTED"
            output = io.StringIO()
            with mock.patch.object(dev_test, "ROOT", root), \
                    mock.patch.object(dev_test, "create_run", return_value=run), \
                    mock.patch.object(dev_test, "read_project_env", return_value={}), \
                    mock.patch.object(dev_test, "resolve_target", return_value=mock.Mock(schema="APP")), \
                    mock.patch.object(dev_test, "builtin_suite", return_value=suite), \
                    mock.patch.object(dev_test, "inspect_suite", return_value=plan), \
                    mock.patch.object(dev_test, "execute_suite", return_value=executed), \
                    mock.patch.object(dev_test, "cleanup_run", side_effect=AssertionError("run must retain fixtures")), \
                    mock.patch.object(dev_test, "save_report"), \
                    contextlib.redirect_stdout(output):
                result = dev_test.main(["--env", "dev", "--run", "--format", "json"])
            self.assertEqual(0, result)
            self.assertEqual("NOT_REQUESTED", executed["cleanup"]["status"])

    def test_planning_refusal_is_saved_without_raw_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = create_run(root)
            output = io.StringIO()
            secretish = "SQLcl failed: credential=do-not-save"
            with mock.patch.object(dev_test, "ROOT", root), \
                    mock.patch.object(dev_test, "read_project_env", return_value={}), \
                    mock.patch.object(dev_test, "resolve_target", return_value=mock.Mock(schema="APP")), \
                    mock.patch.object(dev_test, "create_run", return_value=run), \
                    mock.patch.object(dev_test, "inspect_suite", side_effect=dev_test.DevTestTargetError(secretish)), \
                    contextlib.redirect_stdout(output):
                result = dev_test.main(["--env", "dev", "--plan", "--format", "json"])
            self.assertEqual(2, result)
            saved = json.loads((run.run_dir / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["diagnostics"]["refused"])
            self.assertEqual("DevTestTargetError", saved["diagnostics"]["errorType"])
            self.assertNotIn(secretish, json.dumps(saved))
            self.assertNotIn(secretish, output.getvalue())

    def test_interrupt_during_planning_persists_prewrite_report_and_returns_130(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = create_run(root)
            output = io.StringIO()
            with mock.patch.object(dev_test, "ROOT", root), \
                    mock.patch.object(dev_test, "read_project_env", return_value={}), \
                    mock.patch.object(dev_test, "resolve_target", return_value=mock.Mock(schema="APP")), \
                    mock.patch.object(dev_test, "create_run", return_value=run), \
                    mock.patch.object(dev_test, "inspect_suite", side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(output):
                result = dev_test.main(["--env", "dev", "--plan", "--format", "json"])
            self.assertEqual(130, result)
            saved = json.loads((run.run_dir / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["diagnostics"]["interrupted"])
            self.assertFalse(saved["diagnostics"]["writeStarted"])
            self.assertEqual(130, dev_test.aggregate_exit(saved))

    def _execution_fixture(self, root: Path, scenarios) -> tuple[Suite, RunEvidence]:
        suite = Suite(
            id="project-suite",
            root=root,
            scenarios=tuple(scenarios),
            manifest_path=root / "suite.json",
        )
        run = create_run(root)
        report = new_report(run, "plan", suite.id, [item.id for item in scenarios], "project")
        report["target"]["observed"] = dict(IDENTITY)
        report["coverage"].update({"complete": True, "eligible": True})
        report["scenarios"] = [
            {
                "id": item.id,
                "status": "PASSED",
                "eligible": True,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": {},
                "plannedObjects": {},
            }
            for item in scenarios
        ]
        save_report(run, report)
        from scripts.dev_test_evidence import _atomic_write_json
        _atomic_write_json(run.run_dir / "plan.json", report)
        return suite, run

    def test_identity_refusal_stops_all_scenario_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenarios = builtin_suite().scenarios[:2]
            suite, run = self._execution_fixture(root, scenarios)
            with mock.patch.object(dev_test, "verify_saved_target_identity", side_effect=RuntimeError("identity changed")), \
                    mock.patch.object(dev_test, "execute_project_scenario", side_effect=AssertionError("must not dispatch")) as execute, \
                    mock.patch.object(dev_test, "run_builtin", side_effect=AssertionError("must not dispatch")) as builtin:
                report = dev_test.execute_suite(suite, mock.Mock(schema="APP"), mock.Mock(values={}), run)
            self.assertEqual([], execute.call_args_list)
            self.assertEqual([], builtin.call_args_list)
            self.assertIn("targetIdentity", report["diagnostics"])
            self.assertTrue(all(item["status"] == "NOT_RUN" for item in report["scenarios"]))
            self.assertEqual(2, dev_test.aggregate_exit(report))

    def test_failed_dependency_blocks_only_dependents_and_independent_work_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = builtin_suite().scenarios
            first = replace(base[0], id="prerequisite", dependsOn=())
            dependent = replace(base[1], id="dependent", dependsOn=("prerequisite",))
            independent = replace(base[2], id="independent", dependsOn=())
            scenarios = (first, dependent, independent)
            suite, run = self._execution_fixture(root, scenarios)
            dispatched = []

            def execute(_target, scenario, _context, _run, *, runner):
                dispatched.append(scenario.id)
                return {
                    "status": "FAILED" if scenario.id == "prerequisite" else "PASSED",
                    "eligible": True,
                    "steps": [],
                    "checks": [],
                    "observations": [],
                    "effects": {},
                }

            with mock.patch.object(dev_test, "verify_saved_target_identity", return_value=dict(IDENTITY)), \
                    mock.patch.object(dev_test, "execute_project_scenario", side_effect=execute):
                report = dev_test.execute_suite(suite, mock.Mock(schema="APP"), mock.Mock(values={}), run)
            self.assertEqual(["prerequisite", "independent"], dispatched)
            self.assertEqual(
                ["FAILED", "NOT_RUN", "PASSED"],
                [item["status"] for item in report["scenarios"]],
            )
            self.assertEqual(2, dev_test.aggregate_exit(report))

    def test_interrupt_status_depends_on_durable_write_intent(self) -> None:
        for write_started, expected_status, expected_exit in (
            (False, "NOT_RUN", 130),
            (True, "UNKNOWN", 2),
        ):
            with self.subTest(write_started=write_started), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scenario = builtin_suite().scenarios[0]
                suite, run = self._execution_fixture(root, (scenario,))

                def interrupt(*_args, _write_started=write_started, _run=run, **_kwargs):
                    if _write_started:
                        append_event(_run, {"type": "write-intent", "writeStarted": True})
                    raise KeyboardInterrupt

                with mock.patch.object(dev_test, "verify_saved_target_identity", return_value=dict(IDENTITY)), \
                        mock.patch.object(dev_test, "execute_project_scenario", side_effect=interrupt):
                    report = dev_test.execute_suite(suite, mock.Mock(schema="APP"), mock.Mock(values={}), run)
                self.assertEqual(expected_status, report["scenarios"][0]["status"])
                self.assertEqual(write_started, report["diagnostics"]["writeStarted"])
                self.assertEqual(expected_exit, dev_test.aggregate_exit(report))

    def test_later_identity_interrupt_does_not_mark_unstarted_scenario_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenarios = builtin_suite().scenarios[:2]
            suite, run = self._execution_fixture(root, scenarios)

            def execute(_target, scenario, _context, _run, *, runner):
                append_event(run, {
                    "type": "step-intent",
                    "scenarioId": scenario.id,
                    "writeStarted": True,
                })
                return {
                    "status": "PASSED",
                    "eligible": True,
                    "steps": [],
                    "checks": [],
                    "observations": [],
                    "effects": {},
                }

            with mock.patch.object(
                dev_test,
                "verify_saved_target_identity",
                side_effect=[dict(IDENTITY), KeyboardInterrupt],
            ), mock.patch.object(dev_test, "execute_project_scenario", side_effect=execute):
                report = dev_test.execute_suite(suite, mock.Mock(schema="APP"), mock.Mock(values={}), run)

            self.assertEqual(["PASSED", "NOT_RUN"], [item["status"] for item in report["scenarios"]])
            self.assertTrue(report["diagnostics"]["writeStarted"])
            self.assertEqual(2, dev_test.aggregate_exit(report))


if __name__ == "__main__":
    unittest.main()
