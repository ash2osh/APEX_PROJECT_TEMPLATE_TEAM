#!/usr/bin/env python3
"""Capture complete, selected-scope Oracle catalog observations via SQLcl."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence

from .db_targets import Target, looks_like_production_identity
from .sqlcl_session import SqlclResult, run_sqlcl


SCHEMA_VERSION = 1
SUPPORTED_ROOT_TYPES = {
    "TABLE", "VIEW", "SEQUENCE", "PACKAGE", "PACKAGE BODY", "PROCEDURE",
    "FUNCTION", "TRIGGER", "SYNONYM", "INDEX", "TYPE", "TYPE BODY",
}
DEPENDENT_TYPES = {"CONSTRAINT", "INDEX", "TRIGGER"}
REQUIRED_IDENTITY = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)
BASE_CATALOGS = {"ALL_OBJECTS"}
CAPTURE_CATALOGS = {
    "ALL_OBJECTS", "ALL_TABLES", "ALL_TAB_COLUMNS", "ALL_TAB_COLS", "ALL_VIEWS",
    "ALL_TAB_IDENTITY_COLS", "ALL_SEQUENCES", "ALL_CONSTRAINTS", "ALL_CONS_COLUMNS", "ALL_INDEXES",
    "ALL_IND_COLUMNS", "ALL_TRIGGERS",
}
FRAME_RE = re.compile(r"^CATALOG_PAYLOAD_(BEGIN|END):(inventory|snapshot)$")
VERIFIED_RE = re.compile(r"^CATALOG_VERIFIED:(inventory|snapshot)$")
HEX_SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)


@dataclass(frozen=True, order=True)
class ObjectKey:
    owner: str
    name: str
    object_type: str


@dataclass(frozen=True)
class ObjectDefinition:
    key: ObjectKey
    attributes: dict
    raw_ddl: str
    dependents: tuple[ObjectKey, ...]
    valid: bool


@dataclass(frozen=True)
class SchemaInventory:
    identity: dict
    objects: dict[ObjectKey, dict]
    coverage: dict
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class SchemaSnapshot:
    identity: dict
    inventory: dict[ObjectKey, dict]
    objects: dict[ObjectKey, ObjectDefinition]
    coverage: dict
    started_at: str
    completed_at: str


class CatalogError(RuntimeError):
    """Catalog capture is partial, unsupported, or internally inconsistent."""

    def __init__(self, message: str, partial: SchemaSnapshot | SchemaInventory | None = None) -> None:
        self.partial = partial
        super().__init__(message)


def same_database_scope(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    """Compare physical database/container/schema/edition; ignore service aliases."""
    required = ("db_unique_name", "container_id", "container_name", "current_schema", "edition")
    if any(not str(left.get(field, "")).strip() or not str(right.get(field, "")).strip() for field in required):
        return False
    return all(left[field] == right[field] for field in required)


def _target_identity(identity: object, target: Target) -> dict:
    if not isinstance(identity, dict):
        raise CatalogError("catalog payload has no identity object")
    missing = [field for field in REQUIRED_IDENTITY if field not in identity or identity[field] is None or not str(identity[field]).strip()]
    if missing:
        raise CatalogError("catalog identity is incomplete: " + ", ".join(missing))
    observed = {field: str(identity[field]) for field in REQUIRED_IDENTITY}
    if observed["session_user"].upper() != target.expected_user:
        raise CatalogError(f"selected target expected session user {target.expected_user}; observed {observed['session_user']}")
    if observed["current_schema"].upper() != target.schema:
        raise CatalogError(f"selected target expected current schema {target.schema}; observed {observed['current_schema']}")
    if target.environment != "prod" and looks_like_production_identity(observed["db_name"], observed["db_unique_name"], observed["service_name"]):
        raise CatalogError("observed database/service identity resembles production but the selected environment is not prod")
    return observed


def _object_rows(rows: object, owner: str) -> dict[ObjectKey, dict]:
    if not isinstance(rows, list):
        raise CatalogError("catalog inventory objects must be an array")
    objects: dict[ObjectKey, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogError("catalog inventory contains a malformed object row")
        row_owner = row.get("owner")
        name = row.get("name")
        object_type = row.get("type")
        if not all(isinstance(value, str) and value for value in (row_owner, name, object_type)):
            raise CatalogError("catalog inventory contains an incomplete object key")
        if row_owner.upper() != owner:
            raise CatalogError(f"catalog inventory escaped selected owner {owner}")
        key = ObjectKey(row_owner, name, object_type)
        if key in objects:
            raise CatalogError(f"catalog inventory repeats {row_owner}.{name} ({object_type})")
        objects[key] = dict(row)
    return objects


def _coverage(payload: Mapping, target: Target, *, snapshot: bool = False) -> dict:
    value = payload.get("coverage")
    if not isinstance(value, dict):
        raise CatalogError("catalog payload has no coverage evidence")
    if payload.get("complete") is not True or value.get("ownerComplete") is not True:
        raise CatalogError("catalog owner visibility is incomplete")
    catalogs = value.get("catalogs")
    if not isinstance(catalogs, list) or not BASE_CATALOGS.issubset(set(catalogs)):
        raise CatalogError("complete ALL_OBJECTS owner inventory was not established")
    path = value.get("path")
    if path == "OWNER_SESSION":
        identity = payload.get("identity")
        if not isinstance(identity, dict) or str(identity.get("session_user", "")).upper() != target.schema:
            raise CatalogError("owner-session visibility was claimed by a non-owner session")
    elif path == "METADATA_PRIVILEGE":
        privileges = value.get("privileges", [])
        accepted = {"SELECT ANY DICTIONARY", "SELECT_CATALOG_ROLE"}
        if value.get("metadataReadable") is not True or not accepted.intersection(privileges if isinstance(privileges, list) else []):
            raise CatalogError("enabled metadata-reader privileges were not validated")
    else:
        raise CatalogError("catalog visibility path is not a validated owner or metadata-reader session")
    if snapshot:
        unsupported = value.get("unsupported", [])
        if unsupported:
            raise CatalogError("catalog reports unsupported selected properties: " + ", ".join(map(str, unsupported)))
        if not CAPTURE_CATALOGS.issubset(set(catalogs)):
            raise CatalogError("selected snapshot omitted required catalog coverage")
    return dict(value)


def _payload_frames(output: str, phase: str) -> list[dict]:
    frames: list[dict] = []
    collecting = False
    pieces: list[str] = []
    verified = False
    for line in output.splitlines():
        marker = FRAME_RE.fullmatch(line.strip())
        if marker:
            operation, found_phase = marker.groups()
            if found_phase != phase:
                continue
            if operation == "BEGIN":
                if collecting:
                    raise CatalogError("catalog payload frames are nested")
                if frames and not verified:
                    raise CatalogError("a catalog payload frame is missing its verification sentinel")
                collecting = True
                verified = False
                pieces = []
                continue
            if not collecting:
                raise CatalogError("catalog payload end marker has no matching begin marker")
            try:
                payload = json.loads("".join(pieces))
            except (json.JSONDecodeError, UnicodeError) as error:
                raise CatalogError(f"catalog payload JSON is truncated or malformed: {error}") from error
            if not isinstance(payload, dict):
                raise CatalogError("catalog payload root must be an object")
            frames.append(payload)
            collecting = False
            verified = False
            continue
        if not collecting and VERIFIED_RE.fullmatch(line.strip()):
            if line.strip() == f"CATALOG_VERIFIED:{phase}" and frames:
                verified = True
            continue
        if collecting:
            pieces.append(line)
    if collecting:
        raise CatalogError("catalog payload is missing its end marker (truncated extraction)")
    if not frames:
        raise CatalogError(f"SQLcl output contains no framed {phase} payload")
    if not verified:
        raise CatalogError(f"catalog {phase} payload is missing its verification sentinel")
    for payload in frames:
        if payload.get("schemaVersion") != SCHEMA_VERSION or payload.get("phase") != phase:
            raise CatalogError("catalog payload schema version or phase is unsupported")
    return frames


def parse_inventory(output: str, target: Target) -> SchemaInventory:
    payloads = _payload_frames(output, "inventory")
    if len(payloads) != 1:
        raise CatalogError("inventory capture must contain exactly one complete payload")
    payload = payloads[0]
    identity = _target_identity(payload.get("identity"), target)
    objects: dict[ObjectKey, dict] = {}
    partial: SchemaInventory | None = None
    try:
        objects = _object_rows(payload.get("objects"), target.schema)
        coverage = _coverage(payload, target)
        started_at, completed_at = _capture_window(payload)
        partial = SchemaInventory(identity, objects, coverage, started_at, completed_at)
        return partial
    except CatalogError as error:
        if error.partial is None:
            error.partial = partial or SchemaInventory(identity, objects, dict(payload.get("coverage") or {}), str(payload.get("started_at", "")), str(payload.get("completed_at", "")))
        raise


def _inventory_signature(objects: Mapping[ObjectKey, Mapping]) -> tuple:
    return tuple(sorted(
        (
            key.owner, key.name, key.object_type, row.get("status"), row.get("last_ddl_time"),
            row.get("object_id"), row.get("data_object_id"), row.get("object_timestamp"), row.get("identity_sequence"),
        )
        for key, row in objects.items()
    ))


def _capture_window(payload: Mapping) -> tuple[str, str]:
    started = payload.get("started_at")
    completed = payload.get("completed_at")
    if not isinstance(started, str) or not isinstance(completed, str):
        raise CatalogError("catalog capture interval is missing")
    try:
        start_time = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError as error:
        raise CatalogError("catalog capture interval is not ISO-8601") from error
    if start_time.tzinfo is None or end_time.tzinfo is None or end_time < start_time:
        raise CatalogError("catalog capture interval is not an ordered timezone-aware window")
    return started, completed


def _definition(row: object, owner: str) -> ObjectDefinition:
    if not isinstance(row, dict):
        raise CatalogError("selected definition is malformed")
    row_owner, name, object_type = row.get("owner"), row.get("name"), row.get("type")
    raw_ddl = row.get("raw_ddl")
    attributes = row.get("attributes", {})
    dependents = row.get("dependents", [])
    if not all(isinstance(value, str) and value for value in (row_owner, name, object_type, raw_ddl)):
        raise CatalogError("selected definition is missing its key or full DDL")
    if row_owner.upper() != owner or not isinstance(attributes, dict) or not isinstance(dependents, list):
        raise CatalogError("selected definition has malformed owner, attributes, or dependents")
    dependent_keys = []
    for child in dependents:
        if not isinstance(child, dict) or not all(isinstance(child.get(field), str) and child[field] for field in ("owner", "name", "type")):
            raise CatalogError("selected definition has a malformed dependent reference")
        if child["owner"].upper() != owner:
            raise CatalogError("selected definition has a dependent outside the selected owner")
        dependent_keys.append(ObjectKey(child["owner"], child["name"], child["type"]))
    valid = row.get("valid")
    if not isinstance(valid, bool):
        raise CatalogError("selected definition omitted validity evidence")
    return ObjectDefinition(ObjectKey(row_owner, name, object_type), dict(attributes), raw_ddl, tuple(dependent_keys), valid)


def parse_snapshot(
    output: str,
    target: Target,
    keys: Sequence[tuple[str, str]] = (),
    inventory: SchemaInventory | None = None,
) -> SchemaSnapshot:
    payloads = _payload_frames(output, "snapshot")
    first = payloads[0]
    identity = _target_identity(first.get("identity"), target)
    coverage = _coverage(first, target, snapshot=True)
    baseline: dict[ObjectKey, dict] = {}
    if inventory is not None:
        if inventory.identity.get("session_user", "").upper() != target.expected_user or inventory.identity.get("current_schema", "").upper() != target.schema:
            raise CatalogError("discovery inventory belongs to a different configured target", inventory)
        if not same_database_scope(inventory.identity, identity):
            raise CatalogError("database, container, schema, or edition changed since owner inventory capture", inventory)
        if inventory.identity.get("database_version") != identity.get("database_version"):
            raise CatalogError("database version changed since owner inventory capture", inventory)
        baseline = inventory.objects

    snapshots: list[SchemaSnapshot] = []
    known_inventory = baseline
    definitions: dict[ObjectKey, ObjectDefinition] = {}
    all_rows_before: tuple | None = None
    all_rows_after: tuple | None = None
    started: str | None = None
    completed: str | None = None
    for payload in payloads:
        payload_started, payload_completed = _capture_window(payload)
        current_identity = _target_identity(payload.get("identity"), target)
        if not same_database_scope(identity, current_identity):
            raise CatalogError("database or schema identity changed during selected definition capture")
        if current_identity["session_user"] != identity["session_user"]:
            raise CatalogError("session user changed during selected definition capture")
        if current_identity["database_version"] != identity["database_version"]:
            raise CatalogError("database version changed during selected definition capture")
        _coverage(payload, target, snapshot=True)
        before = _object_rows(payload.get("before"), target.schema)
        after = _object_rows(payload.get("after"), target.schema)
        if _inventory_signature(before) != _inventory_signature(after):
            partial = SchemaSnapshot(identity, before, dict(definitions), coverage, str(started or ""), str(completed or ""))
            raise CatalogError("owner inventory changed while selected definitions were being retrieved", partial)
        if known_inventory and _inventory_signature(known_inventory) != _inventory_signature(before):
            partial = SchemaSnapshot(identity, before, dict(definitions), coverage, str(started or ""), str(completed or ""))
            raise CatalogError("owner inventory changed since selector discovery", partial)
        if all_rows_before is not None and (all_rows_before != _inventory_signature(before) or all_rows_after != _inventory_signature(after)):
            partial = SchemaSnapshot(identity, before, dict(definitions), coverage, str(started or ""), str(completed or ""))
            raise CatalogError("owner inventory changed between selected definition batches", partial)
        all_rows_before = _inventory_signature(before)
        all_rows_after = _inventory_signature(after)
        known_inventory = before
        if started is None:
            started = payload_started
        completed = payload_completed
        raw_definitions = payload.get("definitions")
        if not isinstance(raw_definitions, list):
            raise CatalogError("selected definition list is malformed", SchemaSnapshot(identity, before, dict(definitions), coverage, str(started or ""), str(completed or "")))
        for raw in raw_definitions:
            definition = _definition(raw, target.schema)
            if definition.key in definitions:
                if definitions[definition.key] != definition:
                    raise CatalogError("selected definition changed between extraction batches", SchemaSnapshot(identity, before, dict(definitions), coverage, str(started or ""), str(completed or "")))
            definitions[definition.key] = definition
        errors = payload.get("metadataErrors", [])
        if not isinstance(errors, list):
            raise CatalogError("metadata error list is malformed")
        if errors:
            partial = SchemaSnapshot(identity, before, dict(definitions), coverage, str(started or ""), str(completed or ""))
            detail = "; ".join(str(row.get("error", "metadata extraction failed")) for row in errors if isinstance(row, dict))
            raise CatalogError("selected metadata extraction failed: " + detail, partial)
        snapshots.append(SchemaSnapshot(identity, before, {}, coverage, payload_started, payload_completed))

    requested = {ObjectKey(target.schema, name, object_type) for name, object_type in keys}
    required = set()
    for key in requested:
        if key not in known_inventory:
            continue  # Absence from a complete owner inventory is an observed absence.
        if key.object_type not in SUPPORTED_ROOT_TYPES:
            raise CatalogError(f"selected object type is unsupported: {key.object_type}", SchemaSnapshot(identity, known_inventory, definitions, coverage, str(started or ""), str(completed or "")))
        required.add(key)
        if key.object_type in {"PACKAGE", "PACKAGE BODY", "TYPE", "TYPE BODY"}:
            sibling_type = {"PACKAGE": "PACKAGE BODY", "PACKAGE BODY": "PACKAGE", "TYPE": "TYPE BODY", "TYPE BODY": "TYPE"}[key.object_type]
            sibling = ObjectKey(key.owner, key.name, sibling_type)
            if sibling in known_inventory:
                required.add(sibling)
    for key in tuple(required):
        definition = definitions.get(key)
        if definition is None:
            raise CatalogError(f"selected object definition is missing for {key.owner}.{key.name} ({key.object_type})", SchemaSnapshot(identity, known_inventory, definitions, coverage, str(started or ""), str(completed or "")))
        if key.object_type == "TABLE":
            columns = definition.attributes.get("columns")
            if not isinstance(columns, list):
                raise CatalogError(f"selected table column metadata is incomplete for {key.owner}.{key.name}", SchemaSnapshot(identity, known_inventory, definitions, coverage, str(started or ""), str(completed or "")))
        required.update(definition.dependents)
    for key in tuple(required):
        if key.object_type in DEPENDENT_TYPES and key not in definitions:
            raise CatalogError(f"table dependent definition is missing for {key.owner}.{key.name} ({key.object_type})", SchemaSnapshot(identity, known_inventory, definitions, coverage, str(started or ""), str(completed or "")))
    for key in required:
        if key not in definitions:
            raise CatalogError(f"selected dependent definition is missing for {key.owner}.{key.name} ({key.object_type})", SchemaSnapshot(identity, known_inventory, definitions, coverage, str(started or ""), str(completed or "")))
    selected_definitions = {key: definitions[key] for key in required}
    return SchemaSnapshot(identity, known_inventory, selected_definitions, coverage, str(started or ""), str(completed or ""))


def _hex(value: str) -> str:
    return value.encode("utf-8").hex().upper()


def _catalog_driver(run_dir: Path, phase: str, target: Target, keys: Sequence[tuple[str, str]] = ()) -> Path:
    if HEX_SCHEMA_RE.fullmatch(target.schema) is None or HEX_SCHEMA_RE.fullmatch(target.expected_user) is None:
        raise CatalogError("catalog target owner and expected user must be uppercase Oracle identifiers")
    prepared = run_dir / "schema_catalog.sql"
    shutil.copyfile(Path(__file__).with_name("schema_catalog.sql"), prepared)
    os.chmod(prepared, 0o600)
    key_rows = [{"name": name, "type": object_type} for name, object_type in keys]
    encoded_keys = _hex(json.dumps(key_rows, ensure_ascii=False, separators=(",", ":")))
    wrapper = run_dir / "catalog-driver.sql"
    lines = [
        "SET ECHO OFF",
        "SET VERIFY OFF",
        "SET FEEDBACK OFF",
        f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};",
        "SET DEFINE ON",
    ]
    # Keep SQLcl's single substitution token small; multiple includes share
    # one SQLcl process and each carries a bounded, hex-only key batch.
    if len(encoded_keys) <= 16000:
        lines.append("SET DEFINE ON")
        lines.append(f"@@schema_catalog.sql {phase} {_hex(target.schema)} {_hex(target.expected_user)} {encoded_keys}")
    else:
        for start in range(0, len(keys), 40):
            chunk = [{"name": name, "type": object_type} for name, object_type in keys[start : start + 40]]
            hex_chunk = _hex(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")))
            if len(hex_chunk) > 16000:
                raise CatalogError("one selected object batch exceeds the SQLcl safe input limit")
            lines.append("SET DEFINE ON")
            lines.append(f"@@schema_catalog.sql {phase} {_hex(target.schema)} {_hex(target.expected_user)} {hex_chunk}")
    lines.extend(("SET DEFINE OFF", "EXIT SUCCESS ROLLBACK", ""))
    wrapper.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    os.chmod(wrapper, 0o600)
    return wrapper


Runner = Callable[[Target, Path, Path], SqlclResult]


def _private_run_dir(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="catalog-", dir=run_dir))


def capture_inventory(target: Target, run_dir: Path, *, _runner: Runner = run_sqlcl) -> SchemaInventory:
    private_dir = _private_run_dir(run_dir)
    driver = _catalog_driver(private_dir, "inventory", target)
    result = _runner(target, driver, private_dir)
    return parse_inventory(result.output, target)


def capture_snapshot(
    target: Target,
    inventory: SchemaInventory,
    keys: Sequence[tuple[str, str]],
    run_dir: Path,
    *,
    _runner: Runner = run_sqlcl,
) -> SchemaSnapshot:
    private_dir = _private_run_dir(run_dir)
    driver = _catalog_driver(private_dir, "snapshot", target, keys)
    result = _runner(target, driver, private_dir)
    return parse_snapshot(result.output, target, keys, inventory)
