"""Tests for DEV test target planning and prerequisite inspection (Task 3)."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from scripts.dev_test_core import builtin_suite
from scripts.dev_test_evidence import create_run, make_object_name
from scripts.dev_test_manifest import (
    Scenario,
    Suite,
    load_suite,
)
from scripts.dev_test_target import (
    DevTestTargetError,
    assert_saved_target,
    inspect_suite,
    _gather_input_paths,
    _parse_planning_output,
)
from scripts.migration_checks import CheckReport, OBSERVED_IDENTITY_FIELDS
from scripts.sqlcl_session import SqlclResult


def _make_dummy_scenario(
    scenario_id: str = "sc-1",
    description: str = "Test scenario",
    depends_on: tuple[str, ...] = (),
    objects_req: tuple[MappingProxyType, ...] = (),
    checks_req: tuple[MappingProxyType, ...] = (),
    fixture_objects: tuple[MappingProxyType, ...] = (),
    step_sql: str = "SELECT 1 FROM dual;",
    step_id: str = "step-1",
    check_id: str = "chk-1",
    check_sql: str = "SELECT 1 FROM dual",
    check_expected: int = 1,
    suite_dir: Path | None = None,
) -> Scenario:
    if suite_dir is None:
        raise ValueError("suite_dir is required")
    sql_path = suite_dir / f"{scenario_id}_{step_id}.sql"
    sql_bytes = step_sql.encode("utf-8")
    sql_path.write_bytes(sql_bytes)
    sha256 = hashlib.sha256(sql_bytes).hexdigest()

    step = MappingProxyType({
        "id": step_id,
        "sqlFile": sql_path.name,
        "path": sql_path,
        "source": sql_bytes,
        "sha256": sha256,
        "expected": MappingProxyType({"kind": "success"}),
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
        "selectors": (),
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
        observations=(),
        cleanup=None,
    )


def _make_dummy_suite(
    suite_dir: Path,
    suite_id: str = "test-suite",
    scenarios: tuple[Scenario, ...] = (),
) -> Suite:
    manifest_path = suite_dir / "suite.json"
    manifest_data = {
        "schemaVersion": 1,
        "id": suite_id,
        "scenarios": [
            {
                "id": s.id,
                "description": s.description,
                "dependsOn": list(s.dependsOn),
                "requirements": {
                    "objects": [dict(o) for o in s.requirements["objects"]],
                    "checks": [dict(c) for c in s.requirements["checks"]],
                },
                "effects": dict(s.effects),
                "fixtures": {
                    "objects": [dict(o) for o in s.fixtures["objects"]],
                    "selectors": [],
                },
                "steps": [
                    {
                        "id": st["id"],
                        "sqlFile": st["sqlFile"],
                        "expected": dict(st["expected"]),
                    }
                    for st in s.steps
                ],
                "checks": [
                    {
                        "id": c["id"],
                        "sql": c["sql"],
                        "expected": c["expected"],
                    }
                    for c in s.checks
                ],
                "observations": [],
                "cleanup": None,
            }
            for s in scenarios
        ],
    }
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    return Suite(
        schemaVersion=1,
        id=suite_id,
        root=suite_dir,
        scenarios=scenarios,
    )


def _base_valid_identity(schema: str = "DEMO") -> dict[str, str]:
    return {
        "session_user": schema,
        "current_schema": schema,
        "db_name": "DEMODB",
        "db_unique_name": "DEMODB_DEV",
        "service_name": "demodev",
        "container_id": "0",
        "container_name": "NON-CDB",
        "edition": "ORA$BASE",
        "database_version": "23.6",
    }


def _make_fake_runner(
    identity: dict | None = None,
    apex_version: str = "26.2.0",
    existing_objects: list[dict] | None = None,
    members: list[dict] | None = None,
    check_results: list[dict] | None = None,
    returncode: int = 0,
    fail_on_read: bool = False,
    space_advisory: dict | None = None,
):
    recorded_drivers: list[str] = []

    def runner(target, driver_path: Path, run_dir: Path, **kwargs):
        content = driver_path.read_text(encoding="utf-8")
        recorded_drivers.append(content)
        if fail_on_read:
            return SqlclResult(1, "SP2-0000: connection failed", run_dir)
        if returncode != 0:
            return SqlclResult(returncode, "SQLcl failed", run_dir)

        payload = {
            "schemaVersion": 1,
            "identity": identity if identity is not None else _base_valid_identity(target.schema),
            "apexVersion": apex_version,
            "existingObjects": existing_objects or [],
            "members": members or [],
            "checkResults": check_results or [],
            "spaceAdvisory": space_advisory or {"dba_space_visible": False, "status": "advisory"},
        }
        output = (
            "PLAN_PAYLOAD_BEGIN:plan\n"
            + json.dumps(payload)
            + "\nPLAN_PAYLOAD_END:plan\nPLAN_VERIFIED:plan\n"
        )
        return SqlclResult(0, output, run_dir)

    runner.recorded_drivers = recorded_drivers
    return runner


class TestDevTestTarget(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.suite_dir = self.root / "suite"
        self.suite_dir.mkdir(parents=True, exist_ok=True)
        self.run = create_run(self.root)
        self.base_env = {
            "PROJECT_NAME": "apex-team-template",
            "DEVELOPER_NAME": "ASH",
            "DB_ENVIRONMENT": "development",
            "APEX_APP_ID": "100",
            "CODE_SCHEMA": "DEMO",
            "CODE_PREFIXES": "*",
            "CODE_SQLCL_CONNECTION": "docker-demo",
            "CODE_EXPECTED_USER": "DEMO",
            "TABLES_SCHEMA": "DEMO",
            "TABLES_PREFIXES": "*",
            "TABLES_SQLCL_CONNECTION": "docker-demo",
            "TABLES_EXPECTED_USER": "DEMO",
            "APEX_PARSING_SCHEMA": "DEMO",
            "APEX_SQLCL_CONNECTION": "docker-demo",
            "APEX_EXPECTED_USER": "DEMO",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_multi_owner_explicit_selection(self):
        """When multiple schemas configured, None requires explicit selection; valid selection succeeds."""
        env = dict(self.base_env)
        env["CODE_SCHEMA"] = "DEMO,APP"
        env["CODE_SQLCL_CONNECTION"] = "docker-demo,docker-app"
        env["CODE_EXPECTED_USER"] = "DEMO,APP"

        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))

        runner = _make_fake_runner(identity=_base_valid_identity("DEMO"))
        # None raises DevTestTargetError asking to name one
        with self.assertRaises(DevTestTargetError) as ctx:
            inspect_suite(env, None, suite, self.run, runner=runner)
        self.assertIn("--schema", str(ctx.exception))

        # Explicit valid schema succeeds
        report = inspect_suite(env, "DEMO", suite, self.run, runner=runner)
        self.assertEqual(report["target"]["configured"]["schema"], "DEMO")

        # Unknown schema raises DevTestTargetError
        with self.assertRaises(DevTestTargetError):
            inspect_suite(env, "UNKNOWN", suite, self.run, runner=runner)

    def test_dev_classification_refusal(self):
        """DB_ENVIRONMENT other than development/test is refused."""
        for env_val in ["production", "staging", "other"]:
            env = dict(self.base_env)
            env["DB_ENVIRONMENT"] = env_val
            sc = _make_dummy_scenario(suite_dir=self.suite_dir)
            suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
            runner = _make_fake_runner()
            with self.assertRaises(DevTestTargetError):
                inspect_suite(env, "DEMO", suite, self.run, runner=runner)

    def test_production_looking_target_refusal(self):
        """Production-resembling connection or database identity is refused."""
        # Configured alias looks like production
        env = dict(self.base_env)
        env["CODE_SQLCL_CONNECTION"] = "erp-prod-db"
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        runner = _make_fake_runner()
        with self.assertRaises(DevTestTargetError):
            inspect_suite(env, "DEMO", suite, self.run, runner=runner)

        # Observed live identity resembles production
        for prod_id_key in ["db_name", "db_unique_name", "service_name", "container_name"]:
            bad_identity = _base_valid_identity("DEMO")
            bad_identity[prod_id_key] = "ERPPROD"
            bad_runner = _make_fake_runner(identity=bad_identity)
            with self.assertRaises(DevTestTargetError):
                inspect_suite(self.base_env, "DEMO", suite, self.run, runner=bad_runner)

    def test_owner_login_equality_and_no_dba_fallback(self):
        """Expected user and observed session user must equal selected schema owner."""
        # Configured expected user does not equal schema
        env = dict(self.base_env)
        env["CODE_EXPECTED_USER"] = "DBA_ADMIN"
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        runner = _make_fake_runner()
        with self.assertRaises(DevTestTargetError) as ctx:
            inspect_suite(env, "DEMO", suite, self.run, runner=runner)
        self.assertIn("session_user", str(ctx.exception).lower())

        # Observed session user does not equal target schema
        bad_identity = _base_valid_identity("DEMO")
        bad_identity["session_user"] = "SYS"
        runner_sys = _make_fake_runner(identity=bad_identity)
        with self.assertRaises(DevTestTargetError) as ctx2:
            inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner_sys)
        self.assertIn("session_user", str(ctx2.exception).lower())

    def test_alias_repoint_is_not_same_target(self):
        """assert_saved_target raises DevTestTargetError when any required identity field differs or is missing."""
        saved = _base_valid_identity("DEMO")

        # Identity changes in separate cases
        mutations = [
            ("db_unique_name", "DEMODB_STAGE"),
            ("service_name", "other_service"),
            ("container_id", "1"),
            ("container_name", "PDB1"),
            ("edition", "NEW_EDITION"),
            ("session_user", "OTHER_USER"),
            ("current_schema", "OTHER_SCHEMA"),
            ("db_name", "OTHERDB"),
            ("database_version", "19.0"),
        ]
        for field, new_val in mutations:
            observed = dict(saved)
            observed[field] = new_val
            with self.assertRaises(DevTestTargetError, msg=f"Should fail for {field}"):
                assert_saved_target(saved, observed)

        # Missing field in saved
        for field in OBSERVED_IDENTITY_FIELDS:
            incomplete_saved = dict(saved)
            del incomplete_saved[field]
            with self.assertRaises(DevTestTargetError, msg=f"Should fail when saved missing {field}"):
                assert_saved_target(incomplete_saved, saved)

        # Missing or empty field in observed
        for field in OBSERVED_IDENTITY_FIELDS:
            incomplete_observed = dict(saved)
            incomplete_observed[field] = ""
            with self.assertRaises(DevTestTargetError, msg=f"Should fail when observed empty {field}"):
                assert_saved_target(saved, incomplete_observed)

        # Exact match succeeds
        self.assertIsNone(assert_saved_target(saved, dict(saved)))

    def test_no_dba_space_visibility_is_advisory(self):
        """Absence of DBA free-space views is advisory and plan remains eligible."""
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))

        runner = _make_fake_runner(
            space_advisory={"dba_space_visible": False, "status": "advisory_unavailable"}
        )
        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        self.assertTrue(report["coverage"]["complete"])
        self.assertTrue(report["coverage"]["eligible"])
        self.assertTrue(report["scenarios"][0]["eligible"])
        self.assertEqual(report["scenarios"][0]["status"], "NOT_RUN")
        # Limitation noted
        self.assertTrue(any("space" in lim.lower() for lim in report.get("limitations", [])))

    def test_missing_dependency_is_not_auto_repaired(self):
        """Missing prerequisite yields BLOCKED with no automatic repair write; dependent is NOT_RUN."""
        req_obj = MappingProxyType({
            "owner": "@selected",
            "name": "MISSING_TABLE",
            "type": "TABLE",
            "requiredStatus": "VALID",
            "member": None,
        })
        sc1 = _make_dummy_scenario(
            scenario_id="sc-1",
            objects_req=(req_obj,),
            suite_dir=self.suite_dir,
        )
        sc2 = _make_dummy_scenario(
            scenario_id="sc-2",
            depends_on=("sc-1",),
            suite_dir=self.suite_dir,
        )
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc1, sc2))

        # Runner reports MISSING_TABLE is not in existing objects
        runner = _make_fake_runner(existing_objects=[])
        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        self.assertTrue(report["coverage"]["complete"])
        self.assertFalse(report["coverage"]["eligible"])

        # sc1 is BLOCKED
        sc1_rep = next(s for s in report["scenarios"] if s["id"] == "sc-1")
        self.assertEqual(sc1_rep["status"], "BLOCKED")
        self.assertFalse(sc1_rep["eligible"])

        # sc2 is NOT_RUN (dependency not eligible)
        sc2_rep = next(s for s in report["scenarios"] if s["id"] == "sc-2")
        self.assertEqual(sc2_rep["status"], "NOT_RUN")
        self.assertFalse(sc2_rep["eligible"])

        # Spy asserts no repair SQL was sent
        for driver in runner.recorded_drivers:
            self.assertNotIn("CREATE TABLE", driver.upper())
            self.assertNotIn("ALTER TABLE", driver.upper())

    def test_member_invisibility_is_unknown_not_fabricated_absence(self):
        """When an object exists but requested member is invisible, prerequisite status is unknown, not missing."""
        req_obj = MappingProxyType({
            "owner": "@selected",
            "name": "CORE_PKG",
            "type": "PACKAGE",
            "requiredStatus": "VALID",
            "member": "HIDDEN_PROC",
        })
        sc = _make_dummy_scenario(
            scenario_id="sc-pkg",
            objects_req=(req_obj,),
            suite_dir=self.suite_dir,
        )
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))

        # Object CORE_PKG exists, but member HIDDEN_PROC is invisible
        runner = _make_fake_runner(
            existing_objects=[{"owner": "DEMO", "name": "CORE_PKG", "type": "PACKAGE", "status": "VALID"}],
            members=[],  # Not returned in catalog
        )
        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        sc_rep = report["scenarios"][0]
        self.assertEqual(sc_rep["status"], "BLOCKED")
        self.assertFalse(sc_rep["eligible"])

        prereq_ev = report["prerequisites"]
        self.assertTrue(len(prereq_ev) > 0)
        member_ev = next((p for p in prereq_ev if p.get("member") == "HIDDEN_PROC"), None)
        self.assertIsNotNone(member_ev)
        self.assertEqual(member_ev["status"], "unknown")

    def test_existing_fixture_name_collision_blocks_creation(self):
        """When planned fixture object already exists in database, scenario is BLOCKED."""
        fixture_obj = MappingProxyType({
            "binding": "items",
            "suffix": "ITEMS",
            "type": "TABLE",
        })
        sc = _make_dummy_scenario(
            scenario_id="sc-coll",
            fixture_objects=(fixture_obj,),
            suite_dir=self.suite_dir,
        )
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))

        # Deterministic prefix for run_id + scenario.id
        prefix = f"DT_{hashlib.sha256((self.run.run_id + 'sc-coll').encode('utf-8')).hexdigest()[:12].upper()}_"
        colliding_name = make_object_name(prefix, "ITEMS")

        # Runner reports this colliding object already exists
        runner = _make_fake_runner(
            existing_objects=[{"owner": "DEMO", "name": colliding_name, "type": "TABLE", "status": "VALID"}]
        )
        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        sc_rep = report["scenarios"][0]
        self.assertEqual(sc_rep["status"], "BLOCKED")
        self.assertFalse(sc_rep["eligible"])
        self.assertFalse(report["coverage"]["eligible"])
        self.assertIn("collision", str(sc_rep).lower())

    def test_plan_spies_assert_no_suite_sql_apex_session_grants_ddl_dml(self):
        """Planning driver must not execute suite SQL, APEX session creation, grants, DDL, or DML."""
        unique_marker = "SELECT /* UNIQUE_SUITE_PAYLOAD_MARKER_998877 */ 1 FROM dual"
        sc = _make_dummy_scenario(
            step_sql=unique_marker,
            suite_dir=self.suite_dir,
        )
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        runner = _make_fake_runner()

        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)
        self.assertTrue(report["coverage"]["complete"])

        self.assertTrue(len(runner.recorded_drivers) > 0)
        for driver in runner.recorded_drivers:
            # No suite step SQL executed
            self.assertNotIn("UNIQUE_SUITE_PAYLOAD_MARKER_998877", driver)
            # No APEX session creation
            self.assertNotIn("APEX_SESSION", driver.upper())
            self.assertNotIn("SET_WORKSPACE", driver.upper())
            # No grants or revokes
            self.assertNotRegex(driver, re.compile(r"\bGRANT\b", re.IGNORECASE))
            self.assertNotRegex(driver, re.compile(r"\bREVOKE\b", re.IGNORECASE))
            # No DDL
            self.assertNotRegex(driver, re.compile(r"\bCREATE\s+(TABLE|VIEW|PACKAGE|PROCEDURE|FUNCTION|USER)\b", re.IGNORECASE))
            self.assertNotRegex(driver, re.compile(r"\bDROP\s+(TABLE|VIEW|PACKAGE|PROCEDURE|FUNCTION|USER)\b", re.IGNORECASE))
            self.assertNotRegex(driver, re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE))
            # No DML
            self.assertNotRegex(driver, re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE))
            self.assertNotRegex(driver, re.compile(r"\bUPDATE\s+[A-Z0-9_$#]+\s+SET\b", re.IGNORECASE))
            self.assertNotRegex(driver, re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE))

    def test_empty_identity_refused(self):
        """Empty or incomplete target identity raises DevTestTargetError."""
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))

        # Empty identity
        runner_empty = _make_fake_runner(identity={})
        with self.assertRaises(DevTestTargetError):
            inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner_empty)

        # Incomplete identity (missing container_name)
        incomplete = _base_valid_identity("DEMO")
        del incomplete["container_name"]
        runner_inc = _make_fake_runner(identity=incomplete)
        with self.assertRaises(DevTestTargetError):
            inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner_inc)

    def test_deterministic_per_scenario_prefix(self):
        """Deterministic prefix per scenario DT_<SHA256(run_id+scenario.id) first12 uppercase>_."""
        fix1 = MappingProxyType({"binding": "items", "suffix": "ITEMS", "type": "TABLE"})
        fix2 = MappingProxyType({"binding": "items", "suffix": "ITEMS", "type": "TABLE"})
        sc1 = _make_dummy_scenario(scenario_id="sc-alpha", fixture_objects=(fix1,), suite_dir=self.suite_dir)
        sc2 = _make_dummy_scenario(scenario_id="sc-beta", fixture_objects=(fix2,), suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc1, sc2))

        runner = _make_fake_runner()
        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        sc1_rep = next(s for s in report["scenarios"] if s["id"] == "sc-alpha")
        sc2_rep = next(s for s in report["scenarios"] if s["id"] == "sc-beta")

        prefix1 = f"DT_{hashlib.sha256((self.run.run_id + 'sc-alpha').encode('utf-8')).hexdigest()[:12].upper()}_"
        prefix2 = f"DT_{hashlib.sha256((self.run.run_id + 'sc-beta').encode('utf-8')).hexdigest()[:12].upper()}_"
        self.assertNotEqual(prefix1, prefix2)

        name1 = sc1_rep["plannedObjects"]["items"]
        name2 = sc2_rep["plannedObjects"]["items"]
        self.assertEqual(name1, f"{prefix1}ITEMS")
        self.assertEqual(name2, f"{prefix2}ITEMS")
        self.assertNotEqual(name1, name2)

        # Inventory objects must remain empty in planning (never labeled created)
        self.assertEqual(report["inventory"]["objects"], [])

    def test_origin_provenance(self):
        """Origin argument is passed to report and preserved."""
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        runner = _make_fake_runner()

        rep_proj = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner, origin="project")
        self.assertEqual(rep_proj["selection"]["origin"], "project")

        rep_builtin = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner, origin="builtin")
        self.assertEqual(rep_builtin["selection"]["origin"], "builtin")

    def test_pre_23_database_blocks_boolean_builtin_during_plan(self):
        """The SQL BOOLEAN fixture is ineligible on Oracle releases before 23ai."""
        builtin = next(sc for sc in builtin_suite().scenarios if sc.id == "datatype-roundtrip")
        scenario = _make_dummy_scenario(
            scenario_id="datatype-roundtrip",
            checks_req=builtin.requirements["checks"],
            fixture_objects=builtin.fixtures["objects"],
            suite_dir=self.suite_dir,
        )
        suite = _make_dummy_suite(self.suite_dir, scenarios=(scenario,))
        identity = _base_valid_identity("DEMO")
        identity["database_version"] = "19.0"

        def failed_boolean_check(_target, checks, *_args, **_kwargs):
            ids = [check.id for check in checks]
            results = [
                {
                    "id": check.id,
                    "passed": "DBMS_DB_VERSION.VERSION" not in check.sql,
                    "value": 0 if "DBMS_DB_VERSION.VERSION" in check.sql else 1,
                }
                for check in checks
            ]
            return CheckReport(
                passed=False,
                complete=True,
                results=tuple(results),
                errors=(),
                coverage={
                    "sessionResultCounts": [len(ids)],
                    "sessionCheckIds": [ids],
                    "sessionIdentities": [identity],
                },
            )

        with patch("scripts.dev_test_target.run_checks", side_effect=failed_boolean_check):
            report = inspect_suite(
                self.base_env,
                "DEMO",
                suite,
                self.run,
                runner=_make_fake_runner(identity=identity),
            )

        result = next(item for item in report["scenarios"] if item["id"] == "datatype-roundtrip")
        self.assertFalse(result["eligible"])
        self.assertEqual("BLOCKED", result["status"])
        self.assertTrue(any(
            item.get("scenarioId") == "datatype-roundtrip"
            and item.get("checkId") == "req-sql-boolean-23ai"
            and item.get("status") == "missing"
            for item in report["prerequisites"]
        ))

    def test_current_schema_mismatch_refuses_planning(self):
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        observed = _base_valid_identity("DEMO")
        observed["current_schema"] = "OTHER_SCHEMA"

        with self.assertRaises(DevTestTargetError):
            inspect_suite(
                self.base_env,
                "DEMO",
                suite,
                self.run,
                runner=_make_fake_runner(identity=observed),
            )

    def test_custom_suite_manifest_is_frozen_as_an_input(self):
        sc = _make_dummy_scenario(suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        suite_path = suite.root / "suite.json"
        custom_path = suite.root / "orders.json"
        suite_path.rename(custom_path)

        loaded = load_suite(custom_path)

        self.assertIn(custom_path.resolve(), {path.resolve() for path in _gather_input_paths(loaded)})

    def test_planning_queries_members_and_runs_declared_requirement_checks(self):
        object_req = MappingProxyType({
            "owner": "@selected",
            "name": "CORE_PKG",
            "type": "PACKAGE",
            "requiredStatus": "VALID",
            "member": "READY",
        })
        check_req = MappingProxyType({
            "id": "has-capability",
            "sql": "SELECT 1 FROM dual",
            "expected": 1,
        })
        sc = _make_dummy_scenario(
            scenario_id="check-prerequisite",
            objects_req=(object_req,),
            checks_req=(check_req,),
            suite_dir=self.suite_dir,
        )
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))
        identity = _base_valid_identity("DEMO")
        check_identity = identity
        check_returncode = 0
        recorded: list[tuple[str, str]] = []

        def runner(target, driver_path: Path, run_dir: Path, **kwargs):
            driver = driver_path.read_text(encoding="utf-8")
            recorded.append((driver_path.name, driver))
            if driver_path.name == "migration-checks.sql":
                payload = {
                    "schemaVersion": 1,
                    "phase": "preconditions",
                    "complete": True,
                    "identity": check_identity,
                    "results": [{
                        "id": "req-00000001",
                        "row_count": 1,
                        "column_count": 1,
                        "numeric": True,
                        "value": 1,
                    }],
                }
                output = (
                    "CHECK_PAYLOAD_BEGIN:preconditions\n"
                    + json.dumps(payload)
                    + "\nCHECK_PAYLOAD_END:preconditions\nCHECK_VERIFIED:preconditions\n"
                )
            else:
                payload = {
                    "schemaVersion": 1,
                    "identity": identity,
                    "apexVersion": "26.2.0",
                    "existingObjects": [{
                        "owner": "DEMO",
                        "name": "CORE_PKG",
                        "type": "PACKAGE",
                        "status": "VALID",
                    }],
                    "members": [{"owner": "DEMO", "name": "CORE_PKG", "member": "READY"}],
                    "spaceAdvisory": {"dba_space_visible": False, "status": "advisory"},
                }
                output = (
                    "PLAN_PAYLOAD_BEGIN:plan\n"
                    + json.dumps(payload)
                    + "\nPLAN_PAYLOAD_END:plan\nPLAN_VERIFIED:plan\n"
                )
            return SqlclResult(check_returncode if driver_path.name == "migration-checks.sql" else 0, output, run_dir)

        report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        plan_driver = next(driver for name, driver in recorded if name == "dev-test-plan-driver.sql")
        check_driver = next(driver for name, driver in recorded if name == "migration-checks.sql")
        self.assertIn("ALL_PROCEDURES", plan_driver)
        self.assertIn("PROCEDURE_NAME", plan_driver)
        self.assertIn("OWNER = l_owner_1", plan_driver)
        self.assertIn("OWNER = l_member_owner_1", plan_driver)
        self.assertIn("CORE_PKG", plan_driver)
        self.assertIn("READY", plan_driver)
        self.assertIn(check_req["sql"].encode("utf-8").hex().upper(), check_driver)
        check_evidence = next(
            item for item in report["prerequisites"] if item.get("checkId") == "has-capability"
        )
        self.assertEqual(check_evidence["status"], "satisfied")

        check_identity = {**identity, "db_name": "OTHERDB"}
        with self.assertRaises(DevTestTargetError):
            inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)

        check_identity = identity
        check_returncode = 1
        incomplete_report = inspect_suite(self.base_env, "DEMO", suite, self.run, runner=runner)
        check_evidence = next(
            item for item in incomplete_report["prerequisites"] if item.get("checkId") == "has-capability"
        )
        self.assertEqual(check_evidence["status"], "unknown")

    def test_missing_external_owner_object_is_unknown(self):
        object_req = MappingProxyType({
            "owner": "PARENT",
            "name": "CORE_PKG",
            "type": "PACKAGE",
            "requiredStatus": "VALID",
            "member": None,
        })
        sc = _make_dummy_scenario(objects_req=(object_req,), suite_dir=self.suite_dir)
        suite = _make_dummy_suite(self.suite_dir, scenarios=(sc,))

        report = inspect_suite(
            self.base_env,
            "DEMO",
            suite,
            self.run,
            runner=_make_fake_runner(),
        )

        self.assertEqual(report["prerequisites"][0]["status"], "unknown")

    def test_duplicate_planning_frames_are_rejected(self):
        payload = json.dumps({"schemaVersion": 1, "identity": _base_valid_identity()})
        frame = (
            "PLAN_PAYLOAD_BEGIN:plan\n"
            + payload
            + "\nPLAN_PAYLOAD_END:plan\nPLAN_VERIFIED:plan\n"
        )

        with self.assertRaises(DevTestTargetError):
            _parse_planning_output(frame + frame)


if __name__ == "__main__":
    unittest.main()
