#!/usr/bin/env python3
"""Select and compare live Oracle schema definitions without database writes."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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
    parse_framed_catalog_payloads,
    same_database_scope,
)
from .schema_normalization import (
    GENERATED_CONSTRAINT,
    GENERATED_INDEX,
    normalize_definition,
    normalization_coverage,
)
from .sqlcl_session import run_sqlcl, safe_rmtree


ROOT = Path(__file__).resolve().parents[1]
EXACT_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*\Z", re.ASCII)
TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*(?:\s+[A-Za-z0-9_$#]+)*\Z", re.ASCII)
PATTERN_CHARS_RE = re.compile(r"[A-Za-z0-9_$#*]+\Z", re.ASCII)
SCOPE_IDENTITY_FIELDS = ("db_unique_name", "container_id", "container_name", "current_schema", "edition")
COMPARE_ENV_SECTIONS = (
    "tables", "columns", "constraints", "indexes", "triggers", "sequences", "synonyms", "views",
    "stored-code", "invalid-objects", "identity-columns", "object-grants",
    "system-privileges", "roles", "network-aces", "ords", "java-mle",
    "installed-options", "versions",
)
COMPARE_ENV_DBA_SECTIONS = frozenset({
    "system-privileges", "roles", "network-aces", "ords", "installed-options",
})
COMPARE_ENV_PAGE_SIZE = 500
COMPARE_ENV_MAX_ROWS = 100_000
COMPARE_ENV_NOT_COMPARED = (
    "Reference rows, including ERP and camp data, are outside this structure comparison.",
    "Foreign-key definitions compare referenced table and column names; reference values must be compared by label, not id.",
    "Object grants are scoped by TABLES_PREFIXES and CODE_PREFIXES; '*' includes every object name.",
    "Sequence runtime position, identity values, table data, comments, storage placement, optimizer statistics, and exact source bytes are not compared.",
    "Column default expressions, function-based index expressions, and nested role grants are not compared.",
    "Stored source and view hashes ignore line 1; Oracle ORA_HASH collisions are possible.",
)
COMPARE_ENV_FIELDS = {
    "tables": ("temporary", "partitioned", "iot_type", "nested", "secondary", "compression", "logging"),
    "columns": ("data_type", "data_type_mod", "data_type_owner", "data_length", "data_precision", "data_scale", "char_used", "char_length", "nullable", "hidden", "virtual"),
    "constraints": ("constraint_type", "columns", "referenced_table", "referenced_columns", "condition", "condition_truncated", "status", "validated", "delete_rule", "deferrable", "deferred", "rely"),
    "indexes": ("table_name", "uniqueness", "columns", "index_type", "visibility", "status"),
    "triggers": ("table_name", "trigger_type", "triggering_event", "base_object_type", "status", "line_count", "source_hash"),
    "sequences": ("increment_by", "min_value", "max_value", "cycle_flag", "order_flag", "cache_size"),
    "synonyms": ("table_owner", "table_name", "db_link"),
    "views": ("status", "line_count", "source_hash"),
    "stored-code": ("status", "line_count", "source_hash"),
    "invalid-objects": ("status",),
    "identity-columns": ("generation_type", "identity_options"),
    "object-grants": ("grantable",),
    "system-privileges": ("admin_option",),
    "roles": ("admin_option", "default_role"),
    "network-aces": ("grant_type", "inverted_principal", "ace_order", "start_date", "end_date"),
    "ords": ("uri_prefix", "module_type", "uri_template", "method", "source_type", "source_hash", "line_count", "status", "enabled", "url_mapping_type", "url_mapping_pattern", "auto_rest_auth", "priority", "items_per_page", "mime_type", "format"),
    "java-mle": ("status", "language"),
    "installed-options": ("value", "status"),
    "versions": ("version",),
}


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


def _printable(value: str) -> str:
    """Value with control characters escaped, so a refusal naming it stays on one line."""
    return "".join(character if character.isprintable() else repr(character)[1:-1] for character in value)


def parse_exact_selector(selector: str) -> tuple[str, str | None]:
    """Parse NAME, \"Quoted Name\", or TYPE:NAME; unquoted names fold uppercase."""
    if not isinstance(selector, str) or not selector:
        raise ValueError("exact object selector cannot be empty")
    type_name = None
    name_part = selector
    if not selector.startswith('"') and ":" in selector:
        type_part, name_part = selector.split(":", 1)
        if not TYPE_RE.fullmatch(type_part.strip()):
            raise ValueError(f"invalid object type qualifier: {_printable(type_part)}")
        type_name = " ".join(type_part.strip().replace("_", " ").upper().split())
        if not type_name:
            raise ValueError("object type qualifier cannot be empty")
    if name_part.startswith('"'):
        name = _parse_quoted_identifier(name_part)
    elif EXACT_IDENTIFIER_RE.fullmatch(name_part):
        name = name_part.upper()
    else:
        raise ValueError(f"invalid exact object name: {_printable(name_part)}")
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
            # Storage/subobject rows belong to the parent table or index DDL.
            # Keep them in the inventory for drift checks, not comparison roots.
            if key.object_type == "LOB" or key.subobject_name:
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
    self_comparison = False
    if source_missing or target_missing:
        differences.append(_unknown("IDENTITY_INCOMPLETE", "database/container/schema/edition identity is insufficient to establish comparison scope", source_missing=source_missing, target_missing=target_missing))
        coverage["complete"] = False
    elif same_database_scope(source_identity, target_identity):
        self_comparison = True
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
    if not self_comparison:
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
    elif self_comparison:
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
        "Exclusions: storage/segment/tablespace settings, object IDs, DDL timestamps, optimizer statistics, sequence runtime position, table and column comments, grants, and application data.",
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


def parse_environment_catalog_output(
    output: str,
    *,
    sections: Sequence[str] | None = None,
    environment: str | None = None,
    schema: str | None = None,
    expected_user: str | None = None,
    dba_capture: bool = False,
) -> dict:
    """Parse one complete, paged compare-env payload from SQLcl output."""
    requested = _validate_compare_env_sections(sections)
    payloads = parse_framed_catalog_payloads(output, "compare-env")
    if len(payloads) != 1:
        raise CatalogError("compare-env capture must contain exactly one complete payload")
    payload = payloads[0]
    if payload.get("schemaVersion") != 2 or payload.get("phase") != "compare-env":
        raise CatalogError("compare-env catalog schema version or phase is unsupported")
    if environment is not None and payload.get("environment") != environment:
        raise CatalogError("compare-env capture returned a different environment label")
    if schema is not None and str(payload.get("schema", "")).upper() != schema.upper():
        raise CatalogError("compare-env capture returned a different selected schema")
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise CatalogError("compare-env capture has no database identity")
    required_identity = ("session_user", "current_schema", "db_name", "db_unique_name", "container_id", "container_name", "edition", "database_version")
    missing_identity = [field for field in required_identity if not str(identity.get(field, "")).strip()]
    if missing_identity:
        raise CatalogError("compare-env identity is incomplete: " + ", ".join(missing_identity))
    if schema is not None and str(identity.get("current_schema", "")).upper() != schema.upper():
        raise CatalogError("compare-env SQLcl current schema does not match the selected schema")
    if expected_user and not dba_capture and str(identity.get("session_user", "")).upper() != expected_user.upper():
        raise CatalogError("compare-env SQLcl session user does not match the selected target")

    coverage = payload.get("coverage")
    all_sections = payload.get("sections")
    if not isinstance(coverage, dict) or not isinstance(all_sections, dict):
        raise CatalogError("compare-env capture omitted its section coverage or rows")
    section_coverage = coverage.get("sections")
    if not isinstance(section_coverage, dict):
        raise CatalogError("compare-env capture omitted per-section page evidence")
    page_size = coverage.get("pageSize")
    max_rows = coverage.get("maxRows")
    if type(page_size) is not int or page_size != COMPARE_ENV_PAGE_SIZE:
        raise CatalogError("compare-env capture has an invalid page size")
    if type(max_rows) is not int or max_rows != COMPARE_ENV_MAX_ROWS:
        raise CatalogError("compare-env capture has an invalid row cap")

    unavailable = payload.get("unavailable", {})
    if not isinstance(unavailable, dict):
        raise CatalogError("compare-env unavailable-section evidence is malformed")
    rows_by_section = {}
    for section in requested:
        if section not in all_sections and section not in unavailable:
            raise CatalogError(f"compare-env capture omitted selected section {section}")
        rows = all_sections.get(section, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CatalogError(f"compare-env {section} rows are malformed")
        state = section_coverage.get(section)
        if not isinstance(state, dict):
            raise CatalogError(f"compare-env {section} has no pagination evidence")
        if state.get("capHit") is True:
            raise CatalogError(f"compare-env {section} catalog reached its {max_rows}-row cap")
        if state.get("complete") is True:
            pages = state.get("pages")
            if not isinstance(pages, list) or not pages:
                raise CatalogError(f"compare-env {section} has no page-completion evidence")
            if any(type(count) is not int or count < 0 or count > page_size for count in pages):
                raise CatalogError(f"compare-env {section} contains an invalid page size")
            if any(count != page_size for count in pages[:-1]):
                raise CatalogError(f"compare-env {section} has a gap or short page before the final page")
            if pages[-1] == page_size:
                raise CatalogError(f"compare-env {section} has no short final page to prove end of results")
            if sum(pages) != len(rows):
                raise CatalogError(f"compare-env {section} page counts do not match its rows")
            if len(rows) > max_rows:
                raise CatalogError(f"compare-env {section} exceeds its {max_rows}-row cap")
            if len(rows) == max_rows and pages[-1] != 0:
                raise CatalogError(f"compare-env {section} reached its {max_rows}-row cap without proving end of results")
        elif section not in unavailable:
            raise CatalogError(f"compare-env {section} capture is incomplete")
        rows_by_section[section] = rows

    parsed = dict(payload)
    parsed["sections"] = rows_by_section
    parsed["unavailable"] = unavailable
    return parsed


def _validate_compare_env_sections(sections: Sequence[str] | None) -> tuple[str, ...]:
    if sections is None or len(sections) == 0:
        return COMPARE_ENV_SECTIONS
    requested = tuple(sections)
    if len(set(requested)) != len(requested):
        raise ValueError("--section cannot be repeated with the same value")
    unknown = [section for section in requested if section not in COMPARE_ENV_SECTIONS]
    if unknown:
        raise ValueError("unsupported compare-env section: " + ", ".join(unknown))
    return requested


def _compare_env_key(section: str, row: Mapping, schema: str) -> tuple:
    if section == "tables":
        fields = ("name",)
    elif section == "columns":
        fields = ("table_name", "name")
    elif section == "constraints":
        fields = ("table_name", "name")
    elif section == "indexes":
        fields = ("name",)
    elif section in {"triggers", "sequences", "views"}:
        fields = ("name",)
    elif section == "synonyms":
        return ("PUBLIC" if str(row.get("owner", "")).upper() == "PUBLIC" else "SCHEMA", row.get("name"))
    elif section == "stored-code":
        fields = ("type", "name")
    elif section == "invalid-objects" or section == "java-mle":
        fields = ("type", "name")
    elif section == "identity-columns":
        fields = ("table_name", "column_name")
    elif section == "object-grants":
        fields = ("object_name", "grantee", "privilege")
    elif section == "system-privileges":
        fields = ("privilege",)
    elif section == "roles":
        fields = ("role",)
    elif section == "network-aces":
        principal = row.get("principal")
        if isinstance(principal, str) and principal.upper() == schema.upper():
            principal = "<CONFIGURED_SCHEMA>"
        return (
            row.get("host"), row.get("lower_port"), row.get("upper_port"), principal,
            row.get("principal_type"), row.get("privilege"), row.get("grant_type"),
            row.get("inverted_principal"), row.get("ace_order"),
        )
    elif section == "ords":
        kind = row.get("kind")
        if kind == "schema":
            return ("schema",)
        if kind == "module":
            return ("module", row.get("name"))
        if kind == "template":
            return ("template", row.get("module_name"), row.get("uri_template"))
        if kind == "handler":
            return ("handler", row.get("module_name"), row.get("uri_template"), row.get("method"))
        return (kind, row.get("name"), row.get("module_name"), row.get("uri_template"), row.get("method"))
    elif section == "installed-options":
        fields = ("name",)
    elif section == "versions":
        fields = ("component",)
    else:
        raise ValueError(f"no key definition for compare-env section {section}")
    return tuple(row.get(field) for field in fields)


def _compare_env_name(section: str, row: Mapping | None, key: tuple) -> str:
    if row is None:
        return ".".join(str(item) for item in key if item is not None)
    if section == "constraints" and len(key) >= 3 and isinstance(key[2], str) and " / " in key[2]:
        return key[2]
    if section == "columns":
        return f"{row.get('table_name', '?')}.{row.get('name', '?')}"
    if section == "identity-columns":
        return f"{row.get('table_name', '?')}.{row.get('column_name', '?')}"
    if section == "stored-code" or section in {"invalid-objects", "java-mle"}:
        return f"{row.get('type', '?')} {row.get('name', '?')}"
    if section == "object-grants":
        return f"{row.get('object_name', '?')} -> {row.get('grantee', '?')} ({row.get('privilege', '?')})"
    if section == "system-privileges":
        return str(row.get("privilege", "?"))
    if section == "roles":
        return str(row.get("role", "?"))
    if section == "network-aces":
        return f"{row.get('host', '?')} {row.get('principal', '?')} {row.get('privilege', '?')}"
    if section == "ords":
        return "/".join(str(item) for item in _compare_env_key(section, row, "") if item is not None)
    if section == "versions":
        return str(row.get("component", "?"))
    if section == "synonyms":
        owner = "PUBLIC" if str(row.get("owner", "")).upper() == "PUBLIC" else "SCHEMA"
        return f"{owner}.{row.get('name', '?')}"
    return str(row.get("name", "?"))


def _constraint_signature(row: Mapping) -> tuple:
    return (
        row.get("table_name"), row.get("constraint_type"), tuple(row.get("columns") or ()),
        row.get("referenced_table"), tuple(row.get("referenced_columns") or ()), row.get("condition"),
    )


def _generated_constraint(row: Mapping) -> bool:
    name = row.get("name")
    return isinstance(name, str) and re.fullmatch(r"SYS_C[0-9]+", name, re.IGNORECASE | re.ASCII) is not None


def _compare_env_records(section: str, source_rows: Sequence[Mapping], target_rows: Sequence[Mapping], source_schema: str, target_schema: str) -> tuple[list[dict], list[dict], list[dict]]:
    differences: list[dict] = []
    identical: list[dict] = []
    blockers: list[dict] = []
    fields = COMPARE_ENV_FIELDS[section]

    def add_pair(source: Mapping | None, target: Mapping | None, key: tuple) -> None:
        name = _compare_env_name(section, source or target, key)
        if source is None:
            classification = "only on target"
        elif target is None:
            classification = "missing on target"
        elif any(source.get(field) != target.get(field) for field in fields):
            classification = "different"
        else:
            classification = "identical"
        item = {
            "section": section,
            "classification": classification,
            "name": name,
            "key": list(key),
            "source": dict(source) if source is not None else None,
            "target": dict(target) if target is not None else None,
        }
        if classification == "different":
            item["fields"] = [field for field in fields if source.get(field) != target.get(field)]
        (identical if classification == "identical" else differences).append(item)

    if section == "constraints":
        source_by_name: dict[tuple, Mapping] = {}
        target_by_name: dict[tuple, Mapping] = {}
        for side, rows, index in (("source", source_rows, source_by_name), ("target", target_rows, target_by_name)):
            for row in rows:
                key = _compare_env_key(section, row, source_schema if side == "source" else target_schema)
                if key in index:
                    blockers.append({"section": section, "message": f"duplicate constraint key in {side}: {_compare_env_name(section, row, key)}"})
                else:
                    index[key] = row
        consumed_source: set[tuple] = set()
        consumed_target: set[tuple] = set()
        for key in sorted(set(source_by_name) & set(target_by_name), key=str):
            add_pair(source_by_name[key], target_by_name[key], key)
            consumed_source.add(key)
            consumed_target.add(key)
        source_generated: dict[tuple, list[tuple[tuple, Mapping]]] = {}
        target_generated: dict[tuple, list[tuple[tuple, Mapping]]] = {}
        for key, row in source_by_name.items():
            if key not in consumed_source and _generated_constraint(row):
                source_generated.setdefault(_constraint_signature(row), []).append((key, row))
        for key, row in target_by_name.items():
            if key not in consumed_target and _generated_constraint(row):
                target_generated.setdefault(_constraint_signature(row), []).append((key, row))
        for signature in sorted(set(source_generated) & set(target_generated), key=str):
            left = sorted(source_generated[signature], key=lambda item: str(item[0]))
            right = sorted(target_generated[signature], key=lambda item: str(item[0]))
            for (source_key, source_row), (target_key, target_row) in zip(left, right):
                display_key = (signature[0], signature[1], f"{source_row.get('name')} / {target_row.get('name')}")
                add_pair(source_row, target_row, display_key)
                consumed_source.add(source_key)
                consumed_target.add(target_key)
        for key, row in source_by_name.items():
            if key not in consumed_source:
                add_pair(row, None, key)
        for key, row in target_by_name.items():
            if key not in consumed_target:
                add_pair(None, row, key)
        return differences, identical, blockers

    source_index: dict[tuple, Mapping] = {}
    target_index: dict[tuple, Mapping] = {}
    for side, rows, index, selected_schema in (
        ("source", source_rows, source_index, source_schema),
        ("target", target_rows, target_index, target_schema),
    ):
        for row in rows:
            key = _compare_env_key(section, row, selected_schema)
            if key in index:
                blockers.append({"section": section, "message": f"duplicate catalog key in {side}: {_compare_env_name(section, row, key)}"})
            else:
                index[key] = row
    for key in sorted(set(source_index) | set(target_index), key=str):
        add_pair(source_index.get(key), target_index.get(key), key)
    return differences, identical, blockers


def compare_environment_catalogs(
    source: Mapping,
    target: Mapping,
    *,
    sections: Sequence[str] | None = None,
    dba_connections: Mapping[str, bool] | None = None,
    prefixes: Sequence[str] = (),
) -> dict:
    """Compare recorded or live catalog payloads by object keys and typed fields."""
    requested = _validate_compare_env_sections(sections)
    dba_available = {"source": True, "target": True}
    if dba_connections is not None:
        dba_available = {
            "source": bool(dba_connections.get(str(source.get("environment", "")), False)),
            "target": bool(dba_connections.get(str(target.get("environment", "")), False)),
        }
    differences: list[dict] = []
    identical: list[dict] = []
    blockers: list[dict] = []
    not_compared = list(COMPARE_ENV_NOT_COMPARED)
    unselected_sections = [section for section in COMPARE_ENV_SECTIONS if section not in requested]
    if unselected_sections:
        not_compared.insert(0, "Sections not selected: " + ", ".join(unselected_sections))
    section_status: dict[str, dict] = {}
    source_sections = source.get("sections", {})
    target_sections = target.get("sections", {})
    source_coverage = source.get("coverage", {}).get("sections", {})
    target_coverage = target.get("coverage", {}).get("sections", {})
    source_schema = str(source.get("schema", source.get("identity", {}).get("current_schema", "")))
    target_schema = str(target.get("schema", target.get("identity", {}).get("current_schema", "")))

    for side, payload in (("source", source), ("target", target)):
        capture_error = payload.get("capture_error")
        if capture_error:
            blockers.append({"section": "capture", "side": side, "message": str(capture_error)})

    for section in requested:
        if section in COMPARE_ENV_DBA_SECTIONS and not all(dba_available.values()):
            missing_sides = [side for side in ("source", "target") if not dba_available[side]]
            for missing_side in missing_sides:
                blockers.append({"section": section, "side": missing_side, "message": "not compared (no DBA connection)"})
            not_compared.append(f"{section}: not compared (no DBA connection for {', '.join(missing_sides)} environment{'s' if len(missing_sides) > 1 else ''})")
            section_status[section] = {"status": "not compared (no DBA connection)"}
            continue
        source_state = source_coverage.get(section)
        target_state = target_coverage.get(section)
        source_unavailable = source.get("unavailable", {}).get(section)
        target_unavailable = target.get("unavailable", {}).get(section)
        incomplete = []
        for side, state, unavailable in (("source", source_state, source_unavailable), ("target", target_state, target_unavailable)):
            if unavailable:
                incomplete.append({"section": section, "side": side, "message": str(unavailable)})
            elif not isinstance(state, Mapping) or state.get("complete") is not True:
                incomplete.append({"section": section, "side": side, "message": "catalog capture is incomplete"})
        if incomplete:
            blockers.extend(incomplete)
            section_status[section] = {"status": "incomplete"}
            continue
        left = list(source_sections.get(section, []))
        right = list(target_sections.get(section, []))
        if section == "object-grants" and prefixes and not any(prefix in {"*", "%"} for prefix in prefixes):
            normalized_prefixes = tuple(prefix.upper() for prefix in prefixes)
            left = [row for row in left if str(row.get("object_name", "")).upper().startswith(normalized_prefixes)]
            right = [row for row in right if str(row.get("object_name", "")).upper().startswith(normalized_prefixes)]
        found, same, invalid_keys = _compare_env_records(section, left, right, source_schema, target_schema)
        differences.extend(found)
        identical.extend(same)
        blockers.extend(invalid_keys)
        if section == "constraints":
            for side, rows in (("source", left), ("target", right)):
                for row in rows:
                    if row.get("condition_truncated") is True or str(row.get("condition_truncated", "")).upper() in {"Y", "YES", "TRUE"}:
                        blockers.append({
                            "section": section,
                            "side": side,
                            "name": str(row.get("name", "?")),
                            "message": "check condition may be truncated in the Oracle catalog view",
                        })
        if section == "invalid-objects":
            for side, rows in (("source", left), ("target", right)):
                for row in rows:
                    blockers.append({"section": section, "side": side, "name": str(row.get("name", "?")), "message": "invalid database object"})
        section_status[section] = {"status": "compared", "sourceRows": len(left), "targetRows": len(right)}

    blockers.sort(key=lambda item: (item.get("section", ""), item.get("side", ""), item.get("name", ""), item.get("message", "")))
    differences.sort(key=lambda item: (item["section"], item["name"], item["classification"]))
    identical.sort(key=lambda item: (item["section"], item["name"]))
    exit_code = 2 if blockers else 1 if differences else 0
    return {
        "schemaVersion": 1,
        "source": {"environment": source.get("environment"), "schema": source_schema, "identity": dict(source.get("identity", {}))},
        "target": {"environment": target.get("environment"), "schema": target_schema, "identity": dict(target.get("identity", {}))},
        "sections": section_status,
        "readiness": "blocked" if blockers else "differences" if differences else "ready",
        "blockers": blockers,
        "differences": differences,
        "identical": identical,
        "notCompared": not_compared,
        "exit_code": exit_code,
    }


def render_environment_report(report: Mapping, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if output_format not in {"text", "markdown"}:
        raise ValueError("compare-env format must be text, json, or markdown")
    markdown = output_format == "markdown"
    lines = [
        "# Environment readiness" if markdown else "Environment readiness",
        f"{report['source']['environment']} ({report['source']['schema']}) -> {report['target']['environment']} ({report['target']['schema']})",
        f"Readiness: {report['readiness']} (exit {report['exit_code']})",
    ]
    for title, key in (("Blockers", "blockers"), ("Differences", "differences"), ("Identical", "identical")):
        lines.extend(("", f"## {title}" if markdown else title))
        rows = report[key]
        if not rows:
            lines.append("- None" if markdown else "  None")
        for row in rows:
            if key == "blockers":
                message = row.get("message", "incomplete comparison")
                lines.append(f"- {row.get('section', 'capture')} [{row.get('side', 'both')}]: {message}" if markdown else f"  {row.get('section', 'capture')} [{row.get('side', 'both')}]: {message}")
            else:
                prefix = "- " if markdown else "  "
                changed_fields = row.get("fields", [])
                field_note = f" (fields: {', '.join(changed_fields)})" if changed_fields else ""
                lines.append(f"{prefix}{row['classification']}: {row['section']} {row['name']}{field_note}")
    lines.extend(("", "## Not compared" if markdown else "Not compared"))
    lines.extend(f"- {item}" if markdown else f"  - {item}" for item in report["notCompared"])
    return "\n".join(lines)


def _sql_identifier(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("cannot emit an empty SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_privilege(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9]*(?: [A-Z][A-Z0-9]*)*", value.upper(), re.ASCII) is None:
        raise ValueError("catalog object privilege is not a safe SQL privilege token")
    return value.upper()


def emit_dba_script(report: Mapping) -> str:
    """Return additive GRANT, ACE, and ORDS enable statements for source gaps."""
    target_schema = str(report["target"].get("schema", ""))
    statements = [
        "-- Review every statement and target identity before use.",
        "-- Generated additions only; this script does not revoke or drop privileges.",
    ]
    for difference in report.get("differences", []):
        row = difference.get("source") or {}
        section = difference.get("section")
        missing = difference.get("classification") == "missing on target"
        grant_upgrade = (
            section == "object-grants"
            and difference.get("classification") == "different"
            and str(row.get("grantable", "NO")).upper() == "YES"
            and str((difference.get("target") or {}).get("grantable", "NO")).upper() != "YES"
        )
        admin_upgrade = (
            section in {"system-privileges", "roles"}
            and difference.get("classification") == "different"
            and str(row.get("admin_option", "NO")).upper() == "YES"
            and str((difference.get("target") or {}).get("admin_option", "NO")).upper() != "YES"
        )
        ords_enable = section == "ords" and row.get("kind") == "schema" and row.get("enabled") is True
        if not missing and not grant_upgrade and not admin_upgrade and not ords_enable:
            continue
        if section == "object-grants" and (missing or grant_upgrade):
            owner = _sql_identifier(target_schema)
            object_name = _sql_identifier(str(row["object_name"]))
            grantee = _sql_identifier(str(row["grantee"]))
            grant = f"GRANT {_sql_privilege(row['privilege'])} ON {owner}.{object_name} TO {grantee}"
            if str(row.get("grantable", "NO")).upper() == "YES":
                grant += " WITH GRANT OPTION"
            statements.append(grant + ";")
        elif section == "system-privileges" and (missing or admin_upgrade):
            grant = f"GRANT {_sql_privilege(row['privilege'])} TO {_sql_identifier(target_schema)}"
            if str(row.get("admin_option", "NO")).upper() == "YES":
                grant += " WITH ADMIN OPTION"
            statements.append(grant + ";")
        elif section == "roles" and (missing or admin_upgrade):
            grant = f"GRANT {_sql_identifier(str(row['role']))} TO {_sql_identifier(target_schema)}"
            if str(row.get("admin_option", "NO")).upper() == "YES":
                grant += " WITH ADMIN OPTION"
            statements.append(grant + ";")
        elif section == "network-aces" and missing:
            if row.get("start_date") or row.get("end_date"):
                statements.append("-- Manual review required: time-bounded network ACE was not emitted.")
                continue
            principal = str(row.get("principal", ""))
            source_schema = str(report["source"].get("schema", ""))
            if principal.upper() == source_schema.upper():
                principal = target_schema
            principal_types = {"DATABASE": "XS_ACL.PTYPE_DB", "ROLE": "XS_ACL.PTYPE_ROLE"}
            principal_type = principal_types.get(str(row.get("principal_type", "DATABASE")).upper())
            if principal_type is None or str(row.get("grant_type", "GRANT")).upper() != "GRANT":
                statements.append("-- Manual review required: unsupported or deny network ACE was not emitted.")
                continue
            lower_port = row.get("lower_port")
            upper_port = row.get("upper_port")
            host = _sql_literal(str(row["host"]))
            port_parts = []
            for argument_name, port_value in (("lower_port", lower_port), ("upper_port", upper_port)):
                if port_value is not None:
                    if type(port_value) is not int:
                        raise ValueError("network ACE ports must be integer values")
                    port_parts.append(f"{argument_name} => {port_value}")
            port_arguments = "\n    " + ",\n    ".join(port_parts) + "," if port_parts else ""
            statements.extend((
                "BEGIN",
                "  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(",
                f"    host => {host},{port_arguments}",
                "    ace => XS$ACE_TYPE(",
                f"      privilege_list => XS$NAME_LIST({_sql_literal(str(row['privilege']))}),",
                f"      principal_name => {_sql_literal(principal)},",
                f"      principal_type => {principal_type}));",
                "END;",
                "/",
            ))
        elif ords_enable:
            statements.extend((
                "BEGIN",
                "  ORDS.ENABLE_SCHEMA(",
                "    p_enabled => TRUE,",
                f"    p_schema => {_sql_literal(target_schema)},",
                f"    p_url_mapping_type => {_sql_literal(str(row.get('url_mapping_type', 'BASE_PATH')))},",
                f"    p_url_mapping_pattern => {_sql_literal(str(row.get('url_mapping_pattern', target_schema.lower())))},",
                f"    p_auto_rest_auth => {'TRUE' if row.get('auto_rest_auth') is True else 'FALSE'});",
                "END;",
                "/",
            ))
    if len(statements) == 2:
        statements.append("-- No additive grant, ACE, or ORDS schema-enable statements were identified.")
    return "\n".join(statements) + "\n"


def _environment_hex(value: str) -> str:
    return value.encode("utf-8").hex().upper()


def _environment_driver(run_dir: Path, target: Target, environment: str, sections: Sequence[str], *, dba_capture: bool) -> Path:
    selected = _validate_compare_env_sections(sections)
    sql_source = Path(__file__).with_name("compare_env_catalog.sql")
    if not sql_source.is_file():
        raise CatalogError("compare-env SQL catalog driver is missing")
    shutil.copyfile(sql_source, run_dir / "compare_env_catalog.sql")
    os.chmod(run_dir / "compare_env_catalog.sql", 0o600)
    section_json = json.dumps(list(selected), separators=(",", ":"))
    driver = run_dir / "compare-env-driver.sql"
    content = "\n".join((
        f"-- COMPARE_ENVIRONMENT:{environment}",
        f"-- COMPARE_ENV_SCHEMA:{target.schema}",
        f"-- COMPARE_ENV_DBA:{'true' if dba_capture else 'false'}",
        "SET ECHO OFF",
        "SET VERIFY OFF",
        "SET FEEDBACK OFF",
        "SET DEFINE ON",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "ALTER SESSION DISABLE COMMIT IN PROCEDURE;",
        "SET TRANSACTION READ ONLY;",
        f"@@compare_env_catalog.sql {_environment_hex(target.schema)} {_environment_hex(target.expected_user if not dba_capture else '')} {_environment_hex(environment)} {'1' if dba_capture else '0'} {_environment_hex(section_json)}",
        "SET DEFINE OFF",
        "EXIT SUCCESS ROLLBACK",
        "",
    ))
    driver.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(driver, 0o600)
    return driver


def capture_environment_catalog(
    target: Target,
    environment: str,
    sections: Sequence[str],
    run_dir: Path,
    *,
    dba_capture: bool = False,
    _runner: Callable = run_sqlcl,
) -> dict:
    """Capture selected environment catalog sections through read-only SQLcl."""
    run_dir = Path(run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        run_dir.chmod(0o700)
    except OSError as error:
        raise CatalogError(f"cannot secure compare-env SQLcl directory: {error}") from error
    if run_dir.is_symlink():
        raise CatalogError("compare-env SQLcl directory cannot be a symlink")
    private_dir = Path(tempfile.mkdtemp(prefix=f"compare-env-{environment}-", dir=run_dir))
    try:
        driver = _environment_driver(private_dir, target, environment, sections, dba_capture=dba_capture)
        result = _runner(target, driver, private_dir, phase="inventory")
        return parse_environment_catalog_output(
            result.output,
            sections=sections,
            environment=environment,
            schema=target.schema,
            expected_user=target.expected_user,
            dba_capture=dba_capture,
        )
    except (CatalogError, OSError, RuntimeError):
        raise


def _empty_environment_capture(environment: str, schema: str, error: BaseException) -> dict:
    return {
        "environment": environment,
        "schema": schema,
        "identity": {},
        "coverage": {"sections": {}},
        "sections": {},
        "unavailable": {},
        "capture_error": str(error),
    }


def _merge_dba_capture(base: dict, privileged: dict) -> dict:
    if not same_database_scope(base.get("identity", {}), privileged.get("identity", {})):
        raise CatalogError("DBA compare-env connection did not reach the same database, container, schema, and edition as the environment connection")
    merged = dict(base)
    merged["sections"] = {**base.get("sections", {}), **privileged.get("sections", {})}
    merged["unavailable"] = {**base.get("unavailable", {}), **privileged.get("unavailable", {})}
    merged["coverage"] = dict(base.get("coverage", {}))
    merged["coverage"]["sections"] = {
        **base.get("coverage", {}).get("sections", {}),
        **privileged.get("coverage", {}).get("sections", {}),
    }
    merged["dbaIdentity"] = dict(privileged.get("identity", {}))
    if privileged.get("capture_error"):
        merged["capture_error"] = str(privileged["capture_error"])
    return merged


def _compare_env_parser() -> _ArgumentParser:
    parser = _ArgumentParser(description="Compare environment catalog structure by object name; the command is read-only.")
    parser.add_argument("--from", required=True, choices=("dev", "staging", "prod"), dest="source")
    parser.add_argument("--to", required=True, choices=("dev", "staging", "prod"), dest="target")
    parser.add_argument("--section", action="append", choices=COMPARE_ENV_SECTIONS, default=[], help="compare one section; repeat to select several (default: all)")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--emit-dba-script", metavar="FILE", help="write an additive DBA script for the captured grants, ACEs, and ORDS schema enablement")
    parser.add_argument("--schema", help="configured schema to compare; required when several are configured")
    return parser


def _write_dba_script(path_value: str, script: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("--emit-dba-script refuses a symbolic link")
    if path.exists() and not path.is_file():
        raise ValueError("--emit-dba-script must name a regular file")
    if not path.parent.is_dir():
        raise ValueError("--emit-dba-script parent directory must already exist")
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(script)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise
    return path


def main_compare_env(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run_dir: Path | None = None,
    capture_environment_fn: Callable = capture_environment_catalog,
) -> int:
    parser = _compare_env_parser()
    try:
        args = parser.parse_args(argv)
        if args.source == args.target:
            raise ValueError("source and target environment labels must differ")
        sections = _validate_compare_env_sections(args.section)
    except SystemExit as error:
        return int(error.code or 0)
    except ValueError as error:
        print(f"compare-env error: {error}", file=sys.stderr)
        return 2

    values = os.environ if environ is None else environ
    schema = args.schema or values.get("PROJECT_SCHEMA") or None
    try:
        source_target = resolve_target(values, args.source, "read", schema=schema)
        target_target = resolve_target(values, args.target, "read", schema=schema)
    except TargetResolutionError as error:
        print(f"compare-env error: {error}", file=sys.stderr)
        return 2

    own_run_dir = run_dir is None
    if own_run_dir:
        scratch = ROOT / "scratch"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="compare-env-", dir=scratch))
    else:
        run_dir = Path(run_dir)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    dba_sections = tuple(section for section in sections if section in COMPARE_ENV_DBA_SECTIONS)
    normal_sections = tuple(section for section in sections if section not in COMPARE_ENV_DBA_SECTIONS)
    if not normal_sections:
        # The unprivileged session supplies the database identity for a DBA-only selection.
        normal_sections = ("versions",)
    catalogs: dict[str, dict] = {}
    dba_available: dict[str, bool] = {}
    keep_logs = True
    try:
        for environment, target in ((args.source, source_target), (args.target, target_target)):
            try:
                catalog_value = capture_environment_fn(target, environment, normal_sections, run_dir)
            except (CatalogError, OSError, RuntimeError) as error:
                catalog_value = _empty_environment_capture(environment, target.schema, error)
            dba_key = f"{environment.upper()}_DBA_SQLCL_CONNECTION"
            dba_connection = values.get(dba_key, "").strip()
            dba_available[environment] = bool(dba_connection)
            if dba_sections and dba_connection:
                try:
                    privileged_target = replace(target, connection=dba_connection)
                    privileged = capture_environment_fn(
                        privileged_target, environment, dba_sections, run_dir, dba_capture=True,
                    )
                    catalog_value = _merge_dba_capture(catalog_value, privileged)
                except (CatalogError, OSError, RuntimeError) as error:
                    catalog_value["capture_error"] = f"DBA catalog capture failed: {error}"
            catalogs[environment] = catalog_value

        prefixes = []
        for key in ("TABLES_PREFIXES", "CODE_PREFIXES"):
            value = values.get(key, "")
            if value == "*":
                prefixes = ["*"]
                break
            prefixes.extend(item for item in value.split(",") if item)
        report = compare_environment_catalogs(
            catalogs[args.source], catalogs[args.target],
            sections=sections,
            dba_connections=dba_available,
            prefixes=tuple(dict.fromkeys(prefixes)),
        )
        if args.emit_dba_script:
            if report["exit_code"] == 2:
                print("compare-env error: cannot emit a DBA script from incomplete catalog evidence", file=sys.stderr)
                print(render_environment_report(report, args.format))
                return 2
            try:
                output_path = _write_dba_script(args.emit_dba_script, emit_dba_script(report))
            except (OSError, ValueError) as error:
                print(f"compare-env error: {error}", file=sys.stderr)
                print(render_environment_report(report, args.format))
                return 2
            report["dbaScript"] = str(output_path)
        print(render_environment_report(report, args.format))
        keep_logs = report["exit_code"] == 2
        return int(report["exit_code"])
    except KeyboardInterrupt:
        keep_logs = False
        print("compare-env interrupted; it only reads, so nothing was changed", file=sys.stderr)
        return 130
    except (CatalogError, OSError, RuntimeError, ValueError) as error:
        print(f"compare-env error: {error}", file=sys.stderr)
        return 2
    finally:
        if own_run_dir and not keep_logs:
            safe_rmtree(run_dir)


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
    parser.add_argument("--schema", help="configured schema to compare; required when several are configured")
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
    capture_environment_fn: Callable = capture_environment_catalog,
) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if actual_argv and actual_argv[0] == "compare-env":
        return main_compare_env(
            actual_argv[1:], environ=environ, run_dir=run_dir,
            capture_environment_fn=capture_environment_fn,
        )
    parser = _parser()
    try:
        args = parser.parse_args(actual_argv)
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
        source_target = resolve_target(values, source_environment, "read", schema=args.schema or values.get("PROJECT_SCHEMA") or None)
        target_target = resolve_target(values, target_environment, "read", schema=args.schema or values.get("PROJECT_SCHEMA") or None)
    except TargetResolutionError as error:
        print(f"compare-schema error: {error}", file=sys.stderr)
        return 2

    own_run_dir = run_dir is None
    if own_run_dir:
        scratch = ROOT / "scratch"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="compare-schema-", dir=scratch))
    else:
        run_dir = Path(run_dir)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # The SQLcl logs in run_dir are diagnostics: a comparison that finished (exit
    # 0 or 1) needs none, so they are kept only when it could not (exit 2).
    keep_logs = True
    try:
        report = _build_live_report(
            source_target, target_target, args.object, args.pattern, run_dir,
            capture_inventory_fn=capture_inventory_fn,
            capture_snapshot_fn=capture_snapshot_fn,
        )
        print(render_report(report, args.format))
        keep_logs = report.exit_code not in (0, 1)
        return report.exit_code
    except KeyboardInterrupt:
        keep_logs = False
        print("compare-schema interrupted; it only reads, so nothing was changed", file=sys.stderr)
        return 130
    finally:
        if own_run_dir and not keep_logs:
            safe_rmtree(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
