"""DEV test runtime execution, sequential project scenario runner, and ambiguous outcome handling (Task 5).

Provides:
    - Row: dictionary subclass with integer indexing support for parsed query rows.
    - read_rows: bounded query execution with explicit columns/rows/truncated/identity evidence.
    - execute_step: single scenario step execution with durable intent and expected error verification.
    - execute_project_scenario: sequential execution of selectors, steps, fresh-session checks, and observations.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.db_targets import (
    ORACLE_IDENTIFIER,
    SQLCL_ALIAS,
    Target,
    looks_like_production_identity,
)
from scripts.dev_test_bindings import (
    MAX_SAFE_INTEGER,
    MIN_SAFE_INTEGER,
    RenderContext,
    render_object_tokens,
)
from scripts.dev_test_evidence import (
    RunEvidence,
    append_event,
    assert_inputs_unchanged,
)
from scripts.dev_test_manifest import Scenario
from scripts.migration_checks import run_checks
from scripts.migration_manifest import QueryCheck
from scripts.read_only_query import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CAPTURE_BYTES,
    prepare_query,
)
from scripts.rollout import validate_sql_script
from scripts.sqlcl_session import (
    SqlclCancelled,
    SqlclError,
    SqlclResult,
    SqlclTimeout,
    run_sqlcl,
)
from scripts.sqlcl_transport import (
    generated_clob_assembly,
    lint_sqlcl_transport,
)

_SQL_PRODUCTION_MARKER = (
    "(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])"
)
_SQL_NON_PRODUCTION_MARKER = "(pre|non)[-_.]?(prod|prd)"
_MARKER_RE = re.compile(r"^ROQ_[0-9a-f]{32}\Z")
_MAX_READ_ROWS = 100


class Row(dict):
    """Row record mapping column names to string/scalar values while supporting integer indexing."""

    def __init__(self, mapping: Mapping[str, Any], sequence: Sequence[Any] | None = None) -> None:
        super().__init__(mapping)
        self._seq = tuple(sequence) if sequence is not None else tuple(mapping.values())

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._seq[key]
        return super().__getitem__(key)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _target_is_dev(target: Target) -> bool:
    return (
        target.environment == "dev"
        and target.classification in {"development", "test"}
        and SQLCL_ALIAS.fullmatch(target.connection) is not None
        and ORACLE_IDENTIFIER.fullmatch(target.expected_user) is not None
        and ORACLE_IDENTIFIER.fullmatch(target.schema) is not None
        and target.expected_user == target.schema
        and not looks_like_production_identity(target.connection)
    )


_CORE_IDENTITY_FIELDS = (
    "session_user",
    "current_schema",
    "db_name",
    "db_unique_name",
    "service_name",
)


def _same_live_target(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    for field in _CORE_IDENTITY_FIELDS:
        expected_value = expected.get(field)
        observed_value = observed.get(field)
        if (
            not isinstance(expected_value, str)
            or not expected_value.strip()
            or not isinstance(observed_value, str)
            or not observed_value.strip()
            or expected_value.upper() != observed_value.upper()
        ):
            return False
    return True


def _project_fixture_names(scenario: Scenario, context: RenderContext) -> dict[str, str]:
    names: dict[str, str] = {}
    for fixture in scenario.fixtures.get("objects", ()):
        binding = fixture["binding"]
        name = context.objects.get(binding)
        if not isinstance(name, str) or not ORACLE_IDENTIFIER.fullmatch(name):
            raise ValueError(f"fixture object binding {binding!r} is missing or invalid")
        names[name] = str(fixture["type"]).upper()
    return names


def _record_project_precreation(
    target: Target,
    scenario: Scenario,
    context: RenderContext,
    run: RunEvidence,
    expected_identity: Mapping[str, Any] | None,
    selectors: Sequence[Mapping[str, Any]],
    runner: Callable,
) -> tuple[Mapping[str, Any], dict[str, str]]:
    from scripts.schema_catalog import SchemaInventory, capture_inventory

    names = _project_fixture_names(scenario, context)
    inventory = capture_inventory(target, run.run_dir, _runner=runner)
    if not isinstance(inventory, SchemaInventory) or inventory.coverage.get("ownerComplete") is not True:
        raise ValueError("complete owner inventory was not available before project scenario writes")
    if not isinstance(inventory.identity, Mapping) or (
        expected_identity is not None and not _same_live_target(expected_identity, inventory.identity)
    ):
        raise ValueError("project pre-creation inventory observed a different target identity")
    present_names = {key.name.upper() for key in inventory.objects}
    collisions = sorted(set(names) & present_names)
    if collisions:
        raise ValueError("project fixture names already exist in the selected owner: " + ", ".join(collisions))
    row_keys = [
        {
            "scenarioId": scenario.id,
            "selectorId": selector["id"],
            "inputBindings": dict(selector["inputBindings"]),
            "bindings": dict(selector["bindings"]),
            "columns": [dict(column) for column in selector["columns"]],
        }
        for selector in selectors
    ]
    append_event(run, {
        "type": "fixture-precreation",
        "scenarioId": scenario.id,
        "targetIdentity": dict(inventory.identity),
        "ownerComplete": True,
        "projectCleanup": True,
        "plannedObjects": [
            {"owner": target.schema, "name": name, "type": object_type, "absent": True}
            for name, object_type in sorted(names.items())
        ],
        "objects": [
            {"owner": target.schema, "name": name, "type": object_type, "absent": True}
            for name, object_type in sorted(names.items())
        ],
        "rows": row_keys,
    })
    return dict(inventory.identity), names


def _save_project_cleanup_inventory(
    target: Target,
    scenario: Scenario,
    context: RenderContext,
    run: RunEvidence,
    expected_identity: Mapping[str, Any],
    object_names: Mapping[str, str],
    selector_records: Sequence[Mapping[str, Any]],
    runner: Callable,
) -> None:
    from scripts.dev_test_core_types import _inventory_object, _save_fixture_inventory
    from scripts.schema_catalog import (
        CatalogError,
        ObjectKey,
        SchemaInventory,
        SchemaSnapshot,
        capture_inventory,
        capture_snapshot,
    )

    inventory = capture_inventory(target, run.run_dir, _runner=runner)
    if not isinstance(inventory, SchemaInventory) or inventory.coverage.get("ownerComplete") is not True:
        raise CatalogError("complete owner inventory was not available after the project scenario")
    if not isinstance(inventory.identity, Mapping) or not _same_live_target(expected_identity, inventory.identity):
        raise CatalogError("project post-scenario inventory observed a different target identity")
    artifacts: list[dict[str, Any]] = []
    for name, object_type in object_names.items():
        matching_names = [key for key in inventory.objects if key.owner == target.schema and key.name == name]
        if not matching_names:
            continue
        key = ObjectKey(target.schema, name, object_type)
        if key not in inventory.objects:
            raise CatalogError(f"project fixture {name} exists with an unexpected object type")
        snapshot = capture_snapshot(target, inventory, [(name, object_type)], run.run_dir, _runner=runner)
        if not isinstance(snapshot, SchemaSnapshot) or not _same_live_target(expected_identity, snapshot.identity):
            raise CatalogError(f"project fixture definition for {name} did not verify target identity")
        definition = snapshot.objects.get(key)
        if definition is None:
            raise CatalogError(f"project fixture definition is incomplete for {name}")
        artifacts.append(_inventory_object(target, name, object_type, inventory, definition, snapshot))

    cleanup = scenario.cleanup
    if not isinstance(cleanup, Mapping):
        raise CatalogError("project cleanup contract is missing")
    contract = {
        "kind": "project",
        "sqlFiles": [
            {"path": str(item["path"]), "sha256": item["sha256"]}
            for item in cleanup["sqlFiles"]
        ],
        "checks": [
            {"id": item["id"], "sql": item["sql"], "expected": item["expected"]}
            for item in cleanup["checks"]
        ],
        "selectors": [dict(item) for item in selector_records],
        "bindings": dict(context.values),
        "objects": dict(object_names),
        "objectBindings": dict(context.objects),
    }
    row_keys = [
        {
            "selectorId": selector["id"],
            "inputBindings": dict(selector["inputBindings"]),
            "bindings": dict(selector["bindings"]),
            "columns": [dict(column) for column in selector["columns"]],
        }
        for selector in selector_records
    ]
    _save_fixture_inventory(
        run,
        scenario.id,
        inventory.identity,
        artifacts,
        row_keys=row_keys,
        cleanup_contract=contract,
        planned_objects=object_names,
    )


def _verified_oracle_error_code(
    error: SqlclError,
    output: str,
    *,
    step_start_end: int,
    completion_count: int,
) -> str | None:
    """Return an Oracle code only from a completed, framed SQLcl error exit."""
    if completion_count != 0:
        return None
    # run_sqlcl includes this exact outcome only after the SQLcl process completed
    # with a nonzero exit status. Timeouts, client-only diagnostics, output caps,
    # process-start errors, and diagnostic-preservation failures use other reasons.
    if re.match(r"SQLcl exited with status [1-9][0-9]*; diagnostics: .+\Z", str(error)) is None:
        return None

    step_output = output[step_start_end:]
    report = re.search(r"(?im)^\s*Error report -\s*$", step_output)
    if report is None:
        return None
    error_output = step_output[report.end():]
    oracle_error = re.search(
        r"(?im)^\s*(?:SQL Error:\s*)?(ORA-[0-9]{5}):\s*[^\r\n]+$",
        error_output,
    )
    return oracle_error.group(1).upper() if oracle_error else None


def _frame_positions(output: str, marker: str, suffix: str) -> list[tuple[int, int]]:
    wanted = marker + suffix
    matches: list[tuple[int, int]] = []
    offset = 0
    for line in output.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if content == wanted:
            matches.append((offset, offset + len(line)))
        offset += len(line)
    return matches


def read_rows(
    target: Target,
    sql: str,
    context: RenderContext,
    run: RunEvidence,
    *,
    max_rows: int,
    runner: Callable = run_sqlcl,
) -> dict:
    """Execute a bounded read-only query in a fresh session and return structured rows and evidence."""
    if type(max_rows) is not int or not 1 <= max_rows <= _MAX_READ_ROWS:
        raise ValueError(f"max_rows must be an integer between 1 and {_MAX_READ_ROWS}")
    if not _target_is_dev(target):
        raise RuntimeError("read_rows refuses any target outside a non-production DEV profile")

    rendered_sql = render_object_tokens(sql, context)
    prepared = prepare_query(rendered_sql, bindings=context.values)

    marker = "ROQ_" + secrets.token_hex(16)
    identity_attributes = (
        ("DB_NAME", "db_name"),
        ("DB_UNIQUE_NAME", "db_unique_name"),
        ("SERVICE_NAME", "service_name"),
    )
    identity_conditions = [
        "UPPER(SYS_CONTEXT('USERENV','SESSION_USER')) = " + _sql_literal(target.expected_user),
        "UPPER(SYS_CONTEXT('USERENV','CURRENT_SCHEMA')) = " + _sql_literal(target.schema),
    ]
    identity_expressions = {
        key: f"SYS_CONTEXT('USERENV','{attribute}')" for attribute, key in identity_attributes
    }
    identity_conditions.extend(
        f"TRIM({identity_expressions[key]}) IS NOT NULL" for _, key in identity_attributes
    )
    if target.environment != "prod":
        production_checks = []
        for _, key in identity_attributes:
            expression = identity_expressions[key]
            production_checks.append(
                "REGEXP_LIKE(REGEXP_REPLACE("
                + expression
                + ", "
                + _sql_literal(_SQL_NON_PRODUCTION_MARKER)
                + ", ' ', 1, 0, 'i'), "
                + _sql_literal(_SQL_PRODUCTION_MARKER)
                + ", 'i')"
            )
        identity_conditions.append("NOT (" + " OR ".join(production_checks) + ")")

    target_identity_check = (
        "SELECT CASE WHEN "
        + " AND ".join(identity_conditions)
        + " THEN 1 ELSE TO_NUMBER('INVALID_QUERY_TARGET') END FROM DUAL;"
    )
    identity_query = (
        "SELECT '" + marker + "_I:' || UPPER(SYS_CONTEXT('USERENV','SESSION_USER')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','CURRENT_SCHEMA')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','DB_NAME')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')) || '|' || "
        "UPPER(SYS_CONTEXT('USERENV','SERVICE_NAME')) FROM DUAL;"
    )

    variable_lines: list[str] = []
    assignment_lines: list[str] = []
    if context.values:
        for name in sorted(context.values.keys()):
            val = context.values[name]
            clean_name = name.lstrip(":")
            if isinstance(val, int):
                variable_lines.append(f"VARIABLE {clean_name} NUMBER")
                assignment_lines.append(f"  :{clean_name} := {val};")
            elif isinstance(val, str):
                variable_lines.append(f"VARIABLE {clean_name} VARCHAR2(4000)")
                hex_raw = val.encode("utf-8").hex().upper()
                assignment_lines.append(
                    f"  :{clean_name} := UTL_I18N.RAW_TO_CHAR(HEXTORAW('{hex_raw}'), 'AL32UTF8');"
                )

    driver_lines: list[str] = [
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "SET DEFINE OFF",
        "SET ECHO OFF",
        "SET VERIFY OFF",
        "SET FEEDBACK OFF",
        "SET HEADING OFF",
        "SET PAGESIZE 0",
        "SET TRIMSPOOL ON",
        "SET TRIMOUT ON",
        "SET LINESIZE 32767",
        "SET LONG 1048576",
        "SET LONGCHUNKSIZE 32767",
        "SET SQLFORMAT DEFAULT",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "ALTER SESSION DISABLE COMMIT IN PROCEDURE;",
        "SET TRANSACTION READ ONLY;",
        target_identity_check,
        f"PROMPT {marker}_IDENTITY_BEGIN",
        identity_query,
        f"PROMPT {marker}_IDENTITY_END",
    ]
    if variable_lines:
        driver_lines.extend(variable_lines)
        driver_lines.append("BEGIN")
        driver_lines.extend(assignment_lines)
        driver_lines.append("END;")
        driver_lines.append("/")
    driver_lines.extend([
        f"PROMPT {marker}_DATA_BEGIN",
        "SET HEADING ON",
        "SET SQLFORMAT CSV",
        f"SELECT * FROM ( {prepared} ) WHERE ROWNUM <= {max_rows + 1};",
        f"PROMPT {marker}_DATA_END",
        "SET SQLFORMAT DEFAULT",
        f"PROMPT {marker}_SUCCESS",
        "EXIT SUCCESS ROLLBACK",
        "",
    ])

    session_dir = run.run_dir / "sessions" / f"read-rows-{secrets.token_hex(8)}"
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    driver_path = session_dir / "driver.sql"
    driver_path.write_text("\n".join(driver_lines), encoding="utf-8", newline="\n")
    try:
        driver_path.chmod(0o600)
    except OSError:
        pass

    try:
        result = runner(
            target,
            driver_path,
            session_dir,
            phase="inventory",
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            output_limit_bytes=MAX_CAPTURE_BYTES,
        )
    except (SqlclTimeout, SqlclCancelled):
        raise
    except SqlclError as error:
        raise RuntimeError(f"read_rows SQLcl execution failed: {error}") from error

    if isinstance(result, SqlclResult):
        if type(result.returncode) is not int or result.returncode != 0:
            raise RuntimeError(f"SQLcl query returned a nonzero exit status: {result.returncode}")
        output = result.output
    else:
        output = str(result)
    frames = {
        name: _frame_positions(output, marker, suffix)
        for name, suffix in (
            ("identity_begin", "_IDENTITY_BEGIN"),
            ("identity_end", "_IDENTITY_END"),
            ("data_begin", "_DATA_BEGIN"),
            ("data_end", "_DATA_END"),
            ("success", "_SUCCESS"),
        )
    }
    if any(len(positions) != 1 for positions in frames.values()):
        raise RuntimeError("Query output is missing or duplicates a required verification frame")

    identity_begin = frames["identity_begin"][0]
    identity_end = frames["identity_end"][0]
    data_begin = frames["data_begin"][0]
    data_end = frames["data_end"][0]
    success = frames["success"][0]

    if not (identity_begin[1] <= identity_end[0] < data_begin[0] < data_begin[1] <= data_end[0] < success[0]):
        raise RuntimeError("Query output verification frames are out of order")

    identity_text = output[identity_begin[1] : identity_end[0]]
    identity_lines = [line.strip() for line in identity_text.splitlines() if line.strip()]
    prefix = marker + "_I:"
    if len(identity_lines) != 1 or not identity_lines[0].startswith(prefix):
        raise RuntimeError("Target identity frame is invalid")
    identity_parts = identity_lines[0][len(prefix) :].split("|")
    if (
        len(identity_parts) != 5
        or identity_parts[0].upper() != target.expected_user.upper()
        or identity_parts[1].upper() != target.schema.upper()
        or any(not v.strip() for v in identity_parts[2:])
    ):
        raise RuntimeError("Target identity did not match expected user, schema, database, or service")

    if target.environment != "prod" and looks_like_production_identity(*identity_parts[2:]):
        raise RuntimeError("Observed database/service identity resembles production for non-prod target")

    observed_identity = {
        "session_user": identity_parts[0],
        "current_schema": identity_parts[1],
        "db_name": identity_parts[2],
        "db_unique_name": identity_parts[3],
        "service_name": identity_parts[4],
    }

    csv_text = output[data_begin[1] : data_end[0]]
    try:
        reader = csv.reader(io.StringIO(csv_text, newline=""), strict=True)
        csv_rows = list(reader)
    except csv.Error as error:
        raise RuntimeError(f"Query output is not complete CSV data: {error}") from error
    if not csv_rows:
        raise RuntimeError("Query output has no column headings")

    header = [c.strip().strip('"').upper() for c in csv_rows[0]]
    if not header or any(not name for name in header) or len(header) != len(set(header)):
        raise RuntimeError("Query output contains empty or duplicate column headings")
    raw_data = csv_rows[1:]
    if len(raw_data) > max_rows:
        truncated = True
        raw_data = raw_data[:max_rows]
    else:
        truncated = False

    rows: list[Row] = []
    for r in raw_data:
        if len(r) != len(header):
            raise RuntimeError("Query output row width does not match its column headings")
        row_dict = {}
        for idx, col in enumerate(header):
            row_dict[col] = r[idx]
        rows.append(Row(row_dict, r))

    return {
        "columns": header,
        "rows": rows,
        "truncated": truncated,
        "count": len(rows),
        "identity": observed_identity,
        "evidence": str(session_dir / "sqlcl-output.log"),
    }


def execute_step(
    target: Target,
    scenario: Scenario,
    step: Mapping,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict:
    """Execute a single scenario step with pre-write intent, exact start/identity checks, and error verification."""
    step_id = str(step.get("id", ""))
    raw_expected = step.get("expected", {})
    expected_record = dict(raw_expected) if isinstance(raw_expected, Mapping) else {}
    try:
        source_bytes = step.get("source")
        if isinstance(source_bytes, bytes):
            source_str = source_bytes.decode("utf-8")
        elif isinstance(source_bytes, str):
            source_str = source_bytes
        else:
            step_path = Path(step["path"]) if "path" in step else Path(step["sqlFile"])
            source_str = step_path.read_text(encoding="utf-8")

        validate_sql_script(source_str, number=1)
        rendered_sql = render_object_tokens(source_str, context)
    except (KeyError, OSError, TypeError, UnicodeError, ValueError) as error:
        return {
            "id": step_id,
            "status": "BLOCKED",
            "phase": "preflight",
            "expected": expected_record,
            "error": f"Scenario step failed local SQL preflight: {error}",
            "evidence": "",
            "writeStarted": False,
        }

    mode = "assembled" if generated_clob_assembly(rendered_sql) else "direct"
    for finding in lint_sqlcl_transport(rendered_sql, mode):
        if finding.severity == "error":
            return {
                "id": step_id,
                "status": "UNKNOWN",
                "phase": "preflight",
                "expected": expected_record,
                "error": f"SQLcl transport preflight refused: payload.sql:{finding.line} ({finding.code}): {finding.message}",
                "evidence": "",
                "writeStarted": False,
            }

    session_dir = run.run_dir / "sessions" / f"{scenario.id}-{step_id}-{secrets.token_hex(6)}"
    payload_path = session_dir / "payload.sql"
    try:
        session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload_path.write_text(rendered_sql, encoding="utf-8", newline="\n")
        try:
            payload_path.chmod(0o600)
        except OSError:
            raise
    except OSError as error:
        return {
            "id": step_id,
            "status": "BLOCKED",
            "phase": "preflight",
            "expected": expected_record,
            "error": f"Cannot prepare private scenario session files: {error}",
            "evidence": str(session_dir),
            "writeStarted": False,
        }

    if not _target_is_dev(target):
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "preflight",
            "expected": expected_record,
            "error": "Scenario steps refuse any target outside a non-production DEV profile",
            "evidence": "",
            "writeStarted": False,
        }

    source_sha256 = hashlib.sha256(source_str.encode("utf-8")).hexdigest()
    expected_source_sha256 = step.get("sha256")
    if expected_source_sha256 is not None and expected_source_sha256 != source_sha256:
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "preflight",
            "expected": expected_record,
            "error": "Step source does not match its frozen SHA-256",
            "evidence": "",
            "writeStarted": False,
        }

    variable_lines: list[str] = []
    assignment_lines: list[str] = []
    if context.values:
        for name in sorted(context.values.keys()):
            val = context.values[name]
            clean_name = name.lstrip(":")
            if isinstance(val, int):
                variable_lines.append(f"VARIABLE {clean_name} NUMBER")
                assignment_lines.append(f"  :{clean_name} := {val};")
            elif isinstance(val, str):
                variable_lines.append(f"VARIABLE {clean_name} VARCHAR2(4000)")
                hex_raw = val.encode("utf-8").hex().upper()
                assignment_lines.append(
                    f"  :{clean_name} := UTL_I18N.RAW_TO_CHAR(HEXTORAW('{hex_raw}'), 'AL32UTF8');"
                )

    driver_lines: list[str] = [
        "SET DEFINE OFF",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "DECLARE",
        f"  c_target_environment CONSTANT VARCHAR2(16) := '{target.environment}';",
        "  v_session_user VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');",
        "  v_database_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_NAME');",
        "  v_db_unique_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME');",
        "  v_service_name VARCHAR2(256) := SYS_CONTEXT('USERENV', 'SERVICE_NAME');",
        "  v_owner_count PLS_INTEGER;",
        "  c_production_marker CONSTANT VARCHAR2(256) :=",
        "    '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])';",
        "  c_non_production_marker CONSTANT VARCHAR2(64) := '(pre|non)[-_.]?(prod|prd)';",
        "  FUNCTION resembles_production(p_name VARCHAR2) RETURN BOOLEAN IS",
        "  BEGIN",
        "    RETURN REGEXP_LIKE(REGEXP_REPLACE(p_name, c_non_production_marker, ' ', 1, 0, 'i'), c_production_marker, 'i');",
        "  END;",
        "BEGIN",
        f"  IF UPPER(v_session_user) <> '{target.expected_user}' THEN",
        "    RAISE_APPLICATION_ERROR(-20071, 'rollout identity mismatch');",
        "  END IF;",
        "  IF v_database_name IS NULL OR v_db_unique_name IS NULL THEN",
        "    RAISE_APPLICATION_ERROR(-20072, 'rollout database identity is unavailable');",
        "  END IF;",
        f"  SELECT COUNT(*) INTO v_owner_count FROM all_users WHERE username = '{target.schema}';",
        "  IF v_owner_count <> 1 THEN",
        "    RAISE_APPLICATION_ERROR(-20073, 'rollout target schema is not visible');",
        "  END IF;",
        "  IF c_target_environment <> 'prod' AND (",
        "       resembles_production(v_database_name)",
        "       OR resembles_production(v_db_unique_name)",
        "       OR resembles_production(v_service_name)) THEN",
        "    RAISE_APPLICATION_ERROR(-20074, 'rollout target identity resembles production');",
        "  END IF;",
        "END;",
        "/",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "DECLARE",
        "  v_session_user VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');",
        "  v_current_schema VARCHAR2(128) := SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA');",
        "  v_database_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_NAME');",
        "  v_db_unique_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME');",
        "  v_service_name VARCHAR2(256) := SYS_CONTEXT('USERENV', 'SERVICE_NAME');",
        "BEGIN",
        f"  IF UPPER(v_session_user) <> '{target.expected_user}' OR UPPER(v_current_schema) <> '{target.schema}' THEN",
        "    RAISE_APPLICATION_ERROR(-20071, 'rollout identity mismatch');",
        "  END IF;",
        "  DBMS_OUTPUT.PUT_LINE('ROLLOUT_IDENTITY:' || UPPER(v_session_user) || ':' || UPPER(v_current_schema));",
        "  DBMS_OUTPUT.PUT_LINE('ROLLOUT_TARGET:' || UPPER(NVL(v_database_name, '<NO_DB_NAME>')) || ':' || UPPER(NVL(v_db_unique_name, '<NO_DB_UNIQUE_NAME>')) || ':' || UPPER(NVL(v_service_name, '<NO_SERVICE>')));",
        "END;",
        "/",
    ]
    if variable_lines:
        driver_lines.extend(variable_lines)
        driver_lines.append("BEGIN")
        driver_lines.extend(assignment_lines)
        driver_lines.append("END;")
        driver_lines.append("/")

    driver_lines.extend([
        f"PROMPT DEVTEST_STEP_START:{step_id}",
        "SET SQLBLANKLINES ON",
        "@@payload.sql",
        f"PROMPT DEVTEST_STEP_COMPLETED:{step_id}",
        # Keep the caller's explicit transaction behavior; never use SQLcl's implicit exit commit.
        "EXIT SUCCESS ROLLBACK",
        "",
    ])

    driver_content = "\n".join(driver_lines)
    for finding in lint_sqlcl_transport(driver_content, "direct"):
        if finding.severity == "error":
            return {
                "id": step_id,
                "status": "UNKNOWN",
                "phase": "preflight",
                "expected": expected_record,
                "error": f"SQLcl transport preflight refused: driver.sql:{finding.line} ({finding.code}): {finding.message}",
                "evidence": "",
                "writeStarted": False,
            }

    try:
        assert_inputs_unchanged(run)
    except (OSError, ValueError) as error:
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "preflight",
            "expected": expected_record,
            "error": f"Frozen inputs changed or could not be verified before step execution: {error}",
            "evidence": "",
            "writeStarted": False,
        }

    driver_path = session_dir / "driver.sql"
    try:
        driver_path.write_text(driver_content, encoding="utf-8", newline="\n")
        driver_path.chmod(0o600)
    except OSError as error:
        return {
            "id": step_id,
            "status": "BLOCKED",
            "phase": "preflight",
            "expected": expected_record,
            "error": f"Cannot prepare private SQLcl driver file: {error}",
            "evidence": str(session_dir),
            "writeStarted": False,
        }

    # Durable intent follows every local refusal check and precedes runner dispatch.
    rendered_sql_sha256 = hashlib.sha256(rendered_sql.encode("utf-8")).hexdigest()
    try:
        append_event(
            run,
            {
                "type": "step-intent",
                "scenarioId": scenario.id,
                "stepId": step_id,
                "target": {
                    "environment": target.environment,
                    "connection": target.connection,
                    "schema": target.schema,
                    "expectedUser": target.expected_user,
                },
                "inputHashes": {"stepSql": source_sha256},
                "renderedSqlSha256": rendered_sql_sha256,
                "writeStarted": True,
            },
        )
    except (OSError, ValueError) as error:
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "intent",
            "expected": expected_record,
            "error": f"Durable step intent could not be recorded; SQLcl was not started: {error}",
            "evidence": str(run.events_dir),
            "writeStarted": False,
        }

    evidence_file = session_dir / "sqlcl-output.log"
    step_expected = expected_record
    expected_kind = step_expected.get("kind", "success")

    try:
        result = runner(
            target,
            driver_path,
            session_dir,
            phase="apply",
            output_limit_bytes=MAX_CAPTURE_BYTES,
        )
        if isinstance(result, SqlclResult):
            output = result.output
            if type(result.returncode) is int and result.returncode == 0:
                sqlcl_error = None
            else:
                sqlcl_error = SqlclError(
                    f"SQLcl exited with status {result.returncode}; diagnostics: {evidence_file}",
                    session_dir,
                    output=output,
                )
        else:
            output = str(result)
            sqlcl_error = None
    except (SqlclTimeout, SqlclCancelled) as err:
        # An interrupted SQLcl process remains ambiguous even if its partial output contains an ORA code.
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "apply",
            "expected": step_expected,
            "error": str(err),
            "evidence": str(evidence_file),
            "writeStarted": True,
        }
    except SqlclError as err:
        output = getattr(err, "output", "")
        sqlcl_error = err
    except Exception as err:
        # Any unexpected runner failure after durable intent leaves the write outcome ambiguous.
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "apply",
            "expected": step_expected,
            "error": f"SQLcl runner failed after step intent: {err}",
            "evidence": str(evidence_file),
            "writeStarted": True,
        }

    # Frame inspection: check identity and step-start
    identity_matches = list(re.finditer(r"(?m)^\s*ROLLOUT_IDENTITY:([A-Z][A-Z0-9_$#]*):([A-Z][A-Z0-9_$#]*)\s*$", output))
    target_matches = list(re.finditer(r"(?m)^\s*ROLLOUT_TARGET:([A-Z0-9_.<>-]+):([A-Z0-9_.<>-]+):([A-Z0-9_.<>-]+)\s*$", output))
    step_start_matches = list(re.finditer(rf"(?m)^\s*DEVTEST_STEP_START:{re.escape(step_id)}\s*$", output))
    step_completions = re.findall(rf"(?m)^\s*DEVTEST_STEP_COMPLETED:{re.escape(step_id)}\s*$", output)

    identity_ok = (
        len(identity_matches) == 1
        and identity_matches[0].groups() == (target.expected_user.upper(), target.schema.upper())
    )
    observed_target_ok = (
        len(target_matches) == 1
        and all(not part.startswith("<NO_") for part in target_matches[0].groups())
        and not (
            target.environment != "prod"
            and looks_like_production_identity(*target_matches[0].groups())
        )
    )
    start_ok = len(step_start_matches) == 1
    frame_order_ok = (
        identity_ok
        and start_ok
        and identity_matches[0].start() < step_start_matches[0].start()
    )

    if not identity_ok or not observed_target_ok or not start_ok or not frame_order_ok:
        # Missing or unverified start/identity frames -> UNKNOWN
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "apply",
            "expected": step_expected,
            "error": "Missing or invalid identity/step-start verification frame",
            "evidence": str(evidence_file),
            "writeStarted": True,
        }

    target_values = target_matches[0].groups()
    step_identity = {
        "session_user": identity_matches[0].group(1),
        "current_schema": identity_matches[0].group(2),
        "db_name": target_values[0],
        "db_unique_name": target_values[1],
        "service_name": target_values[2],
    }

    if sqlcl_error is not None:
        oracle_error_code = _verified_oracle_error_code(
            sqlcl_error,
            output,
            step_start_end=step_start_matches[0].end(),
            completion_count=len(step_completions),
        )
        if expected_kind == "oracle-error":
            expected_code = step_expected.get("code", "")
            if expected_code and oracle_error_code == expected_code:
                return {
                    "id": step_id,
                    "status": "PASSED",
                    "phase": "apply",
                    "expected": step_expected,
                    "identity": step_identity,
                    "evidence": str(evidence_file),
                    "writeStarted": True,
                }
            if oracle_error_code is None:
                return {
                    "id": step_id,
                    "status": "UNKNOWN",
                    "phase": "apply",
                    "expected": step_expected,
                    "error": f"Expected Oracle error outcome could not be verified: {sqlcl_error}",
                    "identity": step_identity,
                    "evidence": str(evidence_file),
                    "writeStarted": True,
                }
            return {
                "id": step_id,
                "status": "FAILED",
                "phase": "apply",
                "expected": step_expected,
                "error": (
                    f"SQLcl raised unexpected Oracle error {oracle_error_code}; "
                    f"expected {expected_code}: {sqlcl_error}"
                ),
                "identity": step_identity,
                "evidence": str(evidence_file),
                "writeStarted": True,
            }
        else:
            if oracle_error_code is None:
                return {
                    "id": step_id,
                    "status": "UNKNOWN",
                    "phase": "apply",
                    "expected": step_expected,
                    "error": f"SQLcl failure outcome could not be verified: {sqlcl_error}",
                    "identity": step_identity,
                    "evidence": str(evidence_file),
                    "writeStarted": True,
                }
            return {
                "id": step_id,
                "status": "FAILED",
                "phase": "apply",
                "expected": step_expected,
                "error": f"Unexpected Oracle error {oracle_error_code}: {sqlcl_error}",
                "identity": step_identity,
                "evidence": str(evidence_file),
                "writeStarted": True,
            }

    # Exit 0 case
    if len(step_completions) != 1:
        return {
            "id": step_id,
            "status": "UNKNOWN",
            "phase": "apply",
            "expected": step_expected,
            "error": "Missing step completion confirmation frame",
            "identity": step_identity,
            "evidence": str(evidence_file),
            "writeStarted": True,
        }

    if expected_kind == "oracle-error":
        return {
            "id": step_id,
            "status": "FAILED",
            "phase": "apply",
            "expected": step_expected,
            "error": "Step completed successfully when an Oracle error was expected",
            "identity": step_identity,
            "evidence": str(evidence_file),
            "writeStarted": True,
        }

    return {
        "id": step_id,
        "status": "PASSED",
        "phase": "apply",
        "expected": step_expected,
        "identity": step_identity,
        "evidence": str(evidence_file),
        "writeStarted": True,
    }


def execute_project_scenario(
    target: Target,
    scenario: Scenario,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict:
    """Execute a project scenario sequentially with frozen inputs, selectors, steps, and assertions."""
    # 1. Assert frozen inputs unchanged before any selector or step can run.
    try:
        assert_inputs_unchanged(run)
    except (OSError, ValueError) as error:
        return {
            "id": scenario.id,
            "status": "BLOCKED",
            "eligible": False,
            "steps": [],
            "checks": [],
            "observations": [],
            "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
            "error": f"Frozen inputs changed or could not be verified: {error}",
        }

    if not _target_is_dev(target):
        return {
            "id": scenario.id,
            "status": "BLOCKED",
            "eligible": False,
            "steps": [],
            "checks": [],
            "observations": [],
            "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
            "error": "Project scenarios require a non-production DEV target",
        }

    effects = scenario.effects
    transaction = effects.get("transaction") if isinstance(effects, Mapping) else None
    external = effects.get("external") if isinstance(effects, Mapping) else None
    if transaction == "unknown" or external != "none":
        return {
            "id": scenario.id,
            "status": "BLOCKED",
            "eligible": False,
            "steps": [],
            "checks": [],
            "observations": [],
            "effects": list(effects) if isinstance(effects, (list, tuple)) else dict(effects),
            "error": "Scenario effects must have a reviewed transaction classification and external=none",
        }

    if not scenario.checks and any(
        step.get("expected", {}).get("kind") == "oracle-error" for step in scenario.steps
    ):
        return {
            "id": scenario.id,
            "status": "BLOCKED",
            "eligible": False,
            "steps": [],
            "checks": [],
            "observations": [],
            "effects": list(effects) if isinstance(effects, (list, tuple)) else dict(effects),
            "error": "Expected Oracle-error scenarios require independent postcondition checks",
        }

    current_context = context
    scenario_identity: Mapping[str, Any] | None = None
    selector_records: list[dict[str, Any]] = []
    project_expected_identity: Mapping[str, Any] | None = None
    project_object_names: dict[str, str] = {}

    # 2. Selectors evaluation
    selectors = scenario.fixtures.get("selectors", ())
    for sel in selectors:
        sel_sql = sel["sql"]
        selector_input_values = dict(current_context.values)
        try:
            res = read_rows(target, sel_sql, current_context, run, max_rows=2, runner=runner)
        except Exception as exc:
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": True,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Selector query failed: {exc}",
            }

        if res["count"] == 0:
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": True,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Selector {sel['id']!r} returned 0 rows (expected exactly 1)",
            }
        if res["count"] > 1:
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": True,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Selector {sel['id']!r} returned multiple rows (expected exactly 1)",
            }
        if res.get("truncated") is not False:
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": False,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Selector {sel['id']!r} did not prove a complete one-row result",
            }

        selector_identity = res.get("identity")
        if not isinstance(selector_identity, Mapping):
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": False,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Selector {sel['id']!r} did not return verified target identity",
            }
        if scenario_identity is None:
            scenario_identity = selector_identity
        elif not _same_live_target(scenario_identity, selector_identity):
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": False,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Selector {sel['id']!r} observed a different live database identity",
            }

        row = res["rows"][0]
        new_values = dict(current_context.values)
        selector_outputs: dict[str, int | str] = {}
        for col in sel["columns"]:
            col_name = col["name"]
            col_binding = col["binding"]
            col_type = col["type"]

            if col_name not in row:
                return {
                    "id": scenario.id,
                    "status": "BLOCKED",
                    "eligible": True,
                    "steps": [],
                    "checks": [],
                    "observations": [],
                    "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                    "error": f"Selector {sel['id']!r} result missing declared column {col_name!r}",
                }

            val = row[col_name]
            if val is None or val == "":
                return {
                    "id": scenario.id,
                    "status": "BLOCKED",
                    "eligible": True,
                    "steps": [],
                    "checks": [],
                    "observations": [],
                    "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                    "error": f"Selector {sel['id']!r} column {col_name!r} returned null or empty value",
                }

            if col_type == "integer":
                if type(val) is int:
                    parsed_int = val
                elif isinstance(val, str) and re.fullmatch(r"[+-]?[0-9]+", val.strip(), re.ASCII):
                    parsed_int = int(val.strip())
                else:
                    return {
                        "id": scenario.id,
                        "status": "BLOCKED",
                        "eligible": True,
                        "steps": [],
                        "checks": [],
                        "observations": [],
                        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                        "error": f"Selector {sel['id']!r} integer column {col_name!r} has invalid value: {val!r}",
                    }
                if not (MIN_SAFE_INTEGER <= parsed_int <= MAX_SAFE_INTEGER):
                    return {
                        "id": scenario.id,
                        "status": "BLOCKED",
                        "eligible": True,
                        "steps": [],
                        "checks": [],
                        "observations": [],
                        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                        "error": f"Selector {sel['id']!r} integer column {col_name!r} exceeds JSON-safe integer range",
                    }
                new_values[col_binding] = parsed_int
                selector_outputs[col_binding] = parsed_int
            elif col_type == "string":
                if not isinstance(val, str):
                    return {
                        "id": scenario.id,
                        "status": "BLOCKED",
                        "eligible": True,
                        "steps": [],
                        "checks": [],
                        "observations": [],
                        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                        "error": f"Selector {sel['id']!r} string column {col_name!r} is not a text value",
                    }
                str_val = val
                try:
                    encoded = str_val.encode("utf-8")
                except UnicodeEncodeError:
                    return {
                        "id": scenario.id,
                        "status": "BLOCKED",
                        "eligible": True,
                        "steps": [],
                        "checks": [],
                        "observations": [],
                        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                        "error": f"Selector {sel['id']!r} string column {col_name!r} contains invalid Unicode",
                    }
                if len(encoded) > 4000:
                    return {
                        "id": scenario.id,
                        "status": "BLOCKED",
                        "eligible": True,
                        "steps": [],
                        "checks": [],
                        "observations": [],
                        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                        "error": f"Selector {sel['id']!r} string column {col_name!r} exceeds 4000 UTF-8 bytes",
                    }
                new_values[col_binding] = str_val
                selector_outputs[col_binding] = str_val

        # RenderContext is immutable; selectors create a validated successor context.
        current_context = RenderContext(
            owner=current_context.owner,
            run_id=current_context.run_id,
            objects=current_context.objects,
            values=new_values,
        )
        selector_records.append({
            "id": sel["id"],
            "sql": sel_sql,
            "inputBindings": selector_input_values,
            "bindings": selector_outputs,
            "columns": [dict(column) for column in sel["columns"]],
        })

    if scenario.cleanup is not None:
        try:
            project_expected_identity, project_object_names = _record_project_precreation(
                target,
                scenario,
                current_context,
                run,
                scenario_identity,
                selector_records,
                runner,
            )
            if scenario_identity is None:
                scenario_identity = project_expected_identity
            elif not _same_live_target(scenario_identity, project_expected_identity):
                raise ValueError("project pre-creation inventory observed a different live target")
        except Exception as exc:
            return {
                "id": scenario.id,
                "status": "BLOCKED",
                "eligible": False,
                "steps": [],
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Project cleanup ownership could not be established before writes: {exc}",
            }

    # 3. Execute Steps
    step_records: list[dict[str, Any]] = []
    for step in scenario.steps:
        step_rec = execute_step(target, scenario, step, current_context, run, runner=runner)
        step_records.append(step_rec)
        step_identity = step_rec.get("identity")
        if not isinstance(step_identity, Mapping):
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Step {step['id']!r} did not return a verified live database identity",
            }
        if scenario_identity is None:
            scenario_identity = step_identity
        elif not _same_live_target(scenario_identity, step_identity):
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": "Scenario execution observed a different live database identity between sessions",
            }
        if step_rec["status"] == "UNKNOWN":
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
            }
        elif step_rec["status"] != "PASSED":
            if scenario.cleanup is not None and scenario_identity is not None:
                try:
                    _save_project_cleanup_inventory(
                        target, scenario, current_context, run, scenario_identity,
                        project_object_names, selector_records, runner,
                    )
                except Exception:
                    return {
                        "id": scenario.id,
                        "status": "UNKNOWN",
                        "eligible": True,
                        "steps": step_records,
                        "checks": [],
                        "observations": [],
                        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                        "error": "Failed step left project cleanup ownership unverified",
                    }
            return {
                "id": scenario.id,
                "status": "FAILED",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
            }

    # 4. Mandatory Checks in fresh session
    check_records: list[dict[str, Any]] = []
    if scenario.checks:
        query_checks = [
            QueryCheck(
                id=chk["id"],
                sql=render_object_tokens(chk["sql"], current_context),
                expected=chk["expected"],
            )
            for chk in scenario.checks
        ]
        check_dir = run.run_dir / "sessions" / f"{scenario.id}-checks-{secrets.token_hex(6)}"
        try:
            check_report = run_checks(
                target,
                query_checks,
                check_dir,
                phase="postconditions",
                _runner=runner,
                bindings=current_context.values,
            )
        except Exception as exc:
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Checks execution failed unexpectedly: {exc}",
            }

        check_coverage = check_report.coverage
        check_identities = check_coverage.get("sessionIdentities") if isinstance(check_coverage, Mapping) else None
        session_count = check_coverage.get("sessions") if isinstance(check_coverage, Mapping) else None
        if (
            not isinstance(check_identities, list)
            or not check_identities
            or type(session_count) is not int
            or len(check_identities) != session_count
            or any(not isinstance(identity, Mapping) for identity in check_identities)
        ):
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": "Mandatory checks did not return verified identity for every SQLcl session",
            }
        if scenario_identity is None:
            scenario_identity = check_identities[0]
        if any(not _same_live_target(scenario_identity, identity) for identity in check_identities):
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": [],
                "observations": [],
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": "Mandatory checks observed a different live database identity between sessions",
            }

        for chk in query_checks:
            matched_res = next(
                (r for r in check_report.results if r.get("check") == chk.id or r.get("id") == chk.id),
                None,
            )
            chk_passed = matched_res.get("passed", False) if matched_res else False
            check_records.append({
                "id": chk.id,
                "expected": chk.expected,
                "actual": matched_res.get("value") if matched_res else None,
                "passed": chk_passed,
                "sql": chk.sql,
            })

        if not check_report.complete:
            scenario_status = "UNKNOWN"
        elif not check_report.passed:
            scenario_status = "FAILED"
        else:
            scenario_status = "PASSED"
    else:
        scenario_status = "PASSED"

    # 5. Optional Observations
    observation_records: list[dict[str, Any]] = []
    for obs in scenario.observations:
        obs_sql = render_object_tokens(obs["sql"], current_context)
        try:
            obs_res = read_rows(
                target, obs_sql, current_context, run, max_rows=obs["maxRows"], runner=runner
            )
            obs_identity = obs_res.get("identity")
            if not isinstance(obs_identity, Mapping) or (
                scenario_identity is not None
                and not _same_live_target(scenario_identity, obs_identity)
            ):
                observation_records.append({
                    "id": obs["id"],
                    "identity": dict(obs_identity) if isinstance(obs_identity, Mapping) else None,
                    "error": "Observation did not verify the scenario's live database identity",
                    "rows": [],
                    "truncated": False,
                })
                return {
                    "id": scenario.id,
                    "status": "UNKNOWN",
                    "eligible": True,
                    "steps": step_records,
                    "checks": check_records,
                    "observations": observation_records,
                    "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                    "error": f"Observation {obs['id']!r} used a different or unverified live database identity",
                }
            observation_records.append({
                "id": obs["id"],
                "identity": dict(obs_identity),
                "rows": obs_res["rows"],
                "truncated": obs_res["truncated"],
            })
        except Exception as exc:
            # Crash / process loss during observation results in UNKNOWN
            if isinstance(exc, (SqlclTimeout, SqlclCancelled, KeyboardInterrupt)):
                return {
                    "id": scenario.id,
                    "status": "UNKNOWN",
                    "eligible": True,
                    "steps": step_records,
                    "checks": check_records,
                    "observations": observation_records,
                    "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                    "error": f"Observation crash: {exc}",
                }
            # Clean failure of optional observation is recorded without masking functional outcome
            observation_records.append({
                "id": obs["id"],
                "error": str(exc),
                "rows": [],
                "truncated": False,
            })

    if scenario.cleanup is not None:
        if scenario_status == "UNKNOWN" or scenario_identity is None:
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": check_records,
                "observations": observation_records,
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": "Project cleanup inventory was not captured because scenario state remained unresolved",
            }
        try:
            _save_project_cleanup_inventory(
                target,
                scenario,
                current_context,
                run,
                scenario_identity,
                project_object_names,
                selector_records,
                runner,
            )
        except Exception as exc:
            return {
                "id": scenario.id,
                "status": "UNKNOWN",
                "eligible": True,
                "steps": step_records,
                "checks": check_records,
                "observations": observation_records,
                "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
                "error": f"Project cleanup inventory could not be verified after the scenario: {exc}",
            }

    return {
        "id": scenario.id,
        "status": scenario_status,
        "eligible": True,
        "steps": step_records,
        "checks": check_records,
        "observations": observation_records,
        "effects": list(scenario.effects) if isinstance(scenario.effects, (list, tuple)) else dict(scenario.effects),
    }
