#!/usr/bin/env python3
"""Focused structural preflight for feature-release checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import feature_manifest, migration_checks
from .feature_manifest import FeatureCheckpoint
from .migration_manifest import MigrationManifestError, QueryCheck, validate_check_query
from .schema_catalog import ObjectKey
from .validate_migration import statement_spans


STRUCTURAL_SECTIONS = (
    "tables", "columns", "constraints", "indexes", "triggers", "sequences", "synonyms", "views",
    "stored-code", "invalid-objects", "identity-columns", "object-grants", "feature-dependencies",
    "feature-synonym-access",
)
COMMON_TYPES = {
    "TABLE", "VIEW", "MATERIALIZED VIEW", "SEQUENCE", "SYNONYM", "PROCEDURE", "FUNCTION",
    "PACKAGE", "PACKAGE BODY", "TYPE", "TYPE BODY", "TRIGGER", "CLUSTER", "JAVA SOURCE",
    "JAVA CLASS", "LIBRARY", "OPERATOR", "INDEXTYPE",
}
IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z][A-Za-z0-9_$#]*)'
SYNONYM_RE = re.compile(
    rf"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(PUBLIC)\s+)?SYNONYM\s+"
    rf"({IDENT})(?:\s*\.\s*({IDENT}))?\s+FOR\s+({IDENT})(?:\s*\.\s*({IDENT}))?"
    rf"(?:\s*@\s*({IDENT}))?\s*\Z",
    re.I | re.ASCII,
)
GRANT_RE = re.compile(
    rf"\s*GRANT\s+(.+?)\s+ON\s+({IDENT})\s*\.\s*({IDENT})\s+TO\s+"
    rf"({IDENT}(?:\s*,\s*{IDENT})*)(?:\s+WITH\s+GRANT\s+OPTION)?\s*\Z",
    re.I | re.ASCII,
)


# A name that is already taken usually means this one-time DDL was applied before.
OCCUPIED_HINT = (
    " (one-time DDL: if this release was already applied, do not release it again; prepare a new revision)"
)


class StructuralPreflightError(ValueError):
    """The selected structural effects are unsafe or cannot be proven."""


@dataclass(frozen=True)
class SelectedScope:
    owner: str
    object_keys: tuple[ObjectKey, ...]
    prerequisite_keys: tuple[ObjectKey, ...]
    sections: tuple[str, ...]


@dataclass(frozen=True)
class StructuralPlan:
    scope: SelectedScope
    expected_before_sha256: str
    effects_sha256: str
    preconditions: tuple[QueryCheck, ...]
    postconditions: tuple[QueryCheck, ...]
    compile_units: tuple[tuple[str, str, str, bool], ...]
    prior_checkpoints: tuple[int, ...]
    operations: tuple[dict, ...] = ()
    prior_effects: tuple[dict, ...] = ()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _query(check_id: str, sql: str) -> QueryCheck:
    try:
        validate_check_query(sql)
    except MigrationManifestError as error:
        raise StructuralPreflightError(f"generated structural check is invalid: {error}") from error
    return QueryCheck(check_id, sql, 1)


def _ddl_operations(checkpoint: FeatureCheckpoint) -> tuple[dict, ...]:
    inputs = tuple(item for item in checkpoint.inputs if item.category == "ddl")
    if not inputs:
        return ()
    try:
        migration = feature_manifest._analysis_migration(inputs, checkpoint.schema)
        raw_operations = migration_checks.analyze_batch((migration,), checkpoint.schema)
    except (MigrationManifestError, migration_checks.MigrationAnalysisError, UnicodeError, ValueError) as error:
        raise StructuralPreflightError(f"selected DDL cannot be analyzed safely: {error}") from error

    by_file = {f"{index:03d}-{Path(item.path).name}": item for index, item in enumerate(inputs, start=1)}
    operations: list[dict] = []
    for raw in raw_operations:
        operation = dict(raw)
        if operation.get("supported") is True:
            operations.append(operation)
            continue
        item = by_file.get(operation.get("file"))
        statement_index = operation.get("statement_index")
        if item is None or type(statement_index) is not int:
            raise StructuralPreflightError("selected DDL operation has no source mapping")
        try:
            spans = statement_spans(item.source.decode("utf-8"))
            start, end = spans[statement_index - 1]
            statement = item.source.decode("utf-8")[start:end].strip()
        except (UnicodeError, IndexError, ValueError) as error:
            raise StructuralPreflightError(f"selected DDL statement could not be mapped: {error}") from error
        grant = GRANT_RE.fullmatch(statement)
        if operation.get("kind") == "REVIEWED_OPERATION" and grant is not None:
            static = feature_manifest._static_grant_object(statement, checkpoint.schema)
            if static is None:
                raise StructuralPreflightError("direct object grant could not be scoped safely")
            privileges = grant.group(1).strip().upper()
            grantees = tuple(feature_manifest._unquote_identifier(value.strip()) for value in grant.group(4).split(","))
            operations.append({
                **operation, "kind": "GRANT_OBJECT", "supported": True,
                "owner": static[0], "name": static[1], "privileges": privileges,
                "grantees": grantees,
            })
            continue
        synonym = SYNONYM_RE.fullmatch(statement)
        if operation.get("kind") in {"OPAQUE", "UNKNOWN"} and synonym is not None:
            public = synonym.group(1) is not None
            first, second = synonym.group(2), synonym.group(3)
            synonym_owner = "PUBLIC" if public else (feature_manifest._unquote_identifier(first) if second else checkpoint.schema)
            synonym_name = feature_manifest._unquote_identifier(second if second else first)
            target_first, target_second, db_link = synonym.group(4), synonym.group(5), synonym.group(6)
            target_owner = feature_manifest._unquote_identifier(target_second and target_first or checkpoint.schema)
            target_name = feature_manifest._unquote_identifier(target_second or target_first)
            if db_link:
                raise StructuralPreflightError("synonym dependency through a database link is outside selected structural coverage")
            if synonym_owner not in {checkpoint.schema, "PUBLIC"}:
                raise StructuralPreflightError("synonym owner is outside the selected schema scope")
            if target_owner != checkpoint.schema:
                raise StructuralPreflightError("external synonym target access is not provable from selected structural coverage")
            operations.append({
                **operation, "kind": "CREATE_SYNONYM", "supported": True,
                "owner": synonym_owner, "name": synonym_name,
                "target_owner": target_owner, "target_name": target_name,
            })
            continue
        reason = operation.get("reason", "operation is outside the structural analyzer")
        raise StructuralPreflightError(f"unsupported structural dependency/effect in {operation.get('file')}: {reason}")
    return tuple(operations)


def _effect_record(operation: Mapping) -> dict:
    fields = (
        "kind", "owner", "name", "object_type", "table", "columns", "dependencies", "replace",
        "unique", "bitmap", "target_owner", "target_name", "privileges", "grantees",
    )
    return {field: operation[field] for field in fields if field in operation}


def _keys_for(checkpoint: FeatureCheckpoint, operations: Sequence[Mapping]) -> tuple[tuple[ObjectKey, ...], tuple[ObjectKey, ...]]:
    objects: set[ObjectKey] = set()
    prerequisites: set[ObjectKey] = set()
    owner = checkpoint.schema
    for operation in operations:
        kind = operation.get("kind")
        name = operation.get("name")
        if isinstance(name, str):
            if kind == "CREATE_INDEX":
                objects.add(ObjectKey(owner, name, "INDEX"))
            elif kind == "CREATE_SYNONYM":
                objects.add(ObjectKey(str(operation.get("owner", owner)), name, "SYNONYM"))
            elif kind == "ALTER_ADD_COLUMN":
                objects.add(ObjectKey(owner, str(operation.get("table", name)), "TABLE"))
            elif kind == "GRANT_OBJECT":
                prerequisites.add(ObjectKey(owner, name, "TABLE"))
                prerequisites.add(ObjectKey(owner, name, "VIEW"))
            else:
                object_type = operation.get("object_type")
                if isinstance(object_type, str):
                    objects.add(ObjectKey(owner, name, object_type))
        table = operation.get("table")
        if isinstance(table, str):
            prerequisites.add(ObjectKey(owner, table, "TABLE"))
        if kind == "CREATE_VIEW":
            for dependency_owner, dependency_name in operation.get("dependencies", ()):
                for object_type in ("TABLE", "VIEW", "MATERIALIZED VIEW", "SYNONYM"):
                    prerequisites.add(ObjectKey(dependency_owner, dependency_name, object_type))
        if kind == "CREATE_SYNONYM":
            prerequisites.add(ObjectKey(str(operation.get("target_owner", owner)), str(operation.get("target_name")), "TABLE"))
            prerequisites.add(ObjectKey(str(operation.get("target_owner", owner)), str(operation.get("target_name")), "VIEW"))
    # Existing selected stored units are part of the guard even when no SQL DDL creates them.
    for item in checkpoint.inputs:
        if item.category == "stored-source" and item.unit is not None:
            unit_owner, object_type, name = item.unit
            objects.add(ObjectKey(unit_owner, name, object_type))
    return tuple(sorted(objects)), tuple(sorted(prerequisites))


def required_scope(checkpoint: FeatureCheckpoint) -> SelectedScope:
    """Derive exact catalog names from both selected effects and their references."""
    if not isinstance(checkpoint, FeatureCheckpoint):
        raise StructuralPreflightError("selected feature checkpoint is malformed")
    operations = _ddl_operations(checkpoint)
    object_keys, prerequisite_keys = _keys_for(checkpoint, operations)
    return SelectedScope(checkpoint.schema, object_keys, prerequisite_keys, STRUCTURAL_SECTIONS)


def _structural(state: Mapping) -> dict:
    raw = state.get("structural")
    coverage = state.get("coverage")
    if not isinstance(raw, Mapping) or not isinstance(coverage, Mapping):
        raise StructuralPreflightError("selected structural catalog coverage is missing")
    if coverage.get("selectedScopeComplete") is not True and coverage.get("ownerComplete") is not True:
        raise StructuralPreflightError("selected structural catalog coverage is incomplete")
    result = {}
    for section in STRUCTURAL_SECTIONS:
        rows = raw.get(section)
        if rows is None and section in {"feature-dependencies", "feature-synonym-access"}:
            rows = []
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise StructuralPreflightError(f"selected structural section {section} is incomplete")
        result[section] = [dict(row) for row in rows]
    return result

def _live_catalog(structural: Mapping) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    common: dict[str, set[str]] = {}
    indexes: dict[str, set[str]] = {}
    columns: dict[str, set[str]] = {}
    for section, object_type in (("tables", "TABLE"), ("views", "VIEW"), ("sequences", "SEQUENCE"), ("stored-code", None)):
        for row in structural[section]:
            name = row.get("name")
            if not isinstance(name, str):
                raise StructuralPreflightError(f"selected {section} row has no object name")
            actual_type = object_type or str(row.get("type", "")).upper()
            if actual_type:
                common.setdefault(name, set()).add(actual_type)
    for row in structural["synonyms"]:
        owner, name = row.get("owner"), row.get("name")
        if owner in {"PUBLIC", "public"} or owner:
            if isinstance(name, str):
                common.setdefault(name, set()).add("SYNONYM")
    for row in structural["indexes"]:
        name, table = row.get("name"), row.get("table_name")
        if isinstance(name, str):
            indexes.setdefault(name, set()).add(str(table or ""))
            common.setdefault(name, set()).add("INDEX")
    for row in structural["columns"]:
        table, name = row.get("table_name"), row.get("name")
        if isinstance(table, str) and isinstance(name, str):
            columns.setdefault(table, set()).add(name)
    for row in structural["invalid-objects"]:
        name, object_type = row.get("name"), row.get("type")
        if isinstance(name, str) and isinstance(object_type, str):
            common.setdefault(name, set()).add(object_type.upper())
    return common, indexes, columns


def _prior_operations(earlier_effects: Sequence[Mapping], owner: str) -> tuple[list[dict], tuple[int, ...]]:
    operations: list[dict] = []
    checkpoints: set[int] = set()
    for record in earlier_effects:
        if not isinstance(record, Mapping) or record.get("schema") != owner:
            continue
        checkpoint = record.get("checkpoint")
        if type(checkpoint) is not int or checkpoint < 1:
            raise StructuralPreflightError("earlier selected structural effect has an invalid checkpoint number")
        plan = record.get("effects")
        digest = record.get("effectsSha256")
        if isinstance(plan, StructuralPlan):
            rows = [_effect_record(item) for item in plan.operations]
            expected = plan.effects_sha256
        elif isinstance(plan, Mapping):
            rows = plan.get("operations")
            expected = _digest(rows) if isinstance(rows, list) else None
        else:
            raise StructuralPreflightError("earlier selected structural effect record is malformed")
        if not isinstance(rows, list) or digest != expected:
            raise StructuralPreflightError("earlier selected structural effect digest does not match")
        operations.extend({**dict(row), "_checkpoint": checkpoint} for row in rows)
        checkpoints.add(checkpoint)
    return operations, tuple(sorted(checkpoints))


def _simulated_catalog(state_structural: Mapping, operations: Sequence[Mapping]) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    common, indexes, columns = _live_catalog(state_structural)
    for operation in operations:
        kind, name = operation.get("kind"), operation.get("name")
        if not isinstance(name, str):
            continue
        if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE"}:
            common.setdefault(name, set()).add({"CREATE_TABLE": "TABLE", "CREATE_VIEW": "VIEW", "CREATE_SEQUENCE": "SEQUENCE"}[kind])
            if kind == "CREATE_TABLE":
                columns[name] = {str(item) for item in operation.get("columns", ())}
        elif kind == "CREATE_INDEX":
            indexes[name] = {str(operation.get("table", ""))}
            common.setdefault(name, set()).add("INDEX")
        elif kind == "ALTER_ADD_COLUMN":
            table = str(operation.get("table", ""))
            columns.setdefault(table, set()).update(str(item) for item in operation.get("columns", ()))
        elif kind == "CREATE_SYNONYM":
            if operation.get("owner") in {"PUBLIC", "public"}:
                common.setdefault(name, set()).add("SYNONYM")
            else:
                common.setdefault(name, set()).add("SYNONYM")
    return common, indexes, columns


def _object_present(common: Mapping[str, set[str]], name: str, types: Sequence[str] | None = None) -> bool:
    existing = common.get(name, set())
    return bool(existing if types is None else existing.intersection(types))


def _common_ddl_check(check_id: str, name: str, *, expected_present: bool = False) -> QueryCheck:
    object_types = ",".join(_literal(item) for item in sorted(COMMON_TYPES))
    exists = (
        "EXISTS (SELECT 1 FROM ALL_OBJECTS WHERE OWNER = :target_schema "
        f"AND OBJECT_NAME = {_literal(name)} AND OBJECT_TYPE IN ({object_types}))"
    )
    condition = exists if expected_present else f"NOT {exists}"
    return _query(check_id, f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END FROM DUAL")


def _index_before_check(check_id: str, name: str, table: str, columns: Sequence[str], *, table_present: bool) -> QueryCheck:
    column_predicate = " AND ".join(
        f"EXISTS (SELECT 1 FROM ALL_TAB_COLUMNS C{i} WHERE C{i}.OWNER = :target_schema "
        f"AND C{i}.TABLE_NAME = {_literal(table)} AND C{i}.COLUMN_NAME = {_literal(column)})"
        for i, column in enumerate(columns, 1)
    ) or "1 = 1"
    table_predicate = (
        f"EXISTS (SELECT 1 FROM ALL_TABLES T WHERE T.OWNER = :target_schema AND T.TABLE_NAME = {_literal(table)})"
        if table_present else "1 = 1"
    )
    sql = (
        "SELECT CASE WHEN "
        f"{table_predicate} AND ({column_predicate}) AND NOT EXISTS "
        "(SELECT 1 FROM ALL_INDEXES I WHERE I.OWNER = :target_schema "
        f"AND I.INDEX_NAME = {_literal(name)}) THEN 1 ELSE 0 END FROM DUAL"
    )
    return _query(check_id, sql)


def _dependency_check(check_id: str, name: str) -> QueryCheck:
    direct = (
        "EXISTS (SELECT 1 FROM ALL_OBJECTS O WHERE O.OWNER = :target_schema "
        f"AND O.OBJECT_NAME = {_literal(name)} AND O.OBJECT_TYPE IN ('TABLE','VIEW','MATERIALIZED VIEW'))"
    )
    private_synonym = (
        "EXISTS (SELECT 1 FROM ALL_SYNONYMS S WHERE S.OWNER = :target_schema "
        f"AND S.SYNONYM_NAME = {_literal(name)} AND S.DB_LINK IS NULL AND ("
        "(S.TABLE_OWNER = :target_schema AND EXISTS (SELECT 1 FROM ALL_OBJECTS O "
        "WHERE O.OWNER = S.TABLE_OWNER AND O.OBJECT_NAME = S.TABLE_NAME "
        "AND O.OBJECT_TYPE IN ('TABLE','VIEW','MATERIALIZED VIEW'))) "
        "OR EXISTS (SELECT 1 FROM ALL_TAB_PRIVS P WHERE P.TABLE_SCHEMA = S.TABLE_OWNER "
        "AND P.TABLE_NAME = S.TABLE_NAME AND P.GRANTEE IN (:target_schema, 'PUBLIC') AND P.PRIVILEGE = 'SELECT')))"
    )
    public_synonym = (
        "NOT EXISTS (SELECT 1 FROM ALL_SYNONYMS S WHERE S.OWNER = :target_schema "
        f"AND S.SYNONYM_NAME = {_literal(name)}) AND EXISTS (SELECT 1 FROM ALL_SYNONYMS S "
        f"WHERE S.OWNER = 'PUBLIC' AND S.SYNONYM_NAME = {_literal(name)} AND S.DB_LINK IS NULL AND ("
        "(S.TABLE_OWNER = :target_schema AND EXISTS (SELECT 1 FROM ALL_OBJECTS O "
        "WHERE O.OWNER = S.TABLE_OWNER AND O.OBJECT_NAME = S.TABLE_NAME "
        "AND O.OBJECT_TYPE IN ('TABLE','VIEW','MATERIALIZED VIEW'))) "
        "OR EXISTS (SELECT 1 FROM ALL_TAB_PRIVS P WHERE P.TABLE_SCHEMA = S.TABLE_OWNER "
        "AND P.TABLE_NAME = S.TABLE_NAME AND P.GRANTEE IN (:target_schema, 'PUBLIC') AND P.PRIVILEGE = 'SELECT')))"
    )
    return _query(
        check_id,
        f"SELECT CASE WHEN {direct} OR {private_synonym} OR ({public_synonym}) THEN 1 ELSE 0 END FROM DUAL",
    )


def _effect_queries(
    operation: Mapping,
    index: int,
    owner: str,
    *,
    replace_existing_view: bool = False,
) -> tuple[list[QueryCheck], list[QueryCheck]]:
    kind, name = operation.get("kind"), operation.get("name")
    if not isinstance(name, str):
        return [], []
    before: list[QueryCheck] = []
    after: list[QueryCheck] = []
    if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE"}:
        if kind == "CREATE_VIEW" and replace_existing_view:
            before.append(_query(
                f"structural-before-{index:04d}",
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_OBJECTS O WHERE O.OWNER = :target_schema "
                f"AND O.OBJECT_NAME = {_literal(name)} AND O.OBJECT_TYPE = 'VIEW') "
                "AND NOT EXISTS (SELECT 1 FROM ALL_OBJECTS O WHERE O.OWNER = :target_schema "
                f"AND O.OBJECT_NAME = {_literal(name)} AND O.OBJECT_TYPE != 'VIEW') THEN 1 ELSE 0 END FROM DUAL",
            ))
        else:
            before.append(_common_ddl_check(f"structural-before-{index:04d}", name))
        if kind == "CREATE_TABLE":
            wanted = tuple(str(item) for item in operation.get("columns", ()))
            column_list = ",".join(_literal(item) for item in wanted) or "NULL"
            query = (
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_TABLES T WHERE T.OWNER = :target_schema "
                f"AND T.TABLE_NAME = {_literal(name)}) AND (SELECT COUNT(*) FROM ALL_TAB_COLUMNS C "
                f"WHERE C.OWNER = :target_schema AND C.TABLE_NAME = {_literal(name)} AND C.COLUMN_NAME IN ({column_list})) = {len(wanted)} "
                f"AND (SELECT COUNT(*) FROM ALL_TAB_COLUMNS C WHERE C.OWNER = :target_schema AND C.TABLE_NAME = {_literal(name)}) = {len(wanted)} "
                f"THEN 1 ELSE 0 END FROM DUAL"
            )
        elif kind == "CREATE_VIEW":
            query = (
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_OBJECTS O WHERE O.OWNER = :target_schema "
                f"AND O.OBJECT_NAME = {_literal(name)} AND O.OBJECT_TYPE = 'VIEW' AND O.STATUS = 'VALID') "
                "THEN 1 ELSE 0 END FROM DUAL"
            )
        else:
            query = (
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_SEQUENCES S WHERE S.SEQUENCE_OWNER = :target_schema "
                f"AND S.SEQUENCE_NAME = {_literal(name)}) THEN 1 ELSE 0 END FROM DUAL"
            )
        after.append(_query(f"structural-after-{index:04d}", query))
    elif kind == "CREATE_INDEX":
        table = str(operation.get("table", ""))
        columns = tuple(str(item) for item in operation.get("columns", ()))
        before.append(_index_before_check(f"structural-before-{index:04d}", name, table, columns, table_present=True))
        column_list = ",".join(_literal(item) for item in columns) or "NULL"
        unique_condition = "UNIQUE" if operation.get("unique") is True else "NONUNIQUE"
        ordered_columns = " AND ".join(
            f"EXISTS (SELECT 1 FROM ALL_IND_COLUMNS C{position} WHERE C{position}.INDEX_OWNER = :target_schema "
            f"AND C{position}.INDEX_NAME = {_literal(name)} AND C{position}.COLUMN_POSITION = {position} "
            f"AND C{position}.COLUMN_NAME = {_literal(column)})"
            for position, column in enumerate(columns, 1)
        ) or "1 = 1"
        query = (
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_INDEXES I WHERE I.OWNER = :target_schema "
            f"AND I.INDEX_NAME = {_literal(name)} AND I.TABLE_NAME = {_literal(table)} AND I.UNIQUENESS = {_literal(unique_condition)}) "
            f"AND (SELECT COUNT(*) FROM ALL_IND_COLUMNS C WHERE C.INDEX_OWNER = :target_schema AND C.INDEX_NAME = {_literal(name)} "
            f"AND C.COLUMN_NAME IN ({column_list})) = {len(columns)} "
            f"AND (SELECT COUNT(*) FROM ALL_IND_COLUMNS C WHERE C.INDEX_OWNER = :target_schema AND C.INDEX_NAME = {_literal(name)}) = {len(columns)} "
            f"AND ({ordered_columns}) THEN 1 ELSE 0 END FROM DUAL"
        )
        after.append(_query(f"structural-after-{index:04d}", query))
    elif kind == "ALTER_ADD_COLUMN":
        table = str(operation.get("table", name))
        columns = tuple(str(item) for item in operation.get("columns", ()))
        for ordinal, column in enumerate(columns, 1):
            before.append(_query(
                f"structural-before-{index:04d}-{ordinal:02d}",
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_TABLES T WHERE T.OWNER = :target_schema "
                f"AND T.TABLE_NAME = {_literal(table)}) AND NOT EXISTS (SELECT 1 FROM ALL_TAB_COLUMNS C "
                f"WHERE C.OWNER = :target_schema AND C.TABLE_NAME = {_literal(table)} AND C.COLUMN_NAME = {_literal(column)}) "
                "THEN 1 ELSE 0 END FROM DUAL",
            ))
            after.append(_query(
                f"structural-after-{index:04d}-{ordinal:02d}",
                "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_TAB_COLUMNS C WHERE C.OWNER = :target_schema "
                f"AND C.TABLE_NAME = {_literal(table)} AND C.COLUMN_NAME = {_literal(column)}) THEN 1 ELSE 0 END FROM DUAL",
            ))
    elif kind == "GRANT_OBJECT":
        grantees = tuple(operation.get("grantees", ()))
        privileges = str(operation.get("privileges", "")).split(",")
        for ordinal, grantee in enumerate(grantees, 1):
            for privilege_index, privilege in enumerate(privileges, 1):
                privilege = privilege.strip().upper()
                before.append(_query(
                    f"structural-before-{index:04d}-{ordinal:02d}-{privilege_index:02d}",
                    "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_OBJECTS O WHERE O.OWNER = :target_schema "
                    f"AND O.OBJECT_NAME = {_literal(name)}) AND NOT EXISTS (SELECT 1 FROM ALL_TAB_PRIVS P "
                    f"WHERE P.TABLE_SCHEMA = :target_schema AND P.TABLE_NAME = {_literal(name)} AND P.GRANTEE = {_literal(grantee)} "
                    f"AND P.PRIVILEGE = {_literal(privilege)}) THEN 1 ELSE 0 END FROM DUAL",
                ))
                after.append(_query(
                    f"structural-after-{index:04d}-{ordinal:02d}-{privilege_index:02d}",
                    "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_TAB_PRIVS P WHERE P.TABLE_SCHEMA = :target_schema "
                    f"AND P.TABLE_NAME = {_literal(name)} AND P.GRANTEE = {_literal(grantee)} "
                    f"AND P.PRIVILEGE = {_literal(privilege)}) THEN 1 ELSE 0 END FROM DUAL",
                ))
    elif kind == "CREATE_SYNONYM":
        synonym_owner = str(operation.get("owner", owner))
        target_owner = str(operation.get("target_owner", owner))
        target_name = str(operation.get("target_name", ""))
        before.append(_query(
            f"structural-before-{index:04d}",
            f"SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ALL_SYNONYMS S WHERE S.OWNER = {_literal(synonym_owner)} "
            f"AND S.SYNONYM_NAME = {_literal(name)}) THEN 1 ELSE 0 END FROM DUAL",
        ))
        after.append(_query(
            f"structural-after-{index:04d}",
            f"SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_SYNONYMS S WHERE S.OWNER = {_literal(synonym_owner)} "
            f"AND S.SYNONYM_NAME = {_literal(name)} AND S.TABLE_OWNER = {_literal(target_owner)} "
            f"AND S.TABLE_NAME = {_literal(target_name)} AND S.DB_LINK IS NULL) THEN 1 ELSE 0 END FROM DUAL",
        ))
    # Every selected effect is followed by a narrow invalid-dependent assertion.
    if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE", "CREATE_INDEX", "ALTER_ADD_COLUMN"}:
        reference_name = str(operation.get("table", name)) if kind in {"CREATE_INDEX", "ALTER_ADD_COLUMN"} else name
        after.append(_query(
            f"structural-dependent-{index:04d}",
            "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ALL_DEPENDENCIES D JOIN ALL_OBJECTS O "
            "ON O.OWNER = D.OWNER AND O.OBJECT_NAME = D.NAME AND O.OBJECT_TYPE = D.TYPE "
            "WHERE D.OWNER = :target_schema AND D.REFERENCED_OWNER = :target_schema "
            f"AND D.REFERENCED_NAME = {_literal(reference_name)} AND O.STATUS = 'INVALID') "
            "THEN 1 ELSE 0 END FROM DUAL",
        ))
    return before, after


def _dependencies(structural: Mapping, name: str, owner: str) -> list[dict]:
    return [
        dict(row) for row in structural["feature-dependencies"]
        if str(row.get("referenced_owner", "")).upper() == owner.upper()
        and str(row.get("referenced_name", "")) == name
    ]


def _scope_with_dependents(
    scope: SelectedScope,
    structural: Mapping,
    operations: Sequence[Mapping],
) -> SelectedScope:
    owner = scope.owner
    affected = {str(operation.get("name", "")) for operation in operations if operation.get("name")}
    for operation in operations:
        if operation.get("kind") in {"CREATE_INDEX", "ALTER_ADD_COLUMN"} and isinstance(operation.get("table"), str):
            affected.add(operation["table"])
    keys = set(scope.object_keys)
    for row in structural["feature-dependencies"]:
        name, object_type = row.get("name"), row.get("type")
        if (
            str(row.get("referenced_owner", "")).upper() == owner.upper()
            and row.get("referenced_name") in affected
            and isinstance(name, str) and isinstance(object_type, str)
        ):
            keys.add(ObjectKey(owner, name, object_type.upper()))
    return SelectedScope(
        owner, tuple(sorted(keys)), scope.prerequisite_keys, scope.sections,
    )


def _synonym_dependency_available(
    structural: Mapping,
    common: Mapping[str, set[str]],
    name: str,
    owner: str,
    staged_synonyms: Mapping[str, tuple[str, str]],
) -> bool:
    synonym_rows = structural["synonyms"]
    private = next((row for row in synonym_rows if str(row.get("owner", "")).upper() == owner.upper() and str(row.get("name", "")) == name), None)
    public = next((row for row in synonym_rows if str(row.get("owner", "")).upper() == "PUBLIC" and str(row.get("name", "")) == name), None)
    selected = private or public
    if selected is None:
        staged = staged_synonyms.get(name)
        if staged is None:
            return False
        target_owner, target_name = staged
    else:
        if str(selected.get("db_link", "") or "").strip():
            return False
        target_owner = str(selected.get("table_owner", "")).upper()
        target_name = str(selected.get("table_name", ""))
    if target_owner == owner.upper():
        target_types = common.get(target_name, set())
        return bool(target_types.intersection({"TABLE", "VIEW", "MATERIALIZED VIEW"}))
    return any(
        str(row.get("owner", "")).upper() == target_owner
        and str(row.get("object_name", "")) == target_name
        and str(row.get("grantee", "")).upper() in {owner.upper(), "PUBLIC"}
        and str(row.get("privilege", "")).upper() == "SELECT"
        for row in structural["feature-synonym-access"]
    )


def _dependency_is_available(
    structural: Mapping,
    common: Mapping[str, set[str]],
    name: str,
    owner: str,
    staged_synonyms: Mapping[str, tuple[str, str]],
) -> bool:
    object_types = common.get(name, set())
    if object_types.intersection({"TABLE", "VIEW", "MATERIALIZED VIEW"}):
        return True
    if "SYNONYM" in object_types:
        return _synonym_dependency_available(structural, common, name, owner, staged_synonyms)
    return False


def prepare_checkpoint(
    checkpoint: FeatureCheckpoint,
    state: Mapping,
    earlier_effects: Sequence[Mapping],
) -> StructuralPlan:
    """Prove the ordered selected DDL against exact selected catalog evidence."""
    if not isinstance(checkpoint, FeatureCheckpoint) or not isinstance(state, Mapping):
        raise StructuralPreflightError("selected checkpoint or catalog state is malformed")
    structural = _structural(state)
    operations = _ddl_operations(checkpoint)
    effect_rows = [_effect_record(operation) for operation in operations]
    prior_ops_with_checkpoint, _seen_prior_checkpoints = _prior_operations(earlier_effects, checkpoint.schema)
    prior_ops = [{key: value for key, value in row.items() if key != "_checkpoint"} for row in prior_ops_with_checkpoint]
    common, indexes, columns = _simulated_catalog(structural, prior_ops)
    staged_names: set[str] = set()
    staged_indexes: set[str] = set()
    staged_synonyms: dict[str, tuple[str, str]] = {}
    prior_names = {str(row.get("name", "")) for row in prior_ops}
    prior_effects = []
    for row in prior_ops_with_checkpoint:
        if not isinstance(row.get("name"), str):
            continue
        effect = {
            "name": row["name"], "kind": str(row.get("kind", "")),
            "table": str(row.get("table", "")), "checkpoint": row["_checkpoint"],
        }
        for field in ("columns", "grantees", "privileges"):
            if field in row:
                effect[field] = row[field]
        prior_effects.append(effect)
    prior_provider = {
        str(row.get("name", "")): row["_checkpoint"]
        for row in prior_ops_with_checkpoint if isinstance(row.get("name"), str)
    }
    preconditions: list[QueryCheck] = []
    postconditions: list[QueryCheck] = []

    # A selected exact catalog is not a complete ALL_OBJECTS inventory; only the
    # rows for names in this scope are used to decide these local effects.
    for row in structural["feature-dependencies"]:
        referenced_name = row.get("referenced_name")
        if not isinstance(referenced_name, str):
            continue
        if str(row.get("referenced_owner", "")).upper() == checkpoint.schema.upper():
            dependent_name, dependent_type = row.get("name"), row.get("type")
            if isinstance(dependent_name, str) and isinstance(dependent_type, str):
                if any(
                    op.get("name", "") == referenced_name
                    for op in operations if isinstance(op.get("name"), str)
                ) and any(
                    item.get("name", "") == dependent_name
                    and item.get("type", "").upper() == dependent_type.upper()
                    for item in structural["invalid-objects"]
                ):
                    raise StructuralPreflightError(f"required dependent {dependent_type} {dependent_name} is already invalid")

    for index, operation in enumerate(operations, 1):
        kind = operation.get("kind")
        name = str(operation.get("name", ""))
        replace_existing_view = False
        if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE"}:
            existing = common.get(name, set())
            replace_view = kind == "CREATE_VIEW" and operation.get("replace") is True and "VIEW" in existing and bool(checkpoint.preconditions)
            replace_existing_view = replace_view
            if name in staged_names or (existing and not replace_view):
                raise StructuralPreflightError(f"object namespace is occupied for {name}: {sorted(existing)}{OCCUPIED_HINT}")
            staged_names.add(name)
            object_type = {"CREATE_TABLE": "TABLE", "CREATE_VIEW": "VIEW", "CREATE_SEQUENCE": "SEQUENCE"}[kind]
            common.setdefault(name, set()).add(object_type)
            if kind == "CREATE_TABLE":
                declared_columns = {str(item) for item in operation.get("columns", ())}
                if not declared_columns:
                    raise StructuralPreflightError(f"CREATE TABLE {name} has no statically known columns")
                columns[name] = declared_columns
        elif kind == "CREATE_INDEX":
            if name in staged_indexes or name in indexes:
                raise StructuralPreflightError(f"index namespace is occupied for {name}{OCCUPIED_HINT}")
            table = str(operation.get("table", ""))
            indexed = {str(item) for item in operation.get("columns", ())}
            available = columns.get(table)
            if available is None:
                raise StructuralPreflightError(f"missing prerequisite table {table} for index {name}")
            missing = sorted(indexed - available)
            if missing:
                raise StructuralPreflightError(f"missing indexed column(s) for {name} on {table}: {', '.join(missing)}")
            staged_indexes.add(name)
            indexes[name] = {table}
        elif kind == "ALTER_ADD_COLUMN":
            table = str(operation.get("table", ""))
            available = columns.get(table)
            if available is None:
                raise StructuralPreflightError(f"missing prerequisite table {table} for ALTER TABLE")
            additions = {str(item) for item in operation.get("columns", ())}
            overlap = sorted(available.intersection(additions))
            if overlap:
                raise StructuralPreflightError(f"column namespace is occupied on {table}: {', '.join(overlap)}{OCCUPIED_HINT}")
            available.update(additions)
        elif kind == "CREATE_VIEW":
            pass
        elif kind == "GRANT_OBJECT":
            if not _object_present(common, name):
                raise StructuralPreflightError(f"missing direct grant target {name}")
        elif kind == "CREATE_SYNONYM":
            synonym_name = name
            target_name = str(operation.get("target_name", ""))
            target_owner = str(operation.get("target_owner", "")).upper()
            if target_owner != checkpoint.schema.upper() or not _object_present(common, target_name):
                raise StructuralPreflightError(f"missing synonym target {target_owner}.{target_name}")
            if synonym_name in staged_names or _object_present(common, synonym_name):
                raise StructuralPreflightError(f"synonym namespace is occupied for {synonym_name}{OCCUPIED_HINT}")
            staged_names.add(synonym_name)
            common.setdefault(synonym_name, set()).add("SYNONYM")
            staged_synonyms[synonym_name] = (target_owner, target_name)
        else:
            raise StructuralPreflightError(f"unsupported structural effect {kind}")

        if kind == "CREATE_VIEW":
            dependency_ordinal = 0
            for dependency_owner, dependency_name in operation.get("dependencies", ()):
                dep_owner, dep_name = str(dependency_owner).upper(), str(dependency_name)
                if dep_owner != checkpoint.schema.upper():
                    raise StructuralPreflightError(f"unsupported external view dependency {dep_owner}.{dep_name}")
                if dep_name == "DUAL":
                    continue
                if not _dependency_is_available(structural, common, dep_name, checkpoint.schema, staged_synonyms):
                    raise StructuralPreflightError(f"missing view dependency {dep_name} required by {name}")
                if dep_name not in staged_names and dep_name not in prior_provider:
                    dependency_ordinal += 1
                    preconditions.append(_dependency_check(
                        f"structural-dependency-{index:04d}-{dependency_ordinal:02d}", dep_name,
                    ))

        before_queries, after_queries = _effect_queries(
            operation, index, checkpoint.schema, replace_existing_view=replace_existing_view,
        )
        # Preconditions for same-checkpoint dependencies are evaluated inside the
        # write session before all payloads, so omit assertions that only become
        # true after an earlier payload in this checkpoint.
        if kind == "CREATE_INDEX" and str(operation.get("table", "")) in staged_names:
            before_queries = [query for query in before_queries if "ALL_TABLES" not in query.sql]
        if kind == "GRANT_OBJECT" and name in prior_names:
            pass
        elif kind == "GRANT_OBJECT" and name in staged_names:
            before_queries = []
        if kind == "CREATE_SYNONYM" and str(operation.get("target_name", "")) in staged_names:
            before_queries = []
        if kind == "CREATE_SYNONYM":
            target_name = str(operation.get("target_name", ""))
            if target_name in common and target_name not in staged_names and target_name not in prior_provider:
                preconditions.append(_dependency_check(f"structural-synonym-target-{index:04d}", target_name))
        preconditions.extend(before_queries)
        postconditions.extend(after_queries)

    required_prior_names: set[str] = set()
    for operation in operations:
        if operation.get("kind") in {"CREATE_INDEX", "ALTER_ADD_COLUMN"}:
            value = operation.get("table")
            if isinstance(value, str):
                required_prior_names.add(value)
        if operation.get("kind") == "GRANT_OBJECT" and isinstance(operation.get("name"), str):
            required_prior_names.add(operation["name"])
        if operation.get("kind") == "CREATE_SYNONYM" and isinstance(operation.get("target_name"), str):
            required_prior_names.add(operation["target_name"])
        if operation.get("kind") == "CREATE_VIEW":
            required_prior_names.update(
                str(name) for owner, name in operation.get("dependencies", ())
                if str(owner).upper() == checkpoint.schema.upper()
            )
    prior_checkpoints = tuple(sorted({
        prior_provider[name] for name in required_prior_names if name in prior_provider
    }))
    prior_effects = [row for row in prior_effects if row["checkpoint"] in prior_checkpoints]
    # A dependent created in a prior checkpoint must still be present in this
    # checkpoint's write-session preflight. The execution ledger later binds its
    # exact verification UUID to this same run.
    for ordinal, name in enumerate(sorted(required_prior_names.intersection(prior_provider)), 1):
        if name in common:
            preconditions.append(_common_ddl_check(f"structural-prior-{ordinal:04d}", name, expected_present=True))

    for operation in operations:
        if operation.get("kind") == "CREATE_VIEW":
            continue
        for dependency_owner, dependency_name in operation.get("dependencies", ()):
            if str(dependency_owner).upper() != checkpoint.schema.upper():
                raise StructuralPreflightError(f"unsupported dependency {dependency_owner}.{dependency_name}")

    # Exact target before-state is hashed by the caller; a previous same-schema
    # checkpoint may add only its recorded effects before this checkpoint starts.
    scope = _scope_with_dependents(required_scope(checkpoint), structural, operations)
    compile_units = tuple(migration_checks.compiled_units(feature_manifest._analysis_migration(checkpoint.inputs, checkpoint.schema), checkpoint.schema)) if checkpoint.inputs else ()
    return StructuralPlan(
        scope=scope,
        expected_before_sha256=_digest(structural),
        effects_sha256=_digest(effect_rows),
        preconditions=tuple(preconditions),
        postconditions=tuple(postconditions),
        compile_units=compile_units,
        prior_checkpoints=prior_checkpoints,
        operations=tuple(dict(item) for item in operations),
        prior_effects=tuple(prior_effects),
    )


def plan_to_dict(plan: StructuralPlan) -> dict:
    return {
        "owner": plan.scope.owner,
        "objectKeys": [[key.owner, key.name, key.object_type, key.subobject_name] for key in plan.scope.object_keys],
        "prerequisiteKeys": [[key.owner, key.name, key.object_type, key.subobject_name] for key in plan.scope.prerequisite_keys],
        "sections": list(plan.scope.sections),
        "expectedBeforeSha256": plan.expected_before_sha256,
        "effectsSha256": plan.effects_sha256,
        "preconditions": [{"id": check.id, "sql": check.sql, "expected": check.expected} for check in plan.preconditions],
        "postconditions": [{"id": check.id, "sql": check.sql, "expected": check.expected} for check in plan.postconditions],
        "compileUnits": [list(unit) for unit in plan.compile_units],
        "priorCheckpoints": list(plan.prior_checkpoints),
        "operations": [_effect_record(item) for item in plan.operations],
        "priorEffects": [dict(item) for item in plan.prior_effects],
    }


def checks_from_record(record: Mapping, label: str) -> tuple[QueryCheck, ...]:
    rows = record.get(label)
    if not isinstance(rows, list):
        raise StructuralPreflightError(f"structural plan {label} are malformed")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"id", "sql", "expected"}:
            raise StructuralPreflightError(f"structural plan {label} contain a malformed query")
        check_id, sql, expected = row.get("id"), row.get("sql"), row.get("expected")
        if not isinstance(check_id, str) or not check_id or check_id in seen or not isinstance(sql, str) or type(expected) is not int:
            raise StructuralPreflightError(f"structural plan {label} contain a duplicate or invalid query")
        try:
            validate_check_query(sql)
        except MigrationManifestError as error:
            raise StructuralPreflightError(f"structural plan {label} contain an invalid query: {error}") from error
        seen.add(check_id)
        result.append(QueryCheck(check_id, sql, expected))
    return tuple(result)


def validate_plan_record(record: Mapping, checkpoint: FeatureCheckpoint, structural_state: Mapping | None = None) -> None:
    """Revalidate the strict target-plan structural record against capsule source."""
    fields = {
        "owner", "objectKeys", "prerequisiteKeys", "sections", "expectedBeforeSha256",
        "effectsSha256", "preconditions", "postconditions", "compileUnits", "priorCheckpoints",
        "operations", "priorEffects",
    }
    if not isinstance(record, Mapping) or set(record) != fields:
        raise StructuralPreflightError("structural plan has unsupported or missing fields")
    expected_scope = required_scope(checkpoint)
    if structural_state is not None:
        structural = _structural({
            "structural": structural_state,
            "coverage": {"selectedScopeComplete": True},
        })
        expected_scope = _scope_with_dependents(expected_scope, structural, _ddl_operations(checkpoint))
    if record.get("owner") != checkpoint.schema or tuple(record.get("sections", ())) != expected_scope.sections:
        raise StructuralPreflightError("structural plan owner or selected sections differ from capsule source")
    for field, expected in (("objectKeys", expected_scope.object_keys), ("prerequisiteKeys", expected_scope.prerequisite_keys)):
        rows = record.get(field)
        canonical = [[key.owner, key.name, key.object_type, key.subobject_name] for key in expected]
        if rows != canonical:
            raise StructuralPreflightError(f"structural plan {field} differ from capsule source")
    for field in ("expectedBeforeSha256", "effectsSha256"):
        value = record.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII) is None:
            raise StructuralPreflightError(f"structural plan {field} is not a lowercase SHA-256 digest")
    expected_operations = [_effect_record(item) for item in _ddl_operations(checkpoint)]
    canonical_operations = json.loads(_canonical(expected_operations))
    try:
        observed_operations = json.loads(_canonical(record.get("operations")))
    except (TypeError, ValueError) as error:
        raise StructuralPreflightError("structural plan operations are not canonical JSON data") from error
    if observed_operations != canonical_operations or record["effectsSha256"] != _digest(expected_operations):
        raise StructuralPreflightError("structural plan effects differ from frozen capsule source")
    checks_from_record(record, "preconditions")
    checks_from_record(record, "postconditions")
    prior = record.get("priorCheckpoints")
    if not isinstance(prior, list) or any(type(value) is not int or value < 1 for value in prior) or prior != sorted(set(prior)):
        raise StructuralPreflightError("structural plan prior checkpoint binding is malformed")
    effects = record.get("priorEffects")
    if not isinstance(effects, list) or any(
        not isinstance(row, Mapping)
        or set(row) - {"name", "kind", "table", "checkpoint", "columns", "grantees", "privileges"}
        or not {"name", "kind", "table", "checkpoint"}.issubset(row)
        or not isinstance(row.get("name"), str) or not isinstance(row.get("kind"), str)
        or not isinstance(row.get("table"), str) or type(row.get("checkpoint")) is not int
        or row.get("checkpoint") not in prior
        or ("columns" in row and (not isinstance(row["columns"], (list, tuple)) or any(not isinstance(value, str) for value in row["columns"])))
        or ("grantees" in row and (not isinstance(row["grantees"], (list, tuple)) or any(not isinstance(value, str) for value in row["grantees"])))
        or ("privileges" in row and not isinstance(row["privileges"], str))
        for row in effects
    ):
        raise StructuralPreflightError("structural plan prior effects are malformed")
    units = record.get("compileUnits")
    try:
        expected_units = [list(unit) for unit in migration_checks.compiled_units(
            feature_manifest._analysis_migration(checkpoint.inputs, checkpoint.schema), checkpoint.schema,
        )]
    except (migration_checks.MigrationAnalysisError, MigrationManifestError, ValueError) as error:
        raise StructuralPreflightError(f"structural plan compile-unit source is invalid: {error}") from error
    if units != expected_units:
        raise StructuralPreflightError("structural plan compile units differ from frozen capsule source")


def strip_prior_effects(structural: Mapping, prior_effects: Sequence[Mapping]) -> dict:
    """Remove only exact earlier effect names from a fresh selected capture."""
    result = {section: [dict(row) for row in rows] for section, rows in structural.items()}
    created = [
        row for row in prior_effects if isinstance(row, Mapping)
        and row.get("kind") in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE", "CREATE_INDEX", "CREATE_SYNONYM"}
    ]
    created_names = {str(row.get("name", "")) for row in created}
    created_by_kind = {
        kind: {str(row.get("name", "")) for row in created if row.get("kind") == kind}
        for kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE", "CREATE_INDEX", "CREATE_SYNONYM"}
    }
    if "tables" in result:
        result["tables"] = [row for row in result["tables"] if row.get("name") not in created_by_kind["CREATE_TABLE"]]
    if "columns" in result:
        removed_table_columns = created_by_kind["CREATE_TABLE"]
        added_columns = {
            (str(row.get("table", "")), str(column))
            for row in prior_effects if isinstance(row, Mapping) and row.get("kind") == "ALTER_ADD_COLUMN"
            for column in row.get("columns", ())
        }
        result["columns"] = [
            row for row in result["columns"]
            if row.get("table_name") not in removed_table_columns
            and (str(row.get("table_name", "")), str(row.get("name", ""))) not in added_columns
        ]
    if "constraints" in result:
        result["constraints"] = [row for row in result["constraints"] if row.get("table_name") not in created_by_kind["CREATE_TABLE"]]
    if "triggers" in result:
        result["triggers"] = [
            row for row in result["triggers"]
            if row.get("table_name") not in created_by_kind["CREATE_TABLE"]
            and row.get("name") not in created_by_kind["CREATE_TABLE"]
        ]
    if "identity-columns" in result:
        result["identity-columns"] = [row for row in result["identity-columns"] if row.get("table_name") not in created_by_kind["CREATE_TABLE"]]
    for section, kind, name_key in (
        ("views", "CREATE_VIEW", "name"), ("sequences", "CREATE_SEQUENCE", "name"),
        ("indexes", "CREATE_INDEX", "name"), ("synonyms", "CREATE_SYNONYM", "name"),
    ):
        if section in result:
            result[section] = [row for row in result[section] if row.get(name_key) not in created_by_kind[kind]]
    if "invalid-objects" in result:
        result["invalid-objects"] = [row for row in result["invalid-objects"] if row.get("name") not in created_names]
    if "feature-dependencies" in result:
        result["feature-dependencies"] = [row for row in result["feature-dependencies"] if row.get("name") not in created_names]
    if "object-grants" in result:
        prior_grants = {
            (str(row.get("name", "")), str(grantee), str(privilege).strip().upper())
            for row in prior_effects if isinstance(row, Mapping) and row.get("kind") == "GRANT_OBJECT"
            for grantee in row.get("grantees", ())
            for privilege in str(row.get("privileges", "")).split(",")
        }
        result["object-grants"] = [
            row for row in result["object-grants"]
            if (str(row.get("object_name", "")), str(row.get("grantee", "")), str(row.get("privilege", "")).upper()) not in prior_grants
        ]
    return result
