"""Tests for DEV test runner sequential project scenario execution and ambiguous outcomes (Task 5)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from scripts.db_targets import Target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_evidence import (
    create_run,
    freeze_inputs,
)
from scripts.dev_test_manifest import Scenario
from scripts.dev_test_runtime import (
    Row,
    execute_project_scenario,
    execute_step,
    read_rows,
)
from scripts.sqlcl_session import SqlclError, SqlclResult, SqlclTimeout
from scripts.sqlcl_transport import TransportFinding


def _make_dummy_scenario(
    suite_dir: Path,
    scenario_id: str = "sc-1",
    description: str = "Test scenario",
    depends_on: tuple[str, ...] = (),
    objects_req: tuple[MappingProxyType, ...] = (),
    checks_req: tuple[MappingProxyType, ...] = (),
    fixture_objects: tuple[MappingProxyType, ...] = (),
    fixture_selectors: tuple[MappingProxyType, ...] = (),
    step_sql: str = "SELECT 1 FROM dual;\n",
    step_id: str = "step-1",
    step_expected: Mapping[str, str] | None = None,
    check_id: str = "chk-1",
    check_sql: str = "SELECT 1 FROM dual",
    check_expected: int = 1,
    observations: tuple[MappingProxyType, ...] = (),
    cleanup: MappingProxyType | None = None,
) -> Scenario:
    sql_path = suite_dir / f"{scenario_id}_{step_id}.sql"
    sql_bytes = step_sql.encode("utf-8")
    sql_path.write_bytes(sql_bytes)
    sha256 = hashlib.sha256(sql_bytes).hexdigest()

    expected_mapping = step_expected or {"kind": "success"}
    step = MappingProxyType({
        "id": step_id,
        "sqlFile": sql_path.name,
        "path": sql_path,
        "source": sql_bytes,
        "sha256": sha256,
        "expected": MappingProxyType(dict(expected_mapping)),
    })
    check = MappingProxyType({
        "id": check_id,
        "sql": check_sql,
        "expected": check_expected,
    })
    requirements = MappingProxyType({
        "objects": objects_req,
        "checks": checks_req,
    })
    effects = MappingProxyType({
        "writes": True,
        "transaction": "commits",
        "external": "none",
        "affectedObjects": ("@fixtures",),
        "backgroundConsumers": (),
    })
    fixtures = MappingProxyType({
        "objects": fixture_objects,
        "selectors": fixture_selectors,
    })
    return Scenario(
        id=scenario_id,
        description=description,
        dependsOn=depends_on,
        requirements=requirements,
        effects=effects,
        fixtures=fixtures,
        steps=(step,),
        checks=(check,),
        observations=observations,
        cleanup=cleanup,
    )


def _make_check_output(phase: str, results: list[dict], target_user: str = "DEMO", target_schema: str = "DEMO") -> str:
    payload = {
        "schemaVersion": 1,
        "phase": phase,
        "complete": True,
        "identity": {
            "session_user": target_user,
            "current_schema": target_schema,
            "db_name": "FREEPDB1",
            "db_unique_name": "FREEPDB1",
            "service_name": "freepdb1",
            "container_id": "3",
            "container_name": "FREEPDB1",
            "edition": "ORA$BASE",
            "database_version": "23.4",
        },
        "results": [
            {
                "id": r["id"],
                "row_count": 1,
                "column_count": 1,
                "numeric": True,
                "value": r["value"],
            }
            for r in results
        ],
    }
    return (
        f"CHECK_PAYLOAD_BEGIN:{phase}\n"
        f"{json.dumps(payload)}\n"
        f"CHECK_PAYLOAD_END:{phase}\n"
        f"CHECK_VERIFIED:{phase}\n"
    )


class DevTestRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        (self.repo_root / "scratch").mkdir(mode=0o700, exist_ok=True)
        self.suite_dir = self.repo_root / "dev-tests" / "test-suite"
        self.suite_dir.mkdir(parents=True, exist_ok=True)
        self.run = create_run(self.repo_root)
        self.target = Target("dev", "docker-demo", "DEMO", "DEMO", "development")
        self.context = RenderContext(
            owner="DEMO",
            run_id=self.run.run_id,
            objects={"items": "DT_0123456789AB_ITEMS"},
            values={"DEVTEST_RUN_ID": self.run.run_id},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _freeze_scenario(self, scenario: Scenario) -> None:
        paths = [s["path"] for s in scenario.steps]
        if scenario.cleanup is not None:
            paths.extend(item["path"] for item in scenario.cleanup["sqlFiles"])
        freeze_inputs(self.run, paths)

    # -----------------------------------------------------------------------
    # 1. read_rows tests
    # -----------------------------------------------------------------------

    def test_read_rows_success(self) -> None:
        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            self.assertIn("SET SQLFORMAT CSV", content)
            self.assertIn("SET HEADING OFF", content)
            self.assertIn("SET HEADING ON", content)
            self.assertLess(content.index("SET HEADING OFF"), content.index("PROMPT ROQ_"))
            data_marker = content.index("_DATA_BEGIN")
            heading_on = content.index("SET HEADING ON")
            csv_format = content.index("SET SQLFORMAT CSV")
            self.assertLess(data_marker, heading_on)
            self.assertLess(heading_on, csv_format)
            self.assertIn("EXIT SUCCESS ROLLBACK", content)
            self.assertEqual(kwargs["phase"], "inventory")
            self.assertEqual(kwargs["timeout_seconds"], 60)
            self.assertEqual(kwargs["output_limit_bytes"], 1024 * 1024)
            # Find the marker
            import re
            m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content)
            self.assertIsNotNone(m)
            marker = m.group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f'"ITEM_ID","ITEM_NAME"\n'
                f'1,"Widget"\n'
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        result = read_rows(
            self.target,
            "SELECT 1 AS item_id, 'Widget' AS item_name FROM dual",
            self.context,
            self.run,
            max_rows=10,
            runner=fake_runner,
        )
        self.assertEqual(result["columns"], ["ITEM_ID", "ITEM_NAME"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["count"], 1)

        row = result["rows"][0]
        self.assertIsInstance(row, Row)
        self.assertEqual(row["ITEM_ID"], "1")
        self.assertEqual(row["ITEM_NAME"], "Widget")
        self.assertEqual(row[0], "1")
        self.assertEqual(row[1], "Widget")
        self.assertEqual(result["identity"]["session_user"], "DEMO")
        self.assertEqual(result["identity"]["current_schema"], "DEMO")

    def test_read_rows_truncation(self) -> None:
        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            import re
            m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", driver_path.read_text(encoding="utf-8"))
            marker = m.group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f'"ID"\n'
                f"1\n"
                f"2\n"
                f"3\n"
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        result = read_rows(
            self.target,
            "SELECT 1 AS id FROM dual UNION ALL SELECT 2 FROM dual UNION ALL SELECT 3 FROM dual",
            self.context,
            self.run,
            max_rows=2,
            runner=fake_runner,
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["rows"][0]["ID"], "1")
        self.assertEqual(result["rows"][1]["ID"], "2")

    def test_read_rows_missing_frames_fails(self) -> None:
        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            return SqlclResult(0, "Incomplete output without frames", run_dir)

        with self.assertRaises(RuntimeError):
            read_rows(
                self.target,
                "SELECT 1 FROM dual",
                self.context,
                self.run,
                max_rows=10,
                runner=fake_runner,
            )

    def test_read_rows_rejects_invalid_limits_and_duplicate_headings(self) -> None:
        for max_rows in (True, 0, 101, 1.5):
            with self.subTest(max_rows=max_rows):
                with self.assertRaises(ValueError):
                    read_rows(
                        self.target,
                        "SELECT 1 AS value FROM dual",
                        self.context,
                        self.run,
                        max_rows=max_rows,
                        runner=lambda *_args, **_kwargs: self.fail("invalid limit must refuse before SQLcl"),
                    )

        def duplicate_heading_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            import re
            content = driver_path.read_text(encoding="utf-8")
            marker = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content).group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                '"VALUE","VALUE"\n'
                '"1","2"\n'
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        with self.assertRaisesRegex(RuntimeError, "duplicate column headings"):
            read_rows(
                self.target,
                "SELECT 1 AS value, 2 AS value FROM dual",
                self.context,
                self.run,
                max_rows=5,
                runner=duplicate_heading_runner,
            )

    def test_read_rows_refuses_non_dev_target(self) -> None:
        production_target = Target("prod", "prod-db", "DEMO", "DEMO", "production")
        with self.assertRaisesRegex(RuntimeError, "outside a non-production DEV profile"):
            read_rows(
                production_target,
                "SELECT 1 AS value FROM dual",
                self.context,
                self.run,
                max_rows=5,
                runner=lambda *_args, **_kwargs: self.fail("non-DEV target must refuse before SQLcl"),
            )

    def test_read_rows_rejects_nonzero_sqlcl_result(self) -> None:
        def failed_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            import re
            content = driver_path.read_text(encoding="utf-8")
            marker = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content).group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                '"VALUE"\n1\n'
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(1, output, run_dir)

        with self.assertRaisesRegex(RuntimeError, "nonzero exit status"):
            read_rows(
                self.target,
                "SELECT 1 AS value FROM dual",
                self.context,
                self.run,
                max_rows=5,
                runner=failed_runner,
            )

    def test_read_rows_requires_column_headings_even_for_zero_rows(self) -> None:
        def empty_data_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            import re
            content = driver_path.read_text(encoding="utf-8")
            marker = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content).group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        with self.assertRaisesRegex(RuntimeError, "no column headings"):
            read_rows(
                self.target,
                "SELECT 1 AS value FROM dual WHERE 1 = 0",
                self.context,
                self.run,
                max_rows=5,
                runner=empty_data_runner,
            )

    # -----------------------------------------------------------------------
    # 2. execute_step tests
    # -----------------------------------------------------------------------

    def test_execute_step_success(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        step = scenario.steps[0]

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            self.assertIn("SET SQLBLANKLINES ON", content)
            self.assertIn("SET DEFINE OFF", content)
            self.assertIn("PROMPT DEVTEST_STEP_START:step-1", content)
            self.assertIn("PROMPT DEVTEST_STEP_COMPLETED:step-1", content)
            self.assertIn("EXIT SUCCESS ROLLBACK", content)
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "1 row selected.\n"
                "DEVTEST_STEP_COMPLETED:step-1\n"
            )
            return SqlclResult(0, output, run_dir)

        step_record = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(step_record["id"], "step-1")
        self.assertEqual(step_record["status"], "PASSED")
        self.assertTrue(step_record["writeStarted"])

    def test_invalid_sql_preflight_returns_blocked_without_recording_intent(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        with patch(
            "scripts.dev_test_runtime.validate_sql_script",
            side_effect=ValueError("invalid scenario SQL"),
        ):
            step_record = execute_step(
                self.target,
                scenario,
                scenario.steps[0],
                self.context,
                self.run,
                runner=lambda *_args, **_kwargs: self.fail("invalid SQL must not reach SQLcl"),
            )

        self.assertEqual(step_record["status"], "BLOCKED")
        self.assertEqual(step_record["phase"], "preflight")
        self.assertFalse(step_record["writeStarted"])
        self.assertEqual(list(self.run.events_dir.glob("*.json")), [])

    def test_unexpected_runner_error_after_intent_is_unknown(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)

        def broken_runner(*_args: Any, **_kwargs: Any) -> SqlclResult:
            raise OSError("SQLcl transport pipe closed")

        step_record = execute_step(
            self.target,
            scenario,
            scenario.steps[0],
            self.context,
            self.run,
            runner=broken_runner,
        )
        self.assertEqual(step_record["status"], "UNKNOWN")
        self.assertEqual(step_record["phase"], "apply")
        self.assertTrue(step_record["writeStarted"])
        self.assertTrue(step_record["evidence"].endswith("sqlcl-output.log"))

    def test_step_nonzero_sqlcl_result_is_unknown_even_with_completion_frame(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)

        def failed_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            return SqlclResult(
                1,
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "DEVTEST_STEP_COMPLETED:step-1\n",
                run_dir,
            )

        record = execute_step(
            self.target,
            scenario,
            scenario.steps[0],
            self.context,
            self.run,
            runner=failed_runner,
        )
        self.assertEqual(record["status"], "UNKNOWN")

    def test_expected_oracle_error_requires_independent_readback(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir,
            step_expected={"kind": "oracle-error", "code": "ORA-01403"},
        )
        self._freeze_scenario(scenario)
        call_order: list[str] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                call_order.append("step")
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "Error report -\n"
                    "SQL Error: ORA-01403: no data found\n"
                )
                raise SqlclError(
                    f"SQLcl exited with status 1; diagnostics: {run_dir / 'sqlcl-output.log'}",
                    run_dir,
                    output=output,
                )
            call_order.append("readback")
            return SqlclResult(0, _make_check_output("postconditions", [{"id": "chk-1", "value": 1}]), run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(call_order, ["step", "readback"])

    def test_changed_database_identity_between_step_and_readback_is_unknown(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                return SqlclResult(
                    0,
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:OTHERDB:OTHERDB:OTHERDB\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n",
                    run_dir,
                )
            return SqlclResult(
                0,
                _make_check_output("postconditions", [{"id": "chk-1", "value": 1}]),
                run_dir,
            )

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "UNKNOWN")

    def test_execute_step_driver_ends_with_exit_success_rollback(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        step = scenario.steps[0]
        recorded_driver: list[str] = []
        recorded_options: list[dict[str, Any]] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            recorded_driver.append(driver_path.read_text(encoding="utf-8"))
            recorded_options.append(dict(kwargs))
            return SqlclResult(
                0,
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "DEVTEST_STEP_COMPLETED:step-1\n",
                run_dir,
            )

        execute_step(self.target, scenario, step, self.context, self.run, runner=fake_runner)
        self.assertEqual(len(recorded_driver), 1)
        driver_text = recorded_driver[0]
        self.assertEqual(recorded_options[0]["phase"], "apply")
        self.assertEqual(recorded_options[0]["output_limit_bytes"], 1024 * 1024)
        lines = [line.strip() for line in driver_text.strip().splitlines()]
        self.assertEqual(lines[-1], "EXIT SUCCESS ROLLBACK")

    def test_execute_step_timeout_is_unknown_even_with_ora(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir, step_expected={"kind": "oracle-error", "code": "ORA-01403"}
        )
        self._freeze_scenario(scenario)
        step = scenario.steps[0]

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            raise SqlclTimeout(
                run_dir,
                phase="apply",
                timeout_seconds=60,
                elapsed_seconds=60,
                output="DEVTEST_STEP_START:step-1\nORA-01403: no data found",
            )

        step_record = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(step_record["status"], "UNKNOWN")

    def test_exact_error_needs_readback_not_substring(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir,
            step_expected={"kind": "oracle-error", "code": "ORA-01403"},
        )
        self._freeze_scenario(scenario)
        step = scenario.steps[0]

        # Case 1: missing step start frame with ORA-01403 is UNKNOWN
        def fake_runner_missing_start(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            raise SqlclError("Error", run_dir, output="ORA-01403: no data found\n")

        rec1 = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner_missing_start
        )
        self.assertEqual(rec1["status"], "UNKNOWN")

        # Case 2: genuine database error with verified start and matching ORA is PASSED
        def fake_runner_matched_error(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "Error report -\n"
                "SQL Error: ORA-01403: no data found\n"
            )
            raise SqlclError(
                f"SQLcl exited with status 1; diagnostics: {run_dir / 'sqlcl-output.log'}",
                run_dir,
                output=output,
            )

        rec2 = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner_matched_error
        )
        self.assertEqual(rec2["status"], "PASSED")

        # A matching ORA string cannot convert an unrelated SQLcl infrastructure error into a pass.
        def fake_runner_infrastructure_error(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "ORA-01403: no data found\n"
            )
            raise SqlclError(
                f"SQLcl could not preserve SQLcl diagnostics (permission denied); "
                f"diagnostics: {run_dir / 'sqlcl-output.log'}",
                run_dir,
                output=output,
            )

        rec_infrastructure = execute_step(
            self.target,
            scenario,
            step,
            self.context,
            self.run,
            runner=fake_runner_infrastructure_error,
        )
        self.assertEqual(rec_infrastructure["status"], "UNKNOWN")

        # A matching output string before the SQLcl error report is not an Oracle error result.
        def fake_runner_spoofed_output(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "ORA-01403: printed by the payload\n"
                "Error report -\n"
                "SQL Error: ORA-00942: table or view does not exist\n"
            )
            raise SqlclError(
                f"SQLcl exited with status 1; diagnostics: {run_dir / 'sqlcl-output.log'}",
                run_dir,
                output=output,
            )

        rec_spoofed = execute_step(
            self.target,
            scenario,
            step,
            self.context,
            self.run,
            runner=fake_runner_spoofed_output,
        )
        self.assertEqual(rec_spoofed["status"], "FAILED")

        # Case 3: different ORA code than expected is FAILED
        def fake_runner_wrong_error(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "Error report -\n"
                "SQL Error: ORA-00942: table or view does not exist\n"
            )
            raise SqlclError(
                f"SQLcl exited with status 1; diagnostics: {run_dir / 'sqlcl-output.log'}",
                run_dir,
                output=output,
            )

        rec3 = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner_wrong_error
        )
        self.assertEqual(rec3["status"], "FAILED")

        # Case 4: expected error but completed with success is FAILED
        def fake_runner_unexpected_success(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "DEVTEST_STEP_COMPLETED:step-1\n"
            )
            return SqlclResult(0, output, run_dir)

        rec4 = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner_unexpected_success
        )
        self.assertEqual(rec4["status"], "FAILED")

    def test_driver_preflight_failure_does_not_record_write_intent(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        error = TransportFinding(
            code="test-driver-refusal",
            line=1,
            severity="error",
            message="simulated driver transport refusal",
        )

        with patch(
            "scripts.dev_test_runtime.lint_sqlcl_transport",
            side_effect=([], [error]),
        ):
            record = execute_step(
                self.target,
                scenario,
                scenario.steps[0],
                self.context,
                self.run,
                runner=lambda *_args, **_kwargs: self.fail("preflight refusal must not launch SQLcl"),
            )

        self.assertEqual(record["status"], "UNKNOWN")
        self.assertFalse(record["writeStarted"])
        self.assertEqual(list(self.run.events_dir.glob("*.json")), [])

    def test_intent_write_failure_refuses_database_dispatch(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        with patch("scripts.dev_test_runtime.append_event", side_effect=OSError("disk full")):
            record = execute_step(
                self.target,
                scenario,
                scenario.steps[0],
                self.context,
                self.run,
                runner=lambda *_args, **_kwargs: self.fail("no durable intent means no SQLcl dispatch"),
            )
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertFalse(record["writeStarted"])

    def test_project_scenario_refuses_non_dev_target_before_selectors_or_steps(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        production_target = Target("prod", "prod-db", "DEMO", "DEMO", "production")
        result = execute_project_scenario(
            production_target,
            scenario,
            self.context,
            self.run,
            runner=lambda *_args, **_kwargs: self.fail("non-DEV target must refuse before SQLcl"),
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["eligible"])
        self.assertEqual(list(self.run.events_dir.glob("*.json")), [])

    def test_project_scenario_blocks_unknown_effects_before_write(self) -> None:
        base = _make_dummy_scenario(self.suite_dir)
        scenario = replace(
            base,
            effects=MappingProxyType({
                "writes": True,
                "transaction": "unknown",
                "external": "unknown",
                "affectedObjects": ("@fixtures",),
                "backgroundConsumers": (),
            }),
        )
        self._freeze_scenario(scenario)
        result = execute_project_scenario(
            self.target,
            scenario,
            self.context,
            self.run,
            runner=lambda *_args, **_kwargs: self.fail("unresolved effects must block before SQLcl"),
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["eligible"])
        self.assertEqual(list(self.run.events_dir.glob("*.json")), [])

    def test_expected_oracle_error_without_independent_checks_blocks_before_write(self) -> None:
        base = _make_dummy_scenario(
            self.suite_dir,
            step_expected={"kind": "oracle-error", "code": "ORA-01403"},
        )
        scenario = replace(base, checks=())
        self._freeze_scenario(scenario)
        result = execute_project_scenario(
            self.target,
            scenario,
            self.context,
            self.run,
            runner=lambda *_args, **_kwargs: self.fail("negative scenario without readback must block"),
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["eligible"])
        self.assertEqual(list(self.run.events_dir.glob("*.json")), [])

    def test_changed_frozen_inputs_block_scenario_before_any_sqlcl_call(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)
        scenario.steps[0]["path"].write_text("SELECT 2 FROM dual;\n", encoding="utf-8")
        result = execute_project_scenario(
            self.target,
            scenario,
            self.context,
            self.run,
            runner=lambda *_args, **_kwargs: self.fail("changed inputs must refuse before SQLcl"),
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["eligible"])
        self.assertEqual(list(self.run.events_dir.glob("*.json")), [])

    # -----------------------------------------------------------------------
    # 3. Call order and execution flow tests
    # -----------------------------------------------------------------------

    def test_call_order_verification(self) -> None:
        call_order: list[str] = []

        scenario = _make_dummy_scenario(
            self.suite_dir,
            observations=(
                MappingProxyType({
                    "id": "obs-1",
                    "sql": "SELECT 1 AS count FROM dual",
                    "maxRows": 5,
                }),
            ),
        )
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                # Check that intent event already exists
                events = list(self.run.events_dir.glob("*.json"))
                self.assertTrue(len(events) >= 1)
                event = json.loads(events[0].read_text(encoding="utf-8"))
                payload = driver_path.parent / "payload.sql"
                self.assertEqual(event["inputHashes"]["stepSql"], scenario.steps[0]["sha256"])
                self.assertEqual(
                    event["renderedSqlSha256"],
                    hashlib.sha256(payload.read_bytes()).hexdigest(),
                )
                call_order.append("step-execution")
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            elif "migration-checks.sql" in str(driver_path) or "CHECK_PAYLOAD_BEGIN" in content or "phase" in content:
                call_order.append("check-execution")
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
                return SqlclResult(0, output, run_dir)
            else:
                call_order.append("observation-execution")
                import re
                m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content)
                marker = m.group(1)
                output = (
                    f"{marker}_IDENTITY_BEGIN\n"
                    f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                    f"{marker}_IDENTITY_END\n"
                    f"{marker}_DATA_BEGIN\n"
                    f'"COUNT"\n'
                    f"1\n"
                    f"{marker}_DATA_END\n"
                    f"{marker}_SUCCESS\n"
                )
                return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(call_order, ["step-execution", "check-execution", "observation-execution"])

    def test_changed_inputs_after_first_step_block_the_next_write(self) -> None:
        first = _make_dummy_scenario(self.suite_dir, step_id="step-1")
        second = _make_dummy_scenario(self.suite_dir, step_id="step-2")
        scenario = replace(first, steps=(first.steps[0], second.steps[0]))
        self._freeze_scenario(scenario)
        calls: list[str] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                calls.append("step-1")
                second.steps[0]["path"].write_text("SELECT 2 FROM dual;\n", encoding="utf-8")
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            calls.append("step-2")
            self.fail("changed frozen inputs must prevent the second SQLcl invocation")

        result = execute_project_scenario(
            self.target,
            scenario,
            self.context,
            self.run,
            runner=fake_runner,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(calls, ["step-1"])
        events = [json.loads(path.read_text(encoding="utf-8")) for path in self.run.events_dir.glob("*.json")]
        self.assertEqual([event["stepId"] for event in events], ["step-1"])

    def test_crash_between_write_and_observation_is_unknown(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir,
            observations=(
                MappingProxyType({
                    "id": "obs-1",
                    "sql": "SELECT 1 FROM dual",
                    "maxRows": 5,
                }),
            ),
        )
        self._freeze_scenario(scenario)
        dispatched_steps: list[str] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                dispatched_steps.append("step-1")
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            elif "migration-checks.sql" in str(driver_path) or "CHECK_PAYLOAD_BEGIN" in content or "phase" in content:
                # checks succeed
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
                return SqlclResult(0, output, run_dir)
            else:
                # Crash / process loss during observation
                raise SqlclTimeout(
                    run_dir,
                    phase="observation",
                    timeout_seconds=60,
                    elapsed_seconds=60,
                    output="",
                )

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "UNKNOWN")
        # Ensure step-1 was dispatched exactly once (no replay!)
        self.assertEqual(dispatched_steps, ["step-1"])

    # -----------------------------------------------------------------------
    # 4. Selector tests
    # -----------------------------------------------------------------------

    def test_selector_zero_rows_blocks(self) -> None:
        selector = MappingProxyType({
            "id": "sel-1",
            "sql": "SELECT 1 AS id FROM dual WHERE 1=0",
            "columns": (
                MappingProxyType({"name": "ID", "binding": "DEVTEST_ITEM_ID", "type": "integer"}),
            ),
        })
        scenario = _make_dummy_scenario(self.suite_dir, fixture_selectors=(selector,))
        self._freeze_scenario(scenario)
        dispatched: list[str] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START" in content:
                dispatched.append("step")
            import re
            m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content)
            marker = m.group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f'"ID"\n'
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(dispatched, [])

    def test_selector_multiple_rows_blocks(self) -> None:
        selector = MappingProxyType({
            "id": "sel-1",
            "sql": "SELECT 1 AS id FROM dual UNION ALL SELECT 2 FROM dual",
            "columns": (
                MappingProxyType({"name": "ID", "binding": "DEVTEST_ITEM_ID", "type": "integer"}),
            ),
        })
        scenario = _make_dummy_scenario(self.suite_dir, fixture_selectors=(selector,))
        self._freeze_scenario(scenario)
        dispatched: list[str] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START" in content:
                dispatched.append("step")
            import re
            m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content)
            marker = m.group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f'"ID"\n'
                f"1\n"
                f"2\n"
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(dispatched, [])

    def test_selector_missing_column_blocks(self) -> None:
        selector = MappingProxyType({
            "id": "sel-1",
            "sql": "SELECT 1 AS other_col FROM dual",
            "columns": (
                MappingProxyType({"name": "ID", "binding": "DEVTEST_ITEM_ID", "type": "integer"}),
            ),
        })
        scenario = _make_dummy_scenario(self.suite_dir, fixture_selectors=(selector,))
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            import re
            m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", driver_path.read_text(encoding="utf-8"))
            marker = m.group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f'"OTHER_COL"\n'
                f"1\n"
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "BLOCKED")

    def test_selector_null_or_invalid_type_blocks(self) -> None:
        cases = [
            ("null", ""),
            ("float", "3.14"),
            ("non-numeric", "hello"),
        ]
        for label, val_str in cases:
            with self.subTest(label=label):
                selector = MappingProxyType({
                    "id": "sel-1",
                    "sql": "SELECT 1 AS id FROM dual",
                    "columns": (
                        MappingProxyType({"name": "ID", "binding": "DEVTEST_ITEM_ID", "type": "integer"}),
                    ),
                })
                scenario = _make_dummy_scenario(self.suite_dir, fixture_selectors=(selector,))
                self._freeze_scenario(scenario)

                def fake_runner(
                    target: Target, driver_path: Path, run_dir: Path, _val: str = val_str, **kwargs: Any
                ) -> SqlclResult:
                    import re
                    m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", driver_path.read_text(encoding="utf-8"))
                    marker = m.group(1)
                    output = (
                        f"{marker}_IDENTITY_BEGIN\n"
                        f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                        f"{marker}_IDENTITY_END\n"
                        f"{marker}_DATA_BEGIN\n"
                        f'"ID"\n'
                        f"{_val}\n"
                        f"{marker}_DATA_END\n"
                        f"{marker}_SUCCESS\n"
                    )
                    return SqlclResult(0, output, run_dir)

                result = execute_project_scenario(
                    self.target, scenario, self.context, self.run, runner=fake_runner
                )
                self.assertEqual(result["status"], "BLOCKED")

        selector = MappingProxyType({
            "id": "sel-typed",
            "sql": "SELECT 1 AS id FROM dual",
            "columns": (
                MappingProxyType({"name": "ID", "binding": "DEVTEST_ITEM_ID", "type": "integer"}),
            ),
        })
        for value in (True, {"unexpected": "mapping"}, 3.14):
            with self.subTest(value=value):
                scenario = _make_dummy_scenario(self.suite_dir, fixture_selectors=(selector,))
                self._freeze_scenario(scenario)
                with patch(
                    "scripts.dev_test_runtime.read_rows",
                    return_value={
                        "count": 1,
                        "rows": [Row({"ID": value})],
                        "identity": {
                            "session_user": "DEMO",
                            "current_schema": "DEMO",
                            "db_name": "FREEPDB1",
                            "db_unique_name": "FREEPDB1",
                            "service_name": "FREEPDB1",
                        },
                    },
                ):
                    result = execute_project_scenario(
                        self.target,
                        scenario,
                        self.context,
                        self.run,
                        runner=lambda *_args, **_kwargs: self.fail("invalid selector scalar must block"),
                    )
                self.assertEqual(result["status"], "BLOCKED")

    def test_selector_unicode_overflow_blocks(self) -> None:
        too_long = "x" * 4001
        selector = MappingProxyType({
            "id": "sel-1",
            "sql": "SELECT 'test' AS txt FROM dual",
            "columns": (
                MappingProxyType({"name": "TXT", "binding": "DEVTEST_TXT", "type": "string"}),
            ),
        })
        scenario = _make_dummy_scenario(self.suite_dir, fixture_selectors=(selector,))
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            import re
            m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", driver_path.read_text(encoding="utf-8"))
            marker = m.group(1)
            output = (
                f"{marker}_IDENTITY_BEGIN\n"
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                f"{marker}_IDENTITY_END\n"
                f"{marker}_DATA_BEGIN\n"
                f'"TXT"\n'
                f'"{too_long}"\n'
                f"{marker}_DATA_END\n"
                f"{marker}_SUCCESS\n"
            )
            return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "BLOCKED")

    def test_changed_database_identity_in_mandatory_checks_is_unknown(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                return SqlclResult(
                    0,
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n",
                    run_dir,
                )
            output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
            output = output.replace('"db_name": "FREEPDB1"', '"db_name": "OTHERDB"')
            output = output.replace('"db_unique_name": "FREEPDB1"', '"db_unique_name": "OTHERDB"')
            output = output.replace('"service_name": "freepdb1"', '"service_name": "otherdb"')
            return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("Mandatory checks observed a different", result["error"])

    def test_changed_database_identity_in_optional_observation_is_unknown(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir,
            observations=(MappingProxyType({
                "id": "obs-1",
                "sql": "SELECT 1 AS count FROM dual",
                "maxRows": 5,
            }),),
        )
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                return SqlclResult(
                    0,
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n",
                    run_dir,
                )
            return SqlclResult(
                0,
                _make_check_output("postconditions", [{"id": "chk-1", "value": 1}]),
                run_dir,
            )

        with patch(
            "scripts.dev_test_runtime.read_rows",
            return_value={
                "count": 1,
                "rows": [Row({"COUNT": "1"})],
                "truncated": False,
                "identity": {
                    "session_user": "DEMO",
                    "current_schema": "DEMO",
                    "db_name": "OTHERDB",
                    "db_unique_name": "OTHERDB",
                    "service_name": "otherdb",
                },
            },
        ):
            result = execute_project_scenario(
                self.target, scenario, self.context, self.run, runner=fake_runner
            )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("different or unverified", result["error"])

    def test_selector_valid_populates_bindings_for_steps_and_checks(self) -> None:
        selector = MappingProxyType({
            "id": "sel-1",
            "sql": "SELECT 42 AS id FROM dual",
            "columns": (
                MappingProxyType({"name": "ID", "binding": "DEVTEST_ITEM_ID", "type": "integer"}),
            ),
        })
        scenario = _make_dummy_scenario(
            self.suite_dir,
            fixture_selectors=(selector,),
            check_sql="SELECT COUNT(*) FROM dual WHERE :DEVTEST_ITEM_ID = 42",
        )
        self._freeze_scenario(scenario)
        observed_step_bind: list[str] = []

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                self.assertIn("VARIABLE DEVTEST_ITEM_ID NUMBER", content)
                self.assertIn(":DEVTEST_ITEM_ID := 42;", content)
                observed_step_bind.append("bound")
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            elif "migration-checks.sql" in str(driver_path) or "CHECK_PAYLOAD_BEGIN" in content or "phase" in content:
                self.assertIn("DEVTEST_ITEM_ID", content)
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
                return SqlclResult(0, output, run_dir)
            else:
                # Selector query
                import re
                m = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content)
                marker = m.group(1)
                output = (
                    f"{marker}_IDENTITY_BEGIN\n"
                    f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                    f"{marker}_IDENTITY_END\n"
                    f"{marker}_DATA_BEGIN\n"
                    f'"ID"\n'
                    f"42\n"
                    f"{marker}_DATA_END\n"
                    f"{marker}_SUCCESS\n"
                )
                return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(observed_step_bind, ["bound"])

    # -----------------------------------------------------------------------
    # 5. Check and observation failure tests
    # -----------------------------------------------------------------------

    def test_mandatory_check_failure_marks_scenario_failed(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir, check_expected=1)
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            else:
                # Check returns 0 instead of expected 1
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 0}])
                return SqlclResult(0, output, run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["checks"][0]["passed"])

    def test_incomplete_mandatory_checks_are_unknown(self) -> None:
        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            return SqlclResult(0, "incomplete check output", run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "UNKNOWN")

    def test_optional_observation_failure_recorded_without_masking(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir,
            observations=(
                MappingProxyType({
                    "id": "obs-1",
                    "sql": "SELECT 1 AS count FROM dual",
                    "maxRows": 5,
                }),
            ),
        )
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            elif "migration-checks.sql" in str(driver_path) or "CHECK_PAYLOAD_BEGIN" in content or "phase" in content:
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
                return SqlclResult(0, output, run_dir)
            else:
                # Observation query fails with SqlclError
                raise SqlclError("Table does not exist", run_dir, output="ORA-00942\n")

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        # Functional outcome remains PASSED!
        self.assertEqual(result["status"], "PASSED")
        # Observation failure is recorded in observations
        self.assertEqual(len(result["observations"]), 1)
        self.assertIn("error", result["observations"][0])

    def test_cancelled_optional_observation_leaves_scenario_unknown(self) -> None:
        from scripts.sqlcl_session import SqlclCancelled

        scenario = _make_dummy_scenario(
            self.suite_dir,
            observations=(MappingProxyType({
                "id": "obs-1",
                "sql": "SELECT 1 AS count FROM dual",
                "maxRows": 5,
            }),),
        )
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                return SqlclResult(
                    0,
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n",
                    run_dir,
                )
            if "CHECK_PAYLOAD_BEGIN" in content or "phase" in content:
                return SqlclResult(
                    0,
                    _make_check_output("postconditions", [{"id": "chk-1", "value": 1}]),
                    run_dir,
                )
            raise SqlclCancelled("observation cancelled", run_dir, output="partial observation")

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "UNKNOWN")

    def test_step_cancelled_is_unknown(self) -> None:
        from scripts.sqlcl_session import SqlclCancelled

        scenario = _make_dummy_scenario(
            self.suite_dir, step_expected={"kind": "oracle-error", "code": "ORA-01403"}
        )
        self._freeze_scenario(scenario)
        step = scenario.steps[0]

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            raise SqlclCancelled(
                "Execution cancelled by caller",
                run_dir,
                output="DEVTEST_STEP_START:step-1\nORA-01403",
            )

        step_record = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(step_record["status"], "UNKNOWN")

    def test_step_duplicate_frames_is_unknown(self) -> None:
        scenario = _make_dummy_scenario(
            self.suite_dir, step_expected={"kind": "oracle-error", "code": "ORA-01403"}
        )
        self._freeze_scenario(scenario)
        step = scenario.steps[0]

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                "DEVTEST_STEP_START:step-1\n"
                "ORA-01403: no data found\n"
            )
            raise SqlclError("Error", run_dir, output=output)

        step_record = execute_step(
            self.target, scenario, step, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(step_record["status"], "UNKNOWN")

    def test_step_transport_error_is_unknown(self) -> None:
        # A payload with direct quoted trailing hyphen triggers transport preflight error
        bad_sql = "SELECT 'hello -\nworld' FROM dual;\n"
        scenario = _make_dummy_scenario(self.suite_dir, step_sql=bad_sql)
        self._freeze_scenario(scenario)
        step = scenario.steps[0]

        step_record = execute_step(
            self.target, scenario, step, self.context, self.run, runner=lambda *args, **kwargs: None
        )
        self.assertEqual(step_record["status"], "UNKNOWN")
        self.assertFalse(step_record["writeStarted"])

    def test_inventory_only_records_observed_fixtures(self) -> None:
        # Declaring a fixture object without creating/observing it leaves inventory empty
        fixture_obj = MappingProxyType({"binding": "items", "suffix": "ITEMS", "type": "TABLE"})
        scenario = _make_dummy_scenario(self.suite_dir, fixture_objects=(fixture_obj,))
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            elif "migration-checks.sql" in str(driver_path) or "CHECK_PAYLOAD_BEGIN" in content or "phase" in content:
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
                return SqlclResult(0, output, run_dir)
            return SqlclResult(0, "", run_dir)

        result = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        self.assertEqual(result["status"], "PASSED")
        # Ensure inventory.json was not created with fabricated objects
        inventory_file = self.run.run_dir / "inventory.json"
        self.assertFalse(inventory_file.exists())

    def test_project_cleanup_records_exact_selector_keys_and_fixture_inventory(self) -> None:
        import hashlib
        from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot

        fixture = MappingProxyType({"binding": "items", "suffix": "ITEMS", "type": "TABLE"})
        selector = MappingProxyType({
            "id": "order-key",
            "sql": "SELECT 101 AS ORDER_ID FROM DUAL",
            "columns": (
                MappingProxyType({"name": "ORDER_ID", "binding": "DEVTEST_ORDER_ID", "type": "integer"}),
            ),
        })
        cleanup_path = self.suite_dir / "cleanup.sql"
        cleanup_source = b"DELETE FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID;\n"
        cleanup_path.write_bytes(cleanup_source)
        cleanup = MappingProxyType({
            "sqlFiles": (MappingProxyType({
                "path": cleanup_path,
                "source": cleanup_source,
                "sha256": hashlib.sha256(cleanup_source).hexdigest(),
            }),),
            "checks": (MappingProxyType({
                "id": "rows-absent",
                "sql": "SELECT COUNT(*) FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID",
                "expected": 0,
            }),),
        })
        base = _make_dummy_scenario(
            self.suite_dir,
            fixture_objects=(fixture,),
            fixture_selectors=(selector,),
            cleanup=cleanup,
        )
        self._freeze_scenario(base)
        name = self.context.objects["items"]
        key = ObjectKey("DEMO", name, "TABLE")
        identity = {
            "session_user": "DEMO", "current_schema": "DEMO",
            "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
            "service_name": "freepdb1", "container_id": "3",
            "container_name": "FREEPDB1", "edition": "ORA$BASE",
            "database_version": "23.4",
        }
        before = SchemaInventory(
            dict(identity), {},
            {"ownerComplete": True}, "start", "end",
        )
        after = SchemaInventory(
            dict(identity),
            {key: {"object_id": 987, "data_object_id": 654, "status": "VALID"}},
            {"ownerComplete": True}, "start", "end",
        )
        definition = ObjectDefinition(key, {}, "CREATE TABLE X (ID NUMBER)", (), True)
        snapshot = SchemaSnapshot(dict(identity), dict(after.objects), {key: definition}, {"complete": True}, "start", "end")
        checked = type("CheckReport", (), {
            "coverage": {"sessions": 1, "sessionIdentities": [identity]},
            "complete": True, "passed": True,
            "results": [{"check": "chk-1", "passed": True, "value": 1}],
        })()

        def fake_runner(target: Target, driver_path: Path, run_dir: Path, **kwargs: Any) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
            else:
                import re
                marker = re.search(r"PROMPT (ROQ_[0-9a-f]+)_IDENTITY_BEGIN", content).group(1)
                output = (
                    f"{marker}_IDENTITY_BEGIN\n{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|freepdb1\n"
                    f"{marker}_IDENTITY_END\n{marker}_DATA_BEGIN\n\"ORDER_ID\"\n101\n"
                    f"{marker}_DATA_END\n{marker}_SUCCESS\n"
                )
            return SqlclResult(0, output, run_dir)

        with patch("scripts.schema_catalog.capture_inventory", side_effect=[before, after]), \
                patch("scripts.schema_catalog.capture_snapshot", return_value=snapshot), \
                patch("scripts.dev_test_runtime.run_checks", return_value=checked):
            result = execute_project_scenario(self.target, base, self.context, self.run, runner=fake_runner)

        self.assertEqual(result["status"], "PASSED", result)
        inventory = json.loads(self.run._inventory_path.read_text(encoding="utf-8"))
        saved = inventory["scenarios"][0]
        self.assertEqual(saved["cleanup"]["kind"], "project")
        self.assertEqual(saved["rows"][0]["bindings"], {"DEVTEST_ORDER_ID": 101})
        self.assertEqual(saved["objects"][0]["objectId"], 987)

    def test_scenario_record_validates_in_report(self) -> None:
        from scripts.dev_test_report import new_report, validate_report

        scenario = _make_dummy_scenario(self.suite_dir)
        self._freeze_scenario(scenario)

        def fake_runner(
            target: Target, driver_path: Path, run_dir: Path, **kwargs: Any
        ) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            if "DEVTEST_STEP_START:step-1" in content:
                output = (
                    "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                    "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                    "DEVTEST_STEP_START:step-1\n"
                    "DEVTEST_STEP_COMPLETED:step-1\n"
                )
                return SqlclResult(0, output, run_dir)
            else:
                output = _make_check_output("postconditions", [{"id": "chk-1", "value": 1}])
                return SqlclResult(0, output, run_dir)

        sc_record = execute_project_scenario(
            self.target, scenario, self.context, self.run, runner=fake_runner
        )
        report = new_report(self.run, "run", "test-suite", [scenario.id], "project")
        report["scenarios"] = [sc_record]
        validated = validate_report(report)
        self.assertIsInstance(validated, dict)


if __name__ == "__main__":
    unittest.main()
