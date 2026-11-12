"""Reference-data readers and the data bundle generator, moved out of scripts/baseline_build.py.

scripts/baseline_build.py keeps the build entry point and the migration folder writers. Names that stay in
scripts/baseline.py are read as baseline.<name> at call time, so patches on scripts.baseline still reach this
code. baseline_build re-exports the moved names that baseline, tests and other scripts reference.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .baseline_sql import _check, _q_literal, _safe_check_slug, _sql_identifier, _sql_literal
from .schema_catalog import REQUIRED_IDENTITY, same_database_scope


DATA_STEP_STATEMENT_LIMIT = 100


DATA_STEP_BYTE_LIMIT = 50_000


DATA_TEXT_CHUNK_CHARS = 32


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)


_ISO_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\Z",
    re.ASCII,
)


_REFERENCE_TIMESTAMP_TYPE_RE = re.compile(
    r"TIMESTAMP(?:\(([0-9])\))?( WITH(?: LOCAL)? TIME ZONE)?\Z",
    re.ASCII,
)


def _reference_temporal_type(data_type: str) -> str | None:
    """Return a canonical supported DATE/TIMESTAMP spelling, or None."""
    normalized = str(data_type).upper()
    if normalized == "DATE":
        return "DATE"
    match = _REFERENCE_TIMESTAMP_TYPE_RE.fullmatch(normalized)
    if match is None:
        return None
    return "TIMESTAMP" + (match.group(2) or "")


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
    temporal_type = _reference_temporal_type(data_type)
    if isinstance(value, bool):
        raise baseline.BaselineError(f"Oracle reference-data column {column.get('name', '?')} returned a JSON boolean")
    if data_type in {"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "DECIMAL", "INTEGER"}:
        if not isinstance(value, (int, float, Decimal)):
            raise baseline.BaselineError(f"numeric reference-data column {column.get('name', '?')} returned non-numeric data")
        try:
            number = Decimal(str(value))
        except InvalidOperation as error:
            raise baseline.BaselineError(f"numeric reference-data column {column.get('name', '?')} returned an invalid number") from error
        if not number.is_finite():
            raise baseline.BaselineError(f"numeric reference-data column {column.get('name', '?')} returned a non-finite number")
        return format(number, "f")
    if temporal_type is not None:
        if not isinstance(value, str):
            raise baseline.BaselineError(f"date reference-data column {column.get('name', '?')} returned non-text data")
        if _ISO_DATE_RE.fullmatch(value):
            try:
                dt.date.fromisoformat(value)
            except ValueError as error:
                raise baseline.BaselineError(f"invalid ISO date in reference-data column {column.get('name', '?')}: {value}") from error
            if temporal_type == "DATE":
                return f"TO_DATE({_sql_literal(value)}, 'YYYY-MM-DD')"
            if temporal_type == "TIMESTAMP WITH TIME ZONE":
                raise baseline.BaselineError(f"timestamp-with-time-zone column {column.get('name', '?')} has no timezone")
            timestamp_text = value + "T00:00:00"
            model = 'YYYY-MM-DD"T"HH24:MI:SS'
        elif _ISO_TIMESTAMP_RE.fullmatch(value):
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                # Python 3.10 accepts only 3- or 6-digit fractions in
                # fromisoformat(), while Oracle timestamp text may use any
                # fractional width. Validate a six-digit parse copy and keep
                # the original fractional text for the generated SQL.
                parseable = re.sub(
                    r"(?<=:\d{2})\.(\d+)",
                    lambda match: "." + match.group(1)[:6].ljust(6, "0"),
                    normalized,
                )
                parsed = dt.datetime.fromisoformat(parseable)
            except ValueError as error:
                raise baseline.BaselineError(f"invalid ISO timestamp in reference-data column {column.get('name', '?')}: {value}") from error
            has_zone = parsed.tzinfo is not None
            if temporal_type == "DATE":
                if has_zone or "." in value:
                    raise baseline.BaselineError(f"Oracle DATE column {column.get('name', '?')} cannot preserve timezone or fractional seconds")
                return f"TO_DATE({_sql_literal(value)}, 'YYYY-MM-DD\"T\"HH24:MI:SS')"
            if temporal_type == "TIMESTAMP WITH TIME ZONE":
                if not has_zone:
                    raise baseline.BaselineError(f"timestamp-with-time-zone column {column.get('name', '?')} has no timezone")
                timestamp_text = normalized
                model = 'YYYY-MM-DD"T"HH24:MI:SS' + (".FF" if "." in normalized else "") + "TZH:TZM"
                return f"TO_TIMESTAMP_TZ({_sql_literal(timestamp_text)}, {_sql_literal(model)})"
            # Keep the existing plain TIMESTAMP behavior for WITH LOCAL TIME ZONE;
            # transfer semantics still depend on the live serialization/session timezone.
            if has_zone:
                raise baseline.BaselineError(f"timestamp column {column.get('name', '?')} cannot preserve a timezone")
            timestamp_text = value
            model = 'YYYY-MM-DD"T"HH24:MI:SS' + (".FF" if "." in value else "")
        else:
            raise baseline.BaselineError(f"date column {column.get('name', '?')} requires a full valid ISO date or timestamp")
        return f"TO_TIMESTAMP({_sql_literal(timestamp_text)}, {_sql_literal(model)})"
    if data_type in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "CLOB", "NCLOB", "LONG"}:
        if not isinstance(value, str):
            raise baseline.BaselineError(f"text reference-data column {column.get('name', '?')} returned non-text data")
        return _text_sql_literal(value)
    raise baseline.BaselineError(f"unsupported reference-data column type {data_type!r} for {column.get('name', '?')}")


def _check_sql_value(value: object, column: Mapping) -> str:
    """Format check literals without q-quotes, which the migration checker forbids."""
    data_type = str(column.get("data_type", "")).upper()
    if data_type.startswith(("DATE", "TIMESTAMP")):
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
            pieces.extend(("CHR(13)", "CHR(10)"))
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
            raise baseline.BaselineError("baseline-data catalog row is malformed")
        name = row["name"]
        if name in result:
            raise baseline.BaselineError(f"baseline-data catalog contains duplicate table {name}")
        specification = next((item for item in config_tables if item["name"] == name), None)
        result[name] = baseline._validate_reference_capture(row, specification, environment, schema)
    if set(result) != {table["name"] for table in config_tables}:
        raise baseline.BaselineError("baseline-data target capture did not include every configured reference table")
    return result


def _read_exported_reference_data(
    data_root: Path,
    config_tables: Sequence[Mapping],
    environment: str,
    schema: str,
    current_source_identity: Mapping,
) -> dict[str, dict]:
    schema_dir = data_root / schema
    baseline._reject_symlink_components(schema_dir)
    for marker_name, state in (
        (baseline.DATA_EXPORT_LOCK_MARKER, "locked or in progress"),
        (baseline.DATA_EXPORT_INCOMPLETE_MARKER, "incomplete"),
    ):
        marker_path = schema_dir / marker_name
        marker_info = baseline._regular_file_info(marker_path, "baseline data export marker", missing_ok=True)
        if marker_info is not None:
            raise baseline.BaselineError(f"reference-data export is {state}; cannot use {schema_dir}")

    manifest_path = schema_dir / "manifest.json"
    manifest, _manifest_bytes = baseline._read_json_object(manifest_path, "baseline data manifest")
    manifest_fields = {
        "schemaVersion", "environment", "schema", "sourceIdentity", "tableCount", "rowCount", "tables", "complete",
    }
    if set(manifest) != manifest_fields or manifest.get("schemaVersion") != baseline.DATA_EXPORT_MANIFEST_VERSION:
        raise baseline.BaselineError("reference-data manifest is invalid or uses an unsupported legacy format")
    if manifest.get("complete") is not True:
        raise baseline.BaselineError("reference-data manifest is incomplete")
    if manifest.get("environment") != environment or manifest.get("schema") != schema:
        raise baseline.BaselineError(f"reference-data manifest identity does not match {environment}/{schema}")

    manifest_identity = manifest.get("sourceIdentity")
    if (
        not isinstance(manifest_identity, Mapping)
        or set(manifest_identity) != set(REQUIRED_IDENTITY)
        or any(not isinstance(manifest_identity.get(field), str) for field in REQUIRED_IDENTITY)
    ):
        raise baseline.BaselineError("reference-data manifest source identity is invalid or incomplete")
    expected_identity = baseline._source_identity(current_source_identity, "current live source")
    if (
        not same_database_scope(manifest_identity, expected_identity)
        or str(manifest_identity["session_user"]).upper() != expected_identity["session_user"].upper()
    ):
        raise baseline.BaselineError("reference-data manifest source identity does not match the current live source")

    records = manifest.get("tables")
    if not isinstance(records, list):
        raise baseline.BaselineError("reference-data manifest table inventory is invalid")
    records_by_name: dict[str, Mapping] = {}
    for record in records:
        if (
            not isinstance(record, Mapping)
            or set(record) != {"name", "file", "sha256", "rowCount"}
            or not isinstance(record.get("name"), str)
            or record["name"] in records_by_name
        ):
            raise baseline.BaselineError("reference-data manifest contains a malformed or duplicate table record")
        records_by_name[record["name"]] = record
    expected_names = {str(table.get("name", "")) for table in config_tables}
    if set(records_by_name) != expected_names:
        raise baseline.BaselineError("reference-data manifest does not match the configured table allow-list")
    if type(manifest.get("tableCount")) is not int or manifest["tableCount"] != len(records):
        raise baseline.BaselineError("reference-data manifest table total is invalid")

    result: dict[str, dict] = {}
    observed_rows = 0
    for specification in config_tables:
        name = specification.get("name")
        if not isinstance(name, str) or baseline.SCHEMA_RE.fullmatch(name) is None:
            raise baseline.BaselineError("configured reference-data table name is unsafe")
        record = records_by_name[name]
        expected_file = f"{name}.json"
        if (
            record.get("file") != expected_file
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"], re.ASCII) is None
            or type(record.get("rowCount")) is not int
            or record["rowCount"] < 0
        ):
            raise baseline.BaselineError(f"reference-data manifest metadata is invalid for {name}")
        path = schema_dir / expected_file
        value, contents = baseline._read_json_object(path, f"reference-data export for {name}", parse_decimal=True)
        if hashlib.sha256(contents).hexdigest() != record["sha256"]:
            raise baseline.BaselineError(f"reference-data export is stale or mismatched for {name}: manifest checksum differs")
        if value.get("environment") != environment or value.get("schema") != schema or value.get("table") != name:
            raise baseline.BaselineError(f"reference-data export identity does not match {environment}/{schema}/{name}")
        validated = baseline._validate_reference_capture(value, specification, environment, schema)
        if validated["rowCount"] != record["rowCount"]:
            raise baseline.BaselineError(f"reference-data export row total does not match its manifest for {name}")
        observed_rows += validated["rowCount"]
        result[name] = validated

    if type(manifest.get("rowCount")) is not int or manifest["rowCount"] != observed_rows:
        raise baseline.BaselineError("reference-data manifest row total is invalid")
    for marker_name, state in (
        (baseline.DATA_EXPORT_LOCK_MARKER, "locked or in progress"),
        (baseline.DATA_EXPORT_INCOMPLETE_MARKER, "incomplete"),
    ):
        marker_path = schema_dir / marker_name
        marker_info = baseline._regular_file_info(marker_path, "baseline data export marker", missing_ok=True)
        if marker_info is not None:
            raise baseline.BaselineError(f"reference-data export became {state} while it was being read; cannot use {schema_dir}")
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
    specifications = {table["name"]: table for table in baseline._reference_tables(config)}
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
            raise baseline.BaselineError(f"target table {name} is missing one or more exported reference-data columns")

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
            raise baseline.BaselineError(f"reference foreign key {constraint.get('name', '?')} has malformed column mapping")
        if any(not isinstance(item, str) for item in child_columns + parent_columns):
            raise baseline.BaselineError(f"reference foreign key {constraint.get('name', '?')} has non-text columns")
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
            raise baseline.BaselineError(
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
                raise baseline.BaselineError(f"reference-data natural key for {table_name} is not present in {side} captured columns")

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
        for side, data_by_table, _fk_map, _expression_map in (
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
                    raise baseline.BaselineError(
                        f"foreign key {relationship['name']} in {side} {child} is partially NULL; "
                        "its parent cannot be resolved safely by label"
                    )
                parents = parent_key_index.get(foreign_values, [])
                if len(parents) != 1:
                    raise baseline.BaselineError(
                        f"foreign key {relationship['name']} in {side} {child} cannot resolve its {parent} parent id "
                        f"{foreign_values!r} to exactly one row"
                    )
                parent_row = parents[0]
                label = tuple(_data_tuple(parent_row[column]) for column in parent_spec["labelColumns"])
                if any(value is None for value in label):
                    raise baseline.BaselineError(f"foreign key {relationship['name']} has a NULL {parent} label")
                if side == "source" and len(label_indexes[("source", parent)].get(label, [])) != 1:
                    raise baseline.BaselineError(f"foreign key {relationship['name']} source parent label {label!r} is duplicated in {parent}")
                target_label_matches = label_indexes[("target", parent)].get(label, [])
                if side == "source" and len(target_label_matches) != 1:
                    reason = "missing" if not target_label_matches else "ambiguous or duplicated"
                    raise baseline.BaselineError(
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
                raise baseline.BaselineError(f"reference-data natural key is duplicated in source table {table_name}: {key!r}")
            existing_indices = target_by_key.get(key, [])
            if len(existing_indices) > 1:
                raise baseline.BaselineError(f"reference-data natural key is ambiguous in target table {table_name}: {key!r}")
            source_columns = column_maps[table_name]
            identity = specification["identity"]
            if identity and identity["generationType"] == "ALWAYS" and identity["column"] in specification["keyColumns"]:
                raise baseline.BaselineError(f"ALWAYS identity column {identity['column']} cannot be a natural key for {table_name}")

            target_columns = target_column_maps[table_name]
            label_key = tuple(_data_tuple(source_row[column]) for column in specification["labelColumns"])
            label_matches = label_indexes[("target", table_name)].get(label_key, [])
            if len(label_matches) > 1:
                raise baseline.BaselineError(f"reference-data label {label_key!r} is ambiguous or duplicated in target table {table_name}")
            def expression_for(column: str, *, for_check: bool = False, _table_name=table_name, _row_index=row_index,
                               _source_row=source_row, _source_columns=source_columns, _target_columns=target_columns) -> str:
                relationship = source_sql_expressions.get((_table_name, _row_index, column))
                if relationship is not None:
                    return relationship["checkSql"] if for_check else relationship["sql"]
                return _check_sql_value(_source_row[column], _target_columns[column]) if for_check else format_sql_value(_source_row[column], _source_columns[column])

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
            raise baseline.BaselineError("one reference-data insert exceeds the 50000-byte migration step limit")
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


def _reference_count_check(check_id: str, count_sql: str, expected_count: int) -> dict:
    if expected_count == 1:
        return _check(check_id, count_sql)
    if expected_count == 0:
        return _check(check_id, f"SELECT CASE WHEN ({count_sql}) = 0 THEN 1 ELSE 0 END FROM DUAL")
    raise baseline.BaselineError("reference-data checks support only zero or one natural-key match")


# Imported last: this module reads baseline.<name> only when it runs, so baseline can bind the names from the
# build modules at its own end without a load-time cycle.
from . import baseline
