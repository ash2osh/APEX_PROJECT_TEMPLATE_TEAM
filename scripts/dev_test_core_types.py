"""Built-in datatype and compiler-settings qualification adapters (Task 6)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts import baseline
from scripts.db_targets import ORACLE_IDENTIFIER, Target, looks_like_production_identity
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_core import builtin_suite
from scripts.dev_test_evidence import (
    RunEvidence,
    append_event,
    assert_inputs_unchanged,
    make_object_name,
    save_inventory,
)
from scripts.dev_test_runtime import (
    _same_live_target,
    _target_is_dev,
    execute_step,
    read_rows,
)
from scripts.migration_checks import QueryCheck, run_checks
from scripts.schema_catalog import (
    CatalogError,
    ObjectDefinition,
    ObjectKey,
    SchemaInventory,
    SchemaSnapshot,
    capture_inventory,
    capture_snapshot,
    same_database_scope,
)
from scripts.sqlcl_session import run_sqlcl


# These are the requested source-fixture types. Copy DDL and postconditions are
# derived from captured Oracle rows, not from this declaration.
DATATYPE_COLUMNS: tuple[dict[str, Any], ...] = (
    {"name": "COL_BOOL", "data_type": "BOOLEAN"},
    {"name": "COL_NUM1", "data_type": "NUMBER", "data_precision": 10, "data_scale": 2},
    {"name": "COL_NUM2", "data_type": "NUMBER", "data_precision": None, "data_scale": 3},
    {"name": "COL_FLOAT", "data_type": "FLOAT", "data_precision": 63, "data_scale": None},
    {"name": "COL_VC", "data_type": "VARCHAR2", "char_length": 8, "char_used": "C"},
    {"name": "COL_RAW", "data_type": "RAW", "data_length": 16},
)

_COMPILER_SETTING_COLUMNS = (
    "PLSQL_OPTIMIZE_LEVEL",
    "PLSQL_CODE_TYPE",
    "PLSQL_DEBUG",
    "PLSQL_WARNINGS",
    "NLS_LENGTH_SEMANTICS",
    "PLSCOPE_SETTINGS",
    "CCFLAGS_STATUS",
    "PLSQL_CCFLAGS",
    "CONTAINER_ID",
    "CONTAINER_NAME",
    "EDITION",
    "DATABASE_VERSION",
)
_DATABASE_SCOPE_FIELDS = ("db_unique_name", "container_id", "container_name", "current_schema", "edition")
_NULLABILITY_FALSE = {"N", "NO", "FALSE", "0"}


class _ScenarioProblem(RuntimeError):
    def __init__(self, status: str, message: str, records: Sequence[Mapping[str, Any]] = ()) -> None:
        if status not in {"BLOCKED", "FAILED", "UNKNOWN"}:
            raise ValueError(f"unsupported scenario problem status: {status}")
        self.status = status
        self.records = [dict(record) for record in records]
        super().__init__(message)


class _FixtureAbsent(RuntimeError):
    """A complete owner inventory proved that an expected fixture is absent."""


def _get_builtin_scenario(scenario_id: str) -> Any:
    suite = builtin_suite()
    return next(scenario for scenario in suite.scenarios if scenario.id == scenario_id)


def _outcome(
    scenario: Any,
    status: str,
    *,
    steps: Sequence[Mapping[str, Any]] = (),
    checks: Sequence[Mapping[str, Any]] = (),
    error: str | None = None,
    eligible: bool = True,
) -> dict[str, Any]:
    result = {
        "id": scenario.id,
        "status": status,
        "eligible": eligible,
        "steps": [dict(step) for step in steps],
        "checks": [dict(check) for check in checks],
        "observations": [],
        "effects": dict(scenario.effects),
    }
    if error:
        result["error"] = error
    return result


def _validate_fixture_name(name: object, label: str) -> str:
    if not isinstance(name, str) or ORACLE_IDENTIFIER.fullmatch(name) is None:
        raise _ScenarioProblem("BLOCKED", f"{label} must be an uppercase Oracle identifier")
    if len(name.encode("utf-8")) > 30:
        raise _ScenarioProblem("BLOCKED", f"{label} exceeds the 30-byte Oracle identifier budget")
    return name


def _identity_matches_target(identity: Mapping[str, Any], target: Target) -> bool:
    required = (
        "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    )
    if any(not isinstance(identity.get(key), str) or not identity[key].strip() for key in required):
        return False
    if identity["session_user"].upper() != target.expected_user.upper():
        return False
    if identity["current_schema"].upper() != target.schema.upper():
        return False
    return target.environment == "prod" or not looks_like_production_identity(
        identity["db_name"], identity["db_unique_name"], identity["service_name"],
    )


def _same_scenario_identity(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    if not _same_live_target(expected, observed):
        return False
    if all(field in expected for field in _DATABASE_SCOPE_FIELDS):
        if not all(field in observed for field in _DATABASE_SCOPE_FIELDS):
            return False
        if not same_database_scope(expected, observed):
            return False
        if expected.get("database_version") != observed.get("database_version"):
            return False
    return True


def _verify_catalog_identity(
    identity: object,
    target: Target,
    expected_identity: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(identity, Mapping) or not _identity_matches_target(identity, target):
        raise CatalogError("catalog capture did not verify the selected DEV owner and database identity")
    if expected_identity is not None:
        if not _same_scenario_identity(expected_identity, identity):
            raise CatalogError("catalog capture observed a different live target identity or database scope during this scenario")
    return identity


def _check_identities(
    report: Any,
    target: Target,
    expected_identity: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    coverage = getattr(report, "coverage", None)
    if not isinstance(coverage, Mapping):
        raise _ScenarioProblem("UNKNOWN", "Checks did not return verified session identity coverage")
    identities = coverage.get("sessionIdentities")
    sessions = coverage.get("sessions")
    if (
        not isinstance(identities, list)
        or not identities
        or type(sessions) is not int
        or sessions != len(identities)
        or any(not isinstance(identity, Mapping) for identity in identities)
    ):
        raise _ScenarioProblem("UNKNOWN", "Checks did not verify identity for every SQLcl session")
    if any(not _identity_matches_target(identity, target) for identity in identities):
        raise _ScenarioProblem("UNKNOWN", "Checks observed an unexpected owner or non-DEV database identity")
    if expected_identity is not None and any(
        not _same_scenario_identity(expected_identity, identity) for identity in identities
    ):
        raise _ScenarioProblem("UNKNOWN", "Checks observed a different live database identity or scope during this scenario")
    return identities


def _run_checked_phase(
    target: Target,
    scenario: Any,
    checks: Sequence[QueryCheck],
    run: RunEvidence,
    runner: Callable,
    *,
    phase: str,
    expected_identity: Mapping[str, Any] | None,
    bindings: Mapping[str, int | str] | None = None,
    failure_status: str,
) -> list[dict[str, Any]]:
    check_dir = run.run_dir / "sessions" / f"{scenario.id}-{phase}-{secrets.token_hex(6)}"
    try:
        report = run_checks(
            target,
            checks,
            check_dir,
            phase=phase,
            _runner=runner,
            bindings=bindings,
        )
    except Exception as error:
        raise _ScenarioProblem("UNKNOWN", f"{phase} checks could not be verified: {error}") from error

    identities = _check_identities(report, target, expected_identity)
    complete = getattr(report, "complete", None)
    raw_results = getattr(report, "results", None)
    if complete is not True or not isinstance(raw_results, (list, tuple)):
        raise _ScenarioProblem("UNKNOWN", f"{phase} checks returned incomplete evidence")

    by_id: dict[str, Mapping[str, Any]] = {}
    for result in raw_results:
        if not isinstance(result, Mapping):
            raise _ScenarioProblem("UNKNOWN", f"{phase} checks returned a malformed result")
        check_id = result.get("check", result.get("id"))
        if not isinstance(check_id, str) or check_id in by_id:
            raise _ScenarioProblem("UNKNOWN", f"{phase} checks returned duplicate or missing check identity")
        by_id[check_id] = result
    if set(by_id) != {check.id for check in checks}:
        raise _ScenarioProblem("UNKNOWN", f"{phase} checks did not return every requested result")

    records: list[dict[str, Any]] = []
    failed: list[str] = []
    for check in checks:
        result = by_id[check.id]
        passed = result.get("passed")
        if type(passed) is not bool:
            raise _ScenarioProblem("UNKNOWN", f"{phase} check {check.id} has no verified result")
        if not passed:
            failed.append(check.id)
        records.append({
            "id": check.id,
            "phase": phase,
            "expected": check.expected,
            "actual": result.get("value"),
            "passed": passed,
            "sql": check.sql,
        })

    report_passed = getattr(report, "passed", None)
    if failed:
        raise _ScenarioProblem(
            failure_status,
            f"{phase} checks failed: {', '.join(failed)}",
            records,
        )
    if report_passed is not True:
        raise _ScenarioProblem("UNKNOWN", f"{phase} check report is inconsistent with its individual results")
    # Keep the observed identity for the caller without trusting a report-level shortcut.
    records[0]["identity"] = dict(identities[0]) if records else {}
    return records


def _run_requirements(
    target: Target,
    scenario: Any,
    run: RunEvidence,
    runner: Callable,
    context: RenderContext,
) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    raw_checks = scenario.requirements.get("checks", ())
    checks = tuple(
        QueryCheck(str(item["id"]), str(item["sql"]), item["expected"])
        for item in raw_checks
    )
    if not checks:
        raise _ScenarioProblem("UNKNOWN", "Built-in scenario declares no executable prerequisite checks")
    records = _run_checked_phase(
        target,
        scenario,
        checks,
        run,
        runner,
        phase="preconditions",
        expected_identity=None,
        bindings=context.values,
        failure_status="BLOCKED",
    )
    identity = records[0].get("identity")
    if not isinstance(identity, Mapping):
        raise _ScenarioProblem("UNKNOWN", "Prerequisite checks did not establish the scenario identity")
    return identity, records


def _capture_inventory(
    target: Target,
    run: RunEvidence,
    runner: Callable,
    expected_identity: Mapping[str, Any] | None = None,
) -> SchemaInventory:
    try:
        inventory = capture_inventory(target, run.run_dir, _runner=runner)
    except Exception as error:
        raise CatalogError(f"owner inventory capture is incomplete: {error}") from error
    if not isinstance(inventory, SchemaInventory):
        raise CatalogError("owner inventory capture returned an invalid record")
    _verify_catalog_identity(inventory.identity, target, expected_identity)
    if inventory.coverage.get("ownerComplete") is not True:
        raise CatalogError("owner inventory visibility is incomplete")
    return inventory


def _capture_fixture(
    target: Target,
    run: RunEvidence,
    runner: Callable,
    name: str,
    object_type: str,
    expected_identity: Mapping[str, Any],
) -> tuple[SchemaInventory, SchemaSnapshot, ObjectDefinition]:
    inventory = _capture_inventory(target, run, runner, expected_identity)
    key = ObjectKey(target.schema, name, object_type)
    if key not in inventory.objects:
        raise _FixtureAbsent(f"complete owner inventory did not find {object_type} {name}")
    try:
        snapshot = capture_snapshot(target, inventory, [(name, object_type)], run.run_dir, _runner=runner)
    except Exception as error:
        raise CatalogError(f"{object_type} {name} metadata capture is incomplete: {error}") from error
    if not isinstance(snapshot, SchemaSnapshot):
        raise CatalogError(f"{object_type} {name} capture returned an invalid definition")
    _verify_catalog_identity(snapshot.identity, target, inventory.identity)
    _verify_catalog_identity(snapshot.identity, target, expected_identity)
    definition = snapshot.objects.get(key)
    if not isinstance(definition, ObjectDefinition):
        raise CatalogError(f"{object_type} {name} definition is missing from the selected snapshot")
    return inventory, snapshot, definition


def _assert_names_absent(inventory: SchemaInventory, names: Sequence[str]) -> None:
    existing = {key.name.upper() for key in inventory.objects}
    collisions = [name for name in names if name.upper() in existing]
    if collisions:
        raise _ScenarioProblem(
            "BLOCKED",
            "Fixture name already exists in the selected owner; refusing to adopt or overwrite it: "
            + ", ".join(collisions),
        )


def _freeze_generated_input(
    run: RunEvidence,
    scenario_id: str,
    step_id: str,
    sql: str,
    ordinal: int,
) -> tuple[bytes, str, Path]:
    payload = sql.encode("utf-8")
    directory = run.inputs_dir / "generated" / scenario_id
    path = directory / f"{ordinal:02d}-{step_id}.sql"
    for component in (run.run_dir, run.inputs_dir, run.inputs_dir / "generated", directory):
        if component.is_symlink():
            raise OSError(f"generated input path contains a symbolic link: {component}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    digest = hashlib.sha256(payload).hexdigest()
    append_event(run, {
        "type": "plan",
        "event": "generated-input",
        "scenarioId": scenario_id,
        "stepId": step_id,
        "path": path.relative_to(run.run_dir).as_posix(),
        "sha256": digest,
        "writeStarted": False,
    })
    # Re-read the private artifact, so the dispatched bytes are the frozen bytes.
    frozen = path.read_bytes()
    if hashlib.sha256(frozen).hexdigest() != digest:
        raise OSError("generated input changed after it was frozen")
    return frozen, digest, path


def _execute_generated_step(
    target: Target,
    scenario: Any,
    step_id: str,
    sql: str,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    expected_identity: Mapping[str, Any],
    ordinal: int,
) -> dict[str, Any]:
    assert_inputs_unchanged(run)
    source, digest, path = _freeze_generated_input(run, scenario.id, step_id, sql, ordinal)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise _ScenarioProblem("BLOCKED", "Generated SQL changed before step dispatch")
    step = {
        "id": step_id,
        "source": source,
        "sha256": digest,
        "expected": {"kind": "success"},
    }
    try:
        result = execute_step(
            target,
            scenario,
            step,
            context,
            run,
            runner=runner,
            expected_identity=expected_identity,
        )
    except Exception as error:
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "apply",
            "writeStarted": True,
            "error": f"Step execution raised unexpectedly after durable input evidence: {error}",
            "input": path.relative_to(run.run_dir).as_posix(),
            "sha256": digest,
        }
    result = dict(result)
    result["input"] = path.relative_to(run.run_dir).as_posix()
    result["sha256"] = digest
    if result.get("status") == "PASSED":
        identity = result.get("identity")
        if not isinstance(identity, Mapping) or not _same_live_target(expected_identity, identity):
            result.update({
                "status": "UNKNOWN",
                "error": "Step completion identity is missing or differs from the scenario target",
            })
    return result


def _column_rows(definition: ObjectDefinition, table_name: str) -> list[dict[str, Any]]:
    rows = definition.attributes.get("columns")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, Mapping) for row in rows):
        raise CatalogError(f"table {table_name} has incomplete captured column metadata")
    names = [row.get("name") for row in rows]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise CatalogError(f"table {table_name} has invalid or duplicate captured column names")
    return [dict(row) for row in rows]


def _validate_datatype_columns(rows: Sequence[Mapping[str, Any]], table_name: str) -> None:
    expected = {
        str(column["name"]): baseline._table_column_type(column)
        for column in DATATYPE_COLUMNS
    }
    actual = {str(row["name"]): baseline._table_column_type(row) for row in rows}
    if actual != expected:
        details = [
            f"{name}: expected {expected.get(name, '<absent>')}, observed {actual.get(name, '<absent>')}"
            for name in sorted(set(expected) | set(actual))
            if expected.get(name) != actual.get(name)
        ]
        raise _ScenarioProblem("FAILED", f"Captured datatype definition differs for {table_name}: " + "; ".join(details))
    for row in rows:
        for attribute in ("hidden", "virtual", "identity"):
            value = row.get(attribute)
            if value is not None and str(value).upper() not in _NULLABILITY_FALSE:
                raise _ScenarioProblem("FAILED", f"Unexpected {attribute} column attribute in {table_name}.{row['name']}")


def _inventory_object(
    target: Target,
    name: str,
    object_type: str,
    inventory: SchemaInventory,
    definition: ObjectDefinition,
    snapshot: SchemaSnapshot | None = None,
) -> dict[str, Any]:
    key = ObjectKey(target.schema, name, object_type)
    row = inventory.objects.get(key, {})
    dependents = []
    for child in definition.dependents:
        child_row = inventory.objects.get(child)
        if not isinstance(child_row, Mapping) or type(child_row.get("object_id")) is not int:
            raise CatalogError(f"dependent identity is incomplete for {child.owner}.{child.name} ({child.object_type})")
        dependents.append({
            "owner": child.owner,
            "name": child.name,
            "type": child.object_type,
            "objectId": child_row["object_id"],
        })
    dependents.sort(key=lambda child: (child["owner"], child["name"], child["type"], child["objectId"]))
    result = {
        "owner": target.schema,
        "name": name,
        "type": object_type,
        "objectId": row.get("object_id"),
        "dataObjectId": row.get("data_object_id"),
        "status": row.get("status"),
        "definitionSha256": hashlib.sha256(definition.raw_ddl.encode("utf-8")).hexdigest(),
        "dependents": dependents,
    }
    if object_type.upper() in {"PACKAGE", "PACKAGE BODY"}:
        if snapshot is None:
            raise CatalogError(f"package fixture {name} requires a complete package/member snapshot")
        members: dict[str, dict[str, Any]] = {}
        for member_type in ("PACKAGE", "PACKAGE BODY"):
            member_key = ObjectKey(target.schema, name, member_type)
            member_row = inventory.objects.get(member_key)
            member_definition = snapshot.objects.get(member_key)
            if member_row is None:
                continue
            if member_definition is None or type(member_row.get("object_id")) is not int:
                raise CatalogError(f"package member evidence is incomplete for {name} ({member_type})")
            members[member_type] = {
                "objectId": member_row["object_id"],
                "definitionSha256": hashlib.sha256(member_definition.raw_ddl.encode("utf-8")).hexdigest(),
            }
        if "PACKAGE" not in members:
            raise CatalogError(f"package specification evidence is missing for {name}")
        result["memberDefinitions"] = members
    return result


def _save_fixture_inventory(
    run: RunEvidence,
    scenario_id: str,
    identity: Mapping[str, Any],
    fixtures: Sequence[Mapping[str, Any]],
    *,
    details: Mapping[str, Any] | None = None,
    row_keys: Sequence[Mapping[str, Any]] = (),
    cleanup_contract: Mapping[str, Any] | None = None,
    planned_objects: Mapping[str, str] | None = None,
) -> None:
    inventory_path = run._inventory_path
    current: dict[str, Any] = {
        "schemaVersion": 1,
        "targetIdentity": dict(identity),
        "objects": [],
        "rows": [],
        "scenarios": [],
    }
    if inventory_path.exists():
        if inventory_path.is_symlink() or not inventory_path.is_file():
            raise CatalogError("existing fixture inventory is not a regular file")
        try:
            loaded = json.loads(inventory_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogError(f"existing fixture inventory is unreadable: {error}") from error
        if (
            not isinstance(loaded, dict)
            or type(loaded.get("schemaVersion")) is not int
            or loaded.get("schemaVersion") != 1
            or not isinstance(loaded.get("targetIdentity"), Mapping)
            or not isinstance(loaded.get("objects"), list)
            or not isinstance(loaded.get("rows"), list)
        ):
            raise CatalogError("existing fixture inventory is malformed; refusing to replace it")
        if dict(loaded["targetIdentity"]) != dict(identity):
            raise CatalogError("fixture inventory target changed during this run")
        current = loaded
        if not isinstance(current.get("scenarios"), list):
            previous_id = current.get("scenarioId")
            previous_objects = current.get("objects", [])
            current["scenarios"] = (
                [{"id": previous_id, "objects": previous_objects, "rows": [], "details": {}}]
                if isinstance(previous_id, str) and isinstance(previous_objects, list)
                else []
            )

    fixture_rows = [dict(fixture) for fixture in fixtures]
    saved_rows = [_plain_json(row) for row in row_keys]
    if not fixture_rows and not saved_rows and cleanup_contract is None:
        raise CatalogError("fixture inventory cannot record an empty ownership and key set")
    for fixture in fixture_rows:
        if not all(isinstance(fixture.get(key), str) and fixture[key] for key in ("owner", "name", "type")):
            raise CatalogError("fixture inventory contains an incomplete object key")
        if type(fixture.get("objectId")) is not int or fixture["objectId"] <= 0:
            raise CatalogError(f"fixture inventory has no observed object identity for {fixture['name']}")
        if not isinstance(fixture.get("definitionSha256"), str) or re.fullmatch(r"[0-9a-f]{64}", fixture["definitionSha256"]) is None:
            raise CatalogError(f"fixture inventory has no definition digest for {fixture['name']}")
        if not isinstance(fixture.get("dependents"), list):
            raise CatalogError(f"fixture inventory has incomplete dependent evidence for {fixture['name']}")

    scenario_rows = current["scenarios"]
    if not all(isinstance(entry, dict) and isinstance(entry.get("id"), str) for entry in scenario_rows):
        raise CatalogError("existing scenario fixture inventory is malformed")
    fixture_keys = {
        (fixture["owner"].upper(), fixture["name"].upper(), fixture["type"].upper())
        for fixture in fixture_rows
    }
    for fixture in fixture_rows:
        duplicate = [
            saved for saved in current["objects"]
            if isinstance(saved, Mapping)
            and (str(saved.get("owner", "")).upper(), str(saved.get("name", "")).upper(), str(saved.get("type", "")).upper())
            == (fixture["owner"].upper(), fixture["name"].upper(), fixture["type"].upper())
        ]
        if duplicate and any(saved.get("objectId") != fixture["objectId"] for saved in duplicate):
            raise CatalogError(f"fixture object identity changed during this run: {fixture['name']}")
    current["objects"] = [
        saved for saved in current["objects"]
        if not isinstance(saved, Mapping)
        or (str(saved.get("owner", "")).upper(), str(saved.get("name", "")).upper(), str(saved.get("type", "")).upper()) not in fixture_keys
    ] + fixture_rows
    for row in saved_rows:
        if (
            not isinstance(row.get("selectorId"), str)
            or not isinstance(row.get("bindings"), Mapping)
            or not isinstance(row.get("inputBindings"), Mapping)
            or not isinstance(row.get("columns"), list)
        ):
            raise CatalogError("fixture inventory contains incomplete exact selector-key evidence")
        row["scenarioId"] = scenario_id
    existing_rows = current.get("rows", [])
    if not isinstance(existing_rows, list):
        raise CatalogError("existing fixture row inventory is malformed")
    current["rows"] = [
        row for row in existing_rows
        if not isinstance(row, Mapping) or row.get("scenarioId") != scenario_id
    ] + saved_rows
    scenario_entry = {
        "id": scenario_id,
        "objects": fixture_rows,
        "rows": saved_rows,
        "details": dict(details or {}),
        "cleanup": (
            _plain_json(cleanup_contract)
            if cleanup_contract is not None
            else {"kind": "builtin"}
        ),
    }
    current["scenarios"] = [entry for entry in scenario_rows if entry.get("id") != scenario_id] + [scenario_entry]
    current.pop("scenarioId", None)
    current.pop("details", None)
    try:
        append_event(run, {
            "type": "fixture-observed",
            "scenarioId": scenario_id,
            "targetIdentity": dict(identity),
            "inventoryComplete": True,
            "objects": fixture_rows,
            "rows": saved_rows,
            "plannedObjects": [
                {"owner": identity.get("current_schema"), "name": name, "type": object_type}
                for name, object_type in sorted((planned_objects or {}).items())
            ],
        })
        save_inventory(run, current)
    except (OSError, ValueError) as error:
        raise CatalogError(f"could not persist exact fixture ownership evidence: {error}") from error


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _row_for_table_check(schema: str, table: str, column: Mapping[str, Any]) -> dict[str, Any]:
    return {**column, "table_name": table}


def _datatype_checks(
    target: Target,
    copy_table: str,
    copied_columns: Sequence[Mapping[str, Any]],
    generated_postconditions: Sequence[Mapping[str, Any]],
    source_table: str,
) -> tuple[QueryCheck, ...]:
    checks: list[QueryCheck] = [
        QueryCheck(str(item["id"]), str(item["sql"]), int(item["expected"]))
        for item in generated_postconditions
    ]
    for column in copied_columns:
        generated = baseline._column_definition_check(
            target.schema,
            _row_for_table_check(target.schema, copy_table, column),
        )
        checks.append(QueryCheck(generated["id"] + "-exact", generated["sql"], 1))

    mismatch_cases = {
        "COL_BOOL": {"data_type": "VARCHAR2", "char_length": 10, "char_used": "C", "data_length": 40},
        "COL_NUM1": {"data_type": "NUMBER", "data_precision": 10, "data_scale": 3},
        "COL_NUM2": {"data_type": "NUMBER", "data_precision": 10, "data_scale": 3},
        "COL_FLOAT": {"data_type": "FLOAT", "data_precision": 64},
        "COL_VC": {"data_type": "VARCHAR2", "char_length": 10, "char_used": "C", "data_length": 40},
        "COL_RAW": {"data_type": "RAW", "data_length": 32},
    }
    columns_by_name = {str(column["name"]): column for column in copied_columns}
    for column_name, wrong_attributes in mismatch_cases.items():
        base = columns_by_name.get(column_name)
        if base is None:
            raise CatalogError(f"captured copy metadata is missing required column {column_name}")
        wrong = {**base, "table_name": copy_table, **wrong_attributes}
        generated = baseline._column_definition_check(target.schema, wrong)
        checks.append(QueryCheck(generated["id"] + "-mismatch", generated["sql"], 0))

    # The built-in creates empty fixtures and never inserts business data.
    for table in (source_table, copy_table):
        checks.append(QueryCheck(
            f"fixture-empty-{table.casefold()}",
            f"SELECT CASE WHEN (SELECT COUNT(*) FROM \"{target.schema}\".\"{table}\") = 0 "
            "THEN 1 ELSE 0 END FROM DUAL",
            1,
        ))
    return tuple(checks)


def _run_common_preflight(
    scenario: Any,
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    fixture_names: Sequence[str],
) -> tuple[Mapping[str, Any], SchemaInventory, list[dict[str, Any]]]:
    if not _target_is_dev(target):
        raise _ScenarioProblem("BLOCKED", "Scenario requires a non-production DEV owner connection")
    try:
        assert_inputs_unchanged(run)
    except (OSError, ValueError) as error:
        raise _ScenarioProblem("BLOCKED", f"Frozen run inputs are missing or changed: {error}") from error
    for name in fixture_names:
        _validate_fixture_name(name, "Fixture name")
    observed_identity, requirement_records = _run_requirements(target, scenario, run, runner, context)
    try:
        inventory = _capture_inventory(target, run, runner, observed_identity)
    except Exception as error:
        raise _ScenarioProblem(
            "UNKNOWN", f"Initial owner inventory capture is unavailable: {error}", requirement_records,
        ) from error
    try:
        _assert_names_absent(inventory, fixture_names)
    except _ScenarioProblem as error:
        error.records = requirement_records
        raise
    owned_fixtures: list[dict[str, str]] = []
    for spec in scenario.fixtures.get("objects", ()):
        if not isinstance(spec, Mapping):
            raise _ScenarioProblem("UNKNOWN", "Fixture ownership contract is malformed")
        binding = spec.get("binding")
        suffix = spec.get("suffix")
        object_type = spec.get("type")
        name = context.objects.get(binding) if isinstance(binding, str) else None
        if not isinstance(name, str) and isinstance(suffix, str):
            name = make_object_name(run.object_prefix, suffix)
        if not isinstance(name, str) or not isinstance(object_type, str):
            raise _ScenarioProblem("UNKNOWN", "Fixture ownership contract is incomplete")
        owned_fixtures.append({"owner": target.schema, "name": name, "type": object_type.upper()})
    if {item["name"].upper() for item in owned_fixtures} != {name.upper() for name in fixture_names}:
        raise _ScenarioProblem("UNKNOWN", "Fixture absence proof does not cover the exact declared fixture set")
    try:
        append_event(run, {
            "type": "fixture-precreation",
            "scenarioId": scenario.id,
            "targetIdentity": dict(inventory.identity),
            "ownerComplete": True,
            "objects": [{**item, "absent": True} for item in owned_fixtures],
        })
    except (OSError, ValueError) as error:
        raise _ScenarioProblem(
            "UNKNOWN", f"Could not preserve fixture absence evidence before writes: {error}", requirement_records,
        ) from error
    # Keep the initial full catalog identity as the scenario baseline. The
    # precondition checks prove the selected owner/database; the catalog
    # capture adds container, edition and database-version scope for every
    # later catalog, settings readback and query-check observation.
    return inventory.identity, inventory, requirement_records


def run_datatype_roundtrip(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Create, capture, generate and independently verify a six-type table copy."""
    scenario = _get_builtin_scenario("datatype-roundtrip")
    source_table = context.objects.get("source_table") or make_object_name(run.object_prefix, "DTT_SRC")
    copy_table = context.objects.get("copy_table") or make_object_name(run.object_prefix, "DTT_CPY")
    steps: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    try:
        source_table = _validate_fixture_name(source_table, "Source fixture name")
        copy_table = _validate_fixture_name(copy_table, "Copy fixture name")
        if source_table.upper() == copy_table.upper():
            raise _ScenarioProblem("BLOCKED", "Source and copy fixture names must be different")
        identity, _initial_inventory, requirement_records = _run_common_preflight(
            scenario, target, context, run, runner, (source_table, copy_table),
        )
        checks.extend(requirement_records)

        source_column_definitions = [
            f'"{column["name"]}" {baseline._table_column_type(column)}'
            for column in DATATYPE_COLUMNS
        ]
        create_source_sql = (
            f'CREATE TABLE "{target.schema}"."{source_table}" (\n  '
            + ",\n  ".join(source_column_definitions)
            + "\n);\n"
        )
        first = _execute_generated_step(
            target, scenario, "create-source-table", create_source_sql,
            context, run, runner, identity, 1,
        )
        steps.append(first)
        if first.get("status") != "PASSED":
            return _outcome(scenario, str(first.get("status", "UNKNOWN")), steps=steps, error=first.get("error"))

        try:
            source_inventory, source_snapshot, source_definition = _capture_fixture(
                target, run, runner, source_table, "TABLE", identity,
            )
        except _FixtureAbsent as error:
            return _outcome(scenario, "FAILED", steps=steps, error=str(error))
        except Exception as error:
            return _outcome(scenario, "UNKNOWN", steps=steps, error=f"Source catalog capture failed: {error}")
        source_columns = _column_rows(source_definition, source_table)
        _validate_datatype_columns(source_columns, source_table)
        _save_fixture_inventory(
            run, scenario.id, source_inventory.identity,
            (_inventory_object(target, source_table, "TABLE", source_inventory, source_definition, source_snapshot),),
            details={"sourceColumns": source_columns},
        )

        # The only input to the generated copy is the real, selected source
        # definition. Project those captured rows onto the copy fixture name.
        projected_columns = [dict(column, table_name=copy_table) for column in source_columns]
        projected_columns.sort(key=lambda column: int(column.get("column_id", 0)))
        raw_copy_ddl = (
            f'CREATE TABLE "{target.schema}"."{copy_table}" (\n  '
            + ",\n  ".join(baseline._column_sql(column) for column in projected_columns)
            + "\n)"
        )
        projected_key = ObjectKey(target.schema, copy_table, "TABLE")
        projected_definition = ObjectDefinition(
            projected_key,
            {**source_definition.attributes, "columns": projected_columns},
            raw_copy_ddl,
            (),
            True,
        )
        projected_snapshot = SchemaSnapshot(
            source_snapshot.identity,
            {projected_key: {"owner": target.schema, "name": copy_table, "type": "TABLE", "status": "VALID"}},
            {projected_key: projected_definition},
            source_snapshot.coverage,
            source_snapshot.started_at,
            source_snapshot.completed_at,
        )
        source_catalog = {
            "sections": {
                "tables": [{"name": copy_table}],
                "columns": projected_columns,
                "constraints": [],
                "indexes": [],
                "sequences": [],
                "identity-columns": [],
            }
        }
        target_catalog = {
            "sections": {
                "tables": [], "columns": [], "constraints": [], "indexes": [],
                "sequences": [], "identity-columns": [],
            }
        }
        config = {"schemas": [target.schema], "prefixes": [run.object_prefix], "excludedObjects": []}
        generated_steps, generated_checks = baseline._structure_bundle(
            config, target.schema, target.schema, source_catalog, target_catalog, projected_snapshot,
        )
        if len(generated_steps) != 1:
            return _outcome(scenario, "UNKNOWN", steps=steps, error="Baseline generator did not return exactly one copy-table step")
        copy_sql = next(iter(generated_steps.values()))

        # Recheck the latest complete owner inventory before the second write.
        _assert_names_absent(source_inventory, (copy_table,))
        second = _execute_generated_step(
            target, scenario, "create-copy-table", copy_sql,
            context, run, runner, identity, 2,
        )
        steps.append(second)
        if second.get("status") != "PASSED":
            return _outcome(scenario, str(second.get("status", "UNKNOWN")), steps=steps, error=second.get("error"))

        try:
            copy_inventory, _copy_snapshot, copy_definition = _capture_fixture(
                target, run, runner, copy_table, "TABLE", identity,
            )
        except _FixtureAbsent as error:
            return _outcome(scenario, "FAILED", steps=steps, error=str(error))
        except Exception as error:
            return _outcome(scenario, "UNKNOWN", steps=steps, error=f"Independent copy capture failed: {error}")
        copied_columns = _column_rows(copy_definition, copy_table)
        _validate_datatype_columns(copied_columns, copy_table)
        if {
            column["name"]: baseline._table_column_type(column) for column in source_columns
        } != {
            column["name"]: baseline._table_column_type(column) for column in copied_columns
        }:
            return _outcome(scenario, "FAILED", steps=steps, error="Captured copy definition differs from the captured source definition")

        query_checks = _datatype_checks(
            target, copy_table, copied_columns,
            generated_checks.get("postconditions", ()), source_table,
        )
        try:
            checks.extend(_run_checked_phase(
                target, scenario, query_checks, run, runner,
                phase="postconditions", expected_identity=identity,
                bindings=context.values, failure_status="FAILED",
            ))
        except _ScenarioProblem as error:
            checks.extend(getattr(error, "records", ()))
            status = error.status
            _save_fixture_inventory(
                run, scenario.id, copy_inventory.identity,
                (
                    _inventory_object(target, source_table, "TABLE", source_inventory, source_definition, source_snapshot),
                    _inventory_object(target, copy_table, "TABLE", copy_inventory, copy_definition, _copy_snapshot),
                ),
                details={"sourceColumns": source_columns, "copyColumns": copied_columns},
            )
            return _outcome(scenario, status, steps=steps, checks=checks, error=str(error))

        _save_fixture_inventory(
            run, scenario.id, copy_inventory.identity,
            (
                _inventory_object(target, source_table, "TABLE", source_inventory, source_definition, source_snapshot),
                _inventory_object(target, copy_table, "TABLE", copy_inventory, copy_definition, _copy_snapshot),
            ),
            details={"sourceColumns": source_columns, "copyColumns": copied_columns},
        )
        return _outcome(scenario, "PASSED", steps=steps, checks=checks)
    except _ScenarioProblem as error:
        checks.extend(error.records)
        return _outcome(scenario, error.status, steps=steps, checks=checks, error=str(error))
    except Exception as error:
        status = "UNKNOWN" if any(step.get("writeStarted") is True for step in steps) else "BLOCKED"
        return _outcome(scenario, status, steps=steps, checks=checks, error=f"Datatype scenario could not be verified: {error}")


def _read_compiler_settings(
    target: Target,
    proc_name: str,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    expected_identity: Mapping[str, Any],
) -> dict[str, Any]:
    query = (
        "SELECT PLSQL_OPTIMIZE_LEVEL, PLSQL_CODE_TYPE, PLSQL_DEBUG, "
        "PLSQL_WARNINGS, NLS_LENGTH_SEMANTICS, PLSCOPE_SETTINGS, "
        "CASE WHEN PLSQL_CCFLAGS IS NULL THEN 'NULL' ELSE 'VALUE' END AS CCFLAGS_STATUS, "
        "PLSQL_CCFLAGS, "
        "NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0') AS CONTAINER_ID, "
        "NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB') AS CONTAINER_NAME, "
        "NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>') AS EDITION, "
        "TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE) AS DATABASE_VERSION "
        "FROM ALL_PLSQL_OBJECT_SETTINGS "
        f"WHERE OWNER = '{target.schema}' AND NAME = '{proc_name}' AND TYPE = 'PROCEDURE'"
    )
    try:
        captured = read_rows(target, query, context, run, max_rows=1, runner=runner)
    except Exception as error:
        raise _ScenarioProblem("UNKNOWN", f"Compiler settings capture failed: {error}") from error
    if (
        not isinstance(captured, Mapping)
        or type(captured.get("count")) is not int
        or captured.get("count") != 1
        or captured.get("truncated") is not False
    ):
        raise _ScenarioProblem("UNKNOWN", "Compiler settings query did not return exactly one complete row")
    if captured.get("columns") != list(_COMPILER_SETTING_COLUMNS):
        raise _ScenarioProblem("UNKNOWN", "Compiler settings query returned unexpected or incomplete columns")
    rows = captured.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise _ScenarioProblem("UNKNOWN", "Compiler settings query returned malformed row evidence")
    observed_identity = captured.get("identity")
    if (
        not isinstance(observed_identity, Mapping)
        or not _identity_matches_target(observed_identity, target)
    ):
        raise _ScenarioProblem("UNKNOWN", "Compiler settings query observed a different live database identity")

    row = rows[0]
    required_values = (*_COMPILER_SETTING_COLUMNS[:6], *_COMPILER_SETTING_COLUMNS[8:])
    if any(key not in row or row[key] is None for key in required_values):
        raise _ScenarioProblem("UNKNOWN", "Compiler settings capture omitted a required setting")
    full_identity = {
        **observed_identity,
        "container_id": str(row.get("CONTAINER_ID", "")),
        "container_name": str(row.get("CONTAINER_NAME", "")),
        "edition": str(row.get("EDITION", "")),
        "database_version": str(row.get("DATABASE_VERSION", "")),
    }
    if not _same_scenario_identity(expected_identity, full_identity):
        raise _ScenarioProblem("UNKNOWN", "Compiler settings query observed a different live database identity or scope")
    ccflags_status = row.get("CCFLAGS_STATUS")
    if ccflags_status == "NULL":
        ccflags = None
    elif ccflags_status == "VALUE" and isinstance(row.get("PLSQL_CCFLAGS"), str) and row["PLSQL_CCFLAGS"]:
        ccflags = row["PLSQL_CCFLAGS"]
    else:
        raise _ScenarioProblem("UNKNOWN", "Compiler settings capture could not distinguish null from non-null CCFLAGS")

    values = {
        "name": proc_name,
        "type": "PROCEDURE",
        "plsql_optimize_level": str(row["PLSQL_OPTIMIZE_LEVEL"]),
        "plsql_code_type": str(row["PLSQL_CODE_TYPE"]),
        "plsql_debug": str(row["PLSQL_DEBUG"]),
        "plsql_warnings": str(row["PLSQL_WARNINGS"]),
        "nls_length_semantics": str(row["NLS_LENGTH_SEMANTICS"]),
        "plscope_settings": str(row["PLSCOPE_SETTINGS"]),
        "plsql_ccflags": ccflags,
    }
    try:
        return baseline._setting_row([values], proc_name, "PROCEDURE")
    except Exception as error:
        raise _ScenarioProblem("UNKNOWN", f"Captured compiler settings are invalid or unsupported: {error}") from error


def _compiler_check_set(
    settings: Mapping[str, Any],
    proc_name: str,
    schema: str,
    *,
    prefix: str,
    include_all_wrong_settings: bool,
) -> tuple[QueryCheck, ...]:
    checks: list[QueryCheck] = []
    exact = baseline._exact_settings_check(settings, proc_name, "PROCEDURE", schema)
    checks.append(QueryCheck(f"{exact['id']}-{prefix}-exact", exact["sql"], 1))

    wrong_scope = (
        ("wrong-owner", "WRONG_OWNER", proc_name, "PROCEDURE"),
        ("wrong-name", schema, "WRONG_NAME", "PROCEDURE"),
        ("wrong-type", schema, proc_name, "FUNCTION"),
    )
    for suffix, owner, name, object_type in wrong_scope:
        generated = baseline._exact_settings_check(settings, name, object_type, owner)
        checks.append(QueryCheck(f"{generated['id']}-{prefix}-{suffix}", generated["sql"], 0))

    if include_all_wrong_settings:
        wrong_values = {
            "plsql_optimize_level": next(
                value for value in ("0", "1", "2", "3")
                if value != str(settings["plsql_optimize_level"])
            ),
            "plsql_code_type": "NATIVE" if settings["plsql_code_type"] != "NATIVE" else "INTERPRETED",
            "plsql_debug": "TRUE" if settings["plsql_debug"] != "TRUE" else "FALSE",
            "plsql_warnings": "DISABLE:ALL" if settings["plsql_warnings"] != "DISABLE:ALL" else "ENABLE:ALL",
            "nls_length_semantics": "CHAR" if settings["nls_length_semantics"] != "CHAR" else "BYTE",
            "plscope_settings": "IDENTIFIERS:ALL" if settings["plscope_settings"] != "IDENTIFIERS:ALL" else "IDENTIFIERS:NONE",
        }
        for setting_name, wrong_value in wrong_values.items():
            wrong_settings = {**settings, setting_name: wrong_value}
            generated = baseline._exact_settings_check(wrong_settings, proc_name, "PROCEDURE", schema)
            checks.append(QueryCheck(
                f"{generated['id']}-{prefix}-mismatch-{setting_name.replace('_', '-')}",
                generated["sql"],
                0,
            ))

    opposite_flags = {**settings, "plsql_ccflags": "DEVTEST_FLAG:TRUE" if settings["plsql_ccflags"] is None else None}
    generated_flags = baseline._exact_settings_check(opposite_flags, proc_name, "PROCEDURE", schema)
    checks.append(QueryCheck(f"{generated_flags['id']}-{prefix}-mismatch-ccflags", generated_flags["sql"], 0))
    return tuple(checks)


def _recompile_sql(target: Target, proc_name: str, settings: Mapping[str, Any], desired_ccflags: str | None) -> str:
    # Reuse the exact captured settings for every compiler switch, changing only
    # CCFLAGS for this isolated fixture. An empty Oracle string represents NULL.
    session_settings = baseline._session_settings_sql(settings)
    ccflags_value = "''" if desired_ccflags is None else "'" + desired_ccflags.replace("'", "''") + "'"
    return (
        session_settings
        + f"\nALTER SESSION SET PLSQL_CCFLAGS = {ccflags_value};\n"
        + f'ALTER PROCEDURE "{target.schema}"."{proc_name}" COMPILE;\n'
    )


def run_compiler_settings(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Capture actual compiler settings and exercise exact/null/non-null checks."""
    scenario = _get_builtin_scenario("compiler-settings")
    proc_name = context.objects.get("unit") or make_object_name(run.object_prefix, "CS_PROC")
    steps: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    fixture_definitions: list[dict[str, Any]] = []
    try:
        proc_name = _validate_fixture_name(proc_name, "Compiler fixture name")
        identity, _initial_inventory, requirement_records = _run_common_preflight(
            scenario, target, context, run, runner, (proc_name,),
        )
        checks.extend(requirement_records)
        create_procedure_sql = (
            f'CREATE PROCEDURE "{target.schema}"."{proc_name}" IS\n'
            "BEGIN\n"
            "  NULL;\n"
            "END;\n"
            "/\n"
        )
        created = _execute_generated_step(
            target, scenario, "create-stored-unit", create_procedure_sql,
            context, run, runner, identity, 1,
        )
        steps.append(created)
        if created.get("status") != "PASSED":
            return _outcome(scenario, str(created.get("status", "UNKNOWN")), steps=steps, error=created.get("error"))

        try:
            created_inventory, _created_snapshot, definition = _capture_fixture(
                target, run, runner, proc_name, "PROCEDURE", identity,
            )
        except _FixtureAbsent as error:
            return _outcome(scenario, "FAILED", steps=steps, error=str(error))
        except Exception as error:
            return _outcome(scenario, "UNKNOWN", steps=steps, error=f"Stored unit catalog capture failed: {error}")
        if not definition.valid:
            return _outcome(scenario, "FAILED", steps=steps, error=f"Captured procedure {proc_name} is INVALID")
        if definition.dependents:
            return _outcome(scenario, "BLOCKED", steps=steps, error="Compiler fixture has dependents; refusing to recompile it")
        fixture_definitions.append(_inventory_object(target, proc_name, "PROCEDURE", created_inventory, definition, _created_snapshot))
        _save_fixture_inventory(
            run, scenario.id, created_inventory.identity, fixture_definitions,
            details={"unitStatus": "VALID"},
        )

        settings = _read_compiler_settings(target, proc_name, context, run, runner, identity)
        initial_checks = _compiler_check_set(
            settings, proc_name, target.schema,
            prefix="initial", include_all_wrong_settings=True,
        )
        checks.extend(_run_checked_phase(
            target, scenario, initial_checks, run, runner,
            phase="postconditions", expected_identity=identity,
            bindings=context.values, failure_status="FAILED",
        ))

        desired_ccflags = "DEVTEST_FLAG:TRUE" if settings["plsql_ccflags"] is None else None
        compile_sql = _recompile_sql(target, proc_name, settings, desired_ccflags)
        recompiled = _execute_generated_step(
            target, scenario, "recompile-with-ccflags", compile_sql,
            context, run, runner, identity, 2,
        )
        steps.append(recompiled)
        if recompiled.get("status") != "PASSED":
            return _outcome(scenario, str(recompiled.get("status", "UNKNOWN")), steps=steps, checks=checks, error=recompiled.get("error"))

        try:
            final_inventory, _final_snapshot, final_definition = _capture_fixture(
                target, run, runner, proc_name, "PROCEDURE", identity,
            )
        except _FixtureAbsent as error:
            return _outcome(scenario, "FAILED", steps=steps, checks=checks, error=str(error))
        except Exception as error:
            return _outcome(scenario, "UNKNOWN", steps=steps, checks=checks, error=f"Recompiled procedure capture failed: {error}")
        if not final_definition.valid:
            return _outcome(scenario, "FAILED", steps=steps, checks=checks, error=f"Recompiled procedure {proc_name} is INVALID")

        final_settings = _read_compiler_settings(target, proc_name, context, run, runner, identity)
        expected_final_settings = {**settings, "plsql_ccflags": desired_ccflags}
        if final_settings != expected_final_settings:
            return _outcome(scenario, "FAILED", steps=steps, checks=checks, error="Recompiled procedure settings do not match the requested CCFLAGS variant")

        variant_checks = _compiler_check_set(
            final_settings, proc_name, target.schema,
            prefix="variant", include_all_wrong_settings=False,
        )
        checks.extend(_run_checked_phase(
            target, scenario, variant_checks, run, runner,
            phase="postconditions", expected_identity=identity,
            bindings=context.values, failure_status="FAILED",
        ))
        fixture_definitions = [_inventory_object(target, proc_name, "PROCEDURE", final_inventory, final_definition, _final_snapshot)]
        _save_fixture_inventory(
            run, scenario.id, final_inventory.identity, fixture_definitions,
            details={"initialCompilerSettings": settings, "finalCompilerSettings": final_settings},
        )
        return _outcome(scenario, "PASSED", steps=steps, checks=checks)
    except _ScenarioProblem as error:
        checks.extend(error.records)
        return _outcome(scenario, error.status, steps=steps, checks=checks, error=str(error))
    except Exception as error:
        status = "UNKNOWN" if any(step.get("writeStarted") is True for step in steps) else "BLOCKED"
        return _outcome(scenario, status, steps=steps, checks=checks, error=f"Compiler-settings scenario could not be verified: {error}")
