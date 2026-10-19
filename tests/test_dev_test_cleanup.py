"""Safety contracts for optional DEV fixture cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401

from scripts.db_targets import Target
from scripts.dev_test_evidence import RunEvidence, append_event, create_run, freeze_inputs, save_inventory
from scripts.dev_test_report import new_report, save_report
from scripts.sqlcl_session import SqlclResult


IDENTITY = {
    "session_user": "DEMO",
    "current_schema": "DEMO",
    "db_name": "FREEPDB1",
    "db_unique_name": "FREEPDB1",
    "service_name": "FREEPDB1",
    "container_id": "3",
    "container_name": "FREEPDB1",
    "edition": "ORA$BASE",
    "database_version": "23.4",
}


def _write_project_cleanup_evidence(run: RunEvidence) -> tuple[dict, dict]:
    from scripts.dev_test_report import new_report

    suite_dir = run.root / "dev-tests" / "cleanup-fixture"
    suite_dir.mkdir(parents=True, exist_ok=True)
    cleanup_path = suite_dir / "cleanup.sql"
    cleanup_source = b"DELETE FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID;\n"
    cleanup_path.write_bytes(cleanup_source)
    cleanup_hash = __import__("hashlib").sha256(cleanup_source).hexdigest()
    selector = {
        "id": "order-key",
        "sql": "SELECT 101 AS ORDER_ID FROM DUAL",
        "inputBindings": {"DEVTEST_RUN_ID": run.run_id},
        "bindings": {"DEVTEST_ORDER_ID": 101},
        "columns": [{"name": "ORDER_ID", "binding": "DEVTEST_ORDER_ID", "type": "integer"}],
    }
    row = {"scenarioId": "project-case", "selectorId": "order-key",
           "inputBindings": dict(selector["inputBindings"]), "bindings": dict(selector["bindings"]),
           "columns": list(selector["columns"])}
    contract = {
        "kind": "project",
        "sqlFiles": [{"path": str(cleanup_path), "sha256": cleanup_hash}],
        "checks": [{
            "id": "rows-absent",
            "sql": "SELECT COUNT(*) FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID",
            "expected": 0,
        }],
        "selectors": [selector],
        "bindings": {"DEVTEST_RUN_ID": run.run_id, "DEVTEST_ORDER_ID": 101},
        "objects": {},
        "objectBindings": {},
    }
    suite_manifest = suite_dir / "suite.json"
    suite_manifest.write_text(json.dumps({
        "schemaVersion": 1,
        "id": "cleanup-fixture",
        "scenarios": [{
            "id": "project-case",
            "fixtures": {
                "objects": [],
                "selectors": [{
                    "id": "order-key",
                    "sql": "SELECT 101 AS ORDER_ID FROM DUAL",
                    "columns": [{"name": "ORDER_ID", "binding": "DEVTEST_ORDER_ID", "type": "integer"}],
                }],
            },
            "cleanup": {
                "sqlFiles": ["cleanup.sql"],
                "checks": [{
                    "id": "rows-absent",
                    "sql": "SELECT COUNT(*) FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID",
                    "expected": 0,
                }],
            },
        }],
    }), encoding="utf-8")
    hashes = freeze_inputs(run, [suite_manifest, cleanup_path])

    report = new_report(run, "run", "cleanup-fixture", ["project-case"], "project")
    report["finishedAt"] = "2026-10-16T12:00:00Z"
    report["source"]["inputHashes"] = hashes
    report["target"]["configured"] = {
        "environment": "dev", "connection": "dev-owner", "expectedUser": "DEMO",
        "schema": "DEMO", "classification": "development",
    }
    report["target"]["observed"] = dict(IDENTITY)
    report["scenarios"] = [{
        "id": "project-case", "status": "PASSED", "eligible": True,
        "steps": [{"id": "seed", "status": "PASSED", "writeStarted": True}],
        "checks": [], "observations": [], "effects": {"writes": True},
    }]
    report["coverage"] = {"complete": True, "eligible": True}
    save_report(run, report)
    save_inventory(run, {
        "schemaVersion": 1,
        "targetIdentity": dict(IDENTITY),
        "objects": [],
        "rows": [dict(row)],
        "scenarios": [{
            "id": "project-case", "objects": [], "rows": [dict(row)],
            "details": {}, "cleanup": contract,
        }],
    })
    append_event(run, {
        "type": "fixture-precreation", "scenarioId": "project-case",
        "targetIdentity": dict(IDENTITY), "ownerComplete": True, "projectCleanup": True,
        "plannedObjects": [], "objects": [], "rows": [dict(row)],
    })
    append_event(run, {
        "type": "step-intent", "scenarioId": "project-case",
        "stepId": "seed", "writeStarted": True,
    })
    append_event(run, {
        "type": "fixture-observed", "scenarioId": "project-case",
        "targetIdentity": dict(IDENTITY), "inventoryComplete": True,
        "objects": [], "rows": [dict(row)], "plannedObjects": [],
    })
    return report, contract


def _artifact(
    run: RunEvidence,
    *,
    name: str | None = None,
    object_id: int | None = 101,
    scenario_id: str = "sqlcl-delivery",
) -> dict:
    if name is None:
        prefix = f"DT_{hashlib.sha256((run.run_id + scenario_id).encode('utf-8')).hexdigest()[:12].upper()}_"
        name = f"{prefix}SD_TAB"
    return {
        "owner": "DEMO",
        "name": name,
        "type": "TABLE",
        "objectId": object_id,
        "dataObjectId": 501,
        "status": "VALID",
        "definitionSha256": "a" * 64,
        "dependents": [],
    }


def _write_evidence(
    run: RunEvidence,
    *,
    origin: str = "builtin",
    scenario_id: str = "sqlcl-delivery",
    artifact: dict | None = None,
    cleanup_contract: object = None,
    add_precreation: bool = True,
    add_observed: bool = True,
) -> dict:
    artifact = artifact or _artifact(run, scenario_id=scenario_id)
    report = new_report(run, "run", "suite", [scenario_id], origin)
    report["finishedAt"] = "2026-10-16T12:00:00Z"
    report["target"]["configured"] = {
        "environment": "dev",
        "connection": "dev-owner",
        "expectedUser": "DEMO",
        "schema": "DEMO",
        "classification": "development",
    }
    report["target"]["observed"] = dict(IDENTITY)
    report["scenarios"] = [{
        "id": scenario_id,
        "status": "PASSED",
        "eligible": True,
        "steps": [{"id": "create", "status": "PASSED", "writeStarted": True}],
        "checks": [],
        "observations": [],
        "effects": {"writes": True},
    }]
    report["coverage"] = {"complete": True, "eligible": True}
    report["inventory"]["objects"] = [dict(artifact)]
    save_report(run, report)

    inventory = {
        "schemaVersion": 1,
        "scenarioId": scenario_id,
        "targetIdentity": dict(IDENTITY),
        "objects": [dict(artifact)],
        "rows": [],
        "scenarios": [{
            "id": scenario_id,
            "objects": [dict(artifact)],
            "rows": [],
            "cleanup": cleanup_contract if origin == "project" else {"kind": "builtin"},
        }],
    }
    save_inventory(run, inventory)

    if add_precreation:
        append_event(run, {
            "type": "fixture-precreation",
            "scenarioId": scenario_id,
            "targetIdentity": dict(IDENTITY),
            "ownerComplete": True,
            "objects": [{
                "owner": artifact["owner"], "name": artifact["name"],
                "type": artifact["type"], "absent": True,
            }],
        })
        append_event(run, {
            "type": "step-intent", "scenarioId": scenario_id,
            "stepId": "create", "writeStarted": True,
        })
    if add_observed:
        append_event(run, {
            "type": "fixture-observed",
            "scenarioId": scenario_id,
            "targetIdentity": dict(IDENTITY),
            "inventoryComplete": True,
            "objects": [dict(artifact)],
        })
    return report


class TestCleanupPlanning(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run = create_run(Path(self.temp.name))
        self.artifact = _artifact(self.run)
        self.report = _write_evidence(self.run, artifact=self.artifact)
        self.target = Target("dev", "dev-owner", "DEMO", "DEMO", "development")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _live(self, artifact: dict | None = None, *, identity: dict | None = None, complete: bool = True) -> dict:
        return {
            "identity": dict(identity or IDENTITY),
            "complete": complete,
            "objects": [] if artifact is None else [dict(artifact)],
        }

    def test_repointed_alias_never_drops(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run

        changed = {**IDENTITY, "db_unique_name": "OTHERDB"}
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(self.artifact, identity=changed)), \
             patch("scripts.dev_test_cleanup._execute_drop") as drop:
            result = cleanup_run(self.run, {})
        self.assertEqual(result["status"], "REFUSED")
        drop.assert_not_called()

    def test_builtin_cleanup_accepts_scenario_scoped_fixture_prefix(self) -> None:
        from scripts.dev_test_cleanup import plan_cleanup

        run = create_run(Path(self.temp.name))
        scenario_id = "sqlcl-delivery"
        prefix = f"DT_{hashlib.sha256((run.run_id + scenario_id).encode('utf-8')).hexdigest()[:12].upper()}_"
        artifact = _artifact(run, name=f"{prefix}SD_TAB")
        _write_evidence(run, scenario_id=scenario_id, artifact=artifact)

        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(artifact)):
            result = plan_cleanup(run, {})

        self.assertEqual("READY", result["status"])
        self.assertEqual([artifact["name"]], [item["name"] for item in result["artifacts"] if item["status"] == "READY"])

    def test_changed_object_identity_or_new_dependent_is_retained(self) -> None:
        from scripts.dev_test_cleanup import plan_cleanup

        changed = {**self.artifact, "objectId": 999}
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(changed)):
            result = plan_cleanup(self.run, {})
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn(self.artifact["name"], {item.get("name") for item in result["refused"]})
        extended = {**self.artifact, "dependents": [{"owner": "DEMO", "name": "NEW_CHILD", "type": "TRIGGER", "objectId": 707}]}
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(extended)):
            result = plan_cleanup(self.run, {})
        self.assertEqual(result["status"], "REFUSED")

    def test_successfully_removed_dependents_are_ignored_for_parent_recheck(self) -> None:
        from scripts.dev_test_cleanup import _compare_artifact

        dependent = {"owner": "DEMO", "name": "CHILD_VIEW", "type": "VIEW", "objectId": 707}
        saved = {**self.artifact, "dependents": [dependent]}
        current = {**self.artifact, "dependents": []}
        key = (dependent["owner"], dependent["name"], dependent["type"])
        self.assertIsNone(_compare_artifact(saved, current, removed_dependents={key}))
        self.assertIn("dependents changed", _compare_artifact(saved, current))

    def test_missing_or_corrupt_inventory_refuses_before_database_read(self) -> None:
        from scripts.dev_test_cleanup import plan_cleanup

        self.run._inventory_path.unlink()
        with patch("scripts.dev_test_cleanup._capture_live_inventory") as capture:
            result = plan_cleanup(self.run, {})
        self.assertEqual(result["status"], "REFUSED")
        capture.assert_not_called()

        save_inventory(self.run, {"broken": True})
        self.run._inventory_path.write_text("{broken", encoding="utf-8")
        with patch("scripts.dev_test_cleanup._capture_live_inventory") as capture:
            result = plan_cleanup(self.run, {})
        self.assertEqual(result["status"], "REFUSED")
        capture.assert_not_called()

    def test_ambiguous_precreation_or_prefix_only_ownership_refuses(self) -> None:
        from scripts.dev_test_cleanup import plan_cleanup

        run_incomplete = create_run(Path(self.temp.name))
        incomplete_artifact = _artifact(run_incomplete)
        _write_evidence(run_incomplete, artifact=incomplete_artifact, add_observed=False)
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(incomplete_artifact)):
            result = plan_cleanup(run_incomplete, {})
        self.assertEqual(result["status"], "REFUSED")

        # Use a second valid run record with no exact absence proof.
        run2 = create_run(Path(self.temp.name))
        artifact2 = _artifact(run2)
        _write_evidence(run2, artifact=artifact2, add_precreation=False)
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(artifact2)):
            result = plan_cleanup(run2, {})
        self.assertEqual(result["status"], "REFUSED")

        artifact3 = {**self.artifact, "objectId": None}
        _write_evidence(self.run, artifact=artifact3)
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(artifact3)):
            result = plan_cleanup(self.run, {})
        self.assertEqual(result["status"], "REFUSED")

    def test_truncated_key_capture_refuses(self) -> None:
        from scripts.dev_test_cleanup import plan_cleanup

        inventory = json.loads(self.run._inventory_path.read_text(encoding="utf-8"))
        inventory["rows"] = [{"owner": "DEMO", "table": self.artifact["name"], "truncated": True}]
        save_inventory(self.run, inventory)
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(self.artifact)):
            result = plan_cleanup(self.run, {})
        self.assertEqual(result["status"], "REFUSED")

    def test_null_project_cleanup_is_unsupported_not_synthesized_delete(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run

        _write_evidence(self.run, origin="project", cleanup_contract=None)
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(self.artifact)), \
             patch("scripts.dev_test_cleanup._execute_drop") as drop:
            result = cleanup_run(self.run, {})
        self.assertEqual(result["status"], "UNSUPPORTED")
        drop.assert_not_called()

    def test_verified_absence_is_complete_and_repeat_is_idempotent(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run

        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(None)), \
             patch("scripts.dev_test_cleanup._execute_drop") as drop:
            result = cleanup_run(self.run, {})
        self.assertEqual(result["status"], "COMPLETE")
        drop.assert_not_called()

        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(None)), \
             patch("scripts.dev_test_cleanup._execute_drop") as drop:
            second = cleanup_run(self.run, {})
        self.assertEqual(second["status"], "COMPLETE")
        drop.assert_not_called()

    def test_successful_drop_can_be_repeated_without_a_second_drop(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run

        live = self._live(self.artifact)
        drop_names: list[str] = []

        def capture(*_args, **_kwargs):
            return live

        def drop(_target, artifact, *_args, **_kwargs):
            drop_names.append(artifact["name"])
            live["objects"] = []
            return {"status": "PASSED"}

        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", side_effect=capture), \
             patch("scripts.dev_test_cleanup._execute_drop", side_effect=drop):
            first = cleanup_run(self.run, {})
        self.assertEqual(first["status"], "COMPLETE")

        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", side_effect=capture), \
             patch("scripts.dev_test_cleanup._execute_drop", side_effect=drop):
            second = cleanup_run(self.run, {})
        self.assertEqual(second["status"], "COMPLETE")
        self.assertEqual(drop_names, [self.artifact["name"]])

    def test_drop_statement_is_exact_and_never_cascades(self) -> None:
        from scripts.dev_test_cleanup import _drop_sql

        sql = _drop_sql(self.target, self.artifact)
        self.assertIn('DROP TABLE "' + self.artifact["name"] + '"', sql)
        self.assertIn("l_object_id <> 101", sql)
        self.assertNotIn("CASCADE", sql.upper())

    def test_only_inventory_objects_are_dropped_other_runs_are_untouched(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run

        other = {**self.artifact, "name": f"{self.run.object_prefix}OTHER_RUN"}
        calls: list[dict] = []
        live = {"identity": dict(IDENTITY), "complete": True, "objects": [self.artifact, other]}

        def capture(*_args, **_kwargs):
            return live

        def drop(_target, artifact, *_args, **_kwargs):
            calls.append(dict(artifact))
            live["objects"] = [other]
            return {"status": "PASSED"}

        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", side_effect=capture), \
             patch("scripts.dev_test_cleanup._execute_drop", side_effect=drop):
            result = cleanup_run(self.run, {})
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual([item["name"] for item in calls], [self.artifact["name"]])

    def test_interrupted_cleanup_is_incomplete_and_preserves_functional_results(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run
        before = json.loads((self.run.run_dir / "report.json").read_text(encoding="utf-8"))
        with patch("scripts.dev_test_cleanup.resolve_target", return_value=self.target), \
             patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=self._live(self.artifact)), \
             patch("scripts.dev_test_cleanup._execute_drop", side_effect=KeyboardInterrupt()):
            result = cleanup_run(self.run, {})
        after = json.loads((self.run.run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(after["scenarios"], before["scenarios"])
        self.assertEqual(after["cleanup"]["status"], "INCOMPLETE")


class TestRunWideInventory(unittest.TestCase):
    def test_fixture_inventory_accumulates_all_scenarios(self) -> None:
        from scripts.dev_test_core_types import _save_fixture_inventory

        with tempfile.TemporaryDirectory() as temp:
            run = create_run(Path(temp))
            first = _artifact(run, name=f"{run.object_prefix}FIRST")
            second = _artifact(run, name=f"{run.object_prefix}SECOND", object_id=102)
            _save_fixture_inventory(run, "first-case", IDENTITY, [first])
            _save_fixture_inventory(run, "second-case", IDENTITY, [second])
            saved = json.loads(run._inventory_path.read_text(encoding="utf-8"))
        self.assertEqual({item["name"] for item in saved["objects"]}, {first["name"], second["name"]})
        self.assertEqual({item["id"] for item in saved["scenarios"]}, {"first-case", "second-case"})


class TestProjectCleanupContract(unittest.TestCase):
    def test_project_delete_requires_exact_recorded_selector_bind(self) -> None:
        from scripts.dev_test_cleanup import _validate_project_cleanup_sql

        result = _validate_project_cleanup_sql(
            "DELETE FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID",
            "DEMO",
            {"DEVTEST_RUN_ID", "DEVTEST_ORDER_ID"},
            {},
        )
        self.assertEqual(result, {
            "kind": "delete",
            "owner": "DEMO",
            "name": "ORDER_LINES",
            "bindings": ["DEVTEST_ORDER_ID"],
        })

    def test_project_cleanup_rejects_unscoped_or_broader_sql(self) -> None:
        from scripts.dev_test_cleanup import _validate_project_cleanup_sql

        unsafe = (
            "DELETE FROM ORDER_LINES",
            "DELETE FROM ORDER_LINES WHERE ORDER_ID = 42",
            "DELETE FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_OTHER_ID",
            "DELETE FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID OR 1 = 1",
            "DELETE FROM OTHER.ORDERS WHERE ORDER_ID = :DEVTEST_ORDER_ID",
            "DELETE FROM ORDER_LINES WHERE ORDER_ID = :DEVTEST_ORDER_ID; DELETE FROM USERS",
            "UPDATE ORDER_LINES SET FLAG = 1 WHERE ORDER_ID = :DEVTEST_ORDER_ID",
        )
        for sql in unsafe:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                _validate_project_cleanup_sql(
                    sql, "DEMO", {"DEVTEST_RUN_ID", "DEVTEST_ORDER_ID"}, {},
                )

    def test_project_drop_is_limited_to_declared_run_fixture(self) -> None:
        from scripts.dev_test_cleanup import _validate_project_cleanup_sql

        objects = {"DT_0123456789AB_ITEMS": "TABLE"}
        valid = _validate_project_cleanup_sql("DROP TABLE DT_0123456789AB_ITEMS", "DEMO", set(), objects)
        self.assertEqual(valid, {
            "kind": "drop",
            "owner": "DEMO",
            "name": "DT_0123456789AB_ITEMS",
            "type": "TABLE",
            "bindings": [],
        })
        for sql in (
            "DROP TABLE DT_0123456789AB_OTHER",
            "DROP TABLE DEMO.DT_0123456789AB_ITEMS",
            "DROP TABLE DT_0123456789AB_ITEMS CASCADE CONSTRAINTS",
            "DROP USER DEMO CASCADE",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                _validate_project_cleanup_sql(sql, "DEMO", set(), objects)

    def test_project_plan_and_cleanup_use_only_frozen_exact_keys(self) -> None:
        from scripts.dev_test_cleanup import cleanup_run, plan_cleanup

        with tempfile.TemporaryDirectory() as temp:
            run = create_run(Path(temp))
            _report, _contract = _write_project_cleanup_evidence(run)
            cleanup_path = Path(_contract["sqlFiles"][0]["path"])
            suite_manifest = next(
                Path(path) for path in _report["source"]["inputHashes"]
                if Path(path).name == "suite.json"
            )
            suite_root = suite_manifest.parent
            alias_suite_root = suite_root.parent / "RUNNER~1" / suite_root.name
            alias_cleanup_path = alias_suite_root / cleanup_path.name
            original_resolve = Path.resolve

            def resolve_with_windows_alias(path, *args, **kwargs):
                resolved = original_resolve(path, *args, **kwargs)
                return alias_cleanup_path if resolved == cleanup_path else resolved

            original_samefile = os.path.samefile

            def samefile_with_windows_alias(left, right):
                if {Path(left), Path(right)} == {alias_suite_root, suite_root}:
                    return True
                return original_samefile(left, right)

            target = Target("dev", "dev-owner", "DEMO", "DEMO", "development")
            live = {"identity": dict(IDENTITY), "complete": True, "objects": []}
            rows = {
                "count": 1,
                "truncated": False,
                "rows": [{"ORDER_ID": "101"}],
                "identity": dict(IDENTITY),
            }
            retained_order_lines = {101}
            check_calls: list[list[str]] = []

            def checks_runner(_target, checks, _directory, **kwargs):
                check_calls.append([check.id for check in checks])
                check_results = [
                    {
                        "check": check.id,
                        "passed": len(retained_order_lines) == check.expected,
                        "value": len(retained_order_lines),
                    }
                    for check in checks
                ]
                return type("CheckReport", (), {
                    "coverage": {"sessions": 1, "sessionIdentities": [dict(IDENTITY)]},
                    "complete": True,
                    "passed": all(result["passed"] for result in check_results),
                    "results": check_results,
                })()

            step_calls: list[str] = []

            def sqlcl_runner(_target, driver_path, run_dir, **_kwargs):
                content = driver_path.read_text(encoding="utf-8")
                payload = (driver_path.parent / "payload.sql").read_text(encoding="utf-8")
                match = __import__("re").search(r"PROMPT DEVTEST_STEP_START:([a-z0-9-]+)", content)
                self.assertIsNotNone(match)
                step_id = match.group(1)
                step_calls.append(step_id)
                if "DELETE FROM ORDER_LINES" in payload and "COMMIT;" in payload:
                    retained_order_lines.discard(101)
                return SqlclResult(
                    0,
                    (
                        "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                        "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
                        f"DEVTEST_STEP_START:{step_id}\n"
                        f"DEVTEST_STEP_COMPLETED:{step_id}\n"
                    ),
                    run_dir,
                )

            with patch.object(Path, "resolve", new=resolve_with_windows_alias), \
                    patch("scripts.path_utils.os.path.samefile", side_effect=samefile_with_windows_alias), \
                    patch("scripts.dev_test_cleanup.resolve_target", return_value=target), \
                    patch("scripts.dev_test_cleanup._capture_live_inventory", return_value=live), \
                    patch("scripts.dev_test_runtime.read_rows", return_value=rows), \
                    patch("scripts.migration_checks.run_checks", side_effect=checks_runner):
                plan = plan_cleanup(run, {})
                self.assertEqual(plan["status"], "READY")
                self.assertEqual(plan["operations"][0]["bindings"], ["DEVTEST_ORDER_ID"])
                first = cleanup_run(run, {}, runner=sqlcl_runner)
                second = cleanup_run(run, {}, runner=sqlcl_runner)

            self.assertEqual(first["status"], "COMPLETE")
            self.assertTrue(first["postconditionsPassed"])
            self.assertEqual(second["status"], "COMPLETE")
            self.assertEqual(len(step_calls), 1)
            self.assertEqual(retained_order_lines, set())
            self.assertEqual(check_calls, [["rows-absent"], ["rows-absent"], ["rows-absent"]])


if __name__ == "__main__":
    unittest.main()
