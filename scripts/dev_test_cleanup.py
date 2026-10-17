"""Plan and perform optional cleanup from a DEV test run's exact evidence.

Cleanup is deliberately a second, explicit operation. It never discovers
objects by prefix, infers a business-row DELETE, or uses CASCADE.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.db_targets import Target, TargetResolutionError, resolve_target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_core import BUILTIN_SCENARIO_IDS
from scripts.dev_test_evidence import RunEvidence, append_event, assert_inputs_unchanged, open_run
from scripts.dev_test_report import save_report, validate_report
from scripts.dev_test_target import DevTestTargetError, assert_saved_target
from scripts.schema_catalog import (
    CatalogError,
    ObjectKey,
    SchemaInventory,
    capture_inventory,
    capture_snapshot,
)
from scripts.sqlcl_session import run_sqlcl


_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DROP_TYPES = frozenset({"TABLE", "VIEW", "PACKAGE", "PROCEDURE", "FUNCTION"})
_DROP_ORDER = {"VIEW": 0, "PACKAGE": 1, "PROCEDURE": 2, "FUNCTION": 3, "TABLE": 4}
_PROJECT_DELETE = re.compile(
    r"\s*DELETE\s+FROM\s+(?:(?P<owner>[A-Z][A-Z0-9_$#]{0,127})\s*\.\s*)?"
    r"(?P<name>[A-Z][A-Z0-9_$#]{0,127})\s+WHERE\s+(?P<predicates>.+?)\s*;?\s*\Z",
    re.IGNORECASE | re.ASCII | re.DOTALL,
)
_PROJECT_PREDICATE = re.compile(
    r"\s*(?P<column>[A-Z][A-Z0-9_$#]{0,127})\s*=\s*:(?P<binding>DEVTEST_[A-Z][A-Z0-9_]*)\s*\Z",
    re.IGNORECASE | re.ASCII,
)
_PROJECT_DROP = re.compile(
    r"\s*DROP\s+(?P<type>TABLE|VIEW|PACKAGE|PROCEDURE|FUNCTION)\s+"
    r"(?P<name>[A-Z][A-Z0-9_$#]{0,127})\s*;?\s*\Z",
    re.IGNORECASE | re.ASCII,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_project_cleanup_sql(
    source: str,
    schema: str,
    recorded_bindings: set[str],
    declared_objects: Mapping[str, str],
) -> dict[str, Any]:
    """Accept one exact-key DELETE or one DROP of a declared fixture only."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("project cleanup SQL is empty")
    delete = _PROJECT_DELETE.fullmatch(source)
    if delete:
        owner = (delete.group("owner") or schema).upper()
        name = delete.group("name").upper()
        if owner != schema.upper():
            raise ValueError("project cleanup cannot address another schema")
        raw_predicates = delete.group("predicates")
        predicates = re.split(r"\s+AND\s+", raw_predicates, flags=re.IGNORECASE | re.ASCII)
        # The single-statement grammar rejects comments, literals, OR, subqueries,
        # statement chaining, and predicates other than column = recorded bind.
        parsed: list[tuple[str, str]] = []
        for predicate in predicates:
            match = _PROJECT_PREDICATE.fullmatch(predicate)
            if not match:
                raise ValueError("project cleanup DELETE must use only exact recorded-key equality predicates")
            binding = match.group("binding").upper()
            if binding not in recorded_bindings:
                raise ValueError(f"project cleanup references an unrecorded key binding: {binding}")
            parsed.append((match.group("column").upper(), binding))
        if not parsed:
            raise ValueError("project cleanup DELETE has no exact recorded-key predicate")
        return {
            "kind": "delete",
            "owner": owner,
            "name": name,
            "bindings": list(dict.fromkeys(binding for _column, binding in parsed)),
        }

    drop = _PROJECT_DROP.fullmatch(source)
    if drop:
        name = drop.group("name").upper()
        object_type = drop.group("type").upper()
        if declared_objects.get(name) != object_type:
            raise ValueError("project cleanup DROP must name an exact declared run fixture")
        return {
            "kind": "drop",
            "owner": schema.upper(),
            "name": name,
            "type": object_type,
            "bindings": [],
        }
    raise ValueError("project cleanup supports one exact-key DELETE or DROP of a declared fixture per file")


def _read_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or is not a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unreadable or corrupt") from error


def _load_run(run: RunEvidence) -> tuple[RunEvidence, dict, dict]:
    opened = open_run(run.root, run.run_id)
    report = _read_json(opened.run_dir / "report.json", "saved run report")
    inventory = _read_json(opened._inventory_path, "saved fixture inventory")
    validate_report(report)
    if report.get("runId") != opened.run_id or report.get("action") != "run":
        raise ValueError("cleanup requires the completed report for this exact run")
    if not isinstance(report.get("finishedAt"), str) or not report["finishedAt"].strip():
        raise ValueError("cleanup refuses a run without completed report evidence")
    if not isinstance(inventory, dict) or inventory.get("schemaVersion") != 1:
        raise ValueError("fixture inventory schema is missing or unsupported")
    if not isinstance(inventory.get("targetIdentity"), Mapping):
        raise ValueError("fixture inventory has no saved target identity")
    if not isinstance(inventory.get("objects"), list) or not isinstance(inventory.get("rows"), list):
        raise ValueError("fixture inventory object or row list is malformed")
    if not isinstance(inventory.get("scenarios"), list):
        raise ValueError("run-wide fixture inventory is missing")
    return opened, report, inventory


def _validate_project_contract(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    contract = entry.get("cleanup")
    if contract is None:
        return None
    if not isinstance(contract, Mapping) or contract.get("kind") != "project":
        raise ValueError("project cleanup contract is malformed or unsupported")
    sql_files = contract.get("sqlFiles")
    checks = contract.get("checks")
    selectors = contract.get("selectors")
    bindings = contract.get("bindings")
    objects = contract.get("objects")
    object_bindings = contract.get("objectBindings")
    rows = entry.get("rows")
    if (
        not isinstance(sql_files, list) or not sql_files
        or not isinstance(checks, list) or not checks
        or not isinstance(selectors, list)
        or not isinstance(bindings, Mapping)
        or not isinstance(objects, Mapping)
        or not isinstance(object_bindings, Mapping)
        or not isinstance(rows, list)
    ):
        raise ValueError("project cleanup contract lacks frozen files, checks, selectors, bindings, or objects")
    for item in sql_files:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or not isinstance(item.get("sha256"), str)
            or not _DIGEST.fullmatch(item["sha256"])
        ):
            raise ValueError("project cleanup SQL file evidence is malformed")
    for item in checks:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("sql"), str)
            or type(item.get("expected")) is not int
            or item["expected"] != 0
        ):
            raise ValueError("project cleanup requires frozen zero-count absence checks")
    if "DEVTEST_RUN_ID" not in bindings or not isinstance(bindings.get("DEVTEST_RUN_ID"), str):
        raise ValueError("project cleanup bindings have no exact run ID")
    for name, object_type in objects.items():
        if (
            not isinstance(name, str)
            or not _IDENTIFIER.fullmatch(name)
            or name != name.upper()
            or object_type not in _DROP_TYPES
        ):
            raise ValueError("project cleanup fixture-object map is invalid")
    if (
        any(not isinstance(binding, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", binding, re.ASCII) for binding in object_bindings)
        or set(object_bindings.values()) != set(objects)
    ):
        raise ValueError("project cleanup object-token bindings differ from declared fixtures")
    selector_ids: set[str] = set()
    output_bindings: set[str] = set()
    for selector in selectors:
        if (
            not isinstance(selector, Mapping)
            or not isinstance(selector.get("id"), str)
            or selector["id"] in selector_ids
            or not isinstance(selector.get("sql"), str)
            or not isinstance(selector.get("inputBindings"), Mapping)
            or not isinstance(selector.get("bindings"), Mapping)
            or not isinstance(selector.get("columns"), list)
        ):
            raise ValueError("project cleanup selector evidence is malformed or duplicated")
        selector_ids.add(selector["id"])
        for binding, value in selector["bindings"].items():
            if (
                not isinstance(binding, str)
                or not binding.startswith("DEVTEST_")
                or binding not in bindings
                or bindings[binding] != value
                or type(value) not in {int, str}
            ):
                raise ValueError("project cleanup selector key is not an exact saved binding")
            if binding in output_bindings:
                raise ValueError("project cleanup repeats a selector-key binding")
            output_bindings.add(binding)
    if len(rows) != len(selectors):
        raise ValueError("project cleanup key inventory does not match its selector list")
    by_selector = {row.get("selectorId"): row for row in rows if isinstance(row, Mapping)}
    if len(by_selector) != len(rows) or set(by_selector) != selector_ids:
        raise ValueError("project cleanup key inventory has missing or duplicate selectors")
    for selector in selectors:
        row = by_selector[selector["id"]]
        if any(row.get(field) != selector.get(field) for field in ("inputBindings", "bindings", "columns")):
            raise ValueError("project cleanup key inventory differs from the frozen selector evidence")
    return contract


def _validate_scenarios(report: Mapping[str, Any], inventory: Mapping[str, Any]) -> tuple[list[dict], str | None]:
    origin = report.get("selection", {}).get("origin")
    report_scenarios = report.get("scenarios")
    inventory_scenarios = inventory.get("scenarios")
    if origin not in {"builtin", "project"} or not isinstance(report_scenarios, list):
        raise ValueError("run report has no valid suite provenance or scenario list")
    if not isinstance(inventory_scenarios, list):
        raise ValueError("fixture inventory has no scenario ownership records")
    by_report = {entry.get("id"): entry for entry in report_scenarios if isinstance(entry, Mapping)}
    if len(by_report) != len(report_scenarios):
        raise ValueError("run report contains malformed scenario results")
    result: list[dict] = []
    for entry in inventory_scenarios:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("fixture inventory contains a malformed scenario record")
        scenario_id = entry["id"]
        scenario_result = by_report.get(scenario_id)
        if not isinstance(scenario_result, Mapping):
            raise ValueError(f"no completed report result exists for fixture scenario {scenario_id}")
        status = scenario_result.get("status")
        if status not in {"PASSED", "FAILED"}:
            raise ValueError(f"scenario {scenario_id} has unresolved execution state")
        steps = scenario_result.get("steps", [])
        if not isinstance(steps, list) or any(
            not isinstance(step, Mapping) or step.get("status") == "UNKNOWN"
            for step in steps
        ):
            raise ValueError(f"scenario {scenario_id} has ambiguous write-step evidence")
        if origin == "builtin":
            if scenario_id not in BUILTIN_SCENARIO_IDS or entry.get("cleanup") != {"kind": "builtin"}:
                raise ValueError(f"built-in cleanup contract is not recognized for {scenario_id}")
        else:
            contract = entry.get("cleanup")
            if contract is None:
                return [], "UNSUPPORTED"
            _validate_project_contract(entry)
        if not isinstance(entry.get("objects"), list):
            raise ValueError(f"fixture ownership list is malformed for {scenario_id}")
        if origin == "builtin" and not entry["objects"]:
            raise ValueError(f"fixture ownership list is empty for {scenario_id}")
        result.append(entry)
    if not result:
        return [], "UNSUPPORTED"
    return result, None


def _frozen_input_bytes(run: RunEvidence, path_text: str, digest: str) -> bytes:
    manifest = _read_json(run._manifest_path, "frozen input manifest")
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(manifest.get("hashes"), Mapping)
        or not isinstance(manifest.get("snapshots"), Mapping)
        or manifest["hashes"].get(path_text) != digest
        or manifest["snapshots"].get(path_text) != f"snapshots/{digest}.bin"
    ):
        raise ValueError("project cleanup file is not part of the run's frozen inputs")
    snapshot = run.snapshots_dir / f"{digest}.bin"
    if snapshot.is_symlink() or not snapshot.is_file():
        raise ValueError("project cleanup SQL snapshot is missing or unsafe")
    content = snapshot.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("project cleanup SQL snapshot digest does not match its contract")
    return content


def _project_program(
    run: RunEvidence,
    report: Mapping[str, Any],
    schema: str,
    scenario: Mapping[str, Any],
) -> dict[str, Any]:
    contract = _validate_project_contract(scenario)
    if contract is None:
        raise ValueError("project cleanup contract is not available")
    assert_inputs_unchanged(run)
    source = report.get("source")
    input_hashes = source.get("inputHashes") if isinstance(source, Mapping) else None
    if not isinstance(input_hashes, Mapping):
        raise ValueError("project cleanup has no frozen suite input hashes")
    suite_candidates: list[tuple[str, dict]] = []
    for path_text, digest in input_hashes.items():
        if not isinstance(path_text, str) or not path_text.lower().endswith(".json"):
            continue
        try:
            payload = json.loads(_frozen_input_bytes(run, path_text, digest).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("id") == report.get("selection", {}).get("suiteId")
            and isinstance(payload.get("scenarios"), list)
        ):
            suite_candidates.append((path_text, payload))
    if len(suite_candidates) != 1:
        raise ValueError("project cleanup cannot identify one frozen suite manifest")
    manifest_path, suite_data = suite_candidates[0]
    raw_scenarios = suite_data["scenarios"]
    raw_scenario = next(
        (item for item in raw_scenarios if isinstance(item, Mapping) and item.get("id") == scenario.get("id")),
        None,
    )
    if not isinstance(raw_scenario, Mapping):
        raise ValueError("project cleanup scenario is absent from the frozen suite manifest")
    raw_cleanup = raw_scenario.get("cleanup")
    raw_fixtures = raw_scenario.get("fixtures")
    if not isinstance(raw_cleanup, Mapping) or not isinstance(raw_fixtures, Mapping):
        raise ValueError("frozen suite manifest has no project cleanup contract")
    raw_checks = raw_cleanup.get("checks")
    if contract["checks"] != raw_checks:
        raise ValueError("saved project cleanup checks differ from the frozen suite manifest")
    raw_sql_files = raw_cleanup.get("sqlFiles")
    if not isinstance(raw_sql_files, list) or len(raw_sql_files) != len(contract["sqlFiles"]):
        raise ValueError("saved project cleanup SQL list differs from the frozen suite manifest")
    hashes = _read_json(run._manifest_path, "frozen input manifest").get("hashes", {})
    expected_objects: dict[str, str] = {}
    expected_object_bindings: dict[str, str] = {}
    prefix = f"DT_{hashlib.sha256((run.run_id + scenario['id']).encode('utf-8')).hexdigest()[:12].upper()}_"
    for fixture in raw_fixtures.get("objects", []):
        if not isinstance(fixture, Mapping) or not isinstance(fixture.get("binding"), str):
            raise ValueError("frozen suite fixture-object declaration is malformed")
        name = f"{prefix}{fixture.get('suffix')}"
        expected_objects[name] = fixture.get("type")
        expected_object_bindings[fixture["binding"]] = name
    if contract["objects"] != expected_objects or contract["objectBindings"] != expected_object_bindings:
        raise ValueError("saved project fixture-object map differs from deterministic fixture names")
    raw_selectors = raw_fixtures.get("selectors")
    if not isinstance(raw_selectors, list) or len(raw_selectors) != len(contract["selectors"]):
        raise ValueError("saved project selector list differs from the frozen suite manifest")
    known_bindings: dict[str, Any] = {"DEVTEST_RUN_ID": run.run_id}
    for raw_selector, selector in zip(raw_selectors, contract["selectors"], strict=True):
        if (
            not isinstance(raw_selector, Mapping)
            or selector.get("id") != raw_selector.get("id")
            or selector.get("sql") != raw_selector.get("sql")
            or selector.get("columns") != raw_selector.get("columns")
            or selector.get("inputBindings") != known_bindings
        ):
            raise ValueError("saved project selector differs from its frozen declaration or binding order")
        for column in raw_selector.get("columns", []):
            binding = column.get("binding") if isinstance(column, Mapping) else None
            if not isinstance(binding, str) or binding not in contract["bindings"]:
                raise ValueError("project selector output is not represented in saved key bindings")
            known_bindings[binding] = contract["bindings"][binding]
        if selector.get("bindings") != {
            column["binding"]: known_bindings[column["binding"]]
            for column in raw_selector.get("columns", [])
        }:
            raise ValueError("saved project selector values do not match the declared output bindings")
    if contract["bindings"] != known_bindings:
        raise ValueError("project cleanup bindings contain undeclared or missing selector values")
    for raw_path, saved_file in zip(raw_sql_files, contract["sqlFiles"], strict=True):
        if not isinstance(raw_path, str):
            raise ValueError("frozen cleanup SQL path is malformed")
        resolved = str((Path(manifest_path).parent / raw_path).resolve())
        if saved_file.get("path") != resolved or hashes.get(resolved) != saved_file.get("sha256"):
            raise ValueError("saved project cleanup SQL file differs from its frozen manifest input")
    from scripts.dev_test_bindings import render_object_tokens
    from scripts.read_only_query import prepare_query

    check_context = RenderContext(
        schema,
        run.run_id,
        contract["objectBindings"],
        contract["bindings"],
    )
    for check in contract["checks"]:
        if check.get("expected") != 0:
            raise ValueError("project cleanup checks must assert zero remaining rows")
        prepare_query(
            render_object_tokens(check["sql"], check_context),
            bindings=contract["bindings"],
        )
    recorded_bindings = set(contract["bindings"])
    operations: list[dict[str, Any]] = []
    seen_files: set[tuple[str, str]] = set()
    for index, item in enumerate(contract["sqlFiles"], 1):
        marker = (item["path"], item["sha256"])
        if marker in seen_files:
            raise ValueError("project cleanup contract repeats an SQL file")
        seen_files.add(marker)
        try:
            source = _frozen_input_bytes(run, item["path"], item["sha256"]).decode("utf-8")
        except UnicodeError as error:
            raise ValueError("project cleanup SQL is not UTF-8") from error
        operation = _validate_project_cleanup_sql(
            source,
            schema,
            recorded_bindings,
            contract["objects"],
        )
        operation.update({
            "index": index,
            "source": source,
            "sha256": item["sha256"],
        })
        operations.append(operation)
    if not operations:
        raise ValueError("project cleanup has no executable operations")
    selector_bindings = {
        binding
        for selector in contract["selectors"]
        for binding in selector["bindings"]
    }
    used_bindings = {
        binding
        for operation in operations
        if operation["kind"] == "delete"
        for binding in operation["bindings"]
    }
    if selector_bindings - used_bindings:
        raise ValueError("project cleanup does not address every recorded selector key")
    drop_pairs = {
        (operation["name"], operation["type"])
        for operation in operations if operation["kind"] == "drop"
    }
    for artifact in scenario["objects"]:
        if (artifact.get("name"), artifact.get("type")) not in drop_pairs:
            raise ValueError(f"project cleanup omits captured fixture object {artifact.get('name')}")
    return {
        "contract": contract,
        "operations": operations,
        "checks": [dict(check) for check in contract["checks"]],
        "bindings": dict(contract["bindings"]),
        "objectBindings": dict(contract["objectBindings"]),
    }


def _verify_project_selector_keys(
    target: Target,
    run: RunEvidence,
    program: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    runner: Callable,
    required_bindings: set[str] | None = None,
    consumed_bindings: set[str] | None = None,
) -> None:
    from scripts.dev_test_runtime import read_rows

    selectors = program["contract"]["selectors"]
    objects = program["objectBindings"]
    consumed = consumed_bindings or set()
    for selector in selectors:
        outputs = selector["bindings"]
        if outputs and set(outputs) <= consumed:
            continue
        if required_bindings is not None and not (set(outputs) & required_bindings):
            continue
        context = RenderContext(
            target.schema,
            run.run_id,
            objects,
            selector["inputBindings"],
        )
        result = read_rows(
            target,
            selector["sql"],
            context,
            run,
            max_rows=2,
            runner=runner,
        )
        if result.get("count") != 1 or result.get("truncated") is not False:
            raise ValueError(f"project selector {selector['id']} no longer returns exactly one complete row")
        assert_saved_target(expected_identity, result.get("identity", {}))
        row = result["rows"][0]
        observed: dict[str, int | str] = {}
        for column in selector["columns"]:
            name, binding, kind = column["name"], column["binding"], column["type"]
            if name not in row or row[name] is None or row[name] == "":
                raise ValueError(f"project selector {selector['id']} no longer returns its exact key column")
            value = row[name]
            if kind == "integer":
                if type(value) is int:
                    parsed = value
                elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip(), re.ASCII):
                    parsed = int(value.strip())
                else:
                    raise ValueError(f"project selector {selector['id']} returned an invalid integer key")
                if not -(2**53 - 1) <= parsed <= 2**53 - 1:
                    raise ValueError(f"project selector {selector['id']} returned an out-of-range key")
                observed[binding] = parsed
            elif kind == "string" and isinstance(value, str):
                observed[binding] = value
            else:
                raise ValueError(f"project selector {selector['id']} returned an invalid string key")
        if observed != dict(outputs):
            raise ValueError(f"project selector {selector['id']} key changed since the run")


def _previous_project_cleanup_verified(run: RunEvidence, report: Mapping[str, Any]) -> bool:
    cleanup = report.get("cleanup")
    history = cleanup.get("history") if isinstance(cleanup, Mapping) else None
    if not isinstance(history, list) or not history or cleanup.get("status") != "COMPLETE":
        return False
    last = history[-1]
    if not isinstance(last, Mapping) or last.get("status") != "COMPLETE":
        return False
    evidence = last.get("evidence")
    if not isinstance(evidence, str) or not evidence.startswith("cleanup/"):
        return False
    relative = Path(evidence)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    result = _read_json(run.run_dir / relative, "previous cleanup result")
    return result.get("status") == "COMPLETE" and result.get("postconditionsPassed") is True


def _run_project_cleanup_checks(
    target: Target,
    run: RunEvidence,
    program: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    runner: Callable,
) -> list[dict[str, Any]]:
    from scripts.dev_test_bindings import render_object_tokens
    from scripts.migration_checks import run_checks
    from scripts.migration_manifest import QueryCheck

    context = RenderContext(
        target.schema,
        run.run_id,
        program["objectBindings"],
        program["bindings"],
    )
    checks = [
        QueryCheck(
            check["id"],
            render_object_tokens(check["sql"], context),
            check["expected"],
        )
        for check in program["checks"]
    ]
    directory = run.run_dir / "sessions" / f"cleanup-checks-{uuid.uuid4().hex[:12]}"
    report = run_checks(
        target,
        checks,
        directory,
        phase="postconditions",
        _runner=runner,
        bindings=program["bindings"],
    )
    coverage = getattr(report, "coverage", None)
    identities = coverage.get("sessionIdentities") if isinstance(coverage, Mapping) else None
    sessions = coverage.get("sessions") if isinstance(coverage, Mapping) else None
    if (
        getattr(report, "complete", None) is not True
        or getattr(report, "passed", None) is not True
        or not isinstance(identities, list)
        or type(sessions) is not int
        or sessions != len(identities)
        or not identities
        or any(not isinstance(identity, Mapping) for identity in identities)
    ):
        raise ValueError("project cleanup absence checks did not complete and pass")
    for identity in identities:
        assert_saved_target(expected_identity, identity)
    results = getattr(report, "results", None)
    if not isinstance(results, (list, tuple)):
        raise ValueError("project cleanup absence checks returned malformed results")
    by_id = {
        result.get("check", result.get("id")): result
        for result in results if isinstance(result, Mapping)
    }
    if set(by_id) != {check.id for check in checks}:
        raise ValueError("project cleanup absence checks did not return every result")
    if any(result.get("passed") is not True or result.get("value") != 0 for result in by_id.values()):
        raise ValueError("project cleanup absence checks did not verify zero remaining rows")
    return [
        {"id": check.id, "passed": True, "expected": 0, "actual": 0}
        for check in checks
    ]


def _verify_unrecorded_project_names(
    target: Target,
    run: RunEvidence,
    scenarios: Sequence[Mapping[str, Any]],
    recorded_artifacts: Sequence[Mapping[str, Any]],
    expected_identity: Mapping[str, Any],
    runner: Callable,
) -> None:
    from scripts.schema_catalog import SchemaInventory, capture_inventory

    recorded = {
        (str(item["name"]).upper(), str(item["type"]).upper())
        for item in recorded_artifacts
    }
    names = {
        (name.upper(), object_type.upper())
        for scenario in scenarios
        for name, object_type in _validate_project_contract(scenario)["objects"].items()
        if (name.upper(), object_type.upper()) not in recorded
    }
    if not names:
        return
    inventory = capture_inventory(target, run.run_dir, _runner=runner)
    if not isinstance(inventory, SchemaInventory) or inventory.coverage.get("ownerComplete") is not True:
        raise CatalogError("current owner inventory is incomplete for unrecorded project fixture names")
    assert_saved_target(expected_identity, inventory.identity)
    live_names = {(key.name.upper(), key.object_type.upper()) for key in inventory.objects}
    if live_names & names:
        raise ValueError("an unrecorded project fixture name now exists; refusing to adopt or delete it")


def _validate_artifact(
    run: RunEvidence,
    schema: str,
    artifact: object,
    *,
    prefix: str | None = None,
) -> dict:
    if not isinstance(artifact, Mapping):
        raise ValueError("fixture inventory contains a malformed artifact")
    owner, name, object_type = (artifact.get(key) for key in ("owner", "name", "type"))
    if not all(isinstance(value, str) and value == value.upper() and _IDENTIFIER.fullmatch(value) for value in (owner, name)):
        raise ValueError("fixture inventory contains an invalid or noncanonical owner/name")
    expected_prefix = prefix or run.object_prefix
    if owner != schema or not name.startswith(expected_prefix):
        raise ValueError("fixture inventory artifact is outside the saved owner/run scope")
    if object_type not in _DROP_TYPES:
        raise ValueError(f"fixture object type is not supported for cleanup: {object_type!r}")
    object_id = artifact.get("objectId")
    if type(object_id) is not int or object_id <= 0:
        raise ValueError(f"fixture {name} has no exact observed object identity")
    if not isinstance(artifact.get("definitionSha256"), str) or not _DIGEST.fullmatch(artifact["definitionSha256"]):
        raise ValueError(f"fixture {name} has no complete definition digest")
    if not isinstance(artifact.get("dependents"), list):
        raise ValueError(f"fixture {name} has no complete dependent-object inventory")
    for dependent in artifact["dependents"]:
        if (
            not isinstance(dependent, Mapping)
            or dependent.get("owner") != schema
            or not isinstance(dependent.get("name"), str)
            or not _IDENTIFIER.fullmatch(dependent["name"])
            or not isinstance(dependent.get("type"), str)
            or type(dependent.get("objectId")) is not int
            or dependent["objectId"] <= 0
        ):
            raise ValueError(f"fixture {name} has incomplete dependent-object identity")
    if object_type == "PACKAGE":
        members = artifact.get("memberDefinitions")
        if not isinstance(members, Mapping) or "PACKAGE" not in members:
            raise ValueError(f"package fixture {name} has no member-definition inventory")
        for member in members.values():
            if (
                not isinstance(member, Mapping)
                or type(member.get("objectId")) is not int
                or not isinstance(member.get("definitionSha256"), str)
                or not _DIGEST.fullmatch(member["definitionSha256"])
            ):
                raise ValueError(f"package fixture {name} has incomplete member identity")
    return dict(artifact)


def _read_events(run: RunEvidence) -> list[dict]:
    if run.events_dir.is_symlink() or not run.events_dir.is_dir():
        raise ValueError("run event directory is missing or unsafe")
    paths = sorted(run.events_dir.glob("*.json"), key=lambda path: path.name)
    events: list[dict] = []
    for path in paths:
        event = _read_json(path, "run event")
        if not isinstance(event, dict):
            raise ValueError("run event is not a JSON object")
        if event.get("type") not in {"plan", "fixture-precreation", "fixture-observed", "step-intent", "cleanup-intent", "cleanup-result"}:
            if event.get("writeStarted", True) is not False:
                raise ValueError("run contains an unrecognized write event; cleanup ownership is ambiguous")
        events.append(event)
    return events


def _event_key(item: object) -> tuple[str, str, str] | None:
    if not isinstance(item, Mapping):
        return None
    values = (item.get("owner"), item.get("name"), item.get("type"))
    if not all(isinstance(value, str) for value in values):
        return None
    return tuple(value.upper() for value in values)  # type: ignore[return-value]


def _verify_fixture_history(
    run: RunEvidence,
    inventory: Mapping[str, Any],
    scenarios: Sequence[Mapping[str, Any]],
    origin: str,
) -> None:
    events = _read_events(run)
    global_keys: set[tuple[str, str, str]] = set()
    global_rows: list[Mapping[str, Any]] = []
    for scenario in scenarios:
        scenario_id = scenario["id"]
        recorded = scenario["objects"]
        expected = {_event_key(item) for item in recorded}
        if None in expected or len(expected) != len(recorded):
            raise ValueError(f"scenario {scenario_id} fixture keys are incomplete or duplicated")
        precreation: list[int] = []
        observed: list[tuple[int, set[tuple[str, str, str]], list[Mapping[str, Any]]]] = []
        intents: list[int] = []
        for index, event in enumerate(events):
            if event.get("scenarioId") != scenario_id:
                continue
            if event.get("type") == "fixture-precreation":
                if event.get("ownerComplete") is not True or not isinstance(event.get("objects"), list):
                    raise ValueError(f"scenario {scenario_id} has incomplete pre-creation evidence")
                if origin == "builtin":
                    keys = {
                        _event_key(item) for item in event["objects"]
                        if isinstance(item, Mapping) and item.get("absent") is True
                    }
                    if keys != expected:
                        raise ValueError(f"scenario {scenario_id} absence evidence does not cover its exact fixtures")
                else:
                    contract = _validate_project_contract(scenario)
                    planned_objects = event.get("plannedObjects")
                    if contract is None or event.get("projectCleanup") is not True or not isinstance(planned_objects, list):
                        raise ValueError(f"scenario {scenario_id} project pre-creation evidence is incomplete")
                    planned = {
                        (inventory["targetIdentity"].get("current_schema"), name, object_type)
                        for name, object_type in contract["objects"].items()
                    }
                    event_planned = {
                        _event_key(item) for item in planned_objects
                        if isinstance(item, Mapping) and item.get("absent") is True
                    }
                    if planned != event_planned or event.get("rows") != scenario.get("rows"):
                        raise ValueError(f"scenario {scenario_id} exact pre-creation evidence differs from its contract")
                try:
                    assert_saved_target(inventory["targetIdentity"], event.get("targetIdentity", {}))
                except DevTestTargetError as error:
                    raise ValueError(f"scenario {scenario_id} pre-creation target evidence differs") from error
                precreation.append(index)
            elif event.get("type") == "fixture-observed":
                if event.get("inventoryComplete") is not True or not isinstance(event.get("objects"), list):
                    raise ValueError(f"scenario {scenario_id} has incomplete observed-fixture evidence")
                keys = {_event_key(item) for item in event["objects"]}
                if None in keys or not keys <= expected:
                    raise ValueError(f"scenario {scenario_id} observed fixture evidence has unknown objects")
                if origin == "project" and (
                    keys != expected or event.get("rows") != scenario.get("rows")
                ):
                    raise ValueError(f"scenario {scenario_id} observed project fixtures differ from saved keys")
                if origin == "project":
                    contract = _validate_project_contract(scenario)
                    planned_objects = event.get("plannedObjects")
                    expected_planned = {
                        (inventory["targetIdentity"].get("current_schema"), name, object_type)
                        for name, object_type in contract["objects"].items()
                    }
                    actual_planned = {
                        _event_key(item) for item in planned_objects
                        if isinstance(item, Mapping)
                    } if isinstance(planned_objects, list) else set()
                    if actual_planned != expected_planned or not expected <= expected_planned:
                        raise ValueError(f"scenario {scenario_id} post-run fixture scope differs from its declared objects")
                try:
                    assert_saved_target(inventory["targetIdentity"], event.get("targetIdentity", {}))
                except DevTestTargetError as error:
                    raise ValueError(f"scenario {scenario_id} observed target evidence differs") from error
                observed.append((index, keys, event["objects"]))
            elif event.get("type") == "step-intent" and event.get("writeStarted") is True:
                intents.append(index)
        if not precreation:
            raise ValueError(f"scenario {scenario_id} has no complete proof that fixture names were absent before creation")
        latest_observed = observed[-1] if observed else None
        if (
            latest_observed is None
            or latest_observed[1] != expected
            or (intents and latest_observed[0] < max(intents))
            or _canonical_objects(latest_observed[2]) != _canonical_objects(recorded)
        ):
            raise ValueError(f"scenario {scenario_id} has a write intent without later verified fixture inventory")
        if global_keys & expected:
            raise ValueError("fixture inventory assigns one object to multiple scenarios")
        global_keys |= expected
        if origin == "project":
            global_rows.extend(scenario.get("rows", []))
    inventory_keys = {_event_key(item) for item in inventory.get("objects", [])}
    if None in inventory_keys or inventory_keys != global_keys:
        raise ValueError("run-wide fixture inventory does not match its per-scenario ownership records")
    if origin == "project":
        rows = inventory.get("rows", [])
        if _canonical_objects(rows) != _canonical_objects(global_rows):
            raise ValueError("run-wide exact-key inventory does not match per-scenario selector evidence")


def _canonical_objects(objects: Sequence[Mapping[str, Any]]) -> str:
    try:
        return json.dumps([dict(item) for item in objects], sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("fixture ownership evidence is not finite JSON") from error


def _capture_live_inventory(
    target: Target,
    run: RunEvidence,
    runner: Callable,
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Capture one complete owner inventory and selected exact definitions."""
    catalog = capture_inventory(target, run.run_dir, _runner=runner)
    if not isinstance(catalog, SchemaInventory) or catalog.coverage.get("ownerComplete") is not True:
        raise CatalogError("current owner inventory is incomplete")
    live: list[dict[str, Any]] = []
    for artifact in artifacts:
        owner, name, object_type = artifact["owner"], artifact["name"], artifact["type"]
        key = ObjectKey(owner, name, object_type)
        row = catalog.objects.get(key)
        if row is None:
            continue
        snapshot = capture_snapshot(target, catalog, [(name, object_type)], run.run_dir, _runner=runner)
        definition = snapshot.objects.get(key)
        if definition is None:
            raise CatalogError(f"current definition is missing for {owner}.{name} ({object_type})")
        dependents = []
        for child in definition.dependents:
            child_row = catalog.objects.get(child)
            if not isinstance(child_row, Mapping) or type(child_row.get("object_id")) is not int:
                raise CatalogError(f"current dependent identity is incomplete for {child.owner}.{child.name}")
            dependents.append({
                "owner": child.owner, "name": child.name, "type": child.object_type,
                "objectId": child_row["object_id"],
            })
        dependents.sort(key=lambda item: (item["owner"], item["name"], item["type"], item["objectId"]))
        current = {
            "owner": owner,
            "name": name,
            "type": object_type,
            "objectId": row.get("object_id"),
            "dataObjectId": row.get("data_object_id"),
            "status": row.get("status"),
            "definitionSha256": hashlib.sha256(definition.raw_ddl.encode("utf-8")).hexdigest(),
            "dependents": dependents,
        }
        if object_type == "PACKAGE":
            members: dict[str, dict[str, Any]] = {}
            for member_type in ("PACKAGE", "PACKAGE BODY"):
                member_key = ObjectKey(owner, name, member_type)
                member_row = catalog.objects.get(member_key)
                member_definition = snapshot.objects.get(member_key)
                if member_row is None:
                    continue
                if member_definition is None or type(member_row.get("object_id")) is not int:
                    raise CatalogError(f"current package member is incomplete for {owner}.{name}")
                members[member_type] = {
                    "objectId": member_row["object_id"],
                    "definitionSha256": hashlib.sha256(member_definition.raw_ddl.encode("utf-8")).hexdigest(),
                }
            current["memberDefinitions"] = members
        live.append(current)
    return {"identity": dict(catalog.identity), "complete": True, "objects": live}


def _compare_artifact(saved: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    for key in ("owner", "name", "type", "objectId", "definitionSha256", "dependents"):
        if saved.get(key) != current.get(key):
            return f"{key} changed since the run recorded this fixture"
    for key in ("dataObjectId", "status", "memberDefinitions"):
        if key in saved and saved.get(key) != current.get(key):
            return f"{key} changed since the run recorded this fixture"
    return None


def _refused_result(run_id: str, reason: str) -> dict[str, Any]:
    return {"runId": run_id, "status": "REFUSED", "reason": reason, "artifacts": [], "eligible": [], "refused": []}


def _build_plan(run: RunEvidence, values: Mapping[str, str], runner: Callable) -> tuple[dict[str, Any], tuple | None]:
    try:
        opened, report, inventory = _load_run(run)
        scenario_entries, special_status = _validate_scenarios(report, inventory)
        if special_status:
            return ({"runId": opened.run_id, "status": special_status, "artifacts": [], "eligible": [], "refused": []}, None)
        origin = report.get("selection", {}).get("origin")
        configured = report.get("target", {}).get("configured")
        if not isinstance(configured, Mapping) or configured.get("environment") != "dev":
            raise ValueError("cleanup requires the run's saved DEV target configuration")
        schema = configured.get("schema")
        if not isinstance(schema, str) or not schema:
            raise ValueError("saved DEV target has no selected owner")
        try:
            target = resolve_target(values, "dev", "migration", schema=schema)
        except TargetResolutionError as error:
            raise ValueError("saved DEV target can no longer be resolved from the configured profile") from error
        if (
            target.connection != configured.get("connection")
            or target.expected_user != configured.get("expectedUser")
            or target.schema != schema
        ):
            raise ValueError("configured DEV profile no longer matches the run's saved target")
        saved_identity = report.get("target", {}).get("observed")
        if not isinstance(saved_identity, Mapping):
            raise ValueError("run report has no independently observed database identity")

        artifacts: list[dict[str, Any]] = []
        programs: dict[str, dict[str, Any]] = {}
        for scenario in scenario_entries:
            prefix = f"DT_{hashlib.sha256((opened.run_id + scenario['id']).encode('utf-8')).hexdigest()[:12].upper()}_"
            if origin == "project":
                program = _project_program(opened, report, schema, scenario)
                programs[scenario["id"]] = program
            for raw in scenario["objects"]:
                item = _validate_artifact(opened, schema, raw, prefix=prefix)
                if origin == "project" and program["contract"]["objects"].get(item["name"]) != item["type"]:
                    raise ValueError(f"project artifact {item['name']} is outside its declared fixture map")
                item["scenarioId"] = scenario["id"]
                artifacts.append(item)
        if origin == "builtin" and not artifacts:
            return ({"runId": opened.run_id, "status": "UNSUPPORTED", "artifacts": [], "eligible": [], "refused": []}, None)
        if len({_event_key(item) for item in artifacts}) != len(artifacts):
            raise ValueError("run inventory repeats an artifact across scenarios")
        rows = inventory.get("rows", [])
        if any(not isinstance(row, Mapping) or row.get("truncated") is True or row.get("complete") is False for row in rows):
            raise ValueError("fixture key inventory is truncated or incomplete")
        _verify_fixture_history(opened, inventory, scenario_entries, origin)
        previous_project_cleanup = origin == "project" and _previous_project_cleanup_verified(opened, report)
        if origin == "project":
            _verify_unrecorded_project_names(
                target, opened, scenario_entries, artifacts, saved_identity, runner,
            )
            if previous_project_cleanup:
                for program in programs.values():
                    _run_project_cleanup_checks(target, opened, program, saved_identity, runner)
            else:
                for program in programs.values():
                    needed = {
                        binding
                        for operation in program["operations"]
                        if operation["kind"] == "delete"
                        for binding in operation["bindings"]
                    }
                    _verify_project_selector_keys(
                        target, opened, program, saved_identity, runner, needed or None,
                    )
        live = _capture_live_inventory(target, opened, runner, artifacts)
        assert_saved_target(saved_identity, live.get("identity", {}))
        if live.get("complete") is not True or not isinstance(live.get("objects"), list):
            raise ValueError("current fixture inventory is incomplete")
        current_by_key = {_event_key(item): item for item in live["objects"] if isinstance(item, Mapping)}
        planned: list[dict[str, Any]] = []
        for artifact in artifacts:
            key = _event_key(artifact)
            current = current_by_key.get(key)
            if current is None:
                planned.append({**artifact, "status": "ALREADY_ABSENT"})
                continue
            mismatch = _compare_artifact(artifact, current)
            if mismatch:
                planned.append({**artifact, "status": "REFUSED", "reason": mismatch})
            else:
                planned.append({**artifact, "status": "READY"})
        planned.sort(key=lambda item: (_DROP_ORDER[item["type"]], item["scenarioId"], item["name"]))
        refused = [item for item in planned if item["status"] == "REFUSED"]
        eligible = [item for item in planned if item["status"] in {"READY", "ALREADY_ABSENT"}]
        operations: list[dict[str, Any]] = []
        if origin == "project":
            planned_by_key = {_event_key(item): item for item in planned}
            for scenario in scenario_entries:
                program = programs[scenario["id"]]
                for operation in program["operations"]:
                    item = {key: value for key, value in operation.items() if key not in {"source", "sha256"}}
                    item["scenarioId"] = scenario["id"]
                    if previous_project_cleanup:
                        item["status"] = "ALREADY_ABSENT"
                    elif item["kind"] == "drop":
                        artifact = next(
                            (
                                value for value in scenario["objects"]
                                if value.get("name") == item["name"] and value.get("type") == item["type"]
                            ),
                            None,
                        )
                        if artifact is None:
                            item["status"] = "ALREADY_ABSENT"
                        else:
                            current_item = planned_by_key.get(_event_key(artifact))
                            if current_item is None:
                                item.update({"status": "REFUSED", "reason": "current object identity was not captured"})
                            elif current_item["status"] == "REFUSED":
                                item.update({"status": "REFUSED", "reason": current_item.get("reason")})
                            elif artifact.get("dependents"):
                                item.update({"status": "REFUSED", "reason": "fixture has dependents; cleanup never cascades"})
                            else:
                                item["status"] = current_item["status"]
                    else:
                        item["status"] = "READY"
                    operations.append(item)
            refused.extend(item for item in operations if item["status"] == "REFUSED")
            status = (
                "REFUSED" if refused
                else "COMPLETE" if operations and all(item["status"] == "ALREADY_ABSENT" for item in operations)
                else "READY"
            )
        else:
            status = "REFUSED" if refused else ("COMPLETE" if all(item["status"] == "ALREADY_ABSENT" for item in planned) else "READY")
        result = {
            "runId": opened.run_id,
            "status": status,
            "artifacts": planned,
            "operations": [
                {
                    key: item[key]
                    for key in ("scenarioId", "index", "kind", "name", "type", "bindings", "status", "reason")
                    if key in item
                }
                for item in operations
            ],
            "eligible": eligible,
            "refused": refused,
        }
        return result, (opened, report, inventory, target, saved_identity, artifacts, programs, origin)
    except (OSError, ValueError, TypeError, RuntimeError, CatalogError, DevTestTargetError) as error:
        return _refused_result(run.run_id, str(error)), None


def plan_cleanup(
    run: RunEvidence,
    values: Mapping[str, str],
    *,
    runner: Callable = run_sqlcl,
) -> dict:
    """Inspect saved ownership and current DEV state without issuing writes."""
    result, _context = _build_plan(run, values, runner)
    return result


def _drop_sql(target: Target, artifact: Mapping[str, Any]) -> str:
    object_type = artifact.get("type")
    if object_type not in _DROP_TYPES:
        raise ValueError("unsupported fixture type")
    name = artifact.get("name")
    owner = artifact.get("owner")
    object_id = artifact.get("objectId")
    if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (name, owner)):
        raise ValueError("invalid fixture identifier")
    if owner != target.schema or type(object_id) is not int or object_id <= 0:
        raise ValueError("fixture does not match the selected owner/object identity")
    literal_name = name.replace("'", "''")
    guard = (
        "DECLARE\n"
        "  l_object_id USER_OBJECTS.OBJECT_ID%TYPE;\n"
        "  l_member_id USER_OBJECTS.OBJECT_ID%TYPE;\n"
        "BEGIN\n"
        "  IF UPPER(SYS_CONTEXT('USERENV','SESSION_USER')) <> '" + target.expected_user + "' "
        "OR UPPER(SYS_CONTEXT('USERENV','CURRENT_SCHEMA')) <> '" + target.schema + "' THEN\n"
        "    RAISE_APPLICATION_ERROR(-20091, 'cleanup owner identity changed');\n"
        "  END IF;\n"
        "  SELECT OBJECT_ID INTO l_object_id FROM USER_OBJECTS "
        "WHERE OBJECT_NAME = '" + literal_name + "' AND OBJECT_TYPE = '" + object_type + "';\n"
        f"  IF l_object_id <> {object_id} THEN\n"
        "    RAISE_APPLICATION_ERROR(-20092, 'cleanup object identity changed');\n"
        "  END IF;\n"
    )
    members = artifact.get("memberDefinitions") if object_type == "PACKAGE" else None
    if isinstance(members, Mapping) and "PACKAGE BODY" in members:
        body_id = members["PACKAGE BODY"].get("objectId")
        if type(body_id) is not int or body_id <= 0:
            raise ValueError("package body has no exact observed object identity")
        guard += (
            "  SELECT OBJECT_ID INTO l_member_id FROM USER_OBJECTS "
            f"WHERE OBJECT_NAME = '{literal_name}' AND OBJECT_TYPE = 'PACKAGE BODY';\n"
            f"  IF l_member_id <> {body_id} THEN\n"
            "    RAISE_APPLICATION_ERROR(-20093, 'cleanup package body identity changed');\n"
            "  END IF;\n"
        )
    return guard + f"  EXECUTE IMMEDIATE 'DROP {object_type} \"{name}\"';\nEND;\n/\n"


def _execute_drop(
    target: Target,
    artifact: Mapping[str, Any],
    run: RunEvidence,
    runner: Callable,
    expected_identity: Mapping[str, Any],
    ordinal: int,
) -> dict:
    from scripts.dev_test_core_types import _execute_generated_step

    step_id = f"drop-{ordinal:03d}"
    scenario = SimpleNamespace(id="cleanup")
    context = RenderContext(target.schema, run.run_id, {}, {})
    return _execute_generated_step(
        target, scenario, step_id, _drop_sql(target, artifact), context,
        run, runner, expected_identity, ordinal,
    )


def _save_cleanup_result(run: RunEvidence, report: dict, result: dict) -> dict:
    cleanup_dir = run.run_dir / "cleanup"
    if cleanup_dir.is_symlink():
        raise OSError("cleanup evidence directory is a symbolic link")
    cleanup_dir.mkdir(mode=0o700, exist_ok=True)
    report_path = run.run_dir / "report.json"
    if report_path.is_symlink():
        raise OSError("saved report is a symbolic link")
    filename = f"{len(list(cleanup_dir.glob('*.json'))):06d}-{uuid.uuid4().hex[:8]}.json"
    result = {**result, "finishedAt": _now(), "evidence": (cleanup_dir / filename).relative_to(run.run_dir).as_posix()}
    encoded = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path = cleanup_dir / filename
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    append_event(run, {"type": "cleanup-result", "status": result["status"], "evidence": result["evidence"]})
    cleanup = report.setdefault("cleanup", {"status": "NOT_REQUESTED", "history": []})
    if not isinstance(cleanup, dict) or not isinstance(cleanup.get("history"), list):
        raise ValueError("saved report cleanup history is malformed")
    cleanup["status"] = result["status"]
    cleanup["history"].append({
        "status": result["status"],
        "startedAt": result.get("startedAt"),
        "finishedAt": result["finishedAt"],
        "artifactCount": len(result.get("artifacts", [])),
        "evidence": result["evidence"],
        "reason": result.get("reason"),
    })
    save_report(run, report)
    return result


def _cleanup_project(
    plan: Mapping[str, Any],
    opened: RunEvidence,
    report: dict,
    inventory: Mapping[str, Any],
    target: Target,
    saved_identity: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    programs: Mapping[str, Mapping[str, Any]],
    scenarios: Sequence[Mapping[str, Any]],
    runner: Callable,
    started: str,
) -> dict[str, Any]:
    from scripts.dev_test_bindings import RenderContext
    from scripts.dev_test_core_types import _execute_generated_step

    result: dict[str, Any] = {
        "runId": opened.run_id,
        "status": "COMPLETE",
        "startedAt": started,
        "artifacts": [],
        "operations": [],
        "checks": [],
        "postconditionsPassed": False,
    }
    if plan.get("status") == "REFUSED":
        result["status"] = "REFUSED"
        result["reason"] = "Project cleanup plan contains a refusal; no writes were started"
        result["operations"] = list(plan.get("operations", []))
        return _save_cleanup_result(opened, report, result)

    planned_lookup = {
        (item.get("scenarioId"), item.get("index")): item
        for item in plan.get("operations", []) if isinstance(item, Mapping)
    }
    artifact_lookup = {
        (item.get("scenarioId"), item.get("name"), item.get("type")): dict(item)
        for item in artifacts
    }
    any_write_started = False
    consumed: dict[str, set[str]] = {scenario_id: set() for scenario_id in programs}
    ordinal = 1
    try:
        for scenario_id, program in programs.items():
            for operation in program["operations"]:
                planned = planned_lookup.get((scenario_id, operation["index"]), {})
                operation_result = {
                    "scenarioId": scenario_id,
                    "index": operation["index"],
                    "kind": operation["kind"],
                    "name": operation["name"],
                    "type": operation.get("type"),
                    "bindings": operation["bindings"],
                }
                status = planned.get("status")
                if status == "REFUSED":
                    operation_result.update({"status": "REFUSED", "reason": planned.get("reason")})
                    result["operations"].append(operation_result)
                    result["status"] = "REFUSED" if not any_write_started else "INCOMPLETE"
                    break
                if status == "ALREADY_ABSENT":
                    operation_result["status"] = "ALREADY_ABSENT"
                    result["operations"].append(operation_result)
                    continue

                assert_inputs_unchanged(opened)
                context = RenderContext(
                    target.schema,
                    opened.run_id,
                    program["objectBindings"],
                    program["bindings"],
                )
                if operation["kind"] == "delete":
                    _verify_project_selector_keys(
                        target,
                        opened,
                        program,
                        saved_identity,
                        runner,
                        set(operation["bindings"]),
                        consumed[scenario_id],
                    )
                    step_id = f"project-{scenario_id}-{operation['index']:03d}"
                    step_scenario = SimpleNamespace(id="cleanup")
                    step_sql = operation["source"].rstrip() + "\nCOMMIT;\n"
                else:
                    artifact = artifact_lookup.get((scenario_id, operation["name"], operation["type"]))
                    if artifact is None:
                        operation_result["status"] = "ALREADY_ABSENT"
                        result["operations"].append(operation_result)
                        continue
                    fresh = _capture_live_inventory(target, opened, runner, [artifact])
                    assert_saved_target(saved_identity, fresh.get("identity", {}))
                    current = next(
                        (item for item in fresh["objects"] if _event_key(item) == _event_key(artifact)),
                        None,
                    )
                    if current is None:
                        operation_result["status"] = "ALREADY_ABSENT"
                        result["operations"].append(operation_result)
                        continue
                    mismatch = _compare_artifact(artifact, current)
                    if mismatch:
                        operation_result.update({"status": "REFUSED", "reason": mismatch})
                        result["operations"].append(operation_result)
                        result["status"] = "REFUSED" if not any_write_started else "INCOMPLETE"
                        break
                    append_event(opened, {
                        "type": "cleanup-intent",
                        "runId": opened.run_id,
                        "scenarioId": scenario_id,
                        "targetIdentity": dict(saved_identity),
                        "artifact": {
                            "owner": artifact["owner"], "name": artifact["name"], "type": artifact["type"],
                            "objectId": artifact["objectId"],
                        },
                        "writeStarted": True,
                    })
                    any_write_started = True
                    step_result = _execute_drop(target, artifact, opened, runner, saved_identity, ordinal)
                    ordinal += 1
                    if not isinstance(step_result, Mapping) or step_result.get("status") != "PASSED":
                        operation_result.update({"status": "INCOMPLETE", "reason": "guarded DROP did not verify completion"})
                        result["operations"].append(operation_result)
                        result["status"] = "INCOMPLETE"
                        break
                    readback = _capture_live_inventory(target, opened, runner, [artifact])
                    assert_saved_target(saved_identity, readback.get("identity", {}))
                    if readback.get("complete") is not True or any(
                        _event_key(item) == _event_key(artifact) for item in readback["objects"]
                    ):
                        operation_result.update({"status": "INCOMPLETE", "reason": "fresh inventory did not verify fixture absence"})
                        result["operations"].append(operation_result)
                        result["status"] = "INCOMPLETE"
                        break
                    operation_result["status"] = "COMPLETE"
                    result["operations"].append(operation_result)
                    continue

                append_event(opened, {
                    "type": "cleanup-intent",
                    "runId": opened.run_id,
                    "scenarioId": scenario_id,
                    "operationIndex": operation["index"],
                    "targetIdentity": dict(saved_identity),
                    "operation": {
                        "kind": operation["kind"], "name": operation["name"],
                        "bindings": list(operation["bindings"]),
                    },
                    "writeStarted": True,
                })
                any_write_started = True
                step_result = _execute_generated_step(
                    target,
                    step_scenario,
                    step_id,
                    step_sql,
                    context,
                    opened,
                    runner,
                    saved_identity,
                    ordinal,
                )
                ordinal += 1
                if not isinstance(step_result, Mapping) or step_result.get("status") != "PASSED":
                    operation_result.update({"status": "INCOMPLETE", "reason": "frozen cleanup SQL did not verify completion"})
                    result["operations"].append(operation_result)
                    result["status"] = "INCOMPLETE"
                    break
                consumed[scenario_id].update(operation["bindings"])
                operation_result["status"] = "COMPLETE"
                result["operations"].append(operation_result)
            if result["status"] != "COMPLETE":
                break

        if result["status"] == "COMPLETE":
            for scenario_id, program in programs.items():
                result["checks"].extend(
                    {"scenarioId": scenario_id, **item}
                    for item in _run_project_cleanup_checks(
                        target, opened, program, saved_identity, runner,
                    )
                )
            if artifacts:
                readback = _capture_live_inventory(target, opened, runner, artifacts)
                assert_saved_target(saved_identity, readback.get("identity", {}))
                if readback.get("complete") is not True or readback.get("objects"):
                    raise RuntimeError("fresh owner inventory found project fixture objects after cleanup")
            _verify_unrecorded_project_names(
                target, opened, scenarios, artifacts, saved_identity, runner,
            )
            result["postconditionsPassed"] = True
            result["reason"] = None
        else:
            result["reason"] = "Project cleanup stopped before all operations and absence checks were verified"
    except KeyboardInterrupt:
        result["status"] = "INCOMPLETE" if any_write_started else "REFUSED"
        result["reason"] = "Project cleanup was interrupted; inspect intent and outcome evidence before retrying"
    except Exception as error:
        result["status"] = "INCOMPLETE" if any_write_started else "REFUSED"
        result["reason"] = f"Project cleanup stopped before verified completion: {type(error).__name__}"
    return _save_cleanup_result(opened, report, result)


def cleanup_run(
    run: RunEvidence,
    values: Mapping[str, str],
    *,
    runner: Callable = run_sqlcl,
) -> dict:
    """Remove only currently verified artifacts from a saved DEV run."""
    started = _now()
    plan, private = _build_plan(run, values, runner)
    if private is None:
        try:
            opened, report, _inventory = _load_run(run)
            result = {**plan, "startedAt": started}
            return _save_cleanup_result(opened, report, result)
        except Exception:
            return {**plan, "startedAt": started}
    opened, report, inventory, target, saved_identity, artifacts, programs, origin = private
    if origin == "project":
        scenario_entries, _special_status = _validate_scenarios(report, inventory)
        return _cleanup_project(
            plan,
            opened,
            report,
            inventory,
            target,
            saved_identity,
            artifacts,
            programs,
            scenario_entries,
            runner,
            started,
        )
    result: dict[str, Any] = {
        "runId": opened.run_id,
        "status": "COMPLETE",
        "startedAt": started,
        "artifacts": [],
    }
    any_write_started = False
    try:
        for ordinal, planned in enumerate(plan["artifacts"], 1):
            artifact = {key: value for key, value in planned.items() if key not in {"status", "reason", "scenarioId"}}
            item_result = {"owner": artifact["owner"], "name": artifact["name"], "type": artifact["type"]}
            if planned["status"] == "REFUSED":
                item_result.update({"status": "REFUSED", "reason": planned.get("reason")})
                result["artifacts"].append(item_result)
                continue

            fresh = _capture_live_inventory(target, opened, runner, [artifact])
            assert_saved_target(saved_identity, fresh.get("identity", {}))
            if fresh.get("complete") is not True:
                raise RuntimeError("fresh owner inventory became incomplete before cleanup")
            live = next((obj for obj in fresh["objects"] if _event_key(obj) == _event_key(artifact)), None)
            if live is None:
                item_result["status"] = "ALREADY_ABSENT"
                result["artifacts"].append(item_result)
                continue
            mismatch = _compare_artifact(artifact, live)
            if mismatch:
                item_result.update({"status": "REFUSED", "reason": mismatch})
                result["artifacts"].append(item_result)
                continue

            append_event(opened, {
                "type": "cleanup-intent",
                "runId": opened.run_id,
                "targetIdentity": dict(saved_identity),
                "artifact": dict(artifact),
                "writeStarted": True,
            })
            any_write_started = True
            drop_result = _execute_drop(target, artifact, opened, runner, saved_identity, ordinal)
            if not isinstance(drop_result, Mapping) or drop_result.get("status") != "PASSED":
                item_result.update({
                    "status": "INCOMPLETE",
                    "reason": "DROP step did not complete with verified target identity",
                })
                result["artifacts"].append(item_result)
                result["status"] = "INCOMPLETE"
                break
            readback = _capture_live_inventory(target, opened, runner, [artifact])
            assert_saved_target(saved_identity, readback.get("identity", {}))
            if readback.get("complete") is not True or any(_event_key(obj) == _event_key(artifact) for obj in readback["objects"]):
                item_result.update({"status": "INCOMPLETE", "reason": "fresh inventory did not verify fixture absence"})
                result["artifacts"].append(item_result)
                result["status"] = "INCOMPLETE"
                break
            item_result["status"] = "COMPLETE"
            result["artifacts"].append(item_result)

        if result["status"] == "COMPLETE" and any(item["status"] == "REFUSED" for item in result["artifacts"]):
            result["status"] = "REFUSED" if not any_write_started else "INCOMPLETE"
        elif result["status"] == "COMPLETE" and plan["status"] == "COMPLETE":
            result["status"] = "COMPLETE"
        result["reason"] = None if result["status"] == "COMPLETE" else "One or more fixtures were retained or cleanup verification was incomplete"
    except KeyboardInterrupt:
        result["status"] = "INCOMPLETE" if any_write_started else "REFUSED"
        result["reason"] = "Cleanup was interrupted; inspect the latest intent and outcome evidence before retrying"
    except Exception as error:
        result["status"] = "INCOMPLETE" if any_write_started else "REFUSED"
        result["reason"] = f"Cleanup stopped before verified completion: {type(error).__name__}"
    return _save_cleanup_result(opened, report, result)


__all__ = ["plan_cleanup", "cleanup_run"]
