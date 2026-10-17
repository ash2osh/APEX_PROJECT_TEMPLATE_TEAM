"""Tests for built-in datatype and compiler-settings qualification (Task 6)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts import baseline
from scripts.db_targets import Target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_core import (
    BUILTIN_SCENARIO_IDS,
    builtin_suite,
    run_builtin,
)
from scripts.dev_test_core_types import (
    _check_identities,
    run_compiler_settings,
    run_datatype_roundtrip,
)
from scripts.dev_test_evidence import (
    create_run,
    freeze_inputs,
    make_object_name,
)
from scripts.dev_test_manifest import Suite, select_scenarios
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot
from scripts.sqlcl_session import SqlclResult


class DevTestCoreTypesTests(unittest.TestCase):
    """Test suite for Task 6 built-in datatype roundtrip and compiler settings."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.target = Target("dev", "demo-dev", "DEMO", "DEMO", "development")
        self.run = create_run(self.root)
        self.context = RenderContext(
            owner="DEMO",
            run_id=self.run.run_id,
            objects={},
            values={},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _freeze_test_inputs(self) -> None:
        dummy_file = self.run.run_dir / "inputs" / "test-dummy.sql"
        dummy_file.parent.mkdir(parents=True, exist_ok=True)
        dummy_file.write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        freeze_inputs(self.run, [dummy_file])

    def _catalog_identity(self) -> dict[str, str]:
        return {
            "session_user": "DEMO", "current_schema": "DEMO",
            "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
            "service_name": "FREEPDB1", "container_id": "3",
            "container_name": "FREEPDB1", "edition": "ORA$BASE",
            "database_version": "23.4",
        }

    def _catalog_inventory(self, names: tuple[tuple[str, str], ...] = ()) -> SchemaInventory:
        objects = {
            ObjectKey("DEMO", name, object_type): {
                "owner": "DEMO", "name": name, "type": object_type,
                "object_id": index + 1, "status": "VALID",
            }
            for index, (name, object_type) in enumerate(names)
        }
        return SchemaInventory(
            self._catalog_identity(), objects,
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"},
            "2026-10-16T00:00:00+00:00", "2026-10-16T00:00:01+00:00",
        )

    def _datatype_catalog_columns(self) -> list[dict[str, Any]]:
        rows = [
            {"name": "COL_BOOL", "data_type": "BOOLEAN", "data_length": 1, "nullable": "Y", "column_id": 3},
            {"name": "COL_NUM1", "data_type": "NUMBER", "data_length": 22, "data_precision": 10, "data_scale": 2, "nullable": "Y", "column_id": 4},
            {"name": "COL_NUM2", "data_type": "NUMBER", "data_length": 22, "data_scale": 3, "nullable": "Y", "column_id": 5},
            {"name": "COL_FLOAT", "data_type": "FLOAT", "data_length": 22, "data_precision": 63, "nullable": "Y", "column_id": 6},
            {"name": "COL_VC", "data_type": "VARCHAR2", "data_length": 32, "char_length": 8, "char_used": "C", "nullable": "Y", "column_id": 7},
            {"name": "COL_RAW", "data_type": "RAW", "data_length": 16, "nullable": "Y", "column_id": 8},
        ]
        for row in rows:
            row.update({"hidden": "NO", "virtual": "NO", "identity": "NO"})
        return rows

    def _table_snapshot(self, table: str, columns: list[dict[str, Any]]) -> SchemaSnapshot:
        key = ObjectKey("DEMO", table, "TABLE")
        inventory = {key: {"owner": "DEMO", "name": table, "type": "TABLE", "status": "VALID"}}
        definition = ObjectDefinition(
            key, {"columns": columns}, f'CREATE TABLE "DEMO"."{table}" ("CAPTURED" NUMBER)', (), True,
        )
        return SchemaSnapshot(
            self._catalog_identity(), inventory, {key: definition},
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS", "ALL_TAB_COLUMNS"], "path": "OWNER_SESSION"},
            "2026-10-16T00:00:00+00:00", "2026-10-16T00:00:01+00:00",
        )

    def _procedure_snapshot(self, name: str) -> SchemaSnapshot:
        key = ObjectKey("DEMO", name, "PROCEDURE")
        inventory = {key: {"owner": "DEMO", "name": name, "type": "PROCEDURE", "status": "VALID"}}
        definition = ObjectDefinition(
            key, {}, f'CREATE PROCEDURE "DEMO"."{name}" IS BEGIN NULL; END;', (), True,
        )
        return SchemaSnapshot(
            self._catalog_identity(), inventory, {key: definition},
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"},
            "2026-10-16T00:00:00+00:00", "2026-10-16T00:00:01+00:00",
        )

    def _check_report(self, query_checks: list[Any], *, passed: bool = True) -> Any:
        identity = self._catalog_identity()
        results = [
            {"id": check.id, "expected": check.expected,
             "value": check.expected if passed else 0, "passed": passed}
            for check in query_checks
        ]
        return SimpleNamespace(
            passed=passed, complete=True, results=results, errors=(),
            coverage={"sessions": 1, "sessionIdentities": [identity]},
        )

    def _compiler_settings_readback(self, ccflags: str | None) -> dict[str, Any]:
        return {
            "count": 1,
            "truncated": False,
            "columns": [
                "PLSQL_OPTIMIZE_LEVEL", "PLSQL_CODE_TYPE", "PLSQL_DEBUG",
                "PLSQL_WARNINGS", "NLS_LENGTH_SEMANTICS", "PLSCOPE_SETTINGS",
                "CCFLAGS_STATUS", "PLSQL_CCFLAGS", "CONTAINER_ID",
                "CONTAINER_NAME", "EDITION", "DATABASE_VERSION",
            ],
            "rows": [{
                "PLSQL_OPTIMIZE_LEVEL": "2",
                "PLSQL_CODE_TYPE": "INTERPRETED",
                "PLSQL_DEBUG": "FALSE",
                "PLSQL_WARNINGS": "ENABLE:ALL",
                "NLS_LENGTH_SEMANTICS": "BYTE",
                "PLSCOPE_SETTINGS": "IDENTIFIERS:NONE",
                "CCFLAGS_STATUS": "NULL" if ccflags is None else "VALUE",
                "PLSQL_CCFLAGS": ccflags,
                "CONTAINER_ID": "3",
                "CONTAINER_NAME": "FREEPDB1",
                "EDITION": "ORA$BASE",
                "DATABASE_VERSION": "23.4",
            }],
            "identity": self._catalog_identity(),
        }

    # -----------------------------------------------------------------------
    # 1. Built-in suite descriptor tests
    # -----------------------------------------------------------------------

    def test_builtin_suite_descriptor(self) -> None:
        """Verify builtin_suite() produces the spec's six IDs in declared display order."""
        suite = builtin_suite()
        self.assertIsInstance(suite, Suite)
        self.assertEqual(suite.id, "builtin")
        self.assertEqual(suite.schemaVersion, 1)

        expected_ids = (
            "datatype-roundtrip",
            "compiler-settings",
            "sqlcl-delivery",
            "migration-success",
            "migration-refusal",
            "invalid-view",
        )
        self.assertEqual(BUILTIN_SCENARIO_IDS, expected_ids)
        self.assertEqual(tuple(s.id for s in suite.scenarios), expected_ids)

        for sc in suite.scenarios:
            self.assertEqual(sc.dependsOn, ())
            self.assertTrue(sc.effects["writes"])
            self.assertEqual(sc.effects["external"], "none")
            self.assertEqual(sc.effects["affectedObjects"], ("@fixtures",))
            self.assertTrue(sc.checks, f"scenario {sc.id} must declare checks")
            self.assertTrue(
                sc.requirements["checks"],
                f"scenario {sc.id} must declare requirement checks",
            )
            self.assertTrue(
                sc.fixtures["objects"],
                f"scenario {sc.id} must declare fixture objects",
            )

    def test_builtin_scenarios_declare_create_table_procedure_and_fixtures(self) -> None:
        """Verify declared CREATE TABLE/PROCEDURE requirements and 30-byte fixtures for all six."""
        suite = builtin_suite()
        scenarios_by_id = {sc.id: sc for sc in suite.scenarios}

        # Check requirements for each scenario
        for sc_id in ("datatype-roundtrip", "sqlcl-delivery", "migration-success", "migration-refusal", "invalid-view"):
            sc = scenarios_by_id[sc_id]
            req_checks = sc.requirements["checks"]
            has_table_priv = any(
                "CREATE TABLE" in chk.get("sql", "") and chk.get("expected") == 1
                for chk in req_checks
            )
            self.assertTrue(has_table_priv, f"scenario {sc_id} missing CREATE TABLE requirement check")

        cs_sc = scenarios_by_id["compiler-settings"]
        has_proc_priv = any(
            "CREATE PROCEDURE" in chk.get("sql", "") and chk.get("expected") == 1
            for chk in cs_sc.requirements["checks"]
        )
        self.assertTrue(has_proc_priv, "compiler-settings missing CREATE PROCEDURE requirement check")
        self.assertTrue(
            any("ALL_TAB_COLUMNS" in chk.get("sql", "") for chk in scenarios_by_id["datatype-roundtrip"].requirements["checks"]),
            "datatype-roundtrip missing accessible column-metadata requirement",
        )
        boolean_version_check = next(
            (
                chk
                for chk in scenarios_by_id["datatype-roundtrip"].requirements["checks"]
                if "DBMS_DB_VERSION.VERSION" in chk.get("sql", "")
            ),
            None,
        )
        self.assertIsNotNone(
            boolean_version_check,
            "datatype-roundtrip must preflight the Oracle 23ai SQL BOOLEAN requirement",
        )
        self.assertEqual(1, boolean_version_check.get("expected"))
        self.assertIn("DBMS_DB_VERSION.VERSION >= 23", boolean_version_check.get("sql", ""))
        self.assertTrue(
            any("ALL_PLSQL_OBJECT_SETTINGS" in chk.get("sql", "") for chk in cs_sc.requirements["checks"]),
            "compiler-settings missing accessible compiler-metadata requirement",
        )

        # Check fixture objects and 30-byte budget for all six scenarios
        for sc in suite.scenarios:
            for fix_obj in sc.fixtures["objects"]:
                suffix = fix_obj["suffix"]
                obj_name = make_object_name(self.run.object_prefix, suffix)
                self.assertLessEqual(
                    len(obj_name.encode("utf-8")),
                    30,
                    f"Fixture object name {obj_name} exceeds 30-byte Oracle budget",
                )

    def test_select_scenarios_preserves_builtin_order(self) -> None:
        suite = builtin_suite()
        selected = select_scenarios(suite, ["invalid-view", "datatype-roundtrip"])
        self.assertEqual(
            tuple(s.id for s in selected),
            ("datatype-roundtrip", "invalid-view"),
        )

    # -----------------------------------------------------------------------
    # 2. Dispatch tests
    # -----------------------------------------------------------------------

    def test_run_builtin_rejects_unknown_id(self) -> None:
        """Unknown scenario IDs cannot reach any built-in adapter."""
        with self.assertRaisesRegex(ValueError, "Unknown built-in scenario id"):
            run_builtin("not-a-builtin", self.target, self.context, self.run)

    # -----------------------------------------------------------------------
    # 3. Step 1: test_roundtrip_uses_production_formatter_and_catalog
    # -----------------------------------------------------------------------

    def test_roundtrip_uses_production_formatter_and_catalog(self) -> None:
        """Spy on production capture/generator functions and check required datatypes."""
        expected_types = [
            ("COL_BOOL", "BOOLEAN", {}),
            ("COL_NUM1", "NUMBER", {"data_precision": 10, "data_scale": 2}),
            ("COL_NUM2", "NUMBER", {"data_precision": None, "data_scale": 3}),
            ("COL_FLOAT", "FLOAT", {"data_precision": 63, "data_scale": None}),
            ("COL_VC", "VARCHAR2", {"char_length": 8, "char_used": "C"}),
            ("COL_RAW", "RAW", {"data_length": 16}),
        ]

        # 1. Check production _table_column_type formatter
        for name, dtype, attrs in expected_types:
            row = {"name": name, "data_type": dtype, **attrs}
            formatted = baseline._table_column_type(row)
            if dtype == "BOOLEAN":
                self.assertEqual(formatted, "BOOLEAN")
            elif dtype == "NUMBER" and attrs.get("data_precision") == 10:
                self.assertEqual(formatted, "NUMBER(10,2)")
            elif dtype == "NUMBER" and attrs.get("data_precision") is None:
                self.assertEqual(formatted, "NUMBER(*,3)")
            elif dtype == "FLOAT":
                self.assertEqual(formatted, "FLOAT(63)")
            elif dtype == "VARCHAR2":
                self.assertEqual(formatted, "VARCHAR2(8 CHAR)")
            elif dtype == "RAW":
                self.assertEqual(formatted, "RAW(16)")

        # 2. Check production _column_definition_check generated SQL against catalog-shaped rows
        # Exact definitions accept 1 and deliberately wrong definitions reject 0
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE DUAL (DUMMY TEXT)")
            conn.execute("INSERT INTO DUAL VALUES ('X')")
            conn.execute(
                "CREATE TABLE ALL_TAB_COLS ("
                "OWNER TEXT, TABLE_NAME TEXT, COLUMN_NAME TEXT, DATA_TYPE TEXT, "
                "DATA_PRECISION INTEGER, DATA_SCALE INTEGER, CHAR_USED TEXT, "
                "CHAR_LENGTH INTEGER, DATA_LENGTH INTEGER)"
            )

            # Insert exact matching rows
            table_name = "TEST_TAB"
            schema = "DEMO"
            conn.executemany(
                "INSERT INTO ALL_TAB_COLS VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (schema, table_name, "COL_BOOL", "BOOLEAN", None, None, None, None, None),
                    (schema, table_name, "COL_NUM1", "NUMBER", 10, 2, None, None, 22),
                    (schema, table_name, "COL_NUM2", "NUMBER", None, 3, None, None, 22),
                    (schema, table_name, "COL_FLOAT", "FLOAT", 63, None, None, None, 22),
                    (schema, table_name, "COL_VC", "VARCHAR2", None, None, "C", 8, 32),
                    (schema, table_name, "COL_RAW", "RAW", None, None, None, None, 16),
                ],
            )

            for name, dtype, attrs in expected_types:
                row = {"name": name, "data_type": dtype, "table_name": table_name, **attrs}
                check = baseline._column_definition_check(schema, row)
                # Exact definition accepts 1
                cur = conn.execute(check["sql"])
                self.assertEqual(cur.fetchone()[0], 1, f"Exact check failed for {name}")

            # Deliberately wrong expectations reject 0
            mismatches = [
                ("COL_BOOL", {"data_type": "VARCHAR2", "char_length": 10, "char_used": "C"}),
                ("COL_NUM1", {"data_type": "NUMBER", "data_precision": 10, "data_scale": 3}),  # wrong scale
                ("COL_NUM1", {"data_type": "NUMBER", "data_precision": 12, "data_scale": 2}),  # wrong precision
                ("COL_NUM2", {"data_type": "NUMBER", "data_precision": 10, "data_scale": 3}),  # precision should be null
                ("COL_FLOAT", {"data_type": "FLOAT", "data_precision": 64}),  # wrong precision
                ("COL_VC", {"data_type": "VARCHAR2", "char_length": 10, "char_used": "C"}),  # wrong length
                ("COL_VC", {"data_type": "VARCHAR2", "char_length": 8, "char_used": "B"}),  # wrong semantics
                ("COL_RAW", {"data_type": "RAW", "data_length": 32}),  # wrong length
            ]
            for col_name, wrong_attrs in mismatches:
                base = next(e for e in expected_types if e[0] == col_name)
                wrong_row = {"name": col_name, "table_name": table_name, **base[2], **wrong_attrs}
                wrong_check = baseline._column_definition_check(schema, wrong_row)
                cur = conn.execute(wrong_check["sql"])
                self.assertEqual(cur.fetchone()[0], 0, f"Deliberately wrong check should return 0 for {col_name}")

    # -----------------------------------------------------------------------
    # 4. Step 1: test_compiler_settings_execute_generated_query
    # -----------------------------------------------------------------------

    def test_compiler_settings_execute_generated_query(self) -> None:
        """Test real generated settings SQL: conjunction repair, CCFLAGS, wrong scope/settings."""
        settings = {
            "plsql_optimize_level": "2",
            "plsql_code_type": "INTERPRETED",
            "plsql_debug": "FALSE",
            "plsql_warnings": "ENABLE:ALL",
            "nls_length_semantics": "BYTE",
            "plscope_settings": "IDENTIFIERS:NONE",
            "plsql_ccflags": None,
        }
        schema = "DEMO"
        proc_name = "DT_0123456789AB_CS_PROC"
        object_type = "PROCEDURE"

        # Verify fixture name fits 30 bytes
        self.assertLessEqual(len(proc_name.encode("utf-8")), 30)

        # Check conjunction repair: SQL must contain 'AND' between TYPE and clauses
        chk = baseline._exact_settings_check(settings, proc_name, object_type, schema)
        self.assertIn("S.TYPE = 'PROCEDURE' AND S.PLSQL_OPTIMIZE_LEVEL", chk["sql"])

        columns = ["OWNER", "NAME", "TYPE", *(key.upper() for key in settings)]
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE DUAL (DUMMY TEXT)")
            conn.execute("INSERT INTO DUAL VALUES ('X')")
            conn.execute("CREATE TABLE ALL_PLSQL_OBJECT_SETTINGS (" + ", ".join(columns) + ")")

            row_values = [schema, proc_name, object_type, *settings.values()]
            conn.execute(
                "INSERT INTO ALL_PLSQL_OBJECT_SETTINGS VALUES (" + ", ".join("?" for _ in row_values) + ")",
                row_values,
            )

            # 1. Null CCFLAGS: exact accepts 1
            query = baseline._exact_settings_check(settings, proc_name, object_type, schema)["sql"]
            self.assertEqual(conn.execute(query).fetchone()[0], 1)

            # 2. Non-null CCFLAGS: exact accepts 1
            settings_with_flags = dict(settings)
            settings_with_flags["plsql_ccflags"] = "APP_FLAG:TRUE"
            conn.execute("UPDATE ALL_PLSQL_OBJECT_SETTINGS SET PLSQL_CCFLAGS = 'APP_FLAG:TRUE'")
            query_with_flags = baseline._exact_settings_check(settings_with_flags, proc_name, object_type, schema)["sql"]
            self.assertEqual(conn.execute(query_with_flags).fetchone()[0], 1)

            # Query with null CCFLAGS expectation now rejects 0
            self.assertEqual(conn.execute(query).fetchone()[0], 0)

            # 3. Wrong scope: wrong owner, wrong name, wrong type
            for wrong_owner in ("OTHER_SCHEMA", "SYS"):
                q = baseline._exact_settings_check(settings_with_flags, proc_name, object_type, wrong_owner)["sql"]
                self.assertEqual(conn.execute(q).fetchone()[0], 0)

            for wrong_name in ("OTHER_PROC", "DT_DIFFERENT"):
                q = baseline._exact_settings_check(settings_with_flags, wrong_name, object_type, schema)["sql"]
                self.assertEqual(conn.execute(q).fetchone()[0], 0)

            for wrong_type in ("FUNCTION", "PACKAGE"):
                q = baseline._exact_settings_check(settings_with_flags, proc_name, wrong_type, schema)["sql"]
                self.assertEqual(conn.execute(q).fetchone()[0], 0)

            # 4. Each wrong setting expectation rejected
            wrong_setting_cases = [
                ("plsql_optimize_level", "3"),
                ("plsql_code_type", "NATIVE"),
                ("plsql_debug", "TRUE"),
                ("plsql_warnings", "DISABLE:ALL"),
                ("nls_length_semantics", "CHAR"),
                ("plscope_settings", "IDENTIFIERS:ALL"),
                ("plsql_ccflags", "APP_FLAG:FALSE"),
            ]
            for setting_key, wrong_val in wrong_setting_cases:
                bad_settings = dict(settings_with_flags)
                bad_settings[setting_key] = wrong_val
                bad_q = baseline._exact_settings_check(bad_settings, proc_name, object_type, schema)["sql"]
                self.assertEqual(
                    conn.execute(bad_q).fetchone()[0],
                    0,
                    f"Wrong expectation for {setting_key} should return 0",
                )

    # -----------------------------------------------------------------------
    # 5. Full roundtrip execution adapter tests
    # -----------------------------------------------------------------------

    def test_run_datatype_roundtrip_full_cycle(self) -> None:
        """Test full datatype roundtrip adapter with mock runner."""
        self._freeze_test_inputs()

        executed_statements: list[str] = []

        def mock_runner(target: Target, driver_path: Path, run_dir: Path, **kwargs: Any) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            executed_statements.append(content)
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
            )
            phase = kwargs.get("phase", "")
            if "DEVTEST_STEP_START" in content:
                step_line = [l for l in content.splitlines() if "DEVTEST_STEP_START:" in l][0]
                step_id = step_line.split(":")[1]
                output += f"DEVTEST_STEP_START:{step_id}\nDEVTEST_STEP_COMPLETED:{step_id}\n"
            elif phase == "postconditions" or "l_payload.put('phase'" in content:
                check_ids = re.findall(r"l_result\.put\('id',\s*'([^']+)'\)", content)
                identity = {
                    "session_user": "DEMO", "current_schema": "DEMO",
                    "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
                    "service_name": "FREEPDB1", "container_id": "3",
                    "container_name": "FREEPDB1", "edition": "ORA$BASE",
                    "database_version": "23.4",
                }
                payload = {
                    "schemaVersion": 1, "phase": "postconditions", "complete": True,
                    "identity": identity,
                    "results": [
                        {
                            "id": cid,
                            "row_count": 1,
                            "column_count": 1,
                            "numeric": True,
                            "value": 0 if ("wrong" in cid or "mismatch" in cid) else 1,
                        }
                        for cid in check_ids
                    ],
                }
                output += (
                    "CHECK_PAYLOAD_BEGIN:postconditions\n"
                    + json.dumps(payload)
                    + "\nCHECK_PAYLOAD_END:postconditions\nCHECK_VERIFIED:postconditions\n"
                )
            return SqlclResult(0, output, run_dir)

        source_table = make_object_name(self.run.object_prefix, "DTT_SRC")
        copy_table = make_object_name(self.run.object_prefix, "DTT_CPY")
        columns = self._datatype_catalog_columns()
        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(),
                self._catalog_inventory(((source_table, "TABLE"),)),
                self._catalog_inventory(((source_table, "TABLE"), (copy_table, "TABLE"))),
            ]),
            patch("scripts.dev_test_core_types.capture_snapshot", side_effect=[
                self._table_snapshot(source_table, columns),
                self._table_snapshot(copy_table, columns),
            ]),
        ):
            result = run_datatype_roundtrip(self.target, self.context, self.run, runner=mock_runner)
        self.assertEqual(result["id"], "datatype-roundtrip")
        self.assertEqual(result["status"], "PASSED")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["steps"])
        self.assertTrue(result["checks"])

        # Verify fixture names in steps fit 30 bytes
        generated_dir = self.run.inputs_dir / "generated" / "datatype-roundtrip"
        generated_sql = [path.read_text(encoding="utf-8") for path in sorted(generated_dir.glob("*.sql"))]
        self.assertEqual(len(generated_sql), 2)
        self.assertFalse(any("INSERT INTO" in sql.upper() for sql in generated_sql))
        self.assertFalse(any("SELECT * FROM" in sql.upper() for sql in generated_sql))

        # Verify inventory has recorded fixtures
        inv_file = self.run.run_dir / "inventory.json"
        self.assertTrue(inv_file.exists())
        inv_data = json.loads(inv_file.read_text(encoding="utf-8"))
        self.assertEqual(len(inv_data["objects"]), 2)
        self.assertEqual(inv_data["rows"], [])
        for obj in inv_data["objects"]:
            self.assertLessEqual(len(obj["name"].encode("utf-8")), 30)

        generated_dir = self.run.inputs_dir / "generated" / "datatype-roundtrip"
        generated_files = sorted(generated_dir.glob("*.sql"))
        self.assertEqual(len(generated_files), 2)
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.run.events_dir.glob("*.json"))
        ]
        plan_events = [event for event in events if event.get("type") == "plan"]
        step_intents = [event for event in events if event.get("type") == "step-intent"]
        precreation = [event for event in events if event.get("type") == "fixture-precreation"]
        observed_fixtures = [event for event in events if event.get("type") == "fixture-observed"]
        self.assertEqual(len(plan_events), 2)
        self.assertEqual(len(step_intents), 2)
        self.assertEqual(len(precreation), 1)
        self.assertTrue(all(item["absent"] is True for item in precreation[0]["objects"]))
        self.assertEqual(
            {item["name"] for item in precreation[0]["objects"]},
            {source_table, copy_table},
        )
        self.assertEqual(
            {item["name"] for item in observed_fixtures[-1]["objects"]},
            {source_table, copy_table},
        )
        self.assertGreater(events.index(observed_fixtures[-1]), events.index(step_intents[-1]))
        self.assertLess(events.index(plan_events[0]), events.index(step_intents[0]))
        self.assertLess(events.index(plan_events[1]), events.index(step_intents[1]))
        for path, event in zip(generated_files, plan_events, strict=True):
            self.assertEqual(event["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_datatype_source_capture_failure_stops_before_copy(self) -> None:
        """Incomplete catalog capture must not fall back to invented metadata."""
        self._freeze_test_inputs()
        executed: list[str] = []

        def runner(target: Target, driver_path: Path, run_dir: Path, **kwargs: Any) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            executed.append(content)
            output = "ROLLOUT_IDENTITY:DEMO:DEMO\nROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
            if "DEVTEST_STEP_START:" in content:
                step_line = next(line for line in content.splitlines() if "DEVTEST_STEP_START:" in line)
                step_id = step_line.split(":", 1)[1]
                output += f"DEVTEST_STEP_START:{step_id}\nDEVTEST_STEP_COMPLETED:{step_id}\n"
            return SqlclResult(0, output, run_dir)

        source_table = make_object_name(self.run.object_prefix, "DTT_SRC")
        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(), self._catalog_inventory(((source_table, "TABLE"),)),
            ]),
            patch(
                "scripts.dev_test_core_types.capture_snapshot",
                side_effect=RuntimeError("catalog capture incomplete"),
            ),
        ):
            result = run_datatype_roundtrip(self.target, self.context, self.run, runner=runner)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(len(result["steps"]), 1)
        self.assertIn("capture", result.get("error", "").lower())
        self.assertFalse(any("create-copy-table" in driver for driver in executed))

    def test_datatype_step_identity_change_stops_before_copy(self) -> None:
        """A step that reports another database identity must not be followed by a write."""
        self._freeze_test_inputs()
        step_calls: list[str] = []

        def execute(_target: Target, _scenario: Any, step: Mapping[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            step_calls.append(str(step["id"]))
            return {
                "id": step["id"], "status": "PASSED",
                "identity": {
                    "session_user": "DEMO", "current_schema": "DEMO",
                    "db_name": "OTHERDB", "db_unique_name": "OTHERDB",
                    "service_name": "OTHERDB",
                },
                "writeStarted": True,
            }

        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.capture_inventory", return_value=self._catalog_inventory()),
            patch("scripts.dev_test_core_types.execute_step", side_effect=execute),
        ):
            result = run_datatype_roundtrip(self.target, self.context, self.run)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(step_calls, ["create-source-table"])
        self.assertIn("identity", result.get("error", "").lower())

    def test_datatype_catalog_container_drift_stops_before_copy(self) -> None:
        """Catalog captures must remain pinned to the initial full database scope."""
        self._freeze_test_inputs()
        source_table = make_object_name(self.run.object_prefix, "DTT_SRC")
        initial_inventory = self._catalog_inventory()
        drifted_inventory = SchemaInventory(
            {**self._catalog_identity(), "container_id": "4"},
            {
                ObjectKey("DEMO", source_table, "TABLE"): {
                    "owner": "DEMO", "name": source_table, "type": "TABLE",
                    "object_id": 1, "status": "VALID",
                },
            },
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"], "path": "OWNER_SESSION"},
            "2026-10-16T00:00:00+00:00", "2026-10-16T00:00:01+00:00",
        )
        step_calls: list[str] = []

        def step(_target: Target, _scenario: Any, value: Mapping[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            step_calls.append(str(value["id"]))
            return {
                "id": value["id"], "status": "PASSED",
                "identity": {
                    "session_user": "DEMO", "current_schema": "DEMO",
                    "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
                    "service_name": "FREEPDB1",
                },
                "writeStarted": True,
            }

        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[initial_inventory, drifted_inventory]),
            patch("scripts.dev_test_core_types.capture_snapshot") as snapshot,
            patch("scripts.dev_test_core_types.execute_step", side_effect=step),
        ):
            result = run_datatype_roundtrip(self.target, self.context, self.run)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(step_calls, ["create-source-table"])
        snapshot.assert_not_called()
        self.assertIn("scope", result.get("error", "").lower())

    def test_query_check_container_drift_is_unknown(self) -> None:
        """Postcondition checks must match the full initial catalog scope."""
        report = SimpleNamespace(coverage={
            "sessions": 1,
            "sessionIdentities": [{**self._catalog_identity(), "container_id": "4"}],
        })

        with self.assertRaisesRegex(RuntimeError, "scope"):
            _check_identities(report, self.target, self._catalog_identity())

    def test_incomplete_datatype_checks_remain_unknown(self) -> None:
        """An incomplete postcondition report must not be downgraded to a functional failure."""
        self._freeze_test_inputs()
        source_table = make_object_name(self.run.object_prefix, "DTT_SRC")
        copy_table = make_object_name(self.run.object_prefix, "DTT_CPY")
        columns = self._datatype_catalog_columns()
        identity = self._catalog_identity()

        def report(_target: Target, query_checks: list[Any], _directory: Path, **kwargs: Any) -> Any:
            if kwargs.get("phase") == "preconditions":
                return self._check_report(query_checks)
            return SimpleNamespace(
                passed=False, complete=False, results=(), errors=(),
                coverage={"sessions": 1, "sessionIdentities": [identity]},
            )

        def step(_target: Target, _scenario: Any, value: Mapping[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"id": value["id"], "status": "PASSED", "identity": identity, "writeStarted": True}

        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=report),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(),
                self._catalog_inventory(((source_table, "TABLE"),)),
                self._catalog_inventory(((source_table, "TABLE"), (copy_table, "TABLE"))),
            ]),
            patch("scripts.dev_test_core_types.capture_snapshot", side_effect=[
                self._table_snapshot(source_table, columns),
                self._table_snapshot(copy_table, columns),
            ]),
            patch("scripts.dev_test_core_types.execute_step", side_effect=step),
        ):
            result = run_datatype_roundtrip(self.target, self.context, self.run)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("incomplete", result.get("error", "").lower())

    def test_missing_create_privilege_blocks_before_fixture_write(self) -> None:
        """An observed missing owner privilege must stop the scenario before DDL intent."""
        identity = {
            "session_user": "DEMO", "current_schema": "DEMO",
            "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
            "service_name": "FREEPDB1",
        }

        def checks(_target: Target, query_checks: list[Any], _directory: Path, **kwargs: Any) -> Any:
            if kwargs.get("phase") == "preconditions":
                results = [
                    {"id": check.id, "check": check.id, "expected": check.expected,
                     "value": 0 if "create-table" in check.id else 1,
                     "passed": "create-table" not in check.id}
                    for check in query_checks
                ]
                return SimpleNamespace(
                    passed=False, complete=True, results=results, errors=(),
                    coverage={"sessions": 1, "sessionIdentities": [identity]},
                )
            return SimpleNamespace(
                passed=True, complete=True, results=[], errors=(),
                coverage={"sessions": 1, "sessionIdentities": [identity]},
            )

        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=checks),
            patch("scripts.dev_test_core_types.execute_step") as execute,
        ):
            self._freeze_test_inputs()
            result = run_datatype_roundtrip(self.target, self.context, self.run)

        execute.assert_not_called()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("create-table", result.get("error", "").lower())

    def test_datatype_copy_generation_uses_captured_source_columns(self) -> None:
        """The production structure generator must receive the captured source catalog rows."""
        self._freeze_test_inputs()
        source_table = make_object_name(self.run.object_prefix, "DTT_SRC")
        copy_table = make_object_name(self.run.object_prefix, "DTT_CPY")
        captured_columns = self._datatype_catalog_columns()
        initial_inventory = self._catalog_inventory()
        source_inventory = self._catalog_inventory(((source_table, "TABLE"),))
        both_inventory = self._catalog_inventory(((source_table, "TABLE"), (copy_table, "TABLE")))
        source_snapshot = self._table_snapshot(source_table, captured_columns)
        copy_snapshot = self._table_snapshot(copy_table, captured_columns)
        identity = self._catalog_identity()
        step_records: list[dict[str, Any]] = []

        def report(_target: Target, query_checks: list[Any], _directory: Path, **_kwargs: Any) -> Any:
            results = [
                {"id": check.id, "check": check.id, "expected": check.expected,
                 "value": check.expected, "passed": True}
                for check in query_checks
            ]
            return SimpleNamespace(
                passed=True, complete=True, results=results, errors=(),
                coverage={"sessions": 1, "sessionIdentities": [identity]},
            )

        def step(_target: Target, _scenario: Any, value: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            step_records.append(value)
            return {"id": value["id"], "status": "PASSED", "identity": identity, "writeStarted": True}

        with (
            patch("scripts.dev_test_core_types.capture_inventory", create=True, side_effect=[initial_inventory, source_inventory, both_inventory]) as inventories,
            patch("scripts.dev_test_core_types.capture_snapshot", side_effect=[source_snapshot, copy_snapshot]) as snapshots,
            patch("scripts.dev_test_core_types.run_checks", side_effect=report),
            patch("scripts.dev_test_core_types.execute_step", side_effect=step),
            patch("scripts.dev_test_core_types.baseline._structure_bundle", wraps=baseline._structure_bundle) as structure,
        ):
            result = run_datatype_roundtrip(self.target, self.context, self.run)

        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(inventories.call_count, 3)
        self.assertEqual(snapshots.call_count, 2)
        source_catalog = structure.call_args.args[3]
        projected = source_catalog["sections"]["columns"]
        self.assertEqual(len(projected), len(captured_columns))
        for observed, captured in zip(projected, captured_columns, strict=True):
            self.assertEqual(observed["column_id"], captured["column_id"])
            self.assertEqual(observed["data_type"], captured["data_type"])
            self.assertEqual(observed["table_name"], copy_table)
        generated_copy = (self.run.inputs_dir / "generated" / "datatype-roundtrip" / "02-create-copy-table.sql").read_text(encoding="utf-8")
        self.assertIn('"COL_NUM1" NUMBER(10,2)', generated_copy)
        self.assertIn('"COL_NUM2" NUMBER(*,3)', generated_copy)

    def test_compiler_settings_capture_failure_stops_before_checks(self) -> None:
        """Missing settings evidence must remain UNKNOWN rather than use guessed defaults."""
        self._freeze_test_inputs()

        def runner(target: Target, driver_path: Path, run_dir: Path, **kwargs: Any) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            output = "ROLLOUT_IDENTITY:DEMO:DEMO\nROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
            if "DEVTEST_STEP_START:" in content:
                step_line = next(line for line in content.splitlines() if "DEVTEST_STEP_START:" in line)
                step_id = step_line.split(":", 1)[1]
                output += f"DEVTEST_STEP_START:{step_id}\nDEVTEST_STEP_COMPLETED:{step_id}\n"
            return SqlclResult(0, output, run_dir)

        proc_name = make_object_name(self.run.object_prefix, "CS_PROC")
        with (
            patch(
                "scripts.dev_test_core_types.read_rows",
                side_effect=RuntimeError("settings capture unavailable"),
            ),
            patch(
                "scripts.dev_test_core_types.run_checks",
                side_effect=lambda _t, query_checks, _d, **_kw: self._check_report(query_checks),
            ) as check_mock,
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(), self._catalog_inventory(((proc_name, "PROCEDURE"),)),
            ]),
            patch(
                "scripts.dev_test_core_types.capture_snapshot",
                return_value=self._procedure_snapshot(proc_name),
            ),
        ):
            result = run_compiler_settings(self.target, self.context, self.run, runner=runner)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("settings", result.get("error", "").lower())
        self.assertFalse(any(call.kwargs.get("phase") == "postconditions" for call in check_mock.call_args_list))

    def test_compiler_settings_scope_drift_stops_before_recompile(self) -> None:
        """The settings readback must match the container captured for the fixture."""
        self._freeze_test_inputs()
        proc_name = make_object_name(self.run.object_prefix, "CS_PROC")
        readback = self._compiler_settings_readback(None)
        readback["rows"][0]["CONTAINER_ID"] = "4"
        step_calls: list[str] = []

        def step(_target: Target, _scenario: Any, value: Mapping[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            step_calls.append(str(value["id"]))
            return {
                "id": value["id"], "status": "PASSED",
                "identity": self._catalog_identity(), "writeStarted": True,
            }

        with (
            patch("scripts.dev_test_core_types.read_rows", return_value=readback) as read_rows,
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(), self._catalog_inventory(((proc_name, "PROCEDURE"),)),
            ]),
            patch("scripts.dev_test_core_types.capture_snapshot", return_value=self._procedure_snapshot(proc_name)),
            patch("scripts.dev_test_core_types.execute_step", side_effect=step),
        ):
            result = run_compiler_settings(self.target, self.context, self.run)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(step_calls, ["create-stored-unit"])
        self.assertIn("scope", result.get("error", "").lower())
        settings_query = read_rows.call_args.args[1]
        self.assertIn("SYS_CONTEXT('USERENV', 'CON_ID')", settings_query)
        self.assertIn("SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME')", settings_query)
        self.assertIn("DBMS_DB_VERSION.VERSION", settings_query)

    # -----------------------------------------------------------------------
    # 6. Full compiler settings execution adapter tests
    # -----------------------------------------------------------------------

    def test_run_compiler_settings_full_cycle(self) -> None:
        """Test full compiler settings adapter with mock runner."""
        self._freeze_test_inputs()
        executed_statements: list[str] = []

        def mock_runner(target: Target, driver_path: Path, run_dir: Path, **kwargs: Any) -> SqlclResult:
            content = driver_path.read_text(encoding="utf-8")
            executed_statements.append((driver_path.parent / "payload.sql").read_text(encoding="utf-8"))
            phase = kwargs.get("phase", "")
            output = (
                "ROLLOUT_IDENTITY:DEMO:DEMO\n"
                "ROLLOUT_TARGET:FREEPDB1:FREEPDB1:FREEPDB1\n"
            )
            if "DEVTEST_STEP_START" in content:
                step_line = [l for l in content.splitlines() if "DEVTEST_STEP_START:" in l][0]
                step_id = step_line.split(":")[1]
                output += f"DEVTEST_STEP_START:{step_id}\nDEVTEST_STEP_COMPLETED:{step_id}\n"
            elif "QUERY_RESULT_BEGIN" in content or "ALL_PLSQL_OBJECT_SETTINGS" in content:
                # Query settings rows
                settings_rows = [{
                    "PLSQL_OPTIMIZE_LEVEL": 2,
                    "PLSQL_CODE_TYPE": "INTERPRETED",
                    "PLSQL_DEBUG": "FALSE",
                    "PLSQL_WARNINGS": "ENABLE:ALL",
                    "NLS_LENGTH_SEMANTICS": "BYTE",
                    "PLSCOPE_SETTINGS": "IDENTIFIERS:NONE",
                    "PLSQL_CCFLAGS": None,
                }]
                output += (
                    "QUERY_RESULT_BEGIN\n"
                    + json.dumps(settings_rows)
                    + "\nQUERY_RESULT_END\nQUERY_COUNT:1\n"
                )
            elif "l_payload.put('phase'" in content or phase == "postconditions":
                check_ids = re.findall(r"l_result\.put\('id',\s*'([^']+)'\)", content)
                identity = {
                    "session_user": "DEMO", "current_schema": "DEMO",
                    "db_name": "FREEPDB1", "db_unique_name": "FREEPDB1",
                    "service_name": "FREEPDB1", "container_id": "3",
                    "container_name": "FREEPDB1", "edition": "ORA$BASE",
                    "database_version": "23.4",
                }
                payload = {
                    "schemaVersion": 1, "phase": "postconditions", "complete": True,
                    "identity": identity,
                    "results": [
                        {
                            "id": cid,
                            "row_count": 1,
                            "column_count": 1,
                            "numeric": True,
                            "value": 0 if ("wrong" in cid or "mismatch" in cid) else 1,
                        }
                        for cid in check_ids
                    ],
                }
                output += (
                    "CHECK_PAYLOAD_BEGIN:postconditions\n"
                    + json.dumps(payload)
                    + "\nCHECK_PAYLOAD_END:postconditions\nCHECK_VERIFIED:postconditions\n"
                )
            return SqlclResult(0, output, run_dir)

        proc_name = make_object_name(self.run.object_prefix, "CS_PROC")
        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.read_rows", side_effect=[
                self._compiler_settings_readback(None),
                self._compiler_settings_readback("DEVTEST_FLAG:TRUE"),
            ]),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(),
                self._catalog_inventory(((proc_name, "PROCEDURE"),)),
                self._catalog_inventory(((proc_name, "PROCEDURE"),)),
            ]),
            patch("scripts.dev_test_core_types.capture_snapshot", side_effect=[
                self._procedure_snapshot(proc_name),
                self._procedure_snapshot(proc_name),
            ]),
        ):
            result = run_compiler_settings(self.target, self.context, self.run, runner=mock_runner)
        self.assertEqual(result["id"], "compiler-settings")
        self.assertEqual(result["status"], "PASSED")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["steps"])
        self.assertTrue(result["checks"])

        # Verify inventory has recorded the unit
        inv_file = self.run.run_dir / "inventory.json"
        self.assertTrue(inv_file.exists())
        inv_data = json.loads(inv_file.read_text(encoding="utf-8"))
        proc_objs = [o for o in inv_data["objects"] if o["type"] == "PROCEDURE"]
        self.assertEqual(len(proc_objs), 1)
        self.assertLessEqual(len(proc_objs[0]["name"].encode("utf-8")), 30)
        self.assertNotIn("CREATE OR REPLACE", executed_statements[0].upper())

    def test_compiler_settings_transitions_nonnull_ccflags_to_null(self) -> None:
        """The NULL CCFLAGS branch is exercised even when the unit starts non-NULL."""
        self._freeze_test_inputs()
        proc_name = make_object_name(self.run.object_prefix, "CS_PROC")
        identity = self._catalog_identity()
        generated_steps: list[dict[str, Any]] = []

        def execute(_target: Target, _scenario: Any, value: Mapping[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            generated_steps.append(dict(value))
            return {"id": value["id"], "status": "PASSED", "identity": identity, "writeStarted": True}

        with (
            patch("scripts.dev_test_core_types.run_checks", side_effect=lambda _t, checks, _d, **_kw: self._check_report(checks)),
            patch("scripts.dev_test_core_types.read_rows", side_effect=[
                self._compiler_settings_readback("USER_FLAG:TRUE"),
                self._compiler_settings_readback(None),
            ]),
            patch("scripts.dev_test_core_types.capture_inventory", side_effect=[
                self._catalog_inventory(),
                self._catalog_inventory(((proc_name, "PROCEDURE"),)),
                self._catalog_inventory(((proc_name, "PROCEDURE"),)),
            ]),
            patch("scripts.dev_test_core_types.capture_snapshot", side_effect=[
                self._procedure_snapshot(proc_name), self._procedure_snapshot(proc_name),
            ]),
            patch("scripts.dev_test_core_types.execute_step", side_effect=execute),
        ):
            result = run_compiler_settings(self.target, self.context, self.run)

        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(len(generated_steps), 2)
        recompile_sql = (self.run.inputs_dir / "generated" / "compiler-settings" / "02-recompile-with-ccflags.sql").read_text(encoding="utf-8")
        self.assertIn("ALTER SESSION SET PLSQL_CCFLAGS = '';", recompile_sql)


if __name__ == "__main__":
    unittest.main()
