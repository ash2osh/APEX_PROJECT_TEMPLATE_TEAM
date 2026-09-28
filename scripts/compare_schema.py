#!/usr/bin/env python3
"""Select and compare live Oracle schema definitions without database writes."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .db_targets import Target, TargetResolutionError, resolve_target
from .schema_catalog import (
    CatalogError,
    ObjectDefinition,
    ObjectKey,
    SchemaInventory,
    SchemaSnapshot,
    SUPPORTED_ROOT_TYPES,
    capture_inventory,
    capture_snapshot,
    same_database_scope,
)
from .schema_normalization import (
    GENERATED_CONSTRAINT,
    GENERATED_INDEX,
    normalize_definition,
    normalization_coverage,
)


ROOT = Path(__file__).resolve().parents[1]
EXACT_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*\Z", re.ASCII)
TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*(?:\s+[A-Za-z0-9_$#]+)*\Z", re.ASCII)
PATTERN_CHARS_RE = re.compile(r"[A-Za-z0-9_$#*]+\Z", re.ASCII)
SCOPE_IDENTITY_FIELDS = ("db_unique_name", "container_id", "container_name", "current_schema", "edition")


@dataclass(frozen=True)
class Selection:
    keys: tuple[tuple[str, str], ...]
    errors: tuple[dict, ...]
    objects: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComparisonReport:
    source: dict
    target: dict
    selection: dict
    coverage: dict
    differences: tuple[dict, ...]
    exit_code: int


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parse_quoted_identifier(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ValueError("quoted exact identifiers must start and end with a double quote")
    result = []
    index = 1
    end = len(value) - 1
    while index < end:
        if value[index] == '"':
            if index + 1 < end and value[index + 1] == '"':
                result.append('"')
                index += 2
                continue
            raise ValueError("embedded double quotes in exact identifiers must be doubled")
        if value[index] in "\r\n\x00":
            raise ValueError("quoted exact identifiers cannot contain line breaks or NUL")
        result.append(value[index])
        index += 1
    name = "".join(result)
    if not name:
        raise ValueError("exact object name cannot be empty")
    return name


def parse_exact_selector(selector: str) -> tuple[str, str | None]:
    """Parse NAME, \"Quoted Name\", or TYPE:NAME; unquoted names fold uppercase."""
    if not isinstance(selector, str) or not selector:
        raise ValueError("exact object selector cannot be empty")
    type_name = None
    name_part = selector
    if not selector.startswith('"') and ":" in selector:
        type_part, name_part = selector.split(":", 1)
        if not TYPE_RE.fullmatch(type_part.strip()):
            raise ValueError(f"invalid object type qualifier: {type_part}")
        type_name = " ".join(type_part.strip().replace("_", " ").upper().split())
        if not type_name:
            raise ValueError("object type qualifier cannot be empty")
    if name_part.startswith('"'):
        name = _parse_quoted_identifier(name_part)
    elif EXACT_IDENTIFIER_RE.fullmatch(name_part):
        name = name_part.upper()
    else:
        raise ValueError(f"invalid exact object name: {name_part}")
    return name, type_name


def _normalize_pattern(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("object pattern cannot be empty")
    if not PATTERN_CHARS_RE.fullmatch(pattern):
        raise ValueError("patterns accept ordinary identifier characters and '*' only")
    return pattern.upper()


def validate_selector_inputs(objects: Sequence[str], patterns: Sequence[str]) -> tuple[tuple[tuple[str, str | None], ...], tuple[str, ...]]:
    if not objects and not patterns:
        raise ValueError("at least one --object or --pattern selector is required")
    parsed_objects = tuple(parse_exact_selector(item) for item in objects)
    parsed_patterns = tuple(_normalize_pattern(item) for item in patterns)
    return parsed_objects, parsed_patterns


def _identity_owner(inventory: SchemaInventory) -> str:
    owner = inventory.identity.get("current_schema")
    return str(owner) if isinstance(owner, str) else ""


def _row_is_identity_sequence(row: Mapping) -> bool:
    value = row.get("identity_sequence")
    return value is True or (isinstance(value, str) and value.upper() == "YES")


def _inventory_contains(inventory: SchemaInventory, name: str, object_type: str) -> bool:
    owner = _identity_owner(inventory)
    return ObjectKey(owner, name, object_type) in inventory.objects


def _ordinary_inventory_entries(inventory: SchemaInventory):
    owner = _identity_owner(inventory)
    for key, row in inventory.objects.items():
        if key.owner != owner:
            continue
        if _row_is_identity_sequence(row):
            continue
        yield (key.name, key.object_type), row


def _matched_exact(candidate: tuple[str, str], parsed: tuple[str, str | None]) -> bool:
    name, type_name = parsed
    candidate_name, candidate_type = candidate
    return candidate_name == name and (type_name is None or candidate_type.upper() == type_name)


def _matched_pattern(name: str, pattern: str) -> bool:
    # Patterns intentionally target ordinary uppercase names. Quoted mixed-case
    # identifiers require an exact quoted --object selector.
    return name == name.upper() and fnmatch.fnmatchcase(name, pattern)


def select_objects(
    source: SchemaInventory,
    target: SchemaInventory,
    objects: Sequence[str],
    patterns: Sequence[str],
) -> Selection:
    parsed_objects, parsed_patterns = validate_selector_inputs(objects, patterns)
    errors: list[dict] = []
    for side, inventory in (("source", source), ("target", target)):
        if inventory.coverage.get("ownerComplete") is not True:
            errors.append({"code": "INCOMPLETE_INVENTORY", "side": side, "message": "complete owner visibility is required for selector matching"})
        if not _identity_owner(inventory):
            errors.append({"code": "IDENTITY_INCOMPLETE", "side": side, "message": "selected owner is missing from inventory identity"})

    entries = {}
    identity_sequences: dict[tuple[str, str], list[tuple[SchemaInventory, dict]]] = {}
    for side, inventory in (("source", source), ("target", target)):
        for key, row in inventory.objects.items():
            if key.owner != _identity_owner(inventory):
                continue
            candidate = (key.name, key.object_type)
            if _row_is_identity_sequence(row):
                identity_sequences.setdefault(candidate, []).append((inventory, row))
            else:
                entries.setdefault(candidate, set()).add(side)

    selected: set[tuple[str, str]] = set()

    def add_identity_table(inventory: SchemaInventory, row: Mapping, selector: str) -> None:
        table_name = row.get("identity_table_name")
        if not isinstance(table_name, str) or not table_name:
            errors.append({"code": "IDENTITY_RELATION_UNKNOWN", "selector": selector, "message": "matched identity sequence has no verified table relationship"})
            return
        if not _inventory_contains(inventory, table_name, "TABLE"):
            errors.append({"code": "IDENTITY_RELATION_UNKNOWN", "selector": selector, "message": f"identity sequence table is not visible: {table_name}"})
            return
        selected.add((table_name, "TABLE"))

    for raw, parsed in zip(objects, parsed_objects, strict=True):
        matched = False
        for candidate in entries:
            if _matched_exact(candidate, parsed):
                selected.add(candidate)
                matched = True
        for candidate, sequences in identity_sequences.items():
            if _matched_exact(candidate, parsed):
                matched = True
                for inventory, row in sequences:
                    add_identity_table(inventory, row, raw)
        if not matched:
            errors.append({"code": "NOT_FOUND", "selector": raw, "message": "exact object was absent from both complete owner inventories"})

    for raw, pattern in zip(patterns, parsed_patterns, strict=True):
        matched = False
        for candidate in entries:
            if _matched_pattern(candidate[0], pattern):
                selected.add(candidate)
                matched = True
        for candidate, sequences in identity_sequences.items():
            if _matched_pattern(candidate[0], pattern):
                matched = True
                for inventory, row in sequences:
                    add_identity_table(inventory, row, raw)
        if not matched:
            errors.append({"code": "NO_MATCH", "selector": raw, "message": "pattern matched no ordinary object in either complete owner inventory"})

    # A package specification is one logical selection and its body is an
    # included dependent when it exists on either environment.
    for name, object_type in tuple(selected):
        if object_type == "PACKAGE" and (
            _inventory_contains(source, name, "PACKAGE BODY") or _inventory_contains(target, name, "PACKAGE BODY")
        ):
            selected.add((name, "PACKAGE BODY"))

    supported = set(SUPPORTED_ROOT_TYPES)
    for name, object_type in sorted(selected):
        if object_type not in supported:
            errors.append({"code": "UNSUPPORTED_TYPE", "name": name, "type": object_type, "message": "matched object type is outside the supported comparison scope"})
    return Selection(tuple(sorted(selected)), tuple(errors), tuple(objects), tuple(patterns))


def _identity_summary(snapshot: SchemaSnapshot | None, environment: str | None = None) -> dict:
    if snapshot is None:
        return {"environment": environment, "identity": {}, "started_at": None, "completed_at": None, "coverage": {"ownerComplete": False}}
    return {
        "environment": environment,
        "identity": dict(snapshot.identity),
        "started_at": snapshot.started_at,
        "completed_at": snapshot.completed_at,
        "coverage": dict(snapshot.coverage),
    }


def _scope_missing(identity: Mapping) -> list[str]:
    return [field for field in SCOPE_IDENTITY_FIELDS if not str(identity.get(field, "")).strip()]


def _unknown(code: str, message: str, **details) -> dict:
    return {"kind": "UNKNOWN/UNSUPPORTED", "code": code, "message": message, **details}


def _normalized_object_key(normalized: Mapping) -> tuple[str, str, str]:
    name = str(normalized["name"])
    object_type = str(normalized["object_type"])
    if name in {GENERATED_CONSTRAINT, GENERATED_INDEX}:
        signature_value = {key: value for key, value in normalized.items() if key not in {"name", "dependents", "coverage", "format_version", "complete"}}
        encoded = json.dumps(signature_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        signature = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    else:
        signature = ""
    return name, object_type, signature


def _normalized_index(snapshot: SchemaSnapshot, side: str) -> tuple[dict[tuple[str, str, str], list[tuple[ObjectDefinition, dict]]], list[dict]]:
    index: dict[tuple[str, str, str], list[tuple[ObjectDefinition, dict]]] = {}
    errors: list[dict] = []
    owner = str(snapshot.identity.get("current_schema", ""))
    for definition in snapshot.objects.values():
        normalized = normalize_definition(definition, owner)
        if not normalized["complete"]:
            errors.append(_unknown("NORMALIZATION_INCOMPLETE", normalized.get("coverage", {}).get("reason", "definition normalization is incomplete"), side=side, name=definition.key.name, object_type=definition.key.object_type, raw_ddl=definition.raw_ddl))
            continue
        logical_key = _normalized_object_key(normalized)
        index.setdefault(logical_key, []).append((definition, normalized))
    return index, errors


def _comparable(normalized: Mapping) -> dict:
    # Dependents are compared as their own selected definitions; comparing the
    # reference list here would make safe generated-name relationships drift.
    return {key: value for key, value in normalized.items() if key != "dependents"}


def _difference(kind: str, name: str, object_type: str, **details) -> dict:
    return {"kind": kind, "name": name, "object_type": object_type, **details}


def compare_snapshots(source: SchemaSnapshot, target: SchemaSnapshot, selection: Selection) -> ComparisonReport:
    differences: list[dict] = []
    source_identity = dict(source.identity)
    target_identity = dict(target.identity)
    source_summary = _identity_summary(source)
    target_summary = _identity_summary(target)
    coverage = {
        "complete": True,
        "source": dict(source.coverage),
        "target": dict(target.coverage),
        "normalization": normalization_coverage(),
        "capture_windows": {
            "source": {"started_at": source.started_at, "completed_at": source.completed_at},
            "target": {"started_at": target.started_at, "completed_at": target.completed_at},
        },
    }

    for side, snapshot in (("source", source), ("target", target)):
        if snapshot.coverage.get("ownerComplete") is not True:
            differences.append(_unknown("INCOMPLETE_VISIBILITY", f"{side} owner inventory visibility is incomplete", side=side))
            coverage["complete"] = False
    source_missing = _scope_missing(source_identity)
    target_missing = _scope_missing(target_identity)
    if source_missing or target_missing:
        differences.append(_unknown("IDENTITY_INCOMPLETE", "database/container/schema/edition identity is insufficient to establish comparison scope", source_missing=source_missing, target_missing=target_missing))
        coverage["complete"] = False
    elif same_database_scope(source_identity, target_identity):
        self_error = {
            "code": "SELF_COMPARISON",
            "message": "source and target resolve to the same database/container/schema/edition",
            "source_service": source_identity.get("service_name"),
            "target_service": target_identity.get("service_name"),
        }
        differences.append(_unknown(self_error["code"], self_error["message"], source_service=self_error["source_service"], target_service=self_error["target_service"]))
        coverage["complete"] = False

    for error in selection.errors:
        differences.append(_unknown(error.get("code", "SELECTION_ERROR"), error.get("message", "selection could not be verified"), **{key: value for key, value in error.items() if key not in {"code", "message"}}))
        coverage["complete"] = False

    source_index, source_errors = _normalized_index(source, "source")
    target_index, target_errors = _normalized_index(target, "target")
    differences.extend(source_errors)
    differences.extend(target_errors)
    if source_errors or target_errors:
        coverage["complete"] = False

    # A selected root present in a complete inventory but absent from its
    # supposedly complete snapshot is an extraction failure, never a missing
    # object claim.
    for name, object_type in selection.keys:
        for side, snapshot in (("source", source), ("target", target)):
            owner = str(snapshot.identity.get("current_schema", ""))
            key = ObjectKey(owner, name, object_type)
            if key in snapshot.inventory and key not in snapshot.objects:
                differences.append(_unknown("MISSING_SELECTED_DEFINITION", "selected object exists in ALL_OBJECTS but its complete DDL was not returned", side=side, name=name, object_type=object_type))
                coverage["complete"] = False

    for key in sorted(set(source_index) | set(target_index)):
        source_values = source_index.get(key, [])
        target_values = target_index.get(key, [])
        name, object_type, _signature = key
        paired = min(len(source_values), len(target_values))
        for index in range(paired):
            source_definition, source_normalized = source_values[index]
            target_definition, target_normalized = target_values[index]
            if not source_definition.valid or not target_definition.valid:
                for side, definition in (("source", source_definition), ("target", target_definition)):
                    if not definition.valid:
                        differences.append(_difference("INVALID_OBJECT", definition.key.name, definition.key.object_type, side=side, owner=definition.key.owner, raw_ddl=definition.raw_ddl))
            if _comparable(source_normalized) != _comparable(target_normalized):
                differences.append(_difference(
                    "DIFFERENT_DEFINITION", name if not name.startswith("__GENERATED_") else source_definition.key.name,
                    object_type, source={"owner": source_definition.key.owner, "name": source_definition.key.name, "raw_ddl": source_definition.raw_ddl, "attributes": source_definition.attributes},
                    target={"owner": target_definition.key.owner, "name": target_definition.key.name, "raw_ddl": target_definition.raw_ddl, "attributes": target_definition.attributes},
                ))
        for definition, _normalized in source_values[paired:]:
            differences.append(_difference("MISSING_ON_TARGET", definition.key.name, definition.key.object_type, source={"owner": definition.key.owner, "raw_ddl": definition.raw_ddl}, target=None))
        for definition, _normalized in target_values[paired:]:
            differences.append(_difference("EXTRA_ON_TARGET", definition.key.name, definition.key.object_type, source=None, target={"owner": definition.key.owner, "raw_ddl": definition.raw_ddl}))

    if not coverage["complete"]:
        exit_code = 2
    elif differences:
        exit_code = 1
    else:
        exit_code = 0
    coverage["selected_types"] = sorted({object_type for _name, object_type in selection.keys})
    coverage["dependent_types"] = sorted({
        definition.key.object_type
        for snapshot in (source, target)
        for definition in snapshot.objects.values()
        if (definition.key.name, definition.key.object_type) not in selection.keys
    })
    selection_errors = list(selection.errors)
    if _scope_missing(source_identity) or _scope_missing(target_identity):
        selection_errors.append({"code": "IDENTITY_INCOMPLETE", "message": "database/container/schema/edition identity is insufficient to establish comparison scope"})
    elif same_database_scope(source_identity, target_identity):
        selection_errors.append({
            "code": "SELF_COMPARISON",
            "message": "source and target resolve to the same database/container/schema/edition",
            "source_service": source_identity.get("service_name"),
            "target_service": target_identity.get("service_name"),
        })

    return ComparisonReport(
        source_summary, target_summary,
        {"objects": list(selection.objects), "patterns": list(selection.patterns), "keys": [list(key) for key in selection.keys], "errors": selection_errors},
        coverage, tuple(differences), exit_code,
    )


def _report_dict(report: ComparisonReport) -> dict:
    return {
        "source": report.source,
        "target": report.target,
        "selection": report.selection,
        "coverage": report.coverage,
        "differences": list(report.differences),
        "exit_code": report.exit_code,
    }


def render_report(report: ComparisonReport, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(_report_dict(report), ensure_ascii=False, sort_keys=True, indent=2)
    if output_format != "text":
        raise ValueError("output format must be text or json")

    def side_line(label: str, side: Mapping) -> str:
        identity = side.get("identity", {})
        return (
            f"{label}: {side.get('environment') or 'configured target'} "
            f"schema={identity.get('current_schema', '?')} db_unique_name={identity.get('db_unique_name', '?')} "
            f"container={identity.get('container_name', '?')}/{identity.get('container_id', '?')} "
            f"edition={identity.get('edition', '?')} service={identity.get('service_name', '?')} "
            f"capture={side.get('started_at', '?')}..{side.get('completed_at', '?')}"
        )

    lines = [
        side_line("Source", report.source),
        side_line("Target", report.target),
        f"Normalization: {report.coverage.get('normalization', {}).get('version', 'unknown')}",
        "Exclusions: storage/segment/tablespace settings, object IDs, DDL timestamps, optimizer statistics, sequence runtime position, grants, and application data.",
        f"Coverage complete: {'yes' if report.coverage.get('complete') else 'no'}",
        f"Result: exit {report.exit_code}; {len(report.differences)} difference(s)",
    ]
    for difference in report.differences:
        kind = difference.get("kind", "UNKNOWN/UNSUPPORTED")
        if kind == "UNKNOWN/UNSUPPORTED":
            lines.append(f"{kind} [{difference.get('code', 'UNKNOWN')}]: {difference.get('message', '')}")
            continue
        lines.append(f"{kind}: {difference.get('object_type')} {difference.get('name')}")
        for side in ("source", "target"):
            observed = difference.get(side)
            if isinstance(observed, dict) and observed.get("raw_ddl"):
                lines.append(f"  {side} DDL: {observed['raw_ddl']}")
    return "\n".join(lines)


def _empty_snapshot(inventory: SchemaInventory) -> SchemaSnapshot:
    return SchemaSnapshot(inventory.identity, inventory.objects, {}, inventory.coverage, inventory.started_at, inventory.completed_at)


def _build_live_report(
    source_target: Target,
    target_target: Target,
    objects: Sequence[str],
    patterns: Sequence[str],
    run_dir: Path,
    *,
    capture_inventory_fn: Callable = capture_inventory,
    capture_snapshot_fn: Callable = capture_snapshot,
) -> ComparisonReport:
    source_inventory = None
    target_inventory = None
    capture_errors: list[dict] = []
    for side, selected_target in (("source", source_target), ("target", target_target)):
        try:
            observed = capture_inventory_fn(selected_target, run_dir)
        except (CatalogError, OSError, RuntimeError) as error:
            capture_errors.append({"code": "INVENTORY_UNAVAILABLE", "side": side, "message": str(error), "partial": getattr(error, "partial", None)})
            continue
        if side == "source":
            source_inventory = observed
        else:
            target_inventory = observed

    if source_inventory is None or target_inventory is None:
        errors = [{key: value for key, value in error.items() if key != "partial"} for error in capture_errors]
        report = ComparisonReport(
            {"environment": source_target.environment, "identity": {}, "started_at": None, "completed_at": None, "coverage": {"ownerComplete": False}},
            {"environment": target_target.environment, "identity": {}, "started_at": None, "completed_at": None, "coverage": {"ownerComplete": False}},
            {"objects": list(objects), "patterns": list(patterns), "keys": [], "errors": errors},
            {"complete": False, "normalization": normalization_coverage(), "capture_errors": errors},
            tuple(_unknown(error["code"], error["message"], side=error["side"]) for error in errors),
            2,
        )
        return report

    selection = select_objects(source_inventory, target_inventory, objects, patterns)
    source_snapshot = _empty_snapshot(source_inventory)
    target_snapshot = _empty_snapshot(target_inventory)
    if not same_database_scope(source_inventory.identity, target_inventory.identity):
        supported_keys = tuple(key for key in selection.keys if key[1] in set(SUPPORTED_ROOT_TYPES))
        if supported_keys:
            for side, selected_target, inventory, setter in (
                ("source", source_target, source_inventory, "source"),
                ("target", target_target, target_inventory, "target"),
            ):
                try:
                    snapshot = capture_snapshot_fn(selected_target, inventory, supported_keys, run_dir)
                except (CatalogError, OSError, RuntimeError) as error:
                    partial = getattr(error, "partial", None)
                    snapshot = partial if isinstance(partial, SchemaSnapshot) else _empty_snapshot(inventory)
                    error_row = {"code": "SNAPSHOT_UNAVAILABLE", "side": side, "message": str(error)}
                    selection = Selection(selection.keys, (*selection.errors, error_row), selection.objects, selection.patterns)
                if setter == "source":
                    source_snapshot = snapshot
                else:
                    target_snapshot = snapshot
    report = compare_snapshots(source_snapshot, target_snapshot, selection)
    report.source["environment"] = source_target.environment
    report.target["environment"] = target_target.environment
    return report


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description="Compare selected live schema objects read-only.")
    parser.add_argument("--from", action="append", dest="sources", choices=("dev", "staging", "prod"))
    parser.add_argument("--to", action="append", dest="targets", choices=("dev", "staging", "prod"))
    parser.add_argument("--env", action="append", dest="environments", choices=("dev", "staging", "prod"), help="short form for --to; source defaults to dev")
    parser.add_argument("--object", action="append", default=[], help="exact object name; repeat for more")
    parser.add_argument("--pattern", action="append", default=[], help="object-name pattern; '*' is a wildcard and '_' is literal")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run_dir: Path | None = None,
    capture_inventory_fn: Callable = capture_inventory,
    capture_snapshot_fn: Callable = capture_snapshot,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if len(args.sources or []) > 1:
            raise ValueError("--from may be specified only once")
        if len(args.targets or []) > 1:
            raise ValueError("--to may be specified only once")
        if len(args.environments or []) > 1:
            raise ValueError("--env may be specified only once")
        if args.targets and args.environments:
            raise ValueError("--env and --to cannot be used together")
        if not args.targets and not args.environments:
            raise ValueError("select a target with --to or --env")
        source_environment = (args.sources or ["dev"])[0]
        target_environment = (args.targets or args.environments)[0]
        if source_environment == target_environment:
            raise ValueError("source and target environment labels must differ")
        validate_selector_inputs(args.object, args.pattern)
    except SystemExit as error:
        return int(error.code or 0)
    except ValueError as error:
        print(f"compare-schema error: {error}", file=sys.stderr)
        return 2

    values = os.environ if environ is None else environ
    try:
        source_target = resolve_target(values, source_environment, "read")
        target_target = resolve_target(values, target_environment, "read")
    except TargetResolutionError as error:
        print(f"compare-schema error: {error}", file=sys.stderr)
        return 2

    if run_dir is None:
        scratch = ROOT / "scratch"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="compare-schema-", dir=scratch))
    else:
        run_dir = Path(run_dir)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    report = _build_live_report(
        source_target, target_target, args.object, args.pattern, run_dir,
        capture_inventory_fn=capture_inventory_fn,
        capture_snapshot_fn=capture_snapshot_fn,
    )
    print(render_report(report, args.format))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
