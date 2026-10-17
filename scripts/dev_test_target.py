"""Existing-owner target planning and prerequisite inspection (Task 3).

Resolves configured target profiles via resolve_target(..., operation="migration").
Verifies identity, eligibility, APEX/SQLcl versions, prerequisites, and fixture collisions.
All planning is strictly read-only: no DDL, DML, grants, or APEX session creation.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.db_targets import (
    Target,
    TargetResolutionError,
    looks_like_production_identity,
    resolve_target,
)
from scripts.dev_test_evidence import (
    RunEvidence,
    assert_inputs_unchanged,
    freeze_inputs,
    make_object_name,
)
from scripts.dev_test_manifest import (
    Suite,
)
from scripts.dev_test_report import (
    new_report,
    validate_report,
)
from scripts.migration_checks import OBSERVED_IDENTITY_FIELDS, run_checks
from scripts.migration_manifest import QueryCheck
from scripts.sqlcl_session import SqlclResult, run_sqlcl
from scripts.sqlcl_version import CompatibilityError, parse_sqlcl_version


class DevTestTargetError(ValueError):
    """Raised when a target cannot be resolved or is ineligible for testing."""


def _catalog_identifier(value: object, field: str) -> str:
    """Validate catalog names before binding them into the planning query."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", value, re.ASCII) is None:
        raise DevTestTargetError(f"Invalid catalog identifier for {field}")
    return value


def _resolve_identity_mapping(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap target.observed or nested identity dicts if present."""
    if not isinstance(mapping, Mapping):
        raise DevTestTargetError("Target identity must be a mapping")
    if "target" in mapping and isinstance(mapping["target"], Mapping):
        mapping = mapping["target"]
    if "observed" in mapping and isinstance(mapping["observed"], Mapping):
        mapping = mapping["observed"]
    return mapping


def assert_saved_target(saved: Mapping[str, Any], observed: Mapping[str, Any]) -> None:
    """Compare all required identity fields; missing fields are not wildcards.

    Args:
        saved: Earlier saved target identity mapping (or report dictionary).
        observed: Current live observed target identity mapping (or report dictionary).

    Raises:
        DevTestTargetError: If any required identity field is missing, empty, or differs.
    """
    saved_id = _resolve_identity_mapping(saved)
    observed_id = _resolve_identity_mapping(observed)

    for field in OBSERVED_IDENTITY_FIELDS:
        val_saved = saved_id.get(field)
        if not isinstance(val_saved, str) or not val_saved.strip():
            raise DevTestTargetError(
                f"Saved target identity missing or empty required field: {field!r}"
            )

        val_obs = observed_id.get(field)
        if not isinstance(val_obs, str) or not val_obs.strip():
            raise DevTestTargetError(
                f"Observed target identity missing or empty required field: {field!r}"
            )

        if val_saved != val_obs:
            raise DevTestTargetError(
                f"Target identity mismatch for {field}: saved={val_saved!r}, observed={val_obs!r}"
            )


def _detect_sqlcl_version() -> str:
    """Detect local SQLcl version from `sql -V` or return 'unavailable'."""
    try:
        proc = subprocess.run(
            ["sql", "-V"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return parse_sqlcl_version(proc.stdout)
    except (CompatibilityError, OSError, subprocess.SubprocessError):
        pass
    return "unavailable"


def _gather_input_paths(suite: Suite) -> list[Path]:
    """Collect all suite files and scenario step/cleanup SQL files."""
    paths: list[Path] = []
    manifest_file = suite.manifest_path
    if manifest_file is not None:
        paths.append(manifest_file)
    elif (suite.root / "suite.json").is_file():
        # Preserve compatibility with manually constructed Suite instances.
        paths.append(suite.root / "suite.json")

    for sc in suite.scenarios:
        for st in sc.steps:
            p = st.get("path")
            if isinstance(p, Path):
                paths.append(p)
        if sc.cleanup is not None and "sqlFiles" in sc.cleanup:
            for cf in sc.cleanup["sqlFiles"]:
                cp = cf.get("path")
                if isinstance(cp, Path):
                    paths.append(cp)

    # Deduplicate while preserving order
    seen: set[Path] = set()
    result: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(p)
    return result


def _render_planning_driver(
    target: Target,
    suite: Suite,
    planned_objects: list[str],
) -> str:
    """Render the strictly read-only inspection driver."""
    filter_items: list[tuple[str, str]] = []
    member_filters: list[tuple[str, str, str]] = []
    for sc in suite.scenarios:
        for req in sc.requirements["objects"]:
            owner = target.schema if req["owner"] == "@selected" else req["owner"]
            owner = _catalog_identifier(owner, "requirement owner")
            name = _catalog_identifier(req["name"], "requirement object")
            filter_items.append((owner, name))
            if req["member"] is not None:
                member_filters.append((
                    owner,
                    name,
                    _catalog_identifier(req["member"], "requirement member"),
                ))
    for planned_name in planned_objects:
        filter_items.append((
            _catalog_identifier(target.schema, "selected schema"),
            _catalog_identifier(planned_name, "planned fixture object"),
        ))
    filter_items = sorted(set(filter_items))
    member_filters = sorted(set(member_filters))

    bind_declarations: list[str] = []
    for index, (owner, name) in enumerate(filter_items, start=1):
        bind_declarations.extend((
            f"  l_owner_{index} CONSTANT VARCHAR2(128) := '{owner}';",
            f"  l_object_{index} CONSTANT VARCHAR2(128) := '{name}';",
        ))
    for index, (owner, name, member) in enumerate(member_filters, start=1):
        bind_declarations.extend((
            f"  l_member_owner_{index} CONSTANT VARCHAR2(128) := '{owner}';",
            f"  l_member_object_{index} CONSTANT VARCHAR2(128) := '{name}';",
            f"  l_member_name_{index} CONSTANT VARCHAR2(128) := '{member}';",
        ))

    lines = [
        "SET ENCODING UTF-8",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "ALTER SESSION DISABLE COMMIT IN PROCEDURE;",
        "SET TRANSACTION READ ONLY;",
        "DECLARE",
        "  l_payload JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_identity JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_objects JSON_ARRAY_T := JSON_ARRAY_T();",
        "  l_members JSON_ARRAY_T := JSON_ARRAY_T();",
        "  l_space JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_item JSON_OBJECT_T;",
        "  l_apex_version VARCHAR2(100) := 'unavailable';",
        "  l_sql CLOB;",
        "  l_chunk VARCHAR2(32767);",
        "  l_offset INTEGER;",
        *bind_declarations,
        "BEGIN",
        "  l_payload.put('schemaVersion', 1);",
        # Capture full 9 identity fields
        "  l_identity.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));",
        "  l_identity.put('current_schema', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'));",
        "  l_identity.put('db_name', SYS_CONTEXT('USERENV', 'DB_NAME'));",
        "  l_identity.put('db_unique_name', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'));",
        "  l_identity.put('service_name', NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>'));",
        "  l_identity.put('container_id', NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0'));",
        "  l_identity.put('container_name', NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB'));",
        "  l_identity.put('edition', NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>'));",
        "  l_identity.put('database_version', TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE));",
        "  l_payload.put('identity', l_identity);",
        # Capture APEX version if visible
        "  BEGIN",
        "    EXECUTE IMMEDIATE 'SELECT APEX_RELEASE.VERSION_NO FROM DUAL' INTO l_apex_version;",
        "  EXCEPTION",
        "    WHEN OTHERS THEN",
        "      l_apex_version := 'unavailable';",
        "  END;",
        "  l_payload.put('apexVersion', l_apex_version);",
    ]

    # Query matching objects in ALL_OBJECTS
    if filter_items:
        conditions = [
            f"(OWNER = l_owner_{index} AND OBJECT_NAME = l_object_{index})"
            for index in range(1, len(filter_items) + 1)
        ]
        where_clause = " OR ".join(conditions)
        lines.extend([
            f"  FOR r IN (SELECT OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS FROM ALL_OBJECTS WHERE {where_clause}) LOOP",
            "    l_item := JSON_OBJECT_T();",
            "    l_item.put('owner', r.OWNER);",
            "    l_item.put('name', r.OBJECT_NAME);",
            "    l_item.put('type', r.OBJECT_TYPE);",
            "    l_item.put('status', r.STATUS);",
            "    l_objects.append(l_item);",
            "  END LOOP;",
        ])

    if member_filters:
        conditions = [
            (
                f"(OWNER = l_member_owner_{index} AND OBJECT_NAME = l_member_object_{index} "
                f"AND PROCEDURE_NAME = l_member_name_{index})"
            )
            for index in range(1, len(member_filters) + 1)
        ]
        where_clause = " OR ".join(conditions)
        lines.extend([
            "  FOR r IN (SELECT DISTINCT OWNER, OBJECT_NAME, PROCEDURE_NAME FROM ALL_PROCEDURES "
            f"WHERE {where_clause}) LOOP",
            "    l_item := JSON_OBJECT_T();",
            "    l_item.put('owner', r.OWNER);",
            "    l_item.put('name', r.OBJECT_NAME);",
            "    l_item.put('member', r.PROCEDURE_NAME);",
            "    l_members.append(l_item);",
            "  END LOOP;",
        ])

    lines.append("  l_payload.put('existingObjects', l_objects);")
    lines.append("  l_payload.put('members', l_members);")

    # Advisory space inspection
    lines.extend([
        "  BEGIN",
        "    l_space.put('dba_space_visible', FALSE);",
        "    l_space.put('status', 'advisory_unavailable');",
        "  END;",
        "  l_payload.put('spaceAdvisory', l_space);",
    ])

    # Emit payload
    lines.extend([
        "  DBMS_OUTPUT.PUT_LINE('PLAN_PAYLOAD_BEGIN:plan');",
        "  l_sql := l_payload.to_clob();",
        "  l_offset := 1;",
        "  WHILE l_offset <= DBMS_LOB.GETLENGTH(l_sql) LOOP",
        "    l_chunk := DBMS_LOB.SUBSTR(l_sql, 8000, l_offset);",
        "    DBMS_OUTPUT.PUT_LINE(l_chunk);",
        "    l_offset := l_offset + LENGTH2(l_chunk);",
        "  END LOOP;",
        "  DBMS_OUTPUT.PUT_LINE('PLAN_PAYLOAD_END:plan');",
        "  DBMS_OUTPUT.PUT_LINE('PLAN_VERIFIED:plan');",
        "END;",
        "/",
        "EXIT SUCCESS ROLLBACK",
        "",
    ])
    return "\n".join(lines)


def _parse_planning_output(output: str) -> dict[str, Any]:
    """Parse framed JSON payload from SQLcl planning driver output."""
    begin = "PLAN_PAYLOAD_BEGIN:plan"
    end = "PLAN_PAYLOAD_END:plan"
    sentinel = "PLAN_VERIFIED:plan"

    lines = output.splitlines()
    if lines.count(begin) != 1 or lines.count(end) != 1 or lines.count(sentinel) != 1:
        raise DevTestTargetError(
            "Target inspection frames are duplicated or ambiguous; cannot verify live target."
        )

    try:
        start_idx = lines.index(begin)
        end_idx = lines.index(end, start_idx + 1)
        sentinel_idx = lines.index(sentinel)
    except ValueError:
        raise DevTestTargetError(
            "Target inspection frame is missing or truncated; cannot verify live target."
        ) from None

    if sentinel_idx <= end_idx:
        raise DevTestTargetError(
            "Target inspection sentinel is missing; live inspection was unverified."
        )

    json_text = "\n".join(lines[start_idx + 1:end_idx]).strip()
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise DevTestTargetError(
            f"Target inspection returned malformed JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise DevTestTargetError("Target inspection payload must be a JSON object")

    return payload


def inspect_suite(
    values: Mapping[str, str],
    schema: str | None,
    suite: Suite,
    run: RunEvidence,
    *,
    runner: Callable[..., SqlclResult] = run_sqlcl,
    origin: str = "project",
) -> dict[str, Any]:
    """Inspect DEV target, prerequisites, fixture collisions, and versions.

    Args:
        values: Environment configuration mapping (e.g. from read_project_env).
        schema: Explicit schema selection (required when multiple are configured).
        suite: Loaded Suite instance.
        run: Durable RunEvidence instance.
        runner: SQLcl runner callable (defaults to run_sqlcl).
        origin: Provenance flag ('project' or 'builtin').

    Returns:
        Schema-1 report dictionary representing the plan.

    Raises:
        DevTestTargetError: On target resolution refusal or identity failure.
    """
    if origin not in {"project", "builtin"}:
        raise DevTestTargetError(f"Invalid suite origin: {origin!r}")

    # 1. Target resolution
    try:
        target = resolve_target(values, "dev", "migration", schema=schema)
    except TargetResolutionError as exc:
        raise DevTestTargetError(str(exc)) from exc

    # First-release rule: session_user must equal selected_schema
    if target.expected_user != target.schema:
        raise DevTestTargetError(
            f"session_user {target.expected_user} does not match selected schema {target.schema}; "
            "first-release execution requires session_user=selected_schema"
        )

    if looks_like_production_identity(target.connection):
        raise DevTestTargetError(
            f"Target connection {target.connection!r} resembles a production database"
        )

    # 2. Frozen inputs and source metadata
    input_paths = _gather_input_paths(suite)
    if run._manifest_path.is_file():
        assert_inputs_unchanged(run)
        try:
            m_data = json.loads(run._manifest_path.read_text(encoding="utf-8"))
            input_hashes = m_data.get("hashes", {})
        except Exception as exc:
            raise DevTestTargetError(f"Failed to read frozen input manifest: {exc}") from exc
    else:
        input_hashes = freeze_inputs(run, input_paths)

    # Git commit and dirty status
    git_sha: str | None = None
    dirty: bool = False
    try:
        p_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(run.root),
            capture_output=True,
            text=True,
            check=False,
        )
        if p_sha.returncode == 0 and p_sha.stdout.strip():
            git_sha = p_sha.stdout.strip()
            args = ["git", "status", "--porcelain", "--untracked-files=all", "--"] + [
                str(p) for p in input_paths
            ]
            p_stat = subprocess.run(
                args,
                cwd=str(run.root),
                capture_output=True,
                text=True,
                check=False,
            )
            if p_stat.returncode == 0 and p_stat.stdout.strip():
                dirty = True
    except Exception:
        pass

    # 3. Local SQLcl version
    sqlcl_ver = _detect_sqlcl_version()

    # 4. Generate planned fixture names with deterministic per-scenario prefix
    # DT_<SHA256(run_id+scenario.id) first12 uppercase>_
    planned_by_scenario: dict[str, dict[str, str]] = {}
    all_planned_names: list[str] = []

    for sc in suite.scenarios:
        sc_prefix = f"DT_{hashlib.sha256((run.run_id + sc.id).encode('utf-8')).hexdigest()[:12].upper()}_"
        sc_planned: dict[str, str] = {}
        for fix in sc.fixtures["objects"]:
            obj_name = make_object_name(sc_prefix, fix["suffix"])
            sc_planned[fix["binding"]] = obj_name
            all_planned_names.append(obj_name)
        planned_by_scenario[sc.id] = sc_planned

    # 5. Run planning inspection driver
    planning_dir = run.run_dir / "planning"
    planning_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    driver_path = planning_dir / "dev-test-plan-driver.sql"
    driver_sql = _render_planning_driver(target, suite, all_planned_names)
    driver_path.write_text(driver_sql, encoding="utf-8", newline="\n")
    try:
        driver_path.chmod(0o600)
    except OSError:
        pass

    result = runner(target, driver_path, planning_dir, phase="preconditions")
    if result.returncode != 0:
        raise DevTestTargetError(
            f"Target inspection failed (exit code {result.returncode}): {result.output[:500]}"
        )

    payload = _parse_planning_output(result.output)

    # 6. Validate observed identity
    observed = payload.get("identity")
    if not isinstance(observed, Mapping) or not observed:
        raise DevTestTargetError(
            "Live target identity is incomplete; the observed database cannot be identified safely."
        )

    for field in OBSERVED_IDENTITY_FIELDS:
        val = observed.get(field)
        if not isinstance(val, str) or not val.strip():
            raise DevTestTargetError(
                f"Live target identity missing or empty field: {field!r}"
            )

    if observed["session_user"] != target.schema:
        raise DevTestTargetError(
            f"Live session_user {observed['session_user']} does not match selected schema {target.schema}"
        )
    if observed["current_schema"] != target.schema:
        raise DevTestTargetError(
            f"Live current_schema {observed['current_schema']} does not match selected schema {target.schema}"
        )

    if looks_like_production_identity(
        observed["db_name"],
        observed["db_unique_name"],
        observed["service_name"],
        observed["container_name"],
    ):
        raise DevTestTargetError(
            "Live target database identity resembles a production database"
        )

    # Catalog results
    existing_objects = {
        (o.get("owner", ""), o.get("name", ""), o.get("type", "")): o.get("status", "")
        for o in payload.get("existingObjects", [])
        if isinstance(o, dict)
    }
    existing_names_in_schema = {
        o.get("name", "")
        for o in payload.get("existingObjects", [])
        if isinstance(o, dict) and o.get("owner") == target.schema
    }
    visible_members = {
        (m.get("owner", ""), m.get("name", ""), m.get("member", ""))
        for m in payload.get("members", [])
        if isinstance(m, dict)
    }
    # Run each declared scalar prerequisite through the existing read-only check
    # engine in a fresh SQLcl session. Never trust a payload field synthesized by
    # the catalog driver to stand in for executing the declared query.
    check_result_by_requirement: dict[tuple[str, str], dict[str, Any]] = {}
    requirement_checks: list[tuple[str, str, QueryCheck]] = []
    for sc in suite.scenarios:
        for chk in sc.requirements["checks"]:
            internal_id = f"req-{len(requirement_checks) + 1:08d}"
            requirement_checks.append((
                sc.id,
                chk["id"],
                QueryCheck(internal_id, chk["sql"], chk["expected"]),
            ))

    if requirement_checks:
        def checked_runner(
            check_target: Target,
            driver_path: Path,
            check_run_dir: Path,
            **runner_options: Any,
        ) -> SqlclResult:
            result = runner(check_target, driver_path, check_run_dir, **runner_options)
            if getattr(result, "returncode", 1) != 0:
                raise RuntimeError("SQLcl exited unsuccessfully during a prerequisite check")
            return result

        check_report = run_checks(
            target,
            [entry[2] for entry in requirement_checks],
            planning_dir / "requirement-checks",
            phase="preconditions",
            jobs=1,
            _runner=checked_runner,
        )
        check_results_by_id = {
            result.get("id"): result
            for result in check_report.results
            if isinstance(result, dict) and isinstance(result.get("id"), str)
        }
        check_result_counts = check_report.coverage.get("sessionResultCounts", [])
        check_session_ids = check_report.coverage.get("sessionCheckIds", [])
        check_session_identities = check_report.coverage.get("sessionIdentities", [])
        identity_verified_ids: set[str] = set()
        for index, batch_ids in enumerate(check_session_ids):
            if not isinstance(batch_ids, list):
                continue
            session_identity = (
                check_session_identities[index]
                if index < len(check_session_identities)
                else None
            )
            if isinstance(session_identity, Mapping):
                try:
                    assert_saved_target(observed, session_identity)
                except DevTestTargetError as exc:
                    raise DevTestTargetError(
                        "Requirement checks observed a different database identity than target planning"
                    ) from exc
                if session_identity.get("current_schema") != target.schema:
                    raise DevTestTargetError(
                        "Requirement checks observed a current schema different from the selected owner"
                    )
                result_count = (
                    check_result_counts[index]
                    if index < len(check_result_counts)
                    else -1
                )
                if result_count == len(batch_ids):
                    identity_verified_ids.update(
                        check_id for check_id in batch_ids if isinstance(check_id, str)
                    )

        for scenario_id, check_id, query_check in requirement_checks:
            check_result_by_requirement[(scenario_id, check_id)] = {
                "result": check_results_by_id.get(query_check.id),
                "trusted": query_check.id in identity_verified_ids,
                "complete": query_check.id in identity_verified_ids,
                "expected": query_check.expected,
            }

    # 7. Evaluate prerequisites and collisions per scenario
    scenario_records: list[dict[str, Any]] = []
    all_prereqs_evidence: list[dict[str, Any]] = []
    scenario_eligibility: dict[str, bool] = {}

    limitations: list[str] = []
    space_adv = payload.get("spaceAdvisory", {})
    if isinstance(space_adv, dict) and not space_adv.get("dba_space_visible", False):
        limitations.append("DBA free-space visibility is unavailable (advisory)")

    for sc in suite.scenarios:
        planned_map = planned_by_scenario[sc.id]
        collision_detected = False
        collision_notes: list[str] = []

        # Check collisions
        for binding, obj_name in planned_map.items():
            if obj_name in existing_names_in_schema:
                collision_detected = True
                collision_notes.append(
                    f"Planned fixture object {obj_name} ({binding}) already exists in schema {target.schema}"
                )

        # Check requirements
        prereqs_met = True
        for req in sc.requirements["objects"]:
            req_owner = target.schema if req["owner"] == "@selected" else req["owner"]
            req_name = req["name"]
            req_type = req["type"]
            req_status = req["requiredStatus"]
            req_member = req["member"]

            key = (req_owner, req_name, req_type)
            if key not in existing_objects:
                prereqs_met = False
                if req_owner == target.schema:
                    ev_status = "missing"
                    reason = f"Object {req_owner}.{req_name} of type {req_type} is not found in the selected owner's catalog"
                else:
                    ev_status = "unknown"
                    reason = f"Object {req_owner}.{req_name} of type {req_type} is not visible in the selected owner's catalog"
            else:
                obs_status = existing_objects[key]
                if req_status == "VALID" and obs_status != "VALID":
                    prereqs_met = False
                    ev_status = "missing"
                    reason = f"Object {req_owner}.{req_name} status is {obs_status!r}, required VALID"
                elif req_member is not None:
                    mem_key = (req_owner, req_name, req_member)
                    if mem_key in visible_members:
                        ev_status = "satisfied"
                        reason = None
                    else:
                        # Member invisibility is unknown, not fabricated absence
                        prereqs_met = False
                        ev_status = "unknown"
                        reason = f"Member {req_member} of {req_owner}.{req_name} is invisible or unverified"
                else:
                    ev_status = "satisfied"
                    reason = None

            ev_entry = {
                "scenarioId": sc.id,
                "owner": req_owner,
                "name": req_name,
                "type": req_type,
                "member": req_member,
                "requiredStatus": req_status,
                "status": ev_status,
            }
            if reason:
                ev_entry["reason"] = reason
            all_prereqs_evidence.append(ev_entry)

        # Check requirements checks
        for chk in sc.requirements["checks"]:
            check_evidence = check_result_by_requirement.get((sc.id, chk["id"]))
            c_res = check_evidence.get("result") if check_evidence else None
            check_trusted = bool(check_evidence and check_evidence.get("trusted"))
            check_complete = bool(check_evidence and check_evidence.get("complete"))
            if check_trusted and check_complete and c_res and c_res.get("passed") is True:
                chk_status = "satisfied"
                chk_reason = None
            elif check_trusted and check_complete and c_res:
                prereqs_met = False
                chk_status = "missing"
                chk_reason = c_res.get("error") or (
                    f"Check did not return exactly one numeric value equal to {chk['expected']}"
                )
            else:
                prereqs_met = False
                chk_status = "unknown"
                chk_reason = "Check result was incomplete or could not be tied to the planned target identity"

            chk_ev = {
                "scenarioId": sc.id,
                "checkId": chk["id"],
                "expected": chk["expected"],
                "status": chk_status,
            }
            if c_res and "value" in c_res:
                chk_ev["observedValue"] = c_res["value"]
            if chk_reason:
                chk_ev["reason"] = chk_reason
            all_prereqs_evidence.append(chk_ev)

        # Initial eligibility before dependency propagation
        if collision_detected or not prereqs_met:
            scenario_eligibility[sc.id] = False
            status = "BLOCKED"
        else:
            scenario_eligibility[sc.id] = True
            status = "NOT_RUN"

        json_safe_steps = []
        for st in sc.steps:
            st_dict = {
                "id": st["id"],
                "sqlFile": st["sqlFile"],
                "expected": dict(st["expected"]),
            }
            if "sha256" in st:
                st_dict["sha256"] = st["sha256"]
            json_safe_steps.append(st_dict)

        sc_record: dict[str, Any] = {
            "id": sc.id,
            "status": status,
            "eligible": scenario_eligibility[sc.id],
            "steps": json_safe_steps,
            "checks": [dict(c) for c in sc.checks],
            "observations": [dict(o) for o in sc.observations],
            "effects": dict(sc.effects),
            "plannedObjects": planned_map,
        }
        if collision_notes:
            sc_record["collisions"] = collision_notes
        scenario_records.append(sc_record)

    # Dependency propagation
    for sc_rec in scenario_records:
        sc_manifest = next(s for s in suite.scenarios if s.id == sc_rec["id"])
        if sc_manifest.dependsOn:
            for dep_id in sc_manifest.dependsOn:
                if not scenario_eligibility.get(dep_id, False):
                    scenario_eligibility[sc_rec["id"]] = False
                    sc_rec["eligible"] = False
                    if sc_rec["status"] != "BLOCKED":
                        sc_rec["status"] = "NOT_RUN"
                    break

    complete = True
    eligible = all(sc_rec["eligible"] for sc_rec in scenario_records)

    # 8. Build report
    apex_ver = payload.get("apexVersion", "unavailable")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = new_report(
        run=run,
        action="plan",
        suite_id=suite.id,
        scenario_ids=[sc.id for sc in suite.scenarios],
        origin=origin,
    )
    report["finishedAt"] = now_iso
    report["source"]["gitSha"] = git_sha
    report["source"]["dirty"] = dirty
    report["source"]["inputHashes"] = input_hashes

    report["target"]["configured"] = {
        "environment": target.environment,
        "connection": target.connection,
        "expectedUser": target.expected_user,
        "schema": target.schema,
        "classification": target.classification,
    }
    report["target"]["observed"] = dict(observed)
    report["target"]["versions"] = {
        "sqlcl": sqlcl_ver,
        "apex": apex_ver,
        "database": observed.get("database_version", "unknown"),
    }

    report["prerequisites"] = all_prereqs_evidence
    report["scenarios"] = scenario_records
    report["inventory"]["objects"] = []
    report["inventory"]["rows"] = []
    report["limitations"] = limitations
    report["coverage"]["complete"] = complete
    report["coverage"]["eligible"] = eligible

    validate_report(report)
    return report


def verify_saved_target_identity(
    target: Target,
    expected: Mapping[str, Any],
    run: RunEvidence,
    *,
    runner: Callable[..., SqlclResult] = run_sqlcl,
) -> dict[str, str]:
    """Reobserve the full target identity in a read-only SQLcl session.

    This uses the same framed catalog driver as suite planning and compares all
    required identity fields before each eligible scenario is dispatched.
    """
    probe_dir = run.run_dir / "sessions" / f"target-identity-{secrets.token_hex(8)}"
    probe_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    driver_path = probe_dir / "driver.sql"
    probe_suite = Suite(id="target-identity-probe", root=run.root, scenarios=())
    driver_path.write_text(
        _render_planning_driver(target, probe_suite, []),
        encoding="utf-8",
        newline="\n",
    )
    try:
        driver_path.chmod(0o600)
    except OSError:
        pass
    result = runner(target, driver_path, probe_dir, phase="preconditions")
    if getattr(result, "returncode", 1) != 0:
        raise DevTestTargetError("Fresh target identity check failed")
    output = getattr(result, "output", str(result))
    payload = _parse_planning_output(output)
    observed = payload.get("identity")
    if not isinstance(observed, Mapping) or not observed:
        raise DevTestTargetError("Fresh target identity check returned no identity")
    for field in OBSERVED_IDENTITY_FIELDS:
        value = observed.get(field)
        if not isinstance(value, str) or not value.strip():
            raise DevTestTargetError(f"Fresh target identity is missing required field {field!r}")
    if observed.get("session_user") != target.expected_user:
        raise DevTestTargetError("Fresh target session user does not match the selected owner")
    if observed.get("current_schema") != target.schema:
        raise DevTestTargetError("Fresh target current schema does not match the selected owner")
    if looks_like_production_identity(
        observed.get("db_name", ""),
        observed.get("db_unique_name", ""),
        observed.get("service_name", ""),
        observed.get("container_name", ""),
    ):
        raise DevTestTargetError("Fresh target identity resembles production")
    assert_saved_target(expected, observed)
    return {str(key): str(value) for key, value in observed.items()}


__all__ = [
    "DevTestTargetError",
    "assert_saved_target",
    "inspect_suite",
    "verify_saved_target_identity",
]
