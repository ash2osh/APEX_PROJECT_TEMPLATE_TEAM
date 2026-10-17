"""Offline production-path tests for built-in migration and rollout scenarios."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401  (guards tests from starting real SQLcl)

from scripts import baseline, dev_test_core_types as core_types, migrate, migration_attempts
from scripts import migration_checks, migration_manifest
from scripts.db_targets import Target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_core import run_builtin
from scripts.dev_test_core_migrations import (
    run_invalid_view,
    run_migration_refusal,
    run_migration_success,
    run_sqlcl_delivery,
)
from scripts.dev_test_evidence import create_run
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot
from scripts.sqlcl_session import SqlclResult


class DevTestCoreMigrationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.target = Target("dev", "demo-dev", "DEMO", "DEMO", "development")
        self.run = create_run(self.root)
        self.context = RenderContext("DEMO", self.run.run_id, {}, {})
        self.identity = {
            "session_user": "DEMO", "current_schema": "DEMO",
            "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
            "service_name": "FREEPDB1", "container_id": "3",
            "container_name": "FREEPDB1", "edition": "ORA$BASE",
            "database_version": "23.4",
        }
        self.state: dict = {
            "objects": {}, "columns": [], "rows": [], "view_status": "VALID",
        }
        self.mode = ""
        self.checked_queries: list[tuple[str, int | None]] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _inventory(self) -> SchemaInventory:
        objects = {
            ObjectKey("DEMO", name, object_type): {
                "owner": "DEMO", "name": name, "type": object_type,
                "object_id": index + 100, "status": (
                    self.state["view_status"] if object_type == "VIEW" else "VALID"
                ),
            }
            for index, (name, object_type) in enumerate(self.state["objects"].items())
        }
        return SchemaInventory(
            dict(self.identity), objects, {
                "ownerComplete": True, "path": "OWNER_SESSION",
                "catalogs": ["ALL_OBJECTS"],
            },
            "2026-10-16T00:00:00+00:00", "2026-10-16T00:00:01+00:00",
        )

    def _snapshot(self, inventory: SchemaInventory, keys: Sequence[tuple[str, str]] = ()) -> SchemaSnapshot:
        objects = {}
        for name, object_type in keys:
            key = ObjectKey("DEMO", name, object_type)
            if key not in inventory.objects:
                continue
            attrs = {"columns": [dict(row) for row in self.state["columns"]]} if object_type == "TABLE" else {}
            raw_ddl = f'CREATE {object_type} "DEMO"."{name}"'
            objects[key] = ObjectDefinition(
                key, attrs, raw_ddl, (), self.state["view_status"] == "VALID" or object_type != "VIEW",
            )
        return SchemaSnapshot(
            dict(inventory.identity), dict(inventory.objects), objects,
            {"ownerComplete": True, "path": "OWNER_SESSION"},
            inventory.started_at, inventory.completed_at,
        )

    def _run_checks(
        self,
        target: Target,
        checks: Sequence,
        run_dir: Path,
        *,
        phase: str,
        **_kwargs,
    ) -> migration_checks.CheckReport:
        results = []
        errors = []
        for check in checks:
            sql = check.sql.upper()
            value = check.expected
            if check.id == "delivery-source-rows":
                value = int(self.state["rows"] == [
                    {"ID": "1", "STATE": "UNCHANGED"},
                    {"ID": "2", "STATE": "UNCHANGED"},
                ])
            elif check.id == "delivery-selected-row-only":
                value = int(self.state["rows"] == [
                    {"ID": "1", "STATE": "UPDATED"},
                    {"ID": "2", "STATE": "UNCHANGED"},
                ])
            elif check.id == "pre-note-column-absent":
                value = int(not any(row.get("name") == "NOTE" for row in self.state["columns"]))
            elif "ALL_OBJECTS" in sql and "STATUS = 'VALID'" in sql:
                value = int(self.state["view_status"] == "VALID")
            elif "COLUMN_NAME = 'NOTE'" in sql and "CHAR_LENGTH = 20" in sql:
                note = next((row for row in self.state["columns"] if row.get("name") == "NOTE"), None)
                value = int(note is not None and note.get("char_length") == 20)
            passed = value == check.expected
            result = {
                "id": check.id, "check": check.id, "expected": check.expected,
                "value": value, "row_count": 1, "passed": passed,
            }
            results.append(result)
            if not passed:
                errors.append({"check": check.id, "code": "CHECK_MISMATCH", "message": "observed value differs"})
            self.checked_queries.append((check.id, check.expected))
        coverage = {
            "sessions": 1,
            "sessionIdentities": [dict(self.identity)],
            "evidence": str(run_dir),
        }
        return migration_checks.CheckReport(
            passed=not errors, complete=True, results=tuple(results),
            errors=tuple(errors), coverage=coverage,
        )

    def _generated_step(self, target, scenario, step_id, sql, context, run, runner, identity, ordinal):
        """Model only initial fixture setup; production rollout/migration paths remain real."""
        table_match = re.search(r'CREATE\s+TABLE\s+"DEMO"\."([A-Z0-9_$#]+)"', sql, re.IGNORECASE)
        if table_match:
            table = table_match.group(1).upper()
            self.state["table"] = table
            self.state["objects"][table] = "TABLE"
            self.state["columns"] = [{
                "table_name": table, "name": "ID", "data_type": "NUMBER",
                "data_precision": 10, "data_scale": 0, "data_length": 22,
                "nullable": "N", "hidden": "NO", "virtual": "NO", "identity": "NO",
            }]
        if "INSERT" in sql.upper():
            self.state["rows"] = [{"ID": "1", "STATE": "UNCHANGED"}, {"ID": "2", "STATE": "UNCHANGED"}]
        view_match = re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+"DEMO"\."([A-Z0-9_$#]+)"', sql, re.IGNORECASE)
        if view_match:
            view = view_match.group(1).upper()
            self.state["view"] = view
            self.state["objects"][view] = "VIEW"
            self.state["view_status"] = "VALID"
        return {
            "id": step_id, "status": "PASSED", "phase": "apply",
            "identity": dict(self.identity), "writeStarted": True,
            "sha256": "a" * 64,
        }

    def _capture_fixture(self, target, run, runner, name, object_type, expected_identity):
        inventory = self._inventory()
        key = ObjectKey("DEMO", name, object_type)
        if key not in inventory.objects:
            raise AssertionError(f"test fixture missing from model: {key}")
        snapshot = self._snapshot(inventory, ((name, object_type),))
        definition = snapshot.objects[key]
        return inventory, snapshot, definition

    def _preflight(self, scenario, target, context, run, runner, fixture_names):
        return dict(self.identity), self._inventory(), []

    def _capture_inventory(self, target, run_dir):
        return self._inventory()

    def _capture_snapshot(self, target, inventory, keys, run_dir):
        return self._snapshot(inventory, keys)

    def _apply_migration_sqlcl(self, target, driver, run_dir, *, phase, **_kwargs):
        source = driver.read_text(encoding="utf-8")
        payloads = re.findall(r"(?m)^PROMPT MIGRATION_PAYLOAD_STARTED:([^\r\n]+)$", source)
        if phase != "apply" or len(payloads) != 1:
            raise AssertionError("unexpected SQLcl call from migration apply path")
        self.assertLess(source.index("SET SQLBLANKLINES ON"), source.index("PROMPT MIGRATION_PAYLOAD_STARTED:"))
        payload_match = re.search(r"(?m)^@@\.\./payload/([^/\r\n]+)/([^\r\n]+)$", source)
        if payload_match is None or payload_match.group(2) != payloads[0]:
            raise AssertionError("migration driver does not reference its frozen payload")
        payload_path = (driver.parent / ".." / "payload" / payload_match.group(1) / payload_match.group(2)).resolve()
        payload_source = payload_path.read_text(encoding="utf-8")
        if self.mode == "migration-success":
            self.state["columns"].append({
                "table_name": self.state["table"], "name": "NOTE", "data_type": "VARCHAR2",
                "char_length": 20, "char_used": "C", "data_length": 80,
                "nullable": "Y", "hidden": "NO", "virtual": "NO", "identity": "NO",
            })
        elif self.mode == "migration-refusal":
            self.state["columns"].append({
                "table_name": self.state["table"], "name": "NOTE", "data_type": "VARCHAR2",
                "char_length": 21, "char_used": "C", "data_length": 84,
                "nullable": "Y", "hidden": "NO", "virtual": "NO", "identity": "NO",
            })
        elif self.mode == "invalid-view":
            self.state["view_status"] = "INVALID"
        elif self.mode == "sqlcl-delivery":
            self.assertIn("\n\nWHERE", payload_source)
            self.assertIn('WHERE "ID" = 1', payload_source)
            self.state["rows"] = [
                {"ID": row["ID"], "STATE": "UPDATED" if row["ID"] == "1" else row["STATE"]}
                for row in self.state["rows"]
            ]
        output = "\n".join((
            "MIGRATION_IDENTITY_BEGIN", json.dumps(self.identity, sort_keys=True),
            "MIGRATION_IDENTITY_END", "MIGRATION_IDENTITY_VERIFIED",
            f"MIGRATION_PAYLOAD_STARTED:{payloads[0]}",
            "MIGRATION_APPLY_COMPLETED", "",
        ))
        return SqlclResult(0, output, run_dir)

    def _runner(self, target, driver, run_dir, *, phase, **_kwargs):
        source = driver.read_text(encoding="utf-8")
        if phase == "inventory":
            marker_match = re.search(r"PROMPT (ROQ_[0-9a-f]{32})_IDENTITY_BEGIN", source)
            if not marker_match:
                raise AssertionError("unexpected inventory SQLcl driver")
            marker = marker_match.group(1)
            output = [
                f"{marker}_IDENTITY_BEGIN",
                f"{marker}_I:DEMO|DEMO|FREEPDB1|FREEPDB1|FREEPDB1",
                f"{marker}_IDENTITY_END", f"{marker}_DATA_BEGIN",
            ]
            output.extend(['"ID","STATE"', *[f'"{row["ID"]}","{row["STATE"]}"' for row in self.state["rows"]]])
            output.extend((f"{marker}_DATA_END", f"{marker}_SUCCESS", ""))
            return SqlclResult(0, "\n".join(output), run_dir)
        step_match = re.search(r"(?m)^PROMPT DEVTEST_STEP_START:([^\r\n]+)$", source)
        if not step_match:
            raise AssertionError("unexpected setup SQLcl driver")
        step_id = step_match.group(1)
        payload = (driver.parent / "payload.sql").read_text(encoding="utf-8")
        if "INSERT" in payload.upper():
            self.state["rows"] = [{"ID": "1", "STATE": "UNCHANGED"}, {"ID": "2", "STATE": "UNCHANGED"}]
        output = "\n".join((
            "ROLLOUT_IDENTITY:DEMO:DEMO", "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1",
            f"DEVTEST_STEP_START:{step_id}", f"DEVTEST_STEP_COMPLETED:{step_id}", "",
        ))
        return SqlclResult(0, output, run_dir)

    def _rollout_sqlcl(self, target, driver, run_dir, *, phase, **_kwargs):
        source = driver.read_text(encoding="utf-8")
        payload = (driver.parent / "payload.sql").read_text(encoding="utf-8")
        self.assertIn("\n\nWHERE", payload)
        self.assertLess(source.index("SET SQLBLANKLINES ON"), source.index("@@payload.sql"))
        if "UPDATE" in payload.upper() and "WHERE" in payload.upper():
            self.state["rows"] = [
                {"ID": row["ID"], "STATE": "UPDATED" if row["ID"] == "1" else row["STATE"]}
                for row in self.state["rows"]
            ]
        output = "\n".join((
            "ROLLOUT_IDENTITY:DEMO:DEMO", "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1",
            "ROLLOUT_SCRIPT_COMPLETED:0001", "",
        ))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "sqlcl-output.log").write_text(output, encoding="utf-8")
        return SqlclResult(0, output, run_dir)

    def _rollout_identity(self, target, run_dir):
        run_dir.mkdir(parents=True, exist_ok=True)
        return dict(self.identity)

    def _patch_common(self):
        stack = ExitStack()
        stack.enter_context(patch.object(core_types, "_run_common_preflight", side_effect=self._preflight))
        stack.enter_context(patch.object(core_types, "_execute_generated_step", side_effect=self._generated_step))
        stack.enter_context(patch.object(core_types, "_capture_fixture", side_effect=self._capture_fixture))
        stack.enter_context(patch.object(core_types, "capture_inventory", side_effect=lambda *_a, **_k: self._inventory()))
        stack.enter_context(patch.object(core_types, "capture_snapshot", side_effect=lambda target, inventory, keys, *_a, **_k: self._snapshot(inventory, keys)))
        stack.enter_context(patch.object(core_types, "run_checks", side_effect=self._run_checks))
        return stack

    def test_delivery_uses_rollout_and_reads_back_only_the_selected_row(self) -> None:
        from scripts import rollout as rollout_module

        stack = self._patch_common()
        env = {
            "DB_ENVIRONMENT": "development", "PROJECT_SCHEMA": "",
            "CODE_SCHEMA": "DEMO", "CODE_SQLCL_CONNECTION": "demo-dev",
            "CODE_EXPECTED_USER": "DEMO",
        }
        with stack, patch.object(rollout_module, "_observe_target_identity", side_effect=self._rollout_identity), patch(
            "scripts.rollout.run_sqlcl", side_effect=self._rollout_sqlcl,
        ) as rollout_sqlcl, patch.dict("os.environ", env, clear=False), patch.object(
            rollout_module, "run_rollout", wraps=rollout_module.run_rollout,
        ) as production_rollout, patch.object(
            rollout_module, "_execute_sql_payload", wraps=rollout_module._execute_sql_payload,
        ) as production_sql_execution:
            self.state["rows"] = []
            self.mode = "sqlcl-delivery"
            result = run_sqlcl_delivery(self.target, self.context, self.run, runner=self._runner)

        self.assertEqual(result["status"], "PASSED", result.get("error"))
        production_rollout.assert_called_once()
        production_sql_execution.assert_called_once()
        rollout_sqlcl.assert_called_once()
        self.assertEqual(production_rollout.call_args.kwargs["repo_root"], self.run.run_dir / "project")
        self.assertEqual(self.state["rows"], [{"ID": "1", "STATE": "UPDATED"}, {"ID": "2", "STATE": "UNCHANGED"}])
        report = result["observations"]
        self.assertEqual(report[0]["rows"], [{"ID": "1", "STATE": "UPDATED"}, {"ID": "2", "STATE": "UNCHANGED"}])
        self.assertFalse((self.root / "migrations").exists())
        project = self.run.run_dir / "project"
        self.assertTrue((project / "delivery.sql").is_file())
        rollout_step = next(step for step in result["steps"] if step["id"] == "rollout-update")
        self.assertEqual(rollout_step["targetBinding"]["connection"], "DEMO")
        self.assertEqual(rollout_step["targetBinding"]["expectedUser"], "DEMO")
        receipt_path = project / rollout_step["receipt"]
        self.assertTrue(receipt_path.is_relative_to(project))
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["status"], "succeeded")

    def test_delivery_refuses_alias_drift_before_creating_fixtures(self) -> None:
        from scripts import rollout as rollout_module

        def different_database(_target, run_dir):
            run_dir.mkdir(parents=True, exist_ok=True)
            return {**self.identity, "db_unique_name": "DIFFERENT_DB"}

        env = {
            "DB_ENVIRONMENT": "development", "PROJECT_SCHEMA": "",
            "CODE_SCHEMA": "DEMO", "CODE_SQLCL_CONNECTION": "demo-dev",
            "CODE_EXPECTED_USER": "DEMO",
        }
        stack = self._patch_common()
        with stack, patch.object(rollout_module, "_observe_target_identity", side_effect=different_database), patch(
            "scripts.rollout.run_sqlcl", side_effect=self._rollout_sqlcl,
        ) as rollout_sqlcl, patch.dict("os.environ", env, clear=False), patch.object(
            rollout_module, "run_rollout", wraps=rollout_module.run_rollout,
        ) as production_rollout:
            result = run_sqlcl_delivery(self.target, self.context, self.run, runner=self._runner)

        self.assertEqual(result["status"], "BLOCKED", result.get("error"))
        self.assertEqual(self.state["objects"], {})
        self.assertEqual(self.state["rows"], [])
        production_rollout.assert_not_called()
        rollout_sqlcl.assert_not_called()

    def test_migration_success_uses_baseline_generator_and_validated_scratch_receipt(self) -> None:
        self.mode = "migration-success"
        stack = self._patch_common()
        with stack, patch("scripts.migrate.run_sqlcl", side_effect=self._apply_migration_sqlcl), patch.object(
            migrate, "apply_batch", wraps=migrate.apply_batch,
        ) as production_apply, patch.object(
            baseline, "_structure_bundle", wraps=baseline._structure_bundle,
        ) as structure_generator, patch.object(
            migration_manifest, "validate_receipt", wraps=migration_manifest.validate_receipt,
        ) as receipt_validator:
            result = run_migration_success(self.target, self.context, self.run, runner=self._runner)

        self.assertEqual(result["status"], "PASSED", result.get("error"))
        production_apply.assert_called_once()
        structure_generator.assert_called_once()
        receipt_validator.assert_called_once()
        project = self.run.run_dir / "project"
        receipts = list(project.rglob("status.dev.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "verified")
        loaded = migration_manifest.load_migration(
            project, receipts[0].parent.relative_to(project).as_posix(),
        )
        self.assertEqual(receipt["checksSha256"], loaded.checks_sha256)
        self.assertEqual(len(loaded.preconditions), 1)
        self.assertEqual(loaded.preconditions[0].expected, 1)
        self.assertIn("COLUMN_NAME = 'NOTE'", loaded.preconditions[0].sql)
        generated_column_check = baseline._column_definition_check("DEMO", {
            "table_name": self.state["table"], "name": "NOTE", "data_type": "VARCHAR2",
            "char_length": 20, "char_used": "C", "data_length": 80, "nullable": "Y",
        })
        self.assertTrue(any(
            check.id == generated_column_check["id"]
            and check.expected == 1
            and check.sql == generated_column_check["sql"]
            for check in loaded.postconditions
        ))
        self.assertTrue(receipts[0].is_relative_to(project))
        self.assertFalse((self.root / "migrations").exists())

    def test_migration_refusal_keeps_pending_lock_and_retry_never_reapplies(self) -> None:
        self.mode = "migration-refusal"
        stack = self._patch_common()
        with stack, patch("scripts.migrate.run_sqlcl", side_effect=self._apply_migration_sqlcl) as sqlcl_apply, patch.object(
            migrate, "apply_batch", wraps=migrate.apply_batch,
        ) as production_apply:
            result = run_migration_refusal(self.target, self.context, self.run, runner=self._runner)

        self.assertEqual(result["status"], "PASSED", result.get("error"))
        self.assertEqual(production_apply.call_count, 2)
        self.assertEqual(sqlcl_apply.call_count, 1, "guarded retry must stop before another payload execution")
        self.assertEqual(self.state["columns"][-1]["char_length"], 21)
        project = self.run.run_dir / "project"
        self.assertEqual(list(project.rglob("status.dev.json")), [])
        event_paths = list(project.rglob("attempt-event.*.json"))
        self.assertTrue(event_paths)
        events = migration_attempts.read_attempt_events(event_paths[0].parent)
        self.assertEqual([event["state"] for event in events], ["pending"])
        self.assertTrue(all(path.is_relative_to(project) for path in event_paths))
        self.assertFalse((self.root / "migrations").exists())

    def test_invalid_view_uses_validity_check_and_preserves_empty_error_observation(self) -> None:
        self.mode = "invalid-view"
        stack = self._patch_common()
        read_calls: list[tuple[str, int]] = []

        def read_rows(target, sql, context, run, *, max_rows, runner):
            read_calls.append((sql, max_rows))
            rows = [{"STATUS": "INVALID"}] if "ALL_OBJECTS" in sql.upper() else []
            columns = ["STATUS"] if rows else ["LINE", "POSITION", "ATTRIBUTE", "TEXT"]
            return {
                "columns": columns, "rows": rows, "count": len(rows), "truncated": False,
                "identity": dict(self.identity), "evidence": str(run.run_dir / "sessions" / "fake-read"),
            }

        with stack, patch("scripts.migrate.run_sqlcl", side_effect=self._apply_migration_sqlcl), patch.object(
            migrate, "apply_batch", wraps=migrate.apply_batch,
        ) as production_apply, patch("scripts.dev_test_core_migrations.read_rows", side_effect=read_rows):
            result = run_invalid_view(self.target, self.context, self.run, runner=self._runner)

        self.assertEqual(result["status"], "PASSED", result.get("error"))
        production_apply.assert_called_once()
        self.assertEqual(self.state["view_status"], "INVALID")
        self.assertEqual([limit for _sql, limit in read_calls], [1, 20])
        self.assertIn("ALL_ERRORS", read_calls[1][0].upper())
        self.assertEqual(result["observations"][-1]["rows"], [])
        self.assertNotIn("diagnostic", json.dumps(result["observations"][-1]).casefold())
        validity = baseline._view_validity_check("DEMO", self.state["view"])
        self.assertTrue(any(query_id == validity["id"] for query_id, _expected in self.checked_queries))
        validity_expectations = [expected for query_id, expected in self.checked_queries if query_id == validity["id"]]
        self.assertIn(1, validity_expectations)
        self.assertIn(0, validity_expectations)
        self.assertEqual(validity["expected"], 1)
        self.assertFalse((self.root / "migrations").exists())

    def test_run_builtin_dispatches_all_task_seven_adapters(self) -> None:
        from scripts import dev_test_core_migrations as adapters

        ids = {
            "sqlcl-delivery": "run_sqlcl_delivery",
            "migration-success": "run_migration_success",
            "migration-refusal": "run_migration_refusal",
            "invalid-view": "run_invalid_view",
        }
        for scenario_id, function_name in ids.items():
            with self.subTest(scenario_id=scenario_id), patch.object(
                adapters, function_name, return_value={"id": scenario_id, "status": "PASSED"},
            ) as adapter:
                result = run_builtin(scenario_id, self.target, self.context, self.run, runner=self._runner)
                self.assertEqual(result["status"], "PASSED")
                self.assertEqual(adapter.call_args.kwargs["runner"], self._runner)


if __name__ == "__main__":
    unittest.main()
