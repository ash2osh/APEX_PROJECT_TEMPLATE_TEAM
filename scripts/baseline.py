"""Read configured environment baselines and generate migration folders."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from .compare_schema import (
    BASELINE_INTERNAL_SECTIONS,
    COMPARE_ENV_FIELDS,
    capture_environment_catalog,
    compare_environment_catalogs,
)
from .db_targets import TargetResolutionError, resolve_target
from .migration_manifest import FOLDER_RE, MigrationManifestError, load_migration, validate_check_query, validate_sql_only
from .migration_revision import inspect_migration_lock
from .rollout import RolloutError, exclude_ords_modules
from .schema_catalog import (
    CatalogError,
    ObjectDefinition,
    ObjectKey,
    SchemaSnapshot,
    capture_inventory_snapshot_with_retries,
    same_database_scope,
)


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ("dev", "staging", "prod")
SOURCE_TYPES = frozenset({"PACKAGE", "PACKAGE BODY", "TRIGGER", "FUNCTION", "PROCEDURE", "TYPE", "TYPE BODY"})
SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
PREFIX_RE = re.compile(r"[A-Za-z0-9_$#]+\Z", re.ASCII)
DATA_PAGE_SIZE = 500
DATA_STEP_STATEMENT_LIMIT = 100
DATA_STEP_BYTE_LIMIT = 50_000
DATA_TEXT_CHUNK_CHARS = 32


class BaselineError(ValueError):
    """Configuration or baseline evidence is incomplete or unsafe."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts/team.sh baseline")
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("export-source", "export-grants", "export-data"):
        export = commands.add_parser(name, help=f"export configured {name.removeprefix('export-')} data")
        export.add_argument("--from", dest="source_environment", choices=ENVIRONMENTS, required=True)
        export.add_argument("--scratch", type=Path, help="scratch output directory (default: scratch/baseline/<env>)")

    build = commands.add_parser("build", help="build structure, grants, exact-source, and optional reference-data migrations")
    build.add_argument("--to", dest="target_environment", choices=ENVIRONMENTS, required=True)
    build.add_argument("--from", dest="source_environment", choices=ENVIRONMENTS, default="dev")
    build.add_argument("--schema", action="append", default=[], help="limit configured schemas; repeat for several")
    build.add_argument("--scratch", type=Path, help="scratch directory (default: scratch/baseline/build)")
    build.add_argument("--data", action="store_true", help="build reference-data migration from a prior export-data capture")
    build.add_argument("--data-dir", type=Path, help="export-data root (default: scratch/baseline/<from>/data)")

    filter_ords = commands.add_parser("filter-ords", help="remove complete named modules from an ORDS schema export")
    filter_ords.add_argument("--exclude-module", action="append", required=True, metavar="NAME")
    filter_ords.add_argument("--input", required=True, type=Path)
    filter_ords.add_argument("--output", required=True, type=Path)
    return parser


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BaselineError(f"baseline.json contains duplicate key {key!r}")
        result[key] = value
    return result


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except OSError as error:
        raise BaselineError(f"cannot read baseline configuration {path}: {error}") from error
    except UnicodeError as error:
        raise BaselineError("baseline.json must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise BaselineError(f"baseline.json is invalid JSON: {error}") from error
    if not isinstance(config, dict) or config.get("schemaVersion") != 1:
        raise BaselineError("baseline.json schemaVersion must be 1")
    schemas = config.get("schemas")
    if not isinstance(schemas, list) or not schemas or any(not isinstance(item, str) or SCHEMA_RE.fullmatch(item) is None for item in schemas):
        raise BaselineError("baseline.json schemas must be a non-empty array of uppercase Oracle schema names")
    if len(set(schemas)) != len(schemas):
        raise BaselineError("baseline.json schemas cannot contain duplicates")
    prefixes = config.get("prefixes", [])
    if not isinstance(prefixes, list) or any(not isinstance(item, str) or PREFIX_RE.fullmatch(item) is None for item in prefixes):
        raise BaselineError("baseline.json prefixes must contain non-empty object-name prefixes")
    excluded = config.get("excludedObjects", [])
    if not isinstance(excluded, list) or any(not isinstance(item, str) or not item for item in excluded):
        raise BaselineError("baseline.json excludedObjects must be an array of object names")
    reference = config.get("referenceData", {})
    if not isinstance(reference, dict) or not isinstance(reference.get("tables", []), list):
        raise BaselineError("baseline.json referenceData.tables must be an array")
    seen_reference_tables: set[str] = set()
    for table in reference.get("tables", []):
        if not isinstance(table, dict) or not isinstance(table.get("name"), str) or SCHEMA_RE.fullmatch(table["name"]) is None:
            raise BaselineError("baseline.json referenceData.tables entries need uppercase Oracle table names")
        name = table["name"]
        if name in seen_reference_tables:
            raise BaselineError(f"baseline.json referenceData.tables cannot repeat table {name}")
        seen_reference_tables.add(name)
        if any(field not in table for field in ("excludeColumns", "keyColumns", "labelColumns", "identity", "rowLimit")):
            raise BaselineError(f"reference table {name} must declare exclusions, key/label columns, identity handling, and rowLimit")
        for field in ("excludeColumns", "keyColumns", "labelColumns"):
            values = table.get(field)
            if not isinstance(values, list) or (field != "excludeColumns" and not values) or any(
                not isinstance(item, str) or SCHEMA_RE.fullmatch(item) is None for item in values
            ):
                raise BaselineError(f"baseline.json referenceData.tables.{field} must contain uppercase column names")
            if len(set(values)) != len(values):
                raise BaselineError(f"baseline.json referenceData.tables.{field} cannot contain duplicates")
        if not set(table["keyColumns"]).isdisjoint(table["excludeColumns"]):
            raise BaselineError(f"reference table {name} cannot exclude a natural-key column")
        if not set(table["labelColumns"]).isdisjoint(table["excludeColumns"]):
            raise BaselineError(f"reference table {name} cannot exclude a label column")
        row_limit = table.get("rowLimit")
        if type(row_limit) is not int or not 1 <= row_limit <= 100_000:
            raise BaselineError(f"reference table {name} rowLimit must be between 1 and 100000")
        identity = table.get("identity")
        if identity is not None:
            if not isinstance(identity, dict) or not isinstance(identity.get("column"), str) or SCHEMA_RE.fullmatch(identity["column"]) is None:
                raise BaselineError(f"reference table {name} identity must be null or name a column and generationType")
            if identity.get("generationType") not in {"ALWAYS", "BY DEFAULT", "BY DEFAULT ON NULL"}:
                raise BaselineError(f"reference table {name} identity generationType must be ALWAYS, BY DEFAULT, or BY DEFAULT ON NULL")
            if identity["column"] in table["excludeColumns"]:
                raise BaselineError(f"reference table {name} cannot exclude its identity column")
            if identity["column"] in table["keyColumns"]:
                raise BaselineError(f"reference table {name} natural keys must not use its identity column")
    grant_policy = config.get("grants", {})
    if not isinstance(grant_policy, dict):
        raise BaselineError("baseline.json grants must be an object")
    for key in ("skipGrantees", "includeGrantees"):
        values = grant_policy.get(key, [])
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
            raise BaselineError(f"baseline.json grants.{key} must be an array of grantees")
    if "keepGrantOptions" in grant_policy and type(grant_policy["keepGrantOptions"]) is not bool:
        raise BaselineError("baseline.json grants.keepGrantOptions must be true or false")
    mappings = config.get("sequenceMappings", [])
    if not isinstance(mappings, list):
        raise BaselineError("baseline.json sequenceMappings must be an array")
    seen_sequences: set[str] = set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or any(
            not isinstance(mapping.get(key), str) or SCHEMA_RE.fullmatch(mapping[key]) is None
            for key in ("sequence", "table", "column")
        ):
            raise BaselineError("baseline.json sequenceMappings entries need uppercase sequence, table, and column names")
        if mapping["sequence"] in seen_sequences:
            raise BaselineError("baseline.json sequenceMappings cannot repeat a sequence")
        seen_sequences.add(mapping["sequence"])
    return config


def _json_bytes(value: object) -> bytes:
    def encode(item: object, depth: int = 0) -> str:
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise BaselineError("reference-data JSON cannot contain a non-finite number")
            return format(item, "f")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("baseline JSON object keys must be strings")
            if not item:
                return "{}"
            indent = " " * (depth + 2)
            fields = [
                f"{indent}{json.dumps(key, ensure_ascii=False)}: {encode(item[key], depth + 2)}"
                for key in sorted(item)
            ]
            return "{\n" + ",\n".join(fields) + "\n" + " " * depth + "}"
        if isinstance(item, (list, tuple)):
            if not item:
                return "[]"
            indent = " " * (depth + 2)
            entries = [f"{indent}{encode(child, depth + 2)}" for child in item]
            return "[\n" + ",\n".join(entries) + "\n" + " " * depth + "]"
        return json.dumps(item, ensure_ascii=False, allow_nan=False)

    return (encode(value) + "\n").encode("utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent.exists()):
        raise BaselineError(f"refusing to write through a symbolic link: {path}")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise BaselineError(f"scratch output already contains an unfinished file: {temporary}")
    try:
        temporary.write_bytes(_json_bytes(value))
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _scratch_path(value: Path | None, default: Path) -> Path:
    selected = default if value is None else value
    if not selected.is_absolute():
        selected = ROOT / selected
    if selected.is_symlink():
        raise BaselineError("scratch directory cannot be a symbolic link")
    selected = selected.resolve()
    return selected


def _schema_filter(config: Mapping, schema: str, name: str) -> bool:
    if name.upper() in {item.upper() for item in config.get("excludedObjects", [])}:
        return False
    prefixes = config.get("prefixes", [])
    return not prefixes or any(name.upper().startswith(prefix.upper()) for prefix in prefixes)


def _selected_schemas(config: Mapping, values: Mapping[str, str], requested: Sequence[str] = ()) -> list[str]:
    selected = list(requested) or ([values["PROJECT_SCHEMA"]] if values.get("PROJECT_SCHEMA") else list(config["schemas"]))
    unknown = sorted(set(selected) - set(config["schemas"]))
    if unknown:
        raise BaselineError("selected schema is not listed in baseline.json: " + ", ".join(unknown))
    if len(set(selected)) != len(selected):
        raise BaselineError("schema selection cannot contain duplicates")
    return selected


def _capture(
    values: Mapping[str, str],
    environment: str,
    schema: str,
    sections: Sequence[str],
    scratch: Path,
    *,
    dba: bool = False,
    baseline_prefixes: Sequence[str] = (),
    baseline_excluded: Sequence[str] = (),
    baseline_data_tables: Sequence[Mapping] = (),
) -> dict:
    try:
        target = resolve_target(values, environment, "read", schema=schema)
        if dba:
            key = f"{environment.upper()}_DBA_SQLCL_CONNECTION"
            connection = values.get(key, "").strip()
            if not connection:
                raise BaselineError(f"set {key} to export system privileges")
            target = replace(target, connection=connection)
        return capture_environment_catalog(
            target, environment, sections, scratch,
            dba_capture=dba,
            baseline_capture=any(section in BASELINE_INTERNAL_SECTIONS for section in sections) or bool(baseline_prefixes or baseline_excluded),
            baseline_prefixes=baseline_prefixes,
            baseline_excluded=baseline_excluded,
            baseline_data_tables=baseline_data_tables,
        )
    except TargetResolutionError as error:
        raise BaselineError(str(error)) from error
    except CatalogError as error:
        raise BaselineError(str(error)) from error


def _complete(catalog: Mapping, sections: Sequence[str]) -> None:
    coverage = catalog.get("coverage", {}).get("sections", {})
    unavailable = catalog.get("unavailable", {})
    for section in sections:
        state = coverage.get(section)
        if not isinstance(state, dict) or state.get("complete") is not True or section in unavailable:
            raise BaselineError(f"{section} capture is unavailable or incomplete; no export was written")


def _source_lines(rows: Sequence[Mapping], name: str, object_type: str) -> list[str]:
    selected = sorted((row for row in rows if row.get("name") == name and row.get("type") == object_type), key=lambda row: row.get("line", -1))
    if not selected:
        raise BaselineError(f"stored source has no lines for {object_type} {name}")
    result: list[str] = []
    for expected, row in enumerate(selected, start=1):
        if row.get("line") != expected:
            raise BaselineError(f"stored source lines are incomplete for {object_type} {name}: expected line {expected}")
        value = row.get("text")
        if value is not None and not isinstance(value, str):
            raise BaselineError(f"stored source line {expected} for {object_type} {name} is malformed")
        if value is None:
            raise BaselineError(f"ALL_SOURCE returned NULL for {object_type} {name} line {expected}; exact stored text is unavailable")
        result.append(value)
    return result


def _encoded_name(value: str) -> str:
    return quote(value, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$#.-")


def _export_source(config: Mapping, values: Mapping[str, str], environment: str, scratch: Path, schemas: Sequence[str] | None = None) -> None:
    sections = ("baseline-source", "baseline-settings", "baseline-views")
    for schema in schemas or config["schemas"]:
        catalog = _capture(
            values, environment, schema, sections, scratch / f"sqlcl-{environment}-{schema}",
            baseline_prefixes=config.get("prefixes", []), baseline_excluded=config.get("excludedObjects", []),
        )
        _complete(catalog, sections)
        source_rows = catalog["sections"]["baseline-source"]
        settings_rows = catalog["sections"]["baseline-settings"]
        view_rows = catalog["sections"]["baseline-views"]
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in source_rows:
            name, object_type = row.get("name"), row.get("type")
            if not isinstance(name, str) or object_type not in SOURCE_TYPES:
                raise BaselineError("baseline-source returned a malformed or unsupported code object")
            if _schema_filter(config, schema, name):
                grouped[(object_type, name)].append(row)
        output = scratch / environment / schema
        source_output = output / "stored-source"
        for (object_type, name), rows in sorted(grouped.items()):
            _write_json(source_output / f"{_encoded_name(name)}__{object_type.replace(' ', '-')}.json", {
                "name": name, "type": object_type, "lines": _source_lines(rows, name, object_type),
            })
        selected_settings = []
        for row in settings_rows:
            name, object_type = row.get("name"), row.get("type")
            if isinstance(name, str) and _schema_filter(config, schema, name) and object_type in SOURCE_TYPES:
                selected_settings.append(dict(row))
        selected_settings.sort(key=lambda row: (str(row.get("type", "")), str(row.get("name", ""))))
        settings_keys = [(row.get("type"), row.get("name")) for row in selected_settings]
        if len(set(settings_keys)) != len(settings_keys) or set(settings_keys) != set(grouped):
            raise BaselineError(f"stored source and compiler settings do not describe the same units in {schema}")
        _write_json(source_output / "settings.json", selected_settings)
        views_output = source_output / "views"
        selected_views = []
        for row in view_rows:
            name, text, ddl = row.get("name"), row.get("text"), row.get("ddl")
            if not isinstance(name, str) or not isinstance(text, str) or not isinstance(ddl, str):
                raise BaselineError("baseline view source returned malformed text")
            if _schema_filter(config, schema, name):
                selected_views.append({"name": name, "text": text, "ddl": ddl, "textSource": "ALL_VIEWS.TEXT"})
        selected_views.sort(key=lambda row: row["name"])
        for row in selected_views:
            _write_json(views_output / f"{_encoded_name(row['name'])}.json", row)
        _write_json(output / "source-manifest.json", {
            "schemaVersion": 1, "environment": environment, "schema": schema,
            "codeUnits": len(grouped), "views": len(selected_views),
            "sourceRows": sum(len(rows) for rows in grouped.values()),
            "settingsRows": len(selected_settings), "complete": True,
        })


def _grant_rows(config: Mapping, rows: Sequence[Mapping], schema: str) -> list[dict]:
    policy = config.get("grants", {})
    skip = {item.upper() for item in policy.get("skipGrantees", [])}
    include = {item.upper() for item in policy.get("includeGrantees", [])}
    result = []
    for row in rows:
        object_name, grantee = row.get("object_name"), row.get("grantee")
        if not isinstance(object_name, str) or not isinstance(grantee, str):
            raise BaselineError("object-grants returned a malformed row")
        owner = row.get("owner")
        if owner is not None and (not isinstance(owner, str) or owner.upper() != schema.upper()):
            raise BaselineError("object-grants escaped the configured schema")
        if not _schema_filter(config, schema, object_name) or grantee.upper() in skip or (include and grantee.upper() not in include):
            continue
        selected = dict(row)
        if policy.get("keepGrantOptions", True) is False:
            selected["grantable"] = "NO"
        result.append(selected)
    return sorted(result, key=lambda row: tuple(str(row.get(key, "")) for key in ("object_name", "grantee", "privilege", "grantable")))


def _export_grants(config: Mapping, values: Mapping[str, str], environment: str, scratch: Path, schemas: Sequence[str] | None = None) -> None:
    dba_key = f"{environment.upper()}_DBA_SQLCL_CONNECTION"
    if not values.get(dba_key, "").strip():
        raise BaselineError(f"set {dba_key} to export system privileges")
    for schema in schemas or config["schemas"]:
        base = _capture(
            values, environment, schema, ("object-grants",), scratch / f"sqlcl-{environment}-{schema}",
            baseline_prefixes=config.get("prefixes", []), baseline_excluded=config.get("excludedObjects", []),
        )
        privileged = _capture(values, environment, schema, ("system-privileges",), scratch / f"sqlcl-dba-{environment}-{schema}", dba=True)
        if not same_database_scope(base.get("identity", {}), privileged.get("identity", {})):
            raise BaselineError("DBA connection did not reach the same database, container, schema, and edition")
        _complete(base, ("object-grants",))
        _complete(privileged, ("system-privileges",))
        output = scratch / environment / schema / "grants"
        _write_json(output / "object-grants.json", _grant_rows(config, base["sections"]["object-grants"], schema))
        system_rows = []
        for row in privileged["sections"]["system-privileges"]:
            grantee = row.get("grantee")
            if not isinstance(grantee, str):
                raise BaselineError("system-privileges returned a malformed grantee")
            if grantee.upper() != schema:
                continue
            if not isinstance(row.get("privilege"), str) or row.get("admin_option") not in {"YES", "NO"}:
                raise BaselineError("system-privileges returned a malformed row")
            selected = dict(row)
            if config.get("grants", {}).get("keepGrantOptions", True) is False:
                selected["admin_option"] = "NO"
            system_rows.append(selected)
        system_rows.sort(key=lambda row: (row["privilege"], row["admin_option"]))
        _write_json(output / "system-privileges.json", system_rows)
        _write_json(output / "manifest.json", {
            "schemaVersion": 1, "environment": environment, "schema": schema,
            "objectGrants": len(_grant_rows(config, base["sections"]["object-grants"], schema)),
            "systemPrivileges": len(system_rows), "complete": True,
        })


def _reference_tables(config: Mapping) -> list[dict]:
    return [dict(table) for table in config.get("referenceData", {}).get("tables", [])]


def _validate_reference_capture(table: Mapping, specification: Mapping, environment: str, schema: str) -> dict:
    if not isinstance(specification, Mapping) or not isinstance(specification.get("name"), str):
        raise BaselineError("baseline-data returned a table outside the configured allow-list")
    name = specification["name"]
    if table.get("name", table.get("table")) != name or table.get("complete") is not True:
        raise BaselineError(f"reference-data capture for {name} is missing or incomplete")
    columns = table.get("columns")
    rows = table.get("rows")
    pages = table.get("pages")
    row_count = table.get("rowCount")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list) or type(row_count) is not int:
        raise BaselineError(f"reference-data capture for {name} has malformed columns or rows")
    if row_count > specification["rowLimit"]:
        raise BaselineError(f"reference-data capture for {name} exceeded its rowLimit {specification['rowLimit']}")
    if row_count != len(rows) or any(not isinstance(row, dict) for row in rows):
        raise BaselineError(f"reference-data capture for {name} is truncated or has a row-count mismatch")
    if not isinstance(pages, list) or not pages or any(type(count) is not int or count < 0 or count > DATA_PAGE_SIZE for count in pages):
        raise BaselineError(f"reference-data capture for {name} has invalid page evidence")
    if any(count != DATA_PAGE_SIZE for count in pages[:-1]) or pages[-1] == DATA_PAGE_SIZE or sum(pages) != row_count:
        raise BaselineError(f"reference-data capture for {name} does not prove a complete final page")
    if row_count == 0 and pages != [0]:
        raise BaselineError(f"reference-data capture for empty table {name} has invalid page evidence")

    column_map: dict[str, dict] = {}
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("name"), str) or not isinstance(column.get("data_type"), str):
            raise BaselineError(f"reference-data capture for {name} has malformed column metadata")
        column_name = column["name"].upper()
        if column_name in column_map:
            raise BaselineError(f"reference-data capture for {name} repeats column {column_name}")
        column_map[column_name] = dict(column)
    excluded = {value.upper() for value in specification["excludeColumns"]}
    if excluded.intersection(column_map):
        raise BaselineError(f"reference-data capture for {name} contains an excluded column")
    required = set(specification["keyColumns"]) | set(specification["labelColumns"])
    identity = specification["identity"]
    if identity is not None:
        required.add(identity["column"])
    if not required.issubset(column_map):
        raise BaselineError(f"reference-data capture for {name} omits a configured key, label, or identity column")
    observed_identity = table.get("identity")
    if observed_identity != identity:
        raise BaselineError(f"reference-data identity handling for {name} does not match the captured table")
    column_names = set(column_map)
    for row_number, row in enumerate(rows, start=1):
        row_columns = {str(key).upper() for key in row}
        if excluded.intersection(row_columns):
            raise BaselineError(f"reference-data row {row_number} for {name} includes an excluded column")
        if row_columns != column_names:
            raise BaselineError(f"reference-data row {row_number} for {name} does not contain exactly the captured columns")
        if any(row.get(column) is None for column in specification["keyColumns"]):
            raise BaselineError(f"reference-data row {row_number} for {name} has a NULL natural key")
        if any(row.get(column) is None for column in specification["labelColumns"]):
            raise BaselineError(f"reference-data row {row_number} for {name} has a NULL matching label")

    return {
        "schemaVersion": 1,
        "environment": environment,
        "schema": schema,
        "table": name,
        "columns": [column_map[key] for key in sorted(column_map, key=lambda value: (column_map[value].get("position", 0), value))],
        "identity": identity,
        "rowCount": row_count,
        "pages": pages,
        "complete": True,
        "rows": rows,
    }


def _export_data(config: Mapping, values: Mapping[str, str], environment: str, scratch: Path, schemas: Sequence[str] | None = None) -> None:
    tables = _reference_tables(config)
    selected_schemas = schemas or config["schemas"]
    if not tables:
        print("No referenceData.tables allow-list is configured; wrote empty data manifests")
    for schema in selected_schemas:
        output = scratch / environment / "data" / schema
        if not tables:
            _write_json(output / "manifest.json", {
                "schemaVersion": 1, "environment": environment, "schema": schema,
                "tableCount": 0, "rowCount": 0, "complete": True,
            })
            continue
        catalog = _capture(
            values, environment, schema, ("baseline-data",), scratch / f"sqlcl-data-{environment}-{schema}",
            baseline_data_tables=tables,
        )
        _complete(catalog, ("baseline-data",))
        captured = catalog["sections"]["baseline-data"]
        by_name = {}
        for row in captured:
            name = row.get("name")
            if not isinstance(name, str) or name in by_name:
                raise BaselineError("baseline-data returned a malformed or duplicate table record")
            by_name[name] = row
        if set(by_name) != {table["name"] for table in tables}:
            raise BaselineError("baseline-data did not return every allow-listed reference table")
        total_rows = 0
        validated_tables: list[tuple[str, dict]] = []
        for specification in tables:
            data = _validate_reference_capture(by_name[specification["name"]], specification, environment, schema)
            encoded = _json_bytes(data)
            if len(encoded) > 128 * 1024 * 1024:
                raise BaselineError(f"reference-data export for {specification['name']} exceeded the 128 MiB file cap")
            validated_tables.append((specification["name"], data))
            total_rows += data["rowCount"]
        for name, data in validated_tables:
            _write_json(output / f"{name}.json", data)
        _write_json(output / "manifest.json", {
            "schemaVersion": 1, "environment": environment, "schema": schema,
            "tableCount": len(tables), "rowCount": total_rows, "complete": True,
        })


def _sql_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or any(character in value for character in "\r\n\x00"):
        raise BaselineError("catalog returned an unsafe empty identifier")
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_Q_DELIMITERS = (
    ("~", "~"), ("!", "!"), ("^", "^"), ("#", "#"), ("$", "$"), ("%", "%"),
    ("&", "&"), ("*", "*"), ("-", "-"), ("+", "+"), ("=", "="), ("?", "?"),
    (":", ":"), (";", ";"), ("@", "@"), ("_", "_"), (",", ","), (".", "."),
    ("/", "/"), ("[", "]"), ("{", "}"), ("(", ")"), ("<", ">"),
)
_PLSQL_SOURCE_CHUNK_CHARS = 32


def _q_literal(value: str) -> str:
    for opening, closing in _Q_DELIMITERS:
        if closing + "'" not in value:
            return f"q'{opening}{value}{closing}'"
    return _sql_literal(value)


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_ISO_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\Z",
    re.ASCII,
)


def _text_sql_literal(value: str) -> str:
    if value == "":
        return "NULL"
    pieces: list[str] = []
    text: list[str] = []

    def flush() -> None:
        line = "".join(text)
        text.clear()
        for offset in range(0, len(line), DATA_TEXT_CHUNK_CHARS):
            chunk = line[offset : offset + DATA_TEXT_CHUNK_CHARS]
            if chunk:
                pieces.append(_q_literal(chunk))

    index = 0
    while index < len(value):
        character = value[index]
        if character not in "\r\n":
            text.append(character)
            index += 1
            continue
        flush()
        if character == "\r":
            pieces.append("CHR(13)")
            if value[index + 1 : index + 2] == "\n":
                pieces.append("CHR(10)")
                index += 2
            else:
                index += 1
        else:
            pieces.append("CHR(10)")
            index += 1
    flush()
    return " || ".join(pieces) if pieces else "NULL"


def format_sql_value(value: object, column: Mapping) -> str:
    """Format one captured reference-data value as a strict Oracle SQL expression."""
    if value is None:
        return "NULL"
    data_type = str(column.get("data_type", "")).upper()
    if isinstance(value, bool):
        raise BaselineError(f"Oracle reference-data column {column.get('name', '?')} returned a JSON boolean")
    if data_type in {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "DECIMAL", "INTEGER"}:
        if not isinstance(value, (int, float, Decimal)):
            raise BaselineError(f"numeric reference-data column {column.get('name', '?')} returned non-numeric data")
        try:
            number = Decimal(str(value))
        except InvalidOperation as error:
            raise BaselineError(f"numeric reference-data column {column.get('name', '?')} returned an invalid number") from error
        if not number.is_finite():
            raise BaselineError(f"numeric reference-data column {column.get('name', '?')} returned a non-finite number")
        return format(number, "f")
    if data_type == "DATE" or data_type.startswith("TIMESTAMP"):
        if not isinstance(value, str):
            raise BaselineError(f"date reference-data column {column.get('name', '?')} returned non-text data")
        if _ISO_DATE_RE.fullmatch(value):
            try:
                dt.date.fromisoformat(value)
            except ValueError as error:
                raise BaselineError(f"invalid ISO date in reference-data column {column.get('name', '?')}: {value}") from error
            if data_type == "DATE":
                return f"TO_DATE({_sql_literal(value)}, 'YYYY-MM-DD')"
            timestamp_text = value + "T00:00:00"
            model = 'YYYY-MM-DD"T"HH24:MI:SS'
        elif _ISO_TIMESTAMP_RE.fullmatch(value):
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = dt.datetime.fromisoformat(normalized)
            except ValueError as error:
                raise BaselineError(f"invalid ISO timestamp in reference-data column {column.get('name', '?')}: {value}") from error
            has_zone = parsed.tzinfo is not None
            if data_type == "DATE":
                if has_zone or "." in value:
                    raise BaselineError(f"Oracle DATE column {column.get('name', '?')} cannot preserve timezone or fractional seconds")
                return f"TO_DATE({_sql_literal(value)}, 'YYYY-MM-DD\"T\"HH24:MI:SS')"
            if data_type.startswith("TIMESTAMP WITH TIME ZONE"):
                if not has_zone:
                    raise BaselineError(f"timestamp-with-time-zone column {column.get('name', '?')} has no timezone")
                timestamp_text = normalized
                model = 'YYYY-MM-DD"T"HH24:MI:SS' + (".FF" if "." in normalized else "") + "TZH:TZM"
                return f"TO_TIMESTAMP_TZ({_sql_literal(timestamp_text)}, {_sql_literal(model)})"
            if has_zone:
                raise BaselineError(f"timestamp column {column.get('name', '?')} cannot preserve a timezone")
            timestamp_text = value
            model = 'YYYY-MM-DD"T"HH24:MI:SS' + (".FF" if "." in value else "")
        else:
            raise BaselineError(f"date column {column.get('name', '?')} requires a full valid ISO date or timestamp")
        return f"TO_TIMESTAMP({_sql_literal(timestamp_text)}, {_sql_literal(model)})"
    if data_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "CLOB", "NCLOB", "LONG"}:
        if not isinstance(value, str):
            raise BaselineError(f"text reference-data column {column.get('name', '?')} returned non-text data")
        return _text_sql_literal(value)
    raise BaselineError(f"unsupported reference-data column type {data_type!r} for {column.get('name', '?')}")


def _check_sql_value(value: object, column: Mapping) -> str:
    """Format check literals without q-quotes, which the migration checker forbids."""
    data_type = str(column.get("data_type", "")).upper()
    if data_type == "DATE" or data_type.startswith("TIMESTAMP"):
        return format_sql_value(value, column)
    if not isinstance(value, str):
        return format_sql_value(value, column)
    if value == "":
        return "NULL"
    pieces: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char not in "\r\n":
            current.append(char)
            index += 1
            continue
        if current:
            pieces.append(_sql_literal("".join(current)))
            current = []
        if char == "\r" and value[index + 1 : index + 2] == "\n":
            pieces.append("CHR(10)")
            index += 2
        else:
            pieces.append("CHR(13)" if char == "\r" else "CHR(10)")
            index += 1
    if current:
        pieces.append(_sql_literal("".join(current)))
    return " || ".join(pieces) if pieces else "NULL"


def _data_tuple(value: object) -> tuple | object:
    if isinstance(value, list):
        return tuple(_data_tuple(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _data_tuple(item)) for key, item in value.items()))
    return value


def _reference_data_tables(catalog: Mapping, config_tables: Sequence[Mapping], environment: str, schema: str) -> dict[str, dict]:
    rows = catalog.get("sections", {}).get("baseline-data", [])
    result: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise BaselineError("baseline-data catalog row is malformed")
        name = row["name"]
        if name in result:
            raise BaselineError(f"baseline-data catalog contains duplicate table {name}")
        specification = next((item for item in config_tables if item["name"] == name), None)
        result[name] = _validate_reference_capture(row, specification, environment, schema)
    if set(result) != {table["name"] for table in config_tables}:
        raise BaselineError("baseline-data target capture did not include every configured reference table")
    return result


def _read_exported_reference_data(data_root: Path, config_tables: Sequence[Mapping], environment: str, schema: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for specification in config_tables:
        path = data_root / schema / f"{specification['name']}.json"
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_float=Decimal,
            )
        except OSError as error:
            raise BaselineError(f"reference-data export is missing for {specification['name']}: {path}") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BaselineError(f"reference-data export is invalid for {specification['name']}: {error}") from error
        if not isinstance(value, dict) or value.get("environment") != environment or value.get("schema") != schema or value.get("table") != specification["name"]:
            raise BaselineError(f"reference-data export identity does not match {environment}/{schema}/{specification['name']}")
        result[specification["name"]] = _validate_reference_capture(value, specification, environment, schema)
    return result


def _reference_data_bundle(
    config: Mapping,
    source_schema: str,
    target_schema: str,
    source_catalog: Mapping,
    target_catalog: Mapping,
    source_data: Mapping[str, Mapping],
    target_data: Mapping[str, Mapping],
) -> tuple[dict[str, str], dict, list[str]]:
    specifications = {table["name"]: table for table in _reference_tables(config)}
    column_maps = {
        name: {column["name"].upper(): column for column in data["columns"]}
        for name, data in source_data.items()
    }
    target_column_maps = {
        name: {column["name"].upper(): column for column in data["columns"]}
        for name, data in target_data.items()
    }
    for name, columns in column_maps.items():
        if name not in target_column_maps or not set(columns).issubset(target_column_maps[name]):
            raise BaselineError(f"target table {name} is missing one or more exported reference-data columns")

    relationships: list[dict] = []
    for constraint in source_catalog.get("sections", {}).get("constraints", []):
        if constraint.get("constraint_type") != "R":
            continue
        child = constraint.get("table_name")
        reference = constraint.get("referenced_table")
        if not isinstance(child, str) or child not in specifications or not isinstance(reference, str):
            continue
        prefix = "<CONFIGURED_SCHEMA>."
        if not reference.startswith(prefix):
            continue
        parent = reference[len(prefix):]
        if parent not in specifications:
            continue
        child_columns = constraint.get("columns")
        parent_columns = constraint.get("referenced_columns")
        if not isinstance(child_columns, list) or not isinstance(parent_columns, list) or not child_columns or len(child_columns) != len(parent_columns):
            raise BaselineError(f"reference foreign key {constraint.get('name', '?')} has malformed column mapping")
        if any(not isinstance(item, str) for item in child_columns + parent_columns):
            raise BaselineError(f"reference foreign key {constraint.get('name', '?')} has non-text columns")
        relationships.append({
            "name": str(constraint.get("name", "foreign-key")),
            "child": child,
            "parent": parent,
            "childColumns": [value.upper() for value in child_columns],
            "parentColumns": [value.upper() for value in parent_columns],
        })

    for relationship in relationships:
        child = relationship["child"]
        parent = relationship["parent"]
        missing = [
            f"{table}.{column}"
            for table, columns in (
                (child, relationship["childColumns"]),
                (parent, relationship["parentColumns"]),
            )
            for column in columns
            if column not in column_maps[table] or column not in target_column_maps[table]
        ]
        if missing:
            raise BaselineError(
                f"reference foreign key {relationship['name']} cannot be label-mapped because "
                f"an exported key column is missing or excluded: {', '.join(missing)}"
            )

    label_indexes: dict[tuple[str, str], dict[tuple, list[dict]]] = {}
    for side, data_by_table in (("source", source_data), ("target", target_data)):
        for table_name, data in data_by_table.items():
            specification = specifications[table_name]
            label_index: dict[tuple, list[dict]] = defaultdict(list)
            for row in data["rows"]:
                label = tuple(_data_tuple(row[column]) for column in specification["labelColumns"])
                label_index[label].append(row)
            label_indexes[(side, table_name)] = label_index
            if not set(specification["keyColumns"]).issubset(column_maps[table_name] if side == "source" else target_column_maps[table_name]):
                raise BaselineError(f"reference-data natural key for {table_name} is not present in {side} captured columns")

    # A label is the cross-environment identity of an allow-listed parent row.
    source_fk_labels: dict[tuple[str, int, str], dict[str, object]] = {}
    target_fk_labels: dict[tuple[str, int, str], dict[str, object]] = {}
    source_sql_expressions: dict[tuple[str, int, str], dict[str, str]] = {}
    target_relationship_labels: dict[tuple[str, int, str], dict[str, object]] = {}
    required_target_labels: dict[tuple[str, tuple], tuple[str, tuple]] = {}

    for relationship in relationships:
        child = relationship["child"]
        parent = relationship["parent"]
        parent_spec = specifications[parent]
        for side, data_by_table, fk_map, expression_map in (
            ("source", source_data, source_fk_labels, source_sql_expressions),
            ("target", target_data, target_fk_labels, None),
        ):
            parent_rows = data_by_table[parent]["rows"]
            parent_key_index: dict[tuple, list[dict]] = defaultdict(list)
            for parent_row in parent_rows:
                parent_key = tuple(_data_tuple(parent_row[column]) for column in relationship["parentColumns"])
                parent_key_index[parent_key].append(parent_row)
            for row_index, child_row in enumerate(data_by_table[child]["rows"]):
                foreign_values = tuple(_data_tuple(child_row[column]) for column in relationship["childColumns"])
                if any(value is None for value in foreign_values):
                    if all(value is None for value in foreign_values):
                        continue
                    raise BaselineError(
                        f"foreign key {relationship['name']} in {side} {child} is partially NULL; "
                        "its parent cannot be resolved safely by label"
                    )
                parents = parent_key_index.get(foreign_values, [])
                if len(parents) != 1:
                    raise BaselineError(
                        f"foreign key {relationship['name']} in {side} {child} cannot resolve its {parent} parent id "
                        f"{foreign_values!r} to exactly one row"
                    )
                parent_row = parents[0]
                label = tuple(_data_tuple(parent_row[column]) for column in parent_spec["labelColumns"])
                if any(value is None for value in label):
                    raise BaselineError(f"foreign key {relationship['name']} has a NULL {parent} label")
                if side == "source" and len(label_indexes[("source", parent)].get(label, [])) != 1:
                    raise BaselineError(f"foreign key {relationship['name']} source parent label {label!r} is duplicated in {parent}")
                target_label_matches = label_indexes[("target", parent)].get(label, [])
                if side == "source" and len(target_label_matches) != 1:
                    reason = "missing" if not target_label_matches else "ambiguous or duplicated"
                    raise BaselineError(
                        f"foreign key {relationship['name']} parent label {label!r} is {reason} on target table {parent}"
                    )
                if side == "source":
                    required_target_labels[(parent, label)] = (parent, label)
                for position, child_column in enumerate(relationship["childColumns"]):
                    if side == "source":
                        source_fk_labels[(child, row_index, child_column)] = {
                            "parent": parent, "label": label, "position": position,
                        }
                        where = " AND ".join(
                            f"P.{_sql_identifier(label_column)} = {format_sql_value(parent_row[label_column], column_maps[parent][label_column])}"
                            for label_column in parent_spec["labelColumns"]
                        )
                        check_where = " AND ".join(
                            f"P.{_sql_identifier(label_column)} = {_check_sql_value(parent_row[label_column], target_column_maps[parent][label_column])}"
                            for label_column in parent_spec["labelColumns"]
                        )
                        source_sql_expressions[(child, row_index, child_column)] = {
                            "sql": f"(SELECT P.{_sql_identifier(relationship['parentColumns'][position])} FROM {_sql_identifier(target_schema)}.{_sql_identifier(parent)} P WHERE {where})",
                            "checkSql": f"(SELECT P.{_sql_identifier(relationship['parentColumns'][position])} FROM {_sql_identifier(target_schema)}.{_sql_identifier(parent)} P WHERE {check_where})",
                        }
                    else:
                        target_fk_labels[(child, row_index, child_column)] = {
                            "parent": parent, "label": label, "position": position,
                        }
                        target_relationship_labels[(child, row_index, child_column)] = {
                            "parent": parent, "label": label,
                        }

    def canonical_row(side: str, table_name: str, row_index: int, row: Mapping) -> dict[str, object]:
        mapping = source_fk_labels if side == "source" else target_fk_labels
        values = {str(column).upper(): _data_tuple(value) for column, value in row.items()}
        for column in values:
            ref = mapping.get((table_name, row_index, column))
            if ref is not None:
                values[column] = ("LABEL", ref["parent"], ref["label"], ref["position"])
        return values

    source_canonical: dict[str, list[dict[str, object]]] = {}
    target_canonical: dict[str, list[dict[str, object]]] = {}
    for side, data_by_table, output in (
        ("source", source_data, source_canonical),
        ("target", target_data, target_canonical),
    ):
        for table_name, data in data_by_table.items():
            values = [canonical_row(side, table_name, index, row) for index, row in enumerate(data["rows"])]
            output[table_name] = values

    statements: list[str] = []
    preconditions: list[dict] = []
    postconditions: list[dict] = []
    differences: list[str] = []
    seen_parent_checks: set[tuple[str, tuple]] = set()
    for parent, label in sorted(required_target_labels, key=lambda item: (item[0], repr(item[1]))):
        if (parent, label) in seen_parent_checks:
            continue
        seen_parent_checks.add((parent, label))
        label_spec = specifications[parent]
        predicates = [
            f"P.{_sql_identifier(column)} = {_check_sql_value(value, target_column_maps[parent][column])}"
            for column, value in zip(label_spec["labelColumns"], label, strict=True)
        ]
        query = f"SELECT COUNT(*) FROM {_sql_identifier(target_schema)}.{_sql_identifier(parent)} P WHERE " + " AND ".join(predicates)
        preconditions.append(_check(f"reference-parent-label-{_safe_check_slug(parent + repr(label))}", query))

    for table_name in sorted(specifications):
        specification = specifications[table_name]
        source_rows = source_data[table_name]["rows"]
        target_rows = target_data[table_name]["rows"]
        source_values = source_canonical[table_name]
        target_values = target_canonical[table_name]
        source_key_counts: dict[tuple, int] = defaultdict(int)
        for values in source_values:
            source_key_counts[tuple(values[column] for column in specification["keyColumns"])] += 1
        target_by_key: dict[tuple, list[int]] = defaultdict(list)
        for row_index, values in enumerate(target_values):
            target_key = tuple(values[column] for column in specification["keyColumns"])
            target_by_key[target_key].append(row_index)
        for row_index, source_row in enumerate(source_rows):
            canonical = source_values[row_index]
            key = tuple(canonical[column] for column in specification["keyColumns"])
            if source_key_counts[key] != 1:
                raise BaselineError(f"reference-data natural key is duplicated in source table {table_name}: {key!r}")
            existing_indices = target_by_key.get(key, [])
            if len(existing_indices) > 1:
                raise BaselineError(f"reference-data natural key is ambiguous in target table {table_name}: {key!r}")
            source_columns = column_maps[table_name]
            identity = specification["identity"]
            if identity and identity["generationType"] == "ALWAYS" and identity["column"] in specification["keyColumns"]:
                raise BaselineError(f"ALWAYS identity column {identity['column']} cannot be a natural key for {table_name}")

            target_columns = target_column_maps[table_name]
            label_key = tuple(_data_tuple(source_row[column]) for column in specification["labelColumns"])
            label_matches = label_indexes[("target", table_name)].get(label_key, [])
            if len(label_matches) > 1:
                raise BaselineError(f"reference-data label {label_key!r} is ambiguous or duplicated in target table {table_name}")
            def expression_for(column: str, *, for_check: bool = False) -> str:
                relationship = source_sql_expressions.get((table_name, row_index, column))
                if relationship is not None:
                    return relationship["checkSql"] if for_check else relationship["sql"]
                return _check_sql_value(source_row[column], target_columns[column]) if for_check else format_sql_value(source_row[column], source_columns[column])

            key_predicates = [
                f"T.{_sql_identifier(column)} = {expression_for(column)}"
                for column in specification["keyColumns"]
            ]
            check_key_predicates = [
                f"T.{_sql_identifier(column)} = {expression_for(column, for_check=True)}"
                for column in specification["keyColumns"]
            ]
            target_count = len(existing_indices)
            row_slug = _safe_check_slug(table_name + repr(key))
            before_check = _reference_count_check(
                f"reference-key-before-{row_slug}",
                f"SELECT COUNT(*) FROM {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} T WHERE " + " AND ".join(check_key_predicates),
                target_count,
            )
            preconditions.append(before_check)
            postconditions.append(_check(
                f"reference-key-after-{row_slug}",
                f"SELECT COUNT(*) FROM {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} T WHERE " + " AND ".join(check_key_predicates),
            ))
            # Baseline preconditions describe the live observation used to generate this migration.
            postconditions[-1]["expected"] = 1

            if label_matches and not existing_indices:
                label_predicates = [
                    f"T.{_sql_identifier(column)} = {_check_sql_value(value, target_columns[column])}"
                    for column, value in zip(specification["labelColumns"], label_key, strict=True)
                ]
                differences.append(
                    f"{table_name} label {label_key!r}: target natural key differs; the existing row is preserved and the label conflict is a migration precondition"
                )
                label_check = _reference_count_check(
                    f"reference-label-conflict-{row_slug}",
                    f"SELECT COUNT(*) FROM {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} T WHERE " + " AND ".join(label_predicates),
                    0,
                )
                preconditions.append(label_check)

            if existing_indices:
                target_index = existing_indices[0]
                target_row = target_rows[target_index]
                target_canonical_values = target_values[target_index]
                ignored = set(specification["keyColumns"])
                if identity is not None:
                    ignored.add(identity["column"])
                differing = [
                    column for column in source_columns
                    if column not in ignored and column in target_row
                    and canonical.get(column) != target_canonical_values.get(column)
                ]
                if differing:
                    key_text = repr(tuple(source_row[column] for column in specification["keyColumns"]))
                    message = f"{table_name} natural key {key_text}: target columns differ: {', '.join(sorted(differing))}; existing row is preserved"
                    differences.append(message)
                    preconditions.append(_check(
                        f"reference-existing-row-{row_slug}",
                        f"SELECT COUNT(*) FROM {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} T WHERE " + " AND ".join(check_key_predicates),
                    ))

            insert_columns = [column for column in source_columns]
            if identity is not None and (
                identity["generationType"] == "ALWAYS"
                or source_row.get(identity["column"]) is None
            ):
                insert_columns.remove(identity["column"])
            values_sql = [expression_for(column) for column in insert_columns]
            statement = (
                f"INSERT INTO {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} "
                f"({', '.join(_sql_identifier(column) for column in insert_columns)})\n"
                f"SELECT {', '.join(values_sql)} FROM DUAL\n"
                f"WHERE NOT EXISTS (SELECT 1 FROM {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} T WHERE {' AND '.join(key_predicates)});"
            )
            statements.append(statement)

    steps: dict[str, str] = {}
    batch: list[str] = []
    bytes_in_batch = 0
    for statement in statements:
        size = len(statement.encode("utf-8"))
        if size + 1 > DATA_STEP_BYTE_LIMIT:
            raise BaselineError("one reference-data insert exceeds the 50000-byte migration step limit")
        if batch and (
            len(batch) >= DATA_STEP_STATEMENT_LIMIT
            or bytes_in_batch + 2 + size + 1 > DATA_STEP_BYTE_LIMIT
        ):
            steps[f"{len(steps) + 1:03d}-reference-data.sql"] = "\n\n".join(batch) + "\n"
            batch, bytes_in_batch = [], 0
        if batch:
            bytes_in_batch += 2
        batch.append(statement)
        bytes_in_batch += size
    if batch:
        steps[f"{len(steps) + 1:03d}-reference-data.sql"] = "\n\n".join(batch) + "\n"

    identity_statements = []
    for table_name in sorted(specifications):
        identity = specifications[table_name]["identity"]
        if not identity or identity["generationType"] not in {"BY DEFAULT", "BY DEFAULT ON NULL"} or not source_data[table_name]["rows"]:
            continue
        generation = "GENERATED BY DEFAULT ON NULL" if identity["generationType"] == "BY DEFAULT ON NULL" else "GENERATED BY DEFAULT"
        identity_statements.append(
            f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} MODIFY "
            f"({_sql_identifier(identity['column'])} {generation} AS IDENTITY (START WITH LIMIT VALUE));"
        )
    for statement in identity_statements:
        steps[f"{len(steps) + 1:03d}-identity-limit.sql"] = statement + "\n"

    return steps, {"preconditions": preconditions, "postconditions": postconditions}, differences


def _filter_ords(args) -> None:
    input_path = args.input if args.input.is_absolute() else ROOT / args.input
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    input_path = input_path.absolute()
    output_path = output_path.absolute()
    if input_path.resolve() == output_path.resolve():
        raise BaselineError("filter-ords input and output must be different files")
    try:
        raw = input_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise BaselineError("ORDS export must be UTF-8 without a byte-order mark")
        source = raw.decode("utf-8")
    except OSError as error:
        raise BaselineError(f"cannot read ORDS export {input_path}: {error}") from error
    except UnicodeError as error:
        raise BaselineError("ORDS export must be strict UTF-8") from error
    schemas = re.findall(r"(?im)^--[ \t]*Schema:[ \t]*([A-Z][A-Z0-9_$#]{0,127})(?:[ \t]+Date:.*)?[ \t]*$", source)
    if len(set(schemas)) != 1:
        raise BaselineError("ORDS export must contain one unambiguous '-- Schema: <OWNER>' header")
    if len(set(args.exclude_module)) != len(args.exclude_module):
        raise BaselineError("filter-ords cannot repeat an excluded module name")
    try:
        filtered, removed = exclude_ords_modules(source, args.exclude_module, schemas[0])
    except RolloutError as error:
        raise BaselineError(str(error)) from error
    if output_path.is_symlink() or (output_path.exists() and not output_path.is_file()):
        raise BaselineError("filter-ords output must be a regular file and cannot be a symbolic link")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise BaselineError(f"filter-ords output has an unfinished temporary file: {temporary}")
    try:
        temporary.write_text(filtered, encoding="utf-8", newline="\n")
        os.chmod(temporary, 0o600)
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"Removed ORDS modules: {', '.join(removed)}")
    print(f"Filtered ORDS export: {output_path}")


def _ddl_without_terminator(ddl: str) -> str:
    return ddl.strip().rstrip(";").rstrip()


def _wrap_guarded_ddl(exists_query: str, ddl: str) -> str:
    statement = _ddl_without_terminator(ddl)
    return (
        "DECLARE\n  l_count NUMBER;\nBEGIN\n"
        f"  SELECT COUNT(*) INTO l_count FROM {exists_query};\n"
        "  IF l_count = 0 THEN\n"
        f"    EXECUTE IMMEDIATE {_q_literal(statement)};\n"
        "  END IF;\nEND;\n/\n"
    )


def _replace_create_header(ddl: str, object_type: str, source_schema: str, target_schema: str) -> str:
    """Rewrite only schema-qualified names in a DBMS_METADATA CREATE header."""
    if source_schema == target_schema:
        return ddl
    identifier = r'("(?:[^"]|"")*"|[A-Za-z][A-Za-z0-9_$#]*)'
    if object_type == "TABLE":
        pattern = re.compile(rf"(?is)(\bCREATE\s+(?:(?:GLOBAL\s+TEMPORARY|PRIVATE\s+TEMPORARY)\s+)?TABLE\s+){identifier}\s*\.\s*{identifier}")
        return pattern.sub(lambda match: f'{match.group(1)}{_sql_identifier(target_schema)}.{match.group(3)}', ddl, count=1)
    if object_type == "SEQUENCE":
        pattern = re.compile(rf"(?is)(\bCREATE\s+SEQUENCE\s+){identifier}\s*\.\s*{identifier}")
        return pattern.sub(lambda match: f'{match.group(1)}{_sql_identifier(target_schema)}.{match.group(3)}', ddl, count=1)
    if object_type == "INDEX":
        pattern = re.compile(rf"(?is)(\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+){identifier}\s*\.\s*{identifier}(\s+ON\s+){identifier}\s*\.\s*{identifier}")
        return pattern.sub(
            lambda match: f'{match.group(1)}{_sql_identifier(target_schema)}.{match.group(3)}{match.group(4)}{_sql_identifier(target_schema)}.{match.group(6)}',
            ddl, count=1,
        )
    if object_type == "VIEW":
        pattern = re.compile(
            rf"(?is)(\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?(?:(?:NON)?EDITIONABLE\s+)?VIEW\s+)"
            rf"{identifier}\s*\.\s*{identifier}"
        )
        return pattern.sub(lambda match: f'{match.group(1)}{_sql_identifier(target_schema)}.{match.group(3)}', ddl, count=1)
    return ddl


def _table_column_type(row: Mapping) -> str:
    data_type = row.get("data_type")
    if not isinstance(data_type, str) or re.fullmatch(r"[A-Z][A-Z0-9_ ]*(?:\([0-9]+(?:,[0-9]+)?\))?(?: WITH (?:LOCAL )?TIME ZONE)?", data_type.upper(), re.ASCII) is None:
        raise BaselineError(f"unsupported column data type in catalog: {data_type!r}")
    value = data_type.upper()
    if value.startswith("NUMBER"):
        precision, scale = row.get("data_precision"), row.get("data_scale")
        if precision is not None:
            value = f"NUMBER({int(precision)}" + (f",{int(scale)}" if scale is not None else "") + ")"
    elif value in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"}:
        length = row.get("char_length") if value in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"} else row.get("data_length")
        if length is None:
            length = row.get("data_length")
        if length is None or int(length) < 1:
            raise BaselineError(f"column {row.get('name', '?')} has no usable length")
        if value in {"VARCHAR2", "CHAR"}:
            semantics = "CHAR" if row.get("char_used") == "C" else "BYTE"
            value = f"{value}({int(length)} {semantics})"
        else:
            value = f"{value}({int(length)})"
    return value


def _column_sql(row: Mapping) -> str:
    return f"{_sql_identifier(str(row['name']))} {_table_column_type(row)}"


def _check(check_id: str, sql: str) -> dict:
    validate_check_query(sql)
    return {"id": check_id, "sql": sql, "expected": 1}


def _reference_count_check(check_id: str, count_sql: str, expected_count: int) -> dict:
    if expected_count == 1:
        return _check(check_id, count_sql)
    if expected_count == 0:
        return _check(check_id, f"SELECT CASE WHEN ({count_sql}) = 0 THEN 1 ELSE 0 END FROM DUAL")
    raise BaselineError("reference-data checks support only zero or one natural-key match")


def _safe_check_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "object"
    return slug[:39].rstrip("-") + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _count_check(check_id: str, table_and_where: str, expected: int) -> dict:
    return _check(
        check_id,
        f"SELECT CASE WHEN (SELECT COUNT(*) FROM {table_and_where}) = {expected} THEN 1 ELSE 0 END FROM DUAL",
    )


def _source_line_matches(name: str, object_type: str, schema: str, line_number: int, line: str, *, final: bool) -> str:
    exact = _sql_literal(line[:-1] if final and line.endswith("\n") else line)
    text = f"S.TEXT = {exact}"
    if final:
        final_newline = _sql_literal((line[:-1] if line.endswith("\n") else line) + "\n")
        text = f"S.TEXT IN ({exact}, {final_newline})"
    return (
        "EXISTS (SELECT 1 FROM ALL_SOURCE S "
        f"WHERE S.OWNER = {_sql_literal(schema)} AND S.NAME = {_sql_literal(name)} AND S.TYPE = {_sql_literal(object_type)} "
        f"AND S.LINE = {line_number} AND {text})"
    )


def _source_check_queries(unit: Mapping, schema: str, *, max_bytes: int = 24000) -> list[dict]:
    name, object_type = str(unit["name"]), str(unit["type"])
    lines = unit["lines"]
    slug = _safe_check_slug(f"{name}-{object_type}")
    expected_rows = [(line_no, line) for line_no, line in enumerate(lines, start=1) if line.rstrip("\n") != ""]

    def query(rows: Sequence[tuple[int, str]]) -> str:
        predicates = [
            _source_line_matches(name, object_type, schema, line_no, text, final=line_no == len(lines))
            for line_no, text in rows
        ]
        text_match = " AND ".join(predicates) if predicates else "1 = 1"
        return (
            "SELECT CASE WHEN "
            f"(SELECT COUNT(*) FROM ALL_SOURCE WHERE OWNER = {_sql_literal(schema)} AND NAME = {_sql_literal(name)} AND TYPE = {_sql_literal(object_type)}) = {len(lines)} "
            f"AND {text_match} THEN 1 ELSE 0 END FROM DUAL"
        )

    result: list[dict] = []
    bucket: list[tuple[int, str]] = []
    for row in expected_rows:
        candidate = bucket + [row]
        if len(query(candidate).encode("utf-8")) > max_bytes and bucket:
            result.append(_check(f"source-{slug}-{len(result) + 1:03d}", query(bucket)))
            bucket = [row]
            if len(query(bucket).encode("utf-8")) > max_bytes:
                raise BaselineError(f"one stored-source line exceeds the exact-check SQL limit: {name} {object_type} line {row[0]}")
        else:
            bucket = candidate
        if len(query(bucket).encode("utf-8")) > max_bytes:
            raise BaselineError(f"one stored-source line exceeds the exact-check SQL limit: {name} {object_type} line {row[0]}")
    if bucket or not result:
        result.append(_check(f"source-{slug}-{len(result) + 1:03d}", query(bucket)))
    return result


def _setting_row(settings: Mapping, name: str, object_type: str) -> dict:
    rows = [row for row in settings if row.get("name") == name and row.get("type") == object_type]
    if len(rows) != 1:
        raise BaselineError(f"expected exactly one compiler-settings row for {object_type} {name}; found {len(rows)}")
    row = rows[0]
    fields = (
        "plsql_optimize_level", "plsql_code_type", "plsql_debug", "plsql_warnings",
        "plsql_ccflags", "nls_length_semantics", "plscope_settings",
    )
    missing = [field for field in fields if field not in row]
    if missing:
        raise BaselineError(f"compiler settings for {object_type} {name} are incomplete: {', '.join(missing)}")
    optimize = str(row["plsql_optimize_level"])
    code_type = str(row["plsql_code_type"]).upper()
    debug = str(row["plsql_debug"]).upper()
    nls = str(row["nls_length_semantics"]).upper()
    if optimize not in {"0", "1", "2", "3"} or code_type not in {"INTERPRETED", "NATIVE"} or debug not in {"TRUE", "FALSE"} or nls not in {"BYTE", "CHAR"}:
        raise BaselineError(f"compiler settings for {object_type} {name} contain unsupported values")
    return {field: row[field] for field in fields}


def _session_settings_sql(settings: Mapping) -> str:
    return "\n".join((
        f"ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = {settings['plsql_optimize_level']};",
        f"ALTER SESSION SET PLSQL_CODE_TYPE = {settings['plsql_code_type']};",
        f"ALTER SESSION SET PLSQL_DEBUG = {settings['plsql_debug']};",
        f"ALTER SESSION SET PLSQL_WARNINGS = {_sql_literal(str(settings['plsql_warnings']))};",
        f"ALTER SESSION SET PLSQL_CCFLAGS = {_sql_literal(str(settings['plsql_ccflags'] or ''))};",
        f"ALTER SESSION SET NLS_LENGTH_SEMANTICS = {settings['nls_length_semantics']};",
        f"ALTER SESSION SET PLSCOPE_SETTINGS = {_sql_literal(str(settings['plscope_settings'] or ''))};",
    ))


def _needs_clob_assembly(lines: Sequence[str]) -> bool:
    for line in lines:
        content = line[:-1] if line.endswith("\n") else line
        if not content or all(character in " \t" for character in content) or content.endswith("-"):
            return True
    return False


def _clob_source_step(unit: Mapping, settings: Mapping) -> str:
    lines = [
        f"-- Exact stored source for {unit['type']} {unit['name']} assembled as a CLOB.",
        _session_settings_sql(settings),
        "DECLARE",
        "  l_source CLOB;",
        "BEGIN",
        "  l_source := TO_CLOB('CREATE OR REPLACE ');",
    ]
    for exported in unit["lines"]:
        has_newline = exported.endswith("\n")
        content = exported[:-1] if has_newline else exported
        chunks = [content[index:index + _PLSQL_SOURCE_CHUNK_CHARS] for index in range(0, len(content), _PLSQL_SOURCE_CHUNK_CHARS)]
        if not chunks:
            if has_newline:
                lines.append("  l_source := l_source || CHR(10);")
            continue
        for index, chunk in enumerate(chunks):
            ending = " || CHR(10)" if has_newline and index == len(chunks) - 1 else ""
            lines.append(f"  l_source := l_source || {_q_literal(chunk)}{ending};")
    lines.extend(("  EXECUTE IMMEDIATE l_source;", "  DBMS_LOB.FREETEMPORARY(l_source);", "END;", "/", ""))
    return "\n".join(lines)


def _direct_source_step(unit: Mapping, settings: Mapping) -> str:
    stored = "".join(unit["lines"])
    sql = (
        f"-- Exact stored source for {unit['type']} {unit['name']}.\n"
        f"{_session_settings_sql(settings)}\n"
        "CREATE OR REPLACE "
        f"{stored}"
    )
    if not stored.endswith("\n"):
        sql += "\n"
    return sql + "/\n"


def _clob_ddl_step(label: str, ddl: str) -> str:
    chunks = [ddl[index:index + _PLSQL_SOURCE_CHUNK_CHARS] for index in range(0, len(ddl), _PLSQL_SOURCE_CHUNK_CHARS)]
    if not chunks:
        raise BaselineError(f"stored DDL for {label} is empty")
    lines = [
        f"-- Stored DDL for {label}, assembled as a CLOB.",
        "DECLARE",
        "  l_ddl CLOB;",
        "BEGIN",
        f"  l_ddl := TO_CLOB({_q_literal(chunks[0])});",
    ]
    lines.extend(f"  l_ddl := l_ddl || {_q_literal(chunk)};" for chunk in chunks[1:])
    lines.extend(("  EXECUTE IMMEDIATE l_ddl;", "  DBMS_LOB.FREETEMPORARY(l_ddl);", "END;", "/", ""))
    return "\n".join(lines)


def _exact_source_step(unit: Mapping, settings: Mapping) -> str:
    return _clob_source_step(unit, settings) if _needs_clob_assembly(unit["lines"]) else _direct_source_step(unit, settings)


def _source_units(catalog: Mapping, config: Mapping, schema: str) -> dict[tuple[str, str], dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in catalog.get("sections", {}).get("baseline-source", []):
        name, object_type = row.get("name"), row.get("type")
        if not isinstance(name, str) or object_type not in SOURCE_TYPES:
            raise BaselineError("baseline-source returned a malformed code object")
        if _schema_filter(config, schema, name):
            grouped[(object_type, name)].append(row)
    result: dict[tuple[str, str], dict] = {}
    for key, rows in grouped.items():
        result[key] = {"name": key[1], "type": key[0], "lines": _source_lines(rows, key[1], key[0])}
    return result


def _settings_map(catalog: Mapping, config: Mapping, schema: str) -> dict[tuple[str, str], dict]:
    result = {}
    for row in catalog.get("sections", {}).get("baseline-settings", []):
        name, object_type = row.get("name"), row.get("type")
        if isinstance(name, str) and object_type in SOURCE_TYPES and _schema_filter(config, schema, name):
            key = (object_type, name)
            if key in result:
                raise BaselineError(f"duplicate compiler-settings row for {object_type} {name}")
            result[key] = dict(row)
    return result


def _exact_settings_check(settings: Mapping, name: str, object_type: str, schema: str) -> dict:
    clauses = [
        f"S.PLSQL_OPTIMIZE_LEVEL = {_sql_literal(str(settings['plsql_optimize_level']))}",
        f"S.PLSQL_CODE_TYPE = {_sql_literal(str(settings['plsql_code_type']))}",
        f"S.PLSQL_DEBUG = {_sql_literal(str(settings['plsql_debug']))}",
        f"S.PLSQL_WARNINGS = {_sql_literal(str(settings['plsql_warnings']))}",
        f"S.NLS_LENGTH_SEMANTICS = {_sql_literal(str(settings['nls_length_semantics']))}",
        f"S.PLSCOPE_SETTINGS = {_sql_literal(str(settings['plscope_settings'] or ''))}",
    ]
    clauses.append("S.PLSQL_CCFLAGS IS NULL" if settings["plsql_ccflags"] is None else f"S.PLSQL_CCFLAGS = {_sql_literal(str(settings['plsql_ccflags']))}")
    query = (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_PLSQL_OBJECT_SETTINGS S "
        f"WHERE S.OWNER = {_sql_literal(schema)} AND S.NAME = {_sql_literal(name)} AND S.TYPE = {_sql_literal(object_type)} "
        + " AND ".join(clauses) + ") THEN 1 ELSE 0 END FROM DUAL"
    )
    return _check(f"settings-{_safe_check_slug(name + '-' + object_type)}", query)


def _compile_all_step(schema: str, prefixes: Sequence[str]) -> str:
    escaped = [prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%" for prefix in prefixes]
    def like_patterns(column: str) -> str:
        if not escaped:
            return "1 = 1"
        return " OR ".join(f"{column} LIKE {_sql_literal(pattern)} ESCAPE '\\'" for pattern in escaped)

    return (
        "-- Recompile configured invalid stored units in dependency passes.\n"
        "DECLARE\n"
        "  FUNCTION quoted(p_value VARCHAR2) RETURN VARCHAR2 IS BEGIN RETURN '''' || REPLACE(p_value, '''', '''''') || ''''; END;\n"
        "  l_previous PLS_INTEGER := -1;\n  l_current PLS_INTEGER := 0;\n  l_pass PLS_INTEGER := 0;\n  l_name VARCHAR2(261);\n"
        f"BEGIN\n  LOOP\n    SELECT COUNT(*) INTO l_current FROM ALL_OBJECTS WHERE OWNER = {_sql_literal(schema)} AND STATUS = 'INVALID' AND ("
        f"{like_patterns('OBJECT_NAME')});\n"
        "    EXIT WHEN l_current = 0 OR l_current = l_previous OR l_pass >= 10;\n"
        "    l_previous := l_current;\n    l_pass := l_pass + 1;\n"
        "    FOR r IN (SELECT O.OBJECT_NAME, O.OBJECT_TYPE, S.PLSQL_OPTIMIZE_LEVEL, S.PLSQL_CODE_TYPE, S.PLSQL_DEBUG, "
        "S.PLSQL_WARNINGS, S.PLSQL_CCFLAGS, S.NLS_LENGTH_SEMANTICS, S.PLSCOPE_SETTINGS "
        "FROM ALL_OBJECTS O LEFT JOIN ALL_PLSQL_OBJECT_SETTINGS S ON S.OWNER = O.OWNER AND S.NAME = O.OBJECT_NAME AND S.TYPE = O.OBJECT_TYPE "
        f"WHERE O.OWNER = {_sql_literal(schema)} AND O.STATUS = 'INVALID' AND O.OBJECT_TYPE IN "
        "('PACKAGE','PACKAGE BODY','FUNCTION','PROCEDURE','TYPE','TYPE BODY','TRIGGER','VIEW') AND ("
        f"{like_patterns('O.OBJECT_NAME')}) ORDER BY CASE O.OBJECT_TYPE WHEN 'TYPE' THEN 1 WHEN 'PACKAGE' THEN 2 ELSE 3 END, O.OBJECT_NAME) LOOP\n"
        "      l_name := '\"' || REPLACE(r.OBJECT_NAME, '\"', '\"\"') || '\"';\n"
        "      BEGIN\n"
        "        IF r.PLSQL_OPTIMIZE_LEVEL IS NOT NULL THEN\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = ' || TO_CHAR(r.PLSQL_OPTIMIZE_LEVEL);\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET PLSQL_CODE_TYPE = ' || r.PLSQL_CODE_TYPE;\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET PLSQL_DEBUG = ' || r.PLSQL_DEBUG;\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET PLSQL_WARNINGS = ' || quoted(r.PLSQL_WARNINGS);\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET PLSQL_CCFLAGS = ' || quoted(r.PLSQL_CCFLAGS);\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET NLS_LENGTH_SEMANTICS = ' || r.NLS_LENGTH_SEMANTICS;\n"
        "          EXECUTE IMMEDIATE 'ALTER SESSION SET PLSCOPE_SETTINGS = ' || quoted(r.PLSCOPE_SETTINGS);\n"
        "        END IF;\n"
        "        IF r.OBJECT_TYPE = 'PACKAGE BODY' THEN EXECUTE IMMEDIATE 'ALTER PACKAGE ' || l_name || ' COMPILE BODY';\n"
        "        ELSIF r.OBJECT_TYPE = 'PACKAGE' THEN EXECUTE IMMEDIATE 'ALTER PACKAGE ' || l_name || ' COMPILE SPECIFICATION';\n"
        "        ELSIF r.OBJECT_TYPE = 'TYPE BODY' THEN EXECUTE IMMEDIATE 'ALTER TYPE ' || l_name || ' COMPILE BODY';\n"
        "        ELSIF r.OBJECT_TYPE = 'TYPE' THEN EXECUTE IMMEDIATE 'ALTER TYPE ' || l_name || ' COMPILE SPECIFICATION';\n"
        "        ELSE EXECUTE IMMEDIATE 'ALTER ' || r.OBJECT_TYPE || ' ' || l_name || ' COMPILE'; END IF;\n"
        "      EXCEPTION WHEN OTHERS THEN NULL;\n      END;\n    END LOOP;\n  END LOOP;\n"
        f"  SELECT COUNT(*) INTO l_current FROM ALL_OBJECTS WHERE OWNER = {_sql_literal(schema)} AND STATUS = 'INVALID' AND ("
        f"{like_patterns('OBJECT_NAME')});\n"
        "  IF l_current > 0 THEN RAISE_APPLICATION_ERROR(-20989, 'configured stored units remain invalid after compile passes'); END IF;\nEND;\n/\n"
    )


def _columns_signature(row: Mapping) -> tuple:
    columns = row.get("columns") or []
    if not isinstance(columns, list):
        return ()
    result = []
    for number, value in enumerate(columns, start=1):
        if isinstance(value, dict):
            result.append((value.get("name"), value.get("position", number), value.get("descend", "ASC")))
        elif isinstance(value, str):
            result.append((value, number, "ASC"))
    return tuple(result)


def _constraint_ddl(row: Mapping, source_schema: str, target_schema: str) -> str:
    name, table, kind = str(row.get("name", "")), str(row.get("table_name", "")), str(row.get("constraint_type", ""))
    columns = row.get("columns") or []
    if not name or not table or not isinstance(columns, list) or any(not isinstance(item, str) for item in columns):
        raise BaselineError("constraint catalog row is incomplete")
    column_list = ", ".join(_sql_identifier(item) for item in columns)
    if kind == "C":
        condition = row.get("condition")
        if not isinstance(condition, str) or row.get("condition_truncated") in {"Y", True}:
            raise BaselineError(f"check constraint {name} has missing or truncated condition text")
        clause = f"CHECK ({condition})"
    elif kind == "P":
        clause = f"PRIMARY KEY ({column_list})"
    elif kind == "U":
        clause = f"UNIQUE ({column_list})"
    elif kind == "R":
        reference, ref_columns = row.get("referenced_table"), row.get("referenced_columns")
        if not isinstance(reference, str) or not isinstance(ref_columns, list) or not ref_columns:
            raise BaselineError(f"foreign-key constraint {name} has incomplete reference metadata")
        ref_owner, dot, ref_name = reference.rpartition(".")
        if not dot:
            ref_owner, ref_name = target_schema, reference
        if ref_owner in {"", "<CONFIGURED_SCHEMA>", source_schema}:
            ref_owner = target_schema
        clause = (
            f"FOREIGN KEY ({column_list}) REFERENCES {_sql_identifier(ref_owner)}.{_sql_identifier(ref_name)} "
            f"({', '.join(_sql_identifier(str(item)) for item in ref_columns)})"
        )
        if row.get("delete_rule") in {"CASCADE", "SET NULL"}:
            clause += f" ON DELETE {row['delete_rule']}"
    else:
        raise BaselineError(f"unsupported constraint type {kind!r} for {name}")
    attributes = []
    if row.get("deferrable") == "DEFERRABLE":
        attributes.append("DEFERRABLE")
        if row.get("deferred") in {"DEFERRED", "IMMEDIATE"}:
            attributes.append(f"INITIALLY {row['deferred']}")
    status = str(row.get("status", "ENABLED")).upper()
    validated = str(row.get("validated", "NOT VALIDATED")).upper()
    if status == "DISABLED":
        attributes.append("DISABLE")
    elif status == "ENABLED":
        attributes.append("ENABLE " + ("VALIDATE" if validated == "VALIDATED" else "NOVALIDATE"))
        if row.get("rely") == "RELY":
            attributes.append("RELY")
    else:
        raise BaselineError(f"constraint {name} has an unsupported status {status!r}")
    return f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table)} ADD CONSTRAINT {_sql_identifier(name)} {clause} {' '.join(attributes)}"


def _condition_key(value: str) -> str:
    output = []
    index = 0
    in_string = False
    while index < len(value):
        char = value[index]
        if char == "'":
            output.append(char)
            if in_string and index + 1 < len(value) and value[index + 1] == "'":
                output.append("'")
                index += 2
                continue
            in_string = not in_string
        elif in_string or not char.isspace():
            output.append(char)
        index += 1
    return "".join(output)


def _wrap_constraint(row: Mapping, source_schema: str, target_schema: str, *, replace_check: bool = False) -> str:
    ddl = _constraint_ddl(row, source_schema, target_schema)
    table, name = str(row["table_name"]), str(row["name"])
    lookup = _sql_literal(name)
    enable_state = "VALIDATE" if str(row.get("validated", "NOT VALIDATED")).upper() == "VALIDATED" else "NOVALIDATE"
    enable = (
        f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table)} ENABLE {enable_state}"
        + f" CONSTRAINT {_sql_identifier(name)}"
    )
    enable_rely = (
        f"    EXECUTE IMMEDIATE {_q_literal(f'ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table)} MODIFY CONSTRAINT {_sql_identifier(name)} RELY')};\n"
        if row.get("rely") == "RELY" else ""
    )
    if replace_check:
        normalized = _condition_key(str(row["condition"]))
        drop = f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table)} DROP CONSTRAINT {_sql_identifier(name)}"
        return (
            "DECLARE\n"
            "  FUNCTION normalized(p_value VARCHAR2) RETURN VARCHAR2 IS\n"
            "    l_result VARCHAR2(4000) := '';\n"
            "    l_index PLS_INTEGER := 1;\n"
            "    l_char VARCHAR2(1);\n"
            "    l_in_string BOOLEAN := FALSE;\n"
            "  BEGIN\n"
            "    WHILE l_index <= LENGTH(p_value) LOOP\n"
            "      l_char := SUBSTR(p_value, l_index, 1);\n"
            "      IF l_char = '''' THEN\n"
            "        l_result := l_result || l_char;\n"
            "        IF l_in_string AND SUBSTR(p_value, l_index + 1, 1) = '''' THEN\n"
            "          l_result := l_result || ''''; l_index := l_index + 2;\n"
            "        ELSE\n"
            "          l_in_string := NOT l_in_string; l_index := l_index + 1;\n"
            "        END IF;\n"
            "      ELSIF l_in_string OR l_char NOT IN (' ', CHR(9), CHR(10), CHR(13)) THEN\n"
            "        l_result := l_result || l_char; l_index := l_index + 1;\n"
            "      ELSE\n"
            "        l_index := l_index + 1;\n"
            "      END IF;\n"
            "    END LOOP;\n"
            "    RETURN l_result;\n"
            "  END;\n"
            "  l_status VARCHAR2(8);\n  l_condition VARCHAR2(4000);\nBEGIN\n  BEGIN\n"
            f"    SELECT STATUS, SEARCH_CONDITION_VC INTO l_status, l_condition FROM ALL_CONSTRAINTS WHERE OWNER = {_sql_literal(target_schema)} AND CONSTRAINT_NAME = {lookup};\n"
            "  EXCEPTION WHEN NO_DATA_FOUND THEN l_status := NULL;\n  END;\n"
            f"  IF l_status IS NOT NULL AND (l_condition IS NULL OR normalized(l_condition) <> {_sql_literal(normalized)}) THEN\n"
            f"    EXECUTE IMMEDIATE {_q_literal(drop)};\n    l_status := NULL;\n  END IF;\n"
            f"  IF l_status IS NULL THEN\n    EXECUTE IMMEDIATE {_q_literal(ddl)};\n"
            f"  ELSIF l_status = 'DISABLED' THEN\n    EXECUTE IMMEDIATE {_q_literal(enable)};\n"
            f"{enable_rely}  END IF;\nEND;\n/\n"
        )
    return (
        "DECLARE\n  l_status VARCHAR2(8);\nBEGIN\n  BEGIN\n"
        f"    SELECT STATUS INTO l_status FROM ALL_CONSTRAINTS WHERE OWNER = {_sql_literal(target_schema)} AND CONSTRAINT_NAME = {lookup};\n"
        "  EXCEPTION WHEN NO_DATA_FOUND THEN l_status := NULL;\n  END;\n"
        f"  IF l_status IS NULL THEN\n    EXECUTE IMMEDIATE {_q_literal(ddl)};\n"
        + (f"  ELSIF l_status = 'DISABLED' THEN\n    EXECUTE IMMEDIATE {_q_literal(enable)};\n{enable_rely}" if row.get("status", "ENABLED") == "ENABLED" else "")
        + "  END IF;\nEND;\n/\n"
    )


def _wrap_not_null(target_schema: str, table: str, column: str) -> str:
    ddl = f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table)} MODIFY ({_sql_identifier(column)} NOT NULL)"
    enable_prefix = f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table)} ENABLE CONSTRAINT "
    return (
        "DECLARE\n  l_nullable VARCHAR2(1);\nBEGIN\n"
        f"  SELECT NULLABLE INTO l_nullable FROM ALL_TAB_COLS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table)} AND COLUMN_NAME = {_sql_literal(column)};\n"
        "  IF l_nullable = 'Y' THEN\n    BEGIN\n"
        f"      EXECUTE IMMEDIATE {_q_literal(ddl)};\n"
        "    EXCEPTION WHEN OTHERS THEN\n      IF SQLCODE = -1442 THEN\n"
        "        FOR c IN (SELECT CONSTRAINT_NAME FROM ALL_CONSTRAINTS "
        f"WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table)} AND CONSTRAINT_TYPE = 'C' AND STATUS = 'DISABLED' "
        f"AND SEARCH_CONDITION_VC = {_sql_literal(_sql_identifier(column) + ' IS NOT NULL')}) LOOP\n"
        f"          EXECUTE IMMEDIATE {_q_literal(enable_prefix)} || {_sql_literal(chr(34))} || c.CONSTRAINT_NAME || {_sql_literal(chr(34))};\n"
        "        END LOOP;\n      ELSE\n        RAISE;\n      END IF;\n    END;\n  END IF;\nEND;\n/\n"
    )


def _sync_identity_sql(target_schema: str, row: Mapping) -> str:
    generation = str(row.get("generation_type", "BY DEFAULT")).upper()
    if generation not in {"ALWAYS", "BY DEFAULT", "BY DEFAULT ON NULL"}:
        raise BaselineError(f"unsupported identity generation type {generation!r}")
    return (
        f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(str(row['table_name']))} MODIFY "
        f"({_sql_identifier(str(row['column_name']))} GENERATED {generation} AS IDENTITY (START WITH LIMIT VALUE));\n"
    )


def _sync_sequence_sql(target_schema: str, row: Mapping, mapping: Mapping) -> str:
    name, table, column = str(row.get("name", "")), str(mapping["table"]), str(mapping["column"])
    increment = row.get("increment_by", 1)
    if type(increment) is not int or increment <= 0:
        raise BaselineError(f"sequence {name} must have a positive increment to sync seeded ids")
    sequence_ref = f"{_sql_identifier(target_schema)}.{_sql_identifier(name)}"
    table_ref = f"{_sql_identifier(target_schema)}.{_sql_identifier(table)}"
    next_query = _sql_literal(f"SELECT {sequence_ref}.NEXTVAL FROM DUAL")
    max_query = _sql_literal(f"SELECT NVL(MAX({_sql_identifier(column)}), 0) FROM {table_ref}")
    return (
        f"-- Move {name} past existing ids in {table}.{column}.\n"
        "DECLARE\n  l_next NUMBER;\n  l_max NUMBER;\n  l_increment_changed BOOLEAN := FALSE;\nBEGIN\n"
        f"  EXECUTE IMMEDIATE {next_query} INTO l_next;\n"
        f"  EXECUTE IMMEDIATE {max_query} INTO l_max;\n"
        "  IF l_next <= l_max THEN\n"
        f"    EXECUTE IMMEDIATE {_sql_literal(f'ALTER SEQUENCE {sequence_ref} INCREMENT BY ')} || TO_CHAR(l_max + 1 - l_next);\n"
        "    l_increment_changed := TRUE;\n"
        "    BEGIN\n"
        f"    EXECUTE IMMEDIATE {next_query} INTO l_next;\n"
        f"    EXECUTE IMMEDIATE {_sql_literal(f'ALTER SEQUENCE {sequence_ref} INCREMENT BY {increment}')};\n"
        "      l_increment_changed := FALSE;\n"
        "    EXCEPTION WHEN OTHERS THEN\n"
        f"      IF l_increment_changed THEN EXECUTE IMMEDIATE {_sql_literal(f'ALTER SEQUENCE {sequence_ref} INCREMENT BY {increment}')}; END IF;\n"
        "      RAISE;\n"
        "    END;\n  END IF;\nEND;\n/\n"
    )


def _structure_bundle(config: Mapping, source_schema: str, target_schema: str, source: Mapping, target: Mapping, snapshot: SchemaSnapshot) -> tuple[dict[str, str], dict]:
    left, right = source["sections"], target["sections"]
    source_tables = {row["name"]: row for row in left.get("tables", []) if _schema_filter(config, source_schema, row.get("name", ""))}
    target_tables = {row["name"]: row for row in right.get("tables", [])}
    source_columns = {(row["table_name"], row["name"]): row for row in left.get("columns", []) if _schema_filter(config, source_schema, row.get("table_name", ""))}
    target_columns = {(row["table_name"], row["name"]): row for row in right.get("columns", [])}
    source_constraints = {(row["table_name"], row["name"]): row for row in left.get("constraints", []) if _schema_filter(config, source_schema, row.get("table_name", ""))}
    target_constraints = {(row["table_name"], row["name"]): row for row in right.get("constraints", [])}
    source_indexes = {row["name"]: row for row in left.get("indexes", []) if _schema_filter(config, source_schema, row.get("name", ""))}
    target_indexes = {row["name"]: row for row in right.get("indexes", [])}
    identity_sequence_names = {
        str(row.get("sequence_name")) for row in left.get("identity-columns", [])
        if isinstance(row.get("sequence_name"), str) and row.get("sequence_name")
    }
    source_sequences = {
        row["name"]: row for row in left.get("sequences", [])
        if _schema_filter(config, source_schema, row.get("name", "")) and row.get("name") not in identity_sequence_names
    }
    target_sequences = {row["name"]: row for row in right.get("sequences", [])}
    source_identity_rows = [row for row in left.get("identity-columns", []) if isinstance(row.get("table_name"), str) and _schema_filter(config, source_schema, row["table_name"])]
    source_identity = {(row.get("table_name"), row.get("column_name")): row for row in source_identity_rows}
    definitions = snapshot.objects
    chunks: list[str] = []
    posts: list[dict] = []

    for name in sorted(source_tables):
        if name in target_tables:
            continue
        definition = definitions.get(ObjectKey(source_schema, name, "TABLE"))
        if definition is None:
            raise BaselineError(f"source table DDL is missing for {name}")
        ddl = _replace_create_header(definition.raw_ddl, "TABLE", source_schema, target_schema)
        chunks.append(_wrap_guarded_ddl(f"ALL_TABLES WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(name)}", ddl))
        posts.append(_count_check(f"table-{_safe_check_slug(name)}", f"ALL_TABLES WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(name)}", 1))

    to_make_not_null = set()
    for key, row in sorted(source_columns.items()):
        table_name, column_name = key
        if table_name not in target_tables:
            continue
        existing = target_columns.get(key)
        if existing is None:
            column_ddl = _column_sql(row)
            identity = source_identity.get(key)
            if identity is not None:
                generation = str(identity.get("generation_type", "BY DEFAULT")).upper()
                if generation not in {"ALWAYS", "BY DEFAULT", "BY DEFAULT ON NULL"}:
                    raise BaselineError(f"unsupported identity generation type {generation!r} for {table_name}.{column_name}")
                column_ddl += f" GENERATED {generation} AS IDENTITY"
            ddl = f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} ADD ({column_ddl})"
            exists_query = f"ALL_TAB_COLS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table_name)} AND COLUMN_NAME = {_sql_literal(column_name)}"
            chunks.append(_wrap_guarded_ddl(exists_query, ddl))
            if row.get("nullable") == "N":
                to_make_not_null.add(key)
        else:
            type_fields = ("data_type", "data_length", "data_precision", "data_scale", "char_used", "char_length")
            changed = [field for field in type_fields if row.get(field) != existing.get(field)]
            if changed:
                raise BaselineError(f"column type changes need a reviewed data migration: {table_name}.{column_name} ({', '.join(changed)})")
            if row.get("nullable") == "N" and existing.get("nullable") == "Y":
                to_make_not_null.add(key)
        if row.get("nullable") == "N":
            posts.append(_count_check(
                f"not-null-{_safe_check_slug(table_name + '-' + column_name)}",
                f"ALL_TAB_COLS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table_name)} AND COLUMN_NAME = {_sql_literal(column_name)} AND NULLABLE = 'Y'", 0,
            ))
    for table_name, column_name in sorted(to_make_not_null):
        chunks.append(_wrap_not_null(target_schema, table_name, column_name))

    for key, row in sorted(source_constraints.items()):
        table_name, name = key
        if name.upper().startswith("SYS_C"):
            continue
        existing = target_constraints.get(key)
        replace_check = bool(
            existing and row.get("constraint_type") == "C" and existing.get("constraint_type") == "C"
            and _condition_key(str(row.get("condition", ""))) != _condition_key(str(existing.get("condition", "")))
        )
        if existing is None or replace_check:
            if ObjectKey(source_schema, name, "CONSTRAINT") not in definitions:
                raise BaselineError(f"source constraint definition is missing for {table_name}.{name}")
            chunks.append(_wrap_constraint(row, source_schema, target_schema, replace_check=replace_check))
            posts.append(_count_check(
                f"constraint-{_safe_check_slug(table_name + '-' + name)}",
                f"ALL_CONSTRAINTS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table_name)} AND CONSTRAINT_NAME = {_sql_literal(name)} "
                f"AND STATUS = {_sql_literal(str(row.get('status', 'ENABLED')).upper())} "
                f"AND VALIDATED = {_sql_literal(str(row.get('validated', 'NOT VALIDATED')).upper())}", 1,
            ))

    for name, row in sorted(source_indexes.items()):
        if name in target_indexes or any(
            row.get("table_name") == target_row.get("table_name") and _columns_signature(row) and _columns_signature(row) == _columns_signature(target_row)
            for target_row in target_indexes.values()
        ):
            continue
        definition = definitions.get(ObjectKey(source_schema, name, "INDEX"))
        if definition is None:
            raise BaselineError(f"source index definition is missing for {name}")
        ddl = _replace_create_header(definition.raw_ddl, "INDEX", source_schema, target_schema)
        chunks.append(_wrap_guarded_ddl(f"ALL_INDEXES WHERE OWNER = {_sql_literal(target_schema)} AND INDEX_NAME = {_sql_literal(name)}", ddl))
        posts.append(_count_check(f"index-{_safe_check_slug(name)}", f"ALL_INDEXES WHERE OWNER = {_sql_literal(target_schema)} AND INDEX_NAME = {_sql_literal(name)}", 1))

    mappings = {item["sequence"]: item for item in config.get("sequenceMappings", [])}
    for name, row in sorted(source_sequences.items()):
        if name not in mappings:
            raise BaselineError(f"add sequenceMappings for {name} so its generator can advance beyond seeded ids")
        if name not in target_sequences:
            definition = definitions.get(ObjectKey(source_schema, name, "SEQUENCE"))
            if definition is None:
                raise BaselineError(f"source sequence definition is missing for {name}")
            ddl = _replace_create_header(definition.raw_ddl, "SEQUENCE", source_schema, target_schema)
            chunks.append(_wrap_guarded_ddl(f"ALL_SEQUENCES WHERE SEQUENCE_OWNER = {_sql_literal(target_schema)} AND SEQUENCE_NAME = {_sql_literal(name)}", ddl))
            posts.append(_count_check(f"sequence-{_safe_check_slug(name)}", f"ALL_SEQUENCES WHERE SEQUENCE_OWNER = {_sql_literal(target_schema)} AND SEQUENCE_NAME = {_sql_literal(name)}", 1))
        chunks.append(_sync_sequence_sql(target_schema, row, mappings[name]))

    target_identity = {(row.get("table_name"), row.get("column_name")) for row in right.get("identity-columns", [])}
    for row in sorted(source_identity_rows, key=lambda item: (item.get("table_name", ""), item.get("column_name", ""))):
        table_name, column_name = row.get("table_name"), row.get("column_name")
        if not isinstance(table_name, str) or not _schema_filter(config, source_schema, table_name):
            continue
        if (table_name, column_name) not in target_identity and table_name in target_tables and (table_name, column_name) in target_columns:
            raise BaselineError(f"identity column {table_name}.{column_name} needs a reviewed conversion migration")
        chunks.append(_sync_identity_sql(target_schema, row))
        posts.append(_count_check(
            f"identity-{_safe_check_slug(table_name + '-' + str(column_name))}",
            f"ALL_TAB_IDENTITY_COLS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table_name)} AND COLUMN_NAME = {_sql_literal(str(column_name))}", 1,
        ))

    steps = {"001-structure-delta.sql": "\n".join(chunks)} if chunks else {}
    return steps, {"preconditions": [], "postconditions": posts}


def _grant_bundle(config: Mapping, source_schema: str, target_schema: str, source: Mapping, target: Mapping) -> tuple[dict[str, str], dict]:
    source_rows = _grant_rows(config, source.get("sections", {}).get("object-grants", []), source_schema)
    target_rows = _grant_rows(config, target.get("sections", {}).get("object-grants", []), target_schema)
    target_map = {(row.get("object_name"), row.get("grantee"), row.get("privilege")): row for row in target_rows}
    grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in source_rows:
        key = (row.get("object_name"), row.get("grantee"), row.get("privilege"))
        present = target_map.get(key)
        source_option = str(row.get("grantable", "NO")).upper()
        target_option = str((present or {}).get("grantable", "NO")).upper()
        if present is None or (source_option == "YES" and target_option != "YES"):
            if source_option not in {"YES", "NO"}:
                raise BaselineError(f"object grant {key} has an invalid grantable flag")
            keep_option = config.get("grants", {}).get("keepGrantOptions", True)
            grouped[(str(key[0]), str(key[1]), "YES" if source_option == "YES" and keep_option else "NO")].add(str(key[2]).upper())
    statements: list[str] = []
    checks: list[dict] = []
    for (object_name, grantee, grantable), privileges in sorted(grouped.items()):
        if any(re.fullmatch(r"[A-Z][A-Z0-9 ]*", value, re.ASCII) is None for value in privileges):
            raise BaselineError(f"grant on {object_name} contains an unsafe privilege")
        grant_to = "PUBLIC" if grantee.upper() == "PUBLIC" else _sql_identifier(grantee)
        privilege_list = ", ".join(sorted(privileges))
        statements.append(
            f"GRANT {privilege_list} ON {_sql_identifier(target_schema)}.{_sql_identifier(object_name)} TO {grant_to}"
            + (" WITH GRANT OPTION" if grantable == "YES" else "") + ";"
        )
        sql_privileges = ", ".join(_sql_literal(item) for item in sorted(privileges))
        checks.append(_count_check(
            f"grant-{_safe_check_slug(object_name + '-' + grantee + '-' + grantable)}",
            f"ALL_TAB_PRIVS WHERE TABLE_SCHEMA = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(object_name)} "
            f"AND GRANTEE = {_sql_literal(grantee)} AND GRANTABLE = {_sql_literal(grantable)} AND PRIVILEGE IN ({sql_privileges})",
            len(privileges),
        ))
    steps: dict[str, str] = {}
    batch: list[str] = []
    bytes_in_batch = 0
    for statement in statements:
        size = len(statement.encode("utf-8")) + 1
        if batch and (len(batch) >= 100 or bytes_in_batch + size > 50_000):
            steps[f"{len(steps) + 1:03d}-object-grants.sql"] = "\n".join(batch) + "\n"
            batch, bytes_in_batch = [], 0
        batch.append(statement)
        bytes_in_batch += size
    if batch:
        steps[f"{len(steps) + 1:03d}-object-grants.sql"] = "\n".join(batch) + "\n"
    return steps, {"preconditions": [], "postconditions": checks}


def _code_bundle(config: Mapping, source_schema: str, target_schema: str, source: Mapping, target: Mapping) -> tuple[dict[str, str], dict]:
    source_units = _source_units(source, config, source_schema)
    target_units = _source_units(target, config, target_schema)
    source_settings = _settings_map(source, config, source_schema)
    target_settings = _settings_map(target, config, target_schema)
    order = {"TYPE": 0, "PACKAGE": 1, "FUNCTION": 2, "PROCEDURE": 2, "TYPE BODY": 3, "PACKAGE BODY": 3, "TRIGGER": 4}
    selected = []
    for key, unit in source_units.items():
        settings = _setting_row(source_settings.values(), key[1], key[0])
        target_rows = [row for row in target_settings.values() if (row.get("type"), row.get("name")) == key]
        target_settings_value = None
        if len(target_rows) == 1:
            target_settings_value = {field: target_rows[0].get(field) for field in settings}
        if target_units.get(key) != unit or target_settings_value != settings:
            selected.append((key, unit, settings))

    steps: dict[str, str] = {}
    postconditions: list[dict] = []
    sequence = 1
    for (object_type, name), unit, settings in sorted(selected, key=lambda item: (order.get(item[0][0], 9), item[0][1])):
        sql = _exact_source_step(unit, settings)
        validate_sql_only(sql)
        slug = _safe_check_slug(name + "-" + object_type)
        steps[f"{sequence:03d}-source-{slug}.sql"] = sql
        sequence += 1
        postconditions.extend(_source_check_queries(unit, target_schema))
        postconditions.append(_exact_settings_check(settings, name, object_type, target_schema))
        postconditions.append(_count_check(
            f"object-{slug}",
            f"ALL_OBJECTS WHERE OWNER = {_sql_literal(target_schema)} AND OBJECT_NAME = {_sql_literal(name)} AND OBJECT_TYPE = {_sql_literal(object_type)}", 1,
        ))
        postconditions.append(_check(
            f"valid-object-{slug}",
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_OBJECTS "
            f"WHERE OWNER = {_sql_literal(target_schema)} AND OBJECT_NAME = {_sql_literal(name)} "
            f"AND OBJECT_TYPE = {_sql_literal(object_type)} AND STATUS = 'VALID') THEN 1 ELSE 0 END FROM DUAL",
        ))

    source_views = {
        row["name"]: row for row in source.get("sections", {}).get("baseline-views", [])
        if isinstance(row.get("name"), str) and _schema_filter(config, source_schema, row["name"])
    }
    target_views = {
        row["name"]: row for row in target.get("sections", {}).get("baseline-views", [])
        if isinstance(row.get("name"), str) and _schema_filter(config, target_schema, row["name"])
    }
    for name, row in sorted(source_views.items()):
        source_ddl = row.get("ddl")
        if not isinstance(source_ddl, str):
            raise BaselineError(f"stored view metadata DDL for {name} is missing")
        target_row = target_views.get(name, {})
        mapped_ddl = _replace_create_header(source_ddl, "VIEW", source_schema, target_schema)
        if target_row.get("text") == row.get("text") and target_row.get("ddl") == mapped_ddl:
            continue
        ddl = source_ddl
        if not isinstance(ddl, str) or not re.match(
            r"(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?(?:(?:NON)?EDITIONABLE\s+)?VIEW\b", ddl,
        ):
            raise BaselineError(f"stored view metadata DDL for {name} is not a CREATE VIEW statement")
        ddl = mapped_ddl
        steps[f"{sequence:03d}-view-{_safe_check_slug(name)}.sql"] = _clob_ddl_step(f"VIEW {name}", _ddl_without_terminator(ddl))
        sequence += 1
        postconditions.append(_count_check(
            f"view-{_safe_check_slug(name)}",
            f"ALL_VIEWS WHERE OWNER = {_sql_literal(target_schema)} AND VIEW_NAME = {_sql_literal(name)}", 1,
        ))
    if selected:
        steps[f"{sequence:03d}-compile-all.sql"] = _compile_all_step(target_schema, config.get("prefixes", []))
    return steps, {"preconditions": [], "postconditions": postconditions}


def _migration_bytes(steps: Mapping[str, str], checks: Mapping, readme: str) -> dict[str, bytes]:
    output = {name: (sql.rstrip("\n") + "\n").encode("utf-8") for name, sql in steps.items()}
    output["checks.json"] = _json_bytes({
        "schemaVersion": 1,
        "preconditions": list(checks["preconditions"]),
        "postconditions": list(checks["postconditions"]),
    })
    output["README.md"] = (readme.rstrip("\n") + "\n").encode("utf-8")
    for name, source in output.items():
        if name.endswith(".sql"):
            validate_sql_only(source.decode("utf-8"))
    return output


def _folder_contents_match(folder: Path, expected: Mapping[str, bytes]) -> bool:
    try:
        observed = {
            path.name: path.read_bytes() for path in folder.iterdir()
            if path.is_file() and not path.is_symlink()
            and not (path.name.startswith("status.") and path.name.endswith(".json"))
        }
    except OSError:
        return False
    return observed == dict(expected)


def _build_migration_folder(root: Path, schema: str, family: str, files: Mapping[str, bytes]) -> Path | None:
    migrations_root = root / "migrations"
    if migrations_root.is_symlink():
        raise BaselineError("migrations directory cannot be a symbolic link")
    parent = root / "migrations" / schema
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise BaselineError(f"migration schema directory is unsafe: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    existing: dict[int, Path] = {}
    for folder in parent.iterdir():
        match = FOLDER_RE.fullmatch(folder.name)
        if match and match.group("family") == family:
            if folder.is_symlink():
                raise BaselineError(f"migration family contains a symbolic link: {folder.name}")
            if not folder.is_dir():
                raise BaselineError(f"migration family entry is not a folder: {folder.name}")
            revision = int(match.group("revision"))
            if revision in existing:
                raise BaselineError(f"duplicate revision r{revision:03d} in migration family {family}")
            existing[revision] = folder
    if existing and set(existing) != set(range(1, max(existing) + 1)):
        raise BaselineError(f"revision history for {family} is incomplete; revisions must be consecutive from r001")
    if existing:
        latest = existing[max(existing)]
        relative = latest.relative_to(root).as_posix()
        lock = inspect_migration_lock(root, relative)
        if lock.status == "unknown":
            raise BaselineError(f"cannot determine whether existing migration is immutable: {relative} ({'; '.join(lock.states)})")
        if _folder_contents_match(latest, files):
            if lock.status == "unlocked":
                print(f"Unchanged migration; left byte-identical: {relative}")
            else:
                print(f"Skipped receipted or attempted migration without changing it: {relative} ({'; '.join(lock.states)})")
            load_migration(root, relative)
            return None
    revision = max(existing, default=0) + 1
    if revision > 999:
        raise BaselineError(f"migration revisions are exhausted for {schema}/{family}")
    folder = parent / f"{dt.date.today().isoformat()}_{family}-r{revision:03d}"
    if folder.exists() or folder.is_symlink():
        raise BaselineError(f"refusing to overwrite an existing migration folder: {folder.relative_to(root).as_posix()}")
    folder.mkdir(mode=0o700)
    try:
        for name, content in sorted(files.items()):
            target = folder / name
            with target.open("xb") as stream:
                stream.write(content)
            os.chmod(target, 0o600)
        relative = folder.relative_to(root).as_posix()
        load_migration(root, relative)
        print(f"Created migration: {relative}")
        return folder
    except BaseException:
        for child in folder.iterdir():
            if child.is_file() and not child.is_symlink():
                child.unlink()
        folder.rmdir()
        raise


def _snapshot_keys(catalog: Mapping, config: Mapping, schema: str) -> tuple[tuple[str, str], ...]:
    key_types = {"tables": "TABLE", "views": "VIEW", "sequences": "SEQUENCE", "indexes": "INDEX", "triggers": "TRIGGER"}
    keys = set()
    for section, object_type in key_types.items():
        for row in catalog.get("sections", {}).get(section, []):
            name = row.get("name")
            if isinstance(name, str) and _schema_filter(config, schema, name):
                keys.add((name, object_type))
    for row in catalog.get("sections", {}).get("stored-code", []):
        name, object_type = row.get("name"), row.get("type")
        if isinstance(name, str) and object_type in SOURCE_TYPES and _schema_filter(config, schema, name):
            keys.add((name, object_type))
    return tuple(sorted(keys))


def _build(args, config: Mapping, values: Mapping[str, str], scratch: Path) -> None:
    if args.source_environment == args.target_environment:
        raise BaselineError("baseline source and target environments must differ")
    schemas = _selected_schemas(config, values, args.schema)
    sections = (
        "tables", "columns", "constraints", "indexes", "sequences", "views", "stored-code",
        "triggers", "identity-columns", "object-grants", "baseline-source", "baseline-settings", "baseline-views",
    )
    compared = sections[:-3]
    reference_specs = _reference_tables(config) if getattr(args, "data", False) else []
    if getattr(args, "data_dir", None) is not None and not getattr(args, "data", False):
        raise BaselineError("--data-dir requires --data")
    if getattr(args, "data", False) and not reference_specs:
        print("No referenceData.tables allow-list is configured; no baseline-data folder will be built")
    data_root = _scratch_path(
        getattr(args, "data_dir", None),
        ROOT / "scratch" / "baseline" / args.source_environment / "data",
    ) if reference_specs else None
    for schema in schemas:
        try:
            source_target = resolve_target(values, args.source_environment, "read", schema=schema)
            target_target = resolve_target(values, args.target_environment, "read", schema=schema)
        except TargetResolutionError as error:
            raise BaselineError(str(error)) from error
        source = capture_environment_catalog(
            source_target, args.source_environment, sections,
            scratch / f"sqlcl-{args.source_environment}-{schema}", baseline_capture=True,
            baseline_prefixes=config.get("prefixes", []), baseline_excluded=config.get("excludedObjects", []),
        )
        target_sections = sections + (("baseline-data",) if reference_specs else ())
        target = capture_environment_catalog(
            target_target, args.target_environment, target_sections,
            scratch / f"sqlcl-{args.target_environment}-{schema}", baseline_capture=True,
            baseline_prefixes=config.get("prefixes", []), baseline_excluded=config.get("excludedObjects", []),
            baseline_data_tables=reference_specs,
        )
        _complete(source, sections)
        _complete(target, target_sections)
        report = compare_environment_catalogs(source, target, sections=compared, prefixes=config.get("prefixes", []))
        if report.get("exit_code") == 2:
            raise BaselineError("compare-env returned incomplete evidence; no migration folder was generated")
        keys = _snapshot_keys(source, config, source_target.schema)
        if keys:
            _inventory, source_snapshot = capture_inventory_snapshot_with_retries(
                source_target, keys, scratch / f"snapshot-{args.source_environment}-{schema}",
            )
        else:
            source_snapshot = SchemaSnapshot(source.get("identity", {}), {}, {}, {}, "", "")
        structure_steps, structure_checks = _structure_bundle(config, source_target.schema, target_target.schema, source, target, source_snapshot)
        grants_steps, grants_checks = _grant_bundle(config, source_target.schema, target_target.schema, source, target)
        code_steps, code_checks = _code_bundle(config, source_target.schema, target_target.schema, source, target)
        bundles = (
            ("baseline-structure", "Structure delta generated from complete source and target catalog observations.", structure_steps, structure_checks),
            ("baseline-grants", "Grouped additive object grants generated from complete source and target grant observations.", grants_steps, grants_checks),
            ("baseline-code", "Exact stored source, per-unit compiler settings, and name-based source checks.", code_steps, code_checks),
        )
        data_bundle = None
        if reference_specs:
            assert data_root is not None
            source_data = _read_exported_reference_data(data_root, reference_specs, args.source_environment, source_target.schema)
            target_data = _reference_data_tables(target, reference_specs, args.target_environment, target_target.schema)
            data_bundle = _reference_data_bundle(
                config, source_target.schema, target_target.schema, source, target, source_data, target_data,
            )
            data_steps, data_checks, data_differences = data_bundle
            if data_differences:
                print(f"Reference-data target differences for {schema} (preconditions make them visible):")
                for difference in data_differences:
                    print(f"  {difference}")
            bundles += ((
                "baseline-data",
                "Natural-key-guarded reference-data inserts with foreign keys resolved from target labels; existing rows are never updated.",
                data_steps,
                data_checks,
            ),)
        for family, note, steps, checks in bundles:
            if not steps:
                print(f"No {family} changes for {schema}; no migration folder created")
                continue
            readme = (
                f"# {family.replace('-', ' ').title()}\n\n{note}\n\n"
                f"Source: {args.source_environment} / {source_target.schema}. Target: {args.target_environment} / {target_target.schema}.\n"
                "Review generated SQL and checks before applying this migration."
            )
            if family == "baseline-data":
                readme += (
                    "\n\nUse the ordinary `migrate <folder> --env <env> --rehearse` command to run the DML in one transaction and roll it back. "
                    "The rehearsal skips DDL, including `START WITH LIMIT VALUE`; review the identity step separately.\n"
                )
                if data_differences:
                    readme += "\n## Target precondition differences\n\n" + "\n".join(f"- {item}" for item in data_differences) + "\n"
            _build_migration_folder(ROOT, target_target.schema, family, _migration_bytes(steps, checks, readme))


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "filter-ords":
            _filter_ords(args)
            return 0
        values = os.environ if environ is None else environ
        config = load_config(ROOT / "baseline.json")
        schemas = _selected_schemas(config, values, getattr(args, "schema", ()))
        if args.command == "export-source":
            scratch = _scratch_path(args.scratch, ROOT / "scratch" / "baseline")
            _export_source(config, values, args.source_environment, scratch, schemas)
        elif args.command == "export-grants":
            scratch = _scratch_path(args.scratch, ROOT / "scratch" / "baseline")
            _export_grants(config, values, args.source_environment, scratch, schemas)
        elif args.command == "export-data":
            scratch = _scratch_path(args.scratch, ROOT / "scratch" / "baseline")
            _export_data(config, values, args.source_environment, scratch, schemas)
        else:
            scratch = _scratch_path(args.scratch, ROOT / "scratch" / "baseline" / "build")
            _build(args, config, values, scratch)
        return 0
    except SystemExit as error:
        return int(error.code or 0)
    except (BaselineError, CatalogError, OSError, RuntimeError, ValueError) as error:
        print(f"baseline error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
