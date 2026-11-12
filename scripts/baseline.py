"""Read configured environment baselines and generate migration folders."""

from __future__ import annotations

import argparse
import datetime as dt  # noqa: F401
import hashlib  # noqa: F401
import json
import os
import re
import shutil  # noqa: F401
import stat
import subprocess  # noqa: F401
import sys
import tempfile  # noqa: F401
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation  # noqa: F401
from pathlib import Path
from urllib.parse import quote

from .compare_schema import (
    BASELINE_INTERNAL_SECTIONS,
    capture_environment_catalog,
    compare_environment_catalogs,  # noqa: F401
)
from .db_targets import TargetResolutionError, resolve_target
from . import directory_install, migration_runtime  # noqa: F401
from .baseline_bundle import (
    BaselineComponent,  # noqa: F401
    combine_baseline_components,  # noqa: F401
    validate_bundle_name,
)
from .migration_manifest import (
    FOLDER_RE,  # noqa: F401
    load_migration,  # noqa: F401
    validate_check_query,  # noqa: F401
    validate_sql_only,  # noqa: F401
    validate_staged_migration,  # noqa: F401
)
from .migration_revision import inspect_migration_lock  # noqa: F401
from .path_utils import relative_to_root  # noqa: F401
from .schema_catalog import (
    CatalogError,
    ObjectKey,  # noqa: F401
    SchemaSnapshot,  # noqa: F401
    capture_inventory_snapshot_with_retries,  # noqa: F401
    REQUIRED_IDENTITY,
    same_database_scope,
)


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ("dev", "staging", "prod")
SOURCE_TYPES = frozenset({"PACKAGE", "PACKAGE BODY", "TRIGGER", "FUNCTION", "PROCEDURE", "TYPE", "TYPE BODY"})
SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
PREFIX_RE = re.compile(r"[A-Za-z0-9_$#]+\Z", re.ASCII)
DATA_PAGE_SIZE = 500
DATA_EXPORT_LOCK_MARKER = ".baseline-export.lock"
DATA_EXPORT_INCOMPLETE_MARKER = ".baseline-export.incomplete"
DATA_EXPORT_MANIFEST_VERSION = 2


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
    build.add_argument("--bundle", help="combine structure, grants, and code changes into one migration folder")

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


def _reject_symlink_components(path: Path) -> None:
    """Refuse scratch inputs whose existing path crosses a symbolic link."""
    selected = path if path.is_absolute() else ROOT / path
    for candidate in (selected, *selected.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BaselineError(f"cannot inspect scratch path {candidate}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise BaselineError(f"refusing a symbolic link in baseline data path: {candidate}")


def _regular_file_info(path: Path, description: str, *, missing_ok: bool = False) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise BaselineError(f"{description} is missing: {path}") from None
    except OSError as error:
        raise BaselineError(f"cannot inspect {description} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BaselineError(f"{description} is not a regular file: {path}")
    return metadata


def _read_json_object(path: Path, description: str, *, parse_decimal: bool = False) -> tuple[dict, bytes]:
    _reject_symlink_components(path)
    _regular_file_info(path, description)
    try:
        contents = path.read_bytes()
        value = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            **({"parse_float": Decimal} if parse_decimal else {}),
        )
    except OSError as error:
        raise BaselineError(f"cannot read {description} {path}: {error}") from error
    except UnicodeError as error:
        raise BaselineError(f"{description} is not UTF-8: {path}") from error
    except json.JSONDecodeError as error:
        raise BaselineError(f"{description} is invalid JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise BaselineError(f"{description} must contain a JSON object: {path}")
    return value, contents


def _source_identity(identity: object, label: str) -> dict[str, str]:
    if not isinstance(identity, Mapping):
        raise BaselineError(f"{label} identity is missing")
    missing = [field for field in REQUIRED_IDENTITY if identity.get(field) is None or not str(identity.get(field)).strip()]
    if missing:
        raise BaselineError(f"{label} identity is incomplete: {', '.join(missing)}")
    return {field: str(identity[field]) for field in REQUIRED_IDENTITY}



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
        identity = catalog.get("identity")
        database_version = identity.get("database_version") if isinstance(identity, Mapping) else None
        if not isinstance(database_version, str) or not database_version.strip():
            raise BaselineError(f"baseline source export did not observe the database release for {schema}")
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
            "databaseVersion": database_version,
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


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "build" and args.bundle is not None:
            validate_bundle_name(args.bundle)
        if args.command == "filter-ords":
            _filter_ords(args)
            return 0
        values = os.environ if environ is None else environ
        config = load_config(ROOT / "baseline.json")
        schemas = _selected_schemas(config, values, getattr(args, "schema", ()))
        migration_runtime.require_migration_sqlcl_version()
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
    # Run the importable module, not this copy, so the moved subcommand code and the CLI share one set of classes.
    from scripts import baseline as _baseline

    raise SystemExit(_baseline.main())


# Imported last: baseline_data reads baseline.<name> at call time. The export entry point is re-exported here
# because main() calls it and the tests call it on scripts.baseline.
from .baseline_data import _export_data


# Imported last: baseline_ords_filter reads baseline.<name> at call time. The filter entry point is re-exported
# here because main() calls it.
from .baseline_ords_filter import _filter_ords


# Imported last: the build modules read baseline.<name> at call time. The names below are re-exported because main()
# calls _build, and tests and other scripts reference them on scripts.baseline. Each name comes from the module that
# defines it, so importing any of them first still works.
from .baseline_sql import (  # noqa: F401
    _sql_literal,
    _q_literal,
    _check,
    _safe_check_slug,
)
from .baseline_reference_data import (  # noqa: F401
    DATA_STEP_STATEMENT_LIMIT,
    DATA_STEP_BYTE_LIMIT,
    _text_sql_literal,
    format_sql_value,
    _check_sql_value,
    _reference_data_tables,
    _read_exported_reference_data,
    _reference_data_bundle,
)
from .baseline_bundles import (  # noqa: F401
    _replace_create_header,
    _table_column_type,
    _column_sql,
    _column_definition_check,
    _view_validity_check,
    _source_check_queries,
    _setting_row,
    _session_settings_sql,
    _needs_clob_assembly,
    _clob_source_step,
    _direct_source_step,
    _exact_source_step,
    _exact_settings_check,
    _compile_all_step,
    _wrap_not_null,
    _sync_identity_sql,
    _sync_sequence_sql,
    _structure_bundle,
    _grant_bundle,
    _code_bundle,
    _migration_bytes,
)
from .baseline_build import (  # noqa: F401
    _write_migration_stage,
    _windows_system_tool,
    _windows_current_user_sid,
    _secure_windows_private_stage,
    _build_migration_folder,
    _build,
)
