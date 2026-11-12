"""The structure, grant and code bundle generators and their SQL helpers, moved out of scripts/baseline_build.py.

scripts/baseline_build.py keeps the build entry point and the migration folder writers. Names that stay in
scripts/baseline.py are read as baseline.<name> at call time, so patches on scripts.baseline still reach this
code. baseline_build re-exports the moved names that baseline, tests and other scripts reference.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal

from .baseline_sql import _check, _q_literal, _safe_check_slug, _sql_identifier, _sql_literal
from .migration_manifest import validate_sql_only
from .schema_catalog import ObjectKey, SchemaSnapshot


_PLSQL_SOURCE_CHUNK_CHARS = 32


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


def _metadata_integer(value: object, description: str) -> int:
    """Read an integer catalog attribute without rounding malformed evidence."""
    if isinstance(value, bool) or not isinstance(value, (int, str, float, Decimal)):
        raise baseline.BaselineError(f"{description} is not an integer: {value!r}")
    try:
        parsed = int(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise baseline.BaselineError(f"{description} is not an integer: {value!r}") from error
    if isinstance(value, (float, Decimal)) and value != parsed:
        raise baseline.BaselineError(f"{description} is not an integer: {value!r}")
    return parsed


def _table_column_type(row: Mapping) -> str:
    data_type = row.get("data_type")
    col_name = row.get("name", "?")
    if isinstance(data_type, bool) or not isinstance(data_type, str):
        raise baseline.BaselineError(f"unsupported column data type in catalog: {data_type!r}")
    value = data_type.upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_ ]*(?:\([0-9]+(?:,[0-9]+)?\))?(?: WITH (?:LOCAL )?TIME ZONE)?", value, re.ASCII) is None:
        raise baseline.BaselineError(f"unsupported column data type in catalog: {data_type!r}")

    if value.startswith("NUMBER"):
        precision_raw = row.get("data_precision")
        scale_raw = row.get("data_scale")
        if isinstance(precision_raw, bool) or isinstance(scale_raw, bool):
            raise baseline.BaselineError(f"column {col_name} has invalid boolean numeric attribute")

        parsed_precision: int | None = None
        if precision_raw is not None:
            parsed_precision = _metadata_integer(precision_raw, f"column {col_name} NUMBER precision")
            if parsed_precision < 1 or parsed_precision > 38:
                raise baseline.BaselineError(f"column {col_name} NUMBER precision {parsed_precision} out of range 1..38")

        parsed_scale: int | None = None
        if scale_raw is not None:
            parsed_scale = _metadata_integer(scale_raw, f"column {col_name} NUMBER scale")
            if parsed_scale < -84 or parsed_scale > 127:
                raise baseline.BaselineError(f"column {col_name} NUMBER scale {parsed_scale} out of range -84..127")

        if parsed_precision is not None and parsed_scale is not None:
            value = f"NUMBER({parsed_precision},{parsed_scale})"
        elif parsed_precision is not None:
            value = f"NUMBER({parsed_precision})"
        elif parsed_scale is not None:
            value = f"NUMBER(*,{parsed_scale})"
        else:
            value = "NUMBER"

    elif value.startswith("FLOAT"):
        precision_raw = row.get("data_precision")
        scale_raw = row.get("data_scale")
        if isinstance(precision_raw, bool) or isinstance(scale_raw, bool):
            raise baseline.BaselineError(f"column {col_name} has invalid boolean FLOAT attribute")
        if scale_raw is not None:
            raise baseline.BaselineError(f"column {col_name} FLOAT does not support scale: {scale_raw!r}")

        if precision_raw is not None:
            parsed_precision = _metadata_integer(precision_raw, f"column {col_name} FLOAT precision")
            if parsed_precision < 1 or parsed_precision > 126:
                raise baseline.BaselineError(f"column {col_name} FLOAT precision {parsed_precision} out of range 1..126")
            value = f"FLOAT({parsed_precision})"
        else:
            value = "FLOAT"

    elif value in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"}:
        length_raw = row.get("char_length") if value in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"} else row.get("data_length")
        if length_raw is None:
            length_raw = row.get("data_length")
        if length_raw is None or isinstance(length_raw, bool):
            raise baseline.BaselineError(f"column {col_name} has no usable length")
        length = _metadata_integer(length_raw, f"column {col_name} length")
        if length < 1:
            raise baseline.BaselineError(f"column {col_name} has no usable length")
        if value in {"VARCHAR2", "CHAR"}:
            semantics = "CHAR" if row.get("char_used") == "C" else "BYTE"
            value = f"{value}({length} {semantics})"
        else:
            value = f"{value}({length})"
    return value


def _column_definition_check(schema: str, row: Mapping) -> dict:
    table_name = str(row["table_name"])
    column_name = str(row["name"])
    data_type_raw = row.get("data_type")
    if isinstance(data_type_raw, bool) or not isinstance(data_type_raw, str):
        raise baseline.BaselineError(f"column {table_name}.{column_name} has invalid data_type: {data_type_raw!r}")
    data_type = data_type_raw.upper()

    predicates = [
        f"OWNER = {_sql_literal(schema)}",
        f"TABLE_NAME = {_sql_literal(table_name)}",
        f"COLUMN_NAME = {_sql_literal(column_name)}",
    ]

    if data_type.startswith("NUMBER"):
        predicates.append("DATA_TYPE = 'NUMBER'")
        precision = row.get("data_precision")
        scale = row.get("data_scale")
        if isinstance(precision, bool) or isinstance(scale, bool):
            raise baseline.BaselineError(f"column {table_name}.{column_name} has invalid boolean numeric attribute")
        if precision is not None:
            p_val = _metadata_integer(precision, f"column {table_name}.{column_name} precision")
            if p_val < 1 or p_val > 38:
                raise baseline.BaselineError(f"column {table_name}.{column_name} precision {p_val} out of range 1..38")
            predicates.append(f"DATA_PRECISION = {p_val}")
        else:
            predicates.append("DATA_PRECISION IS NULL")

        if scale is not None:
            s_val = _metadata_integer(scale, f"column {table_name}.{column_name} scale")
            if s_val < -84 or s_val > 127:
                raise baseline.BaselineError(f"column {table_name}.{column_name} scale {s_val} out of range -84..127")
            predicates.append(f"DATA_SCALE = {s_val}")
        else:
            predicates.append("DATA_SCALE IS NULL")

    elif data_type.startswith("FLOAT"):
        predicates.append("DATA_TYPE = 'FLOAT'")
        precision = row.get("data_precision")
        scale = row.get("data_scale")
        if isinstance(precision, bool) or isinstance(scale, bool):
            raise baseline.BaselineError(f"column {table_name}.{column_name} has invalid boolean FLOAT attribute")
        if scale is not None:
            raise baseline.BaselineError(f"column {table_name}.{column_name} FLOAT cannot have scale: {scale!r}")
        if precision is not None:
            p_val = _metadata_integer(precision, f"column {table_name}.{column_name} precision")
            if p_val < 1 or p_val > 126:
                raise baseline.BaselineError(f"column {table_name}.{column_name} FLOAT precision {p_val} out of range 1..126")
            predicates.append(f"DATA_PRECISION = {p_val}")
        else:
            predicates.append("DATA_PRECISION IS NULL")
        predicates.append("DATA_SCALE IS NULL")

    elif data_type in {"VARCHAR2", "CHAR"}:
        predicates.append(f"DATA_TYPE = {_sql_literal(data_type)}")
        char_used = row.get("char_used")
        if char_used == "C":
            char_length = row.get("char_length")
            if char_length is None or isinstance(char_length, bool):
                raise baseline.BaselineError(f"column {table_name}.{column_name} has no usable char_length for CHAR semantics")
            cl_val = _metadata_integer(char_length, f"column {table_name}.{column_name} char_length")
            if cl_val < 1:
                raise baseline.BaselineError(f"column {table_name}.{column_name} invalid char_length: {char_length!r}")
            predicates.append("CHAR_USED = 'C'")
            predicates.append(f"CHAR_LENGTH = {cl_val}")
        else:
            data_length = row.get("data_length")
            if data_length is None:
                data_length = row.get("char_length")
            if data_length is None or isinstance(data_length, bool):
                raise baseline.BaselineError(f"column {table_name}.{column_name} has no usable data_length for BYTE semantics")
            dl_val = _metadata_integer(data_length, f"column {table_name}.{column_name} data_length")
            if dl_val < 1:
                raise baseline.BaselineError(f"column {table_name}.{column_name} invalid data_length: {data_length!r}")
            predicates.append("(CHAR_USED = 'B' OR CHAR_USED IS NULL)")
            predicates.append(f"DATA_LENGTH = {dl_val}")

    elif data_type in {"NVARCHAR2", "NCHAR"}:
        predicates.append(f"DATA_TYPE = {_sql_literal(data_type)}")
        char_length = row.get("char_length")
        if char_length is None:
            char_length = row.get("data_length")
        if char_length is None or isinstance(char_length, bool):
            raise baseline.BaselineError(f"column {table_name}.{column_name} has no usable char_length")
        cl_val = _metadata_integer(char_length, f"column {table_name}.{column_name} char_length")
        if cl_val < 1:
            raise baseline.BaselineError(f"column {table_name}.{column_name} invalid char_length: {char_length!r}")
        predicates.append(f"CHAR_LENGTH = {cl_val}")

    elif data_type == "RAW":
        predicates.append("DATA_TYPE = 'RAW'")
        data_length = row.get("data_length")
        if data_length is None or isinstance(data_length, bool):
            raise baseline.BaselineError(f"column {table_name}.{column_name} RAW has no usable data_length")
        dl_val = _metadata_integer(data_length, f"column {table_name}.{column_name} data_length")
        if dl_val < 1:
            raise baseline.BaselineError(f"column {table_name}.{column_name} invalid data_length: {data_length!r}")
        predicates.append(f"DATA_LENGTH = {dl_val}")

    else:
        predicates.append(f"DATA_TYPE = {_sql_literal(data_type)}")

    where_clause = " AND ".join(predicates)
    sql = f"SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_TAB_COLS WHERE {where_clause}) THEN 1 ELSE 0 END FROM DUAL"
    check_id = f"column-def-{_safe_check_slug(table_name + '-' + column_name)}"
    return _check(check_id, sql)


def _view_validity_check(schema: str, name: str) -> dict:
    sql = (
        "SELECT CASE WHEN EXISTS ("
        f"SELECT 1 FROM ALL_OBJECTS WHERE OWNER = {_sql_literal(schema)} "
        f"AND OBJECT_NAME = {_sql_literal(name)} AND OBJECT_TYPE = 'VIEW' AND STATUS = 'VALID'"
        ") AND NOT EXISTS ("
        f"SELECT 1 FROM ALL_ERRORS WHERE OWNER = {_sql_literal(schema)} "
        f"AND NAME = {_sql_literal(name)} AND TYPE = 'VIEW' AND ATTRIBUTE = 'ERROR'"
        ") THEN 1 ELSE 0 END FROM DUAL"
    )
    check_id = f"valid-view-{_safe_check_slug(name)}"
    return _check(check_id, sql)


def _column_sql(row: Mapping) -> str:
    return f"{_sql_identifier(str(row['name']))} {_table_column_type(row)}"


def _count_check(check_id: str, table_and_where: str, expected: int) -> dict:
    return _check(
        check_id,
        f"SELECT CASE WHEN (SELECT COUNT(*) FROM {table_and_where}) = {expected} THEN 1 ELSE 0 END FROM DUAL",
    )


def _source_line_predicate(line_number: int, line: str, *, final: bool) -> str:
    exact = _sql_literal(line[:-1] if final and line.endswith("\n") else line)
    text = f"(S.TEXT || CHR(1)) = ({exact} || CHR(1))"
    if final:
        final_newline = _sql_literal((line[:-1] if line.endswith("\n") else line) + "\n")
        text = (
            f"(S.TEXT || CHR(1)) = ({exact} || CHR(1)) OR "
            f"(S.TEXT || CHR(1)) = ({final_newline} || CHR(1))"
        )
    return f"(S.LINE = {line_number} AND ({text}))"


def _source_check_queries(unit: Mapping, schema: str, *, max_bytes: int = 24000) -> list[dict]:
    name, object_type = str(unit["name"]), str(unit["type"])
    lines = unit["lines"]
    slug = _safe_check_slug(f"{name}-{object_type}")
    expected_rows = list(enumerate(lines, start=1))

    def query(rows: Sequence[tuple[int, str]]) -> str:
        predicates = [
            _source_line_predicate(line_no, text, final=line_no == len(lines))
            for line_no, text in rows
        ]
        text_match = " OR ".join(predicates) if predicates else "1 = 0"
        return (
            "SELECT CASE WHEN "
            f"COUNT(*) = {len(lines)} AND COUNT(DISTINCT CASE WHEN {text_match} THEN S.LINE END) = {len(rows)} "
            "THEN 1 ELSE 0 END FROM ALL_SOURCE S "
            f"WHERE S.OWNER = {_sql_literal(schema)} AND S.NAME = {_sql_literal(name)} "
            f"AND S.TYPE = {_sql_literal(object_type)}"
        )

    result: list[dict] = []
    bucket: list[tuple[int, str]] = []
    for row in expected_rows:
        candidate = bucket + [row]
        if len(query(candidate).encode("utf-8")) > max_bytes and bucket:
            result.append(_check(f"source-{slug}-{len(result) + 1:03d}", query(bucket)))
            bucket = [row]
            if len(query(bucket).encode("utf-8")) > max_bytes:
                raise baseline.BaselineError(f"one stored-source line exceeds the exact-check SQL limit: {name} {object_type} line {row[0]}")
        else:
            bucket = candidate
        if len(query(bucket).encode("utf-8")) > max_bytes:
            raise baseline.BaselineError(f"one stored-source line exceeds the exact-check SQL limit: {name} {object_type} line {row[0]}")
    if bucket or not result:
        result.append(_check(f"source-{slug}-{len(result) + 1:03d}", query(bucket)))
    return result


def _setting_row(settings: Mapping, name: str, object_type: str) -> dict:
    rows = [row for row in settings if row.get("name") == name and row.get("type") == object_type]
    if len(rows) != 1:
        raise baseline.BaselineError(f"expected exactly one compiler-settings row for {object_type} {name}; found {len(rows)}")
    row = rows[0]
    fields = (
        "plsql_optimize_level", "plsql_code_type", "plsql_debug", "plsql_warnings",
        "plsql_ccflags", "nls_length_semantics", "plscope_settings",
    )
    missing = [field for field in fields if field not in row]
    if missing:
        raise baseline.BaselineError(f"compiler settings for {object_type} {name} are incomplete: {', '.join(missing)}")
    optimize = str(row["plsql_optimize_level"])
    code_type = str(row["plsql_code_type"]).upper()
    debug = str(row["plsql_debug"]).upper()
    nls = str(row["nls_length_semantics"]).upper()
    if optimize not in {"0", "1", "2", "3"} or code_type not in {"INTERPRETED", "NATIVE"} or debug not in {"TRUE", "FALSE"} or nls not in {"BYTE", "CHAR"}:
        raise baseline.BaselineError(f"compiler settings for {object_type} {name} contain unsupported values")
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


def _clob_source_step(unit: Mapping, settings: Mapping | None) -> str:
    lines = [f"-- Exact stored source for {unit['type']} {unit['name']} assembled as a CLOB."]
    if settings is not None:
        lines.append(_session_settings_sql(settings))
    lines.extend([
        "DECLARE",
        "  l_source CLOB;",
        "BEGIN",
        "  l_source := TO_CLOB('CREATE OR REPLACE ');",
    ])
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


def _direct_source_step(unit: Mapping, settings: Mapping | None) -> str:
    stored = "".join(unit["lines"])
    settings_sql = f"{_session_settings_sql(settings)}\n" if settings is not None else ""
    sql = (
        f"-- Exact stored source for {unit['type']} {unit['name']}.\n"
        f"{settings_sql}"
        "CREATE OR REPLACE "
        f"{stored}"
    )
    if not stored.endswith("\n"):
        sql += "\n"
    return sql + "/\n"


def _clob_ddl_step(label: str, ddl: str) -> str:
    chunks = [ddl[index:index + _PLSQL_SOURCE_CHUNK_CHARS] for index in range(0, len(ddl), _PLSQL_SOURCE_CHUNK_CHARS)]
    if not chunks:
        raise baseline.BaselineError(f"stored DDL for {label} is empty")
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


def _exact_settings_check(settings: Mapping, name: str, object_type: str, schema: str) -> dict:
    clauses = [
        f"S.PLSQL_OPTIMIZE_LEVEL = {_sql_literal(str(settings['plsql_optimize_level']))}",
        f"S.PLSQL_CODE_TYPE = {_sql_literal(str(settings['plsql_code_type']))}",
        f"S.PLSQL_DEBUG = {_sql_literal(str(settings['plsql_debug']))}",
        f"S.PLSQL_WARNINGS = {_sql_literal(str(settings['plsql_warnings']))}",
        f"S.NLS_LENGTH_SEMANTICS = {_sql_literal(str(settings['nls_length_semantics']))}",
        f"S.PLSCOPE_SETTINGS = {_sql_literal(str(settings['plscope_settings'] or ''))}",
    ]
    # An empty flag string is stored as NULL, and '' = NULL is never true.
    clauses.append("S.PLSQL_CCFLAGS IS NULL" if not settings["plsql_ccflags"] else f"S.PLSQL_CCFLAGS = {_sql_literal(str(settings['plsql_ccflags']))}")
    query = (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_PLSQL_OBJECT_SETTINGS S "
        f"WHERE S.OWNER = {_sql_literal(schema)} AND S.NAME = {_sql_literal(name)} AND S.TYPE = {_sql_literal(object_type)} AND "
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
        raise baseline.BaselineError("constraint catalog row is incomplete")
    column_list = ", ".join(_sql_identifier(item) for item in columns)
    if kind == "C":
        condition = row.get("condition")
        if not isinstance(condition, str) or row.get("condition_truncated") in {"Y", True}:
            raise baseline.BaselineError(f"check constraint {name} has missing or truncated condition text")
        clause = f"CHECK ({condition})"
    elif kind == "P":
        clause = f"PRIMARY KEY ({column_list})"
    elif kind == "U":
        clause = f"UNIQUE ({column_list})"
    elif kind == "R":
        reference, ref_columns = row.get("referenced_table"), row.get("referenced_columns")
        if not isinstance(reference, str) or not isinstance(ref_columns, list) or not ref_columns:
            raise baseline.BaselineError(f"foreign-key constraint {name} has incomplete reference metadata")
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
        raise baseline.BaselineError(f"unsupported constraint type {kind!r} for {name}")
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
        raise baseline.BaselineError(f"constraint {name} has an unsupported status {status!r}")
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
        raise baseline.BaselineError(f"unsupported identity generation type {generation!r}")
    return (
        f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(str(row['table_name']))} MODIFY "
        f"({_sql_identifier(str(row['column_name']))} GENERATED {generation} AS IDENTITY (START WITH LIMIT VALUE));\n"
    )


def _sync_sequence_sql(target_schema: str, row: Mapping, mapping: Mapping) -> str:
    name, table, column = str(row.get("name", "")), str(mapping["table"]), str(mapping["column"])
    increment = row.get("increment_by", 1)
    if type(increment) is not int or increment <= 0:
        raise baseline.BaselineError(f"sequence {name} must have a positive increment to sync seeded ids")
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
    source_tables = {row["name"]: row for row in left.get("tables", []) if baseline._schema_filter(config, source_schema, row.get("name", ""))}
    target_tables = {row["name"]: row for row in right.get("tables", [])}
    source_columns = {(row["table_name"], row["name"]): row for row in left.get("columns", []) if baseline._schema_filter(config, source_schema, row.get("table_name", ""))}
    target_columns = {(row["table_name"], row["name"]): row for row in right.get("columns", [])}
    source_constraints = {(row["table_name"], row["name"]): row for row in left.get("constraints", []) if baseline._schema_filter(config, source_schema, row.get("table_name", ""))}
    target_constraints = {(row["table_name"], row["name"]): row for row in right.get("constraints", [])}
    source_indexes = {row["name"]: row for row in left.get("indexes", []) if baseline._schema_filter(config, source_schema, row.get("name", ""))}
    target_indexes = {row["name"]: row for row in right.get("indexes", [])}
    identity_sequence_names = {
        str(row.get("sequence_name")) for row in left.get("identity-columns", [])
        if isinstance(row.get("sequence_name"), str) and row.get("sequence_name")
    }
    source_sequences = {
        row["name"]: row for row in left.get("sequences", [])
        if baseline._schema_filter(config, source_schema, row.get("name", "")) and row.get("name") not in identity_sequence_names
    }
    target_sequences = {row["name"]: row for row in right.get("sequences", [])}
    source_identity_rows = [row for row in left.get("identity-columns", []) if isinstance(row.get("table_name"), str) and baseline._schema_filter(config, source_schema, row["table_name"])]
    source_identity = {(row.get("table_name"), row.get("column_name")): row for row in source_identity_rows}
    definitions = snapshot.objects
    chunks: list[str] = []
    posts: list[dict] = []
    preconditions: list[dict] = []

    for name in sorted(source_tables):
        if name in target_tables:
            continue
        definition = definitions.get(ObjectKey(source_schema, name, "TABLE"))
        if definition is None:
            raise baseline.BaselineError(f"source table DDL is missing for {name}")
        ddl = _replace_create_header(definition.raw_ddl, "TABLE", source_schema, target_schema)
        chunks.append(_wrap_guarded_ddl(f"ALL_TABLES WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(name)}", ddl))
        posts.append(_count_check(f"table-{_safe_check_slug(name)}", f"ALL_TABLES WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(name)}", 1))

    to_make_not_null = set()
    checked_empty_tables: set[str] = set()
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
                    raise baseline.BaselineError(f"unsupported identity generation type {generation!r} for {table_name}.{column_name}")
                column_ddl += f" GENERATED {generation} AS IDENTITY"
            ddl = f"ALTER TABLE {_sql_identifier(target_schema)}.{_sql_identifier(table_name)} ADD ({column_ddl})"
            exists_query = f"ALL_TAB_COLS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table_name)} AND COLUMN_NAME = {_sql_literal(column_name)}"
            chunks.append(_wrap_guarded_ddl(exists_query, ddl))
            posts.append(_column_definition_check(target_schema, row))
            if row.get("nullable") == "N":
                to_make_not_null.add(key)
                if table_name not in checked_empty_tables:
                    checked_empty_tables.add(table_name)
                    preconditions.append(_count_check(
                        f"not-null-empty-table-{_safe_check_slug(table_name)}",
                        f"{_sql_identifier(target_schema)}.{_sql_identifier(table_name)}", 0,
                    ))
        else:
            type_fields = ("data_type", "data_length", "data_precision", "data_scale", "char_used", "char_length")
            changed = [field for field in type_fields if row.get(field) != existing.get(field)]
            if changed:
                raise baseline.BaselineError(f"column type changes need a reviewed data migration: {table_name}.{column_name} ({', '.join(changed)})")
            if row.get("nullable") == "N" and existing.get("nullable") == "Y":
                to_make_not_null.add(key)
                preconditions.append(_count_check(
                    f"not-null-no-null-rows-{_safe_check_slug(table_name + '-' + column_name)}",
                    f"{_sql_identifier(target_schema)}.{_sql_identifier(table_name)} WHERE {_sql_identifier(column_name)} IS NULL", 0,
                ))
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
        constraint_fields = (
            "constraint_type", "columns", "referenced_table", "referenced_columns", "condition",
            "condition_truncated", "status", "validated", "delete_rule", "deferrable", "deferred", "rely",
        )
        changed_fields = [
            field for field in constraint_fields
            if (
                _condition_key(str(row.get(field) or "")) != _condition_key(str(existing.get(field) or ""))
                if field == "condition" and existing is not None
                else existing is not None and row.get(field) != existing.get(field)
            )
        ]
        truncated_condition = any(
            str(constraint.get("condition_truncated", "")).upper() in {"Y", "YES", "TRUE", "1"}
            for constraint in (row, existing or {})
        )
        replace_check = bool(
            existing and changed_fields == ["condition"]
            and row.get("constraint_type") == "C" and existing.get("constraint_type") == "C"
            and not truncated_condition
        )
        if existing is not None and changed_fields and not replace_check:
            raise baseline.BaselineError(
                f"same-name constraint differences need a reviewed migration: "
                f"{table_name}.{name} ({', '.join(changed_fields)})"
            )
        if existing is None or replace_check:
            if ObjectKey(source_schema, name, "CONSTRAINT") not in definitions:
                raise baseline.BaselineError(f"source constraint definition is missing for {table_name}.{name}")
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
            raise baseline.BaselineError(f"source index definition is missing for {name}")
        ddl = _replace_create_header(definition.raw_ddl, "INDEX", source_schema, target_schema)
        chunks.append(_wrap_guarded_ddl(f"ALL_INDEXES WHERE OWNER = {_sql_literal(target_schema)} AND INDEX_NAME = {_sql_literal(name)}", ddl))
        posts.append(_count_check(f"index-{_safe_check_slug(name)}", f"ALL_INDEXES WHERE OWNER = {_sql_literal(target_schema)} AND INDEX_NAME = {_sql_literal(name)}", 1))

    mappings = {item["sequence"]: item for item in config.get("sequenceMappings", [])}
    for name, row in sorted(source_sequences.items()):
        if name not in mappings:
            raise baseline.BaselineError(f"add sequenceMappings for {name} so its generator can advance beyond seeded ids")
        if name not in target_sequences:
            definition = definitions.get(ObjectKey(source_schema, name, "SEQUENCE"))
            if definition is None:
                raise baseline.BaselineError(f"source sequence definition is missing for {name}")
            ddl = _replace_create_header(definition.raw_ddl, "SEQUENCE", source_schema, target_schema)
            chunks.append(_wrap_guarded_ddl(f"ALL_SEQUENCES WHERE SEQUENCE_OWNER = {_sql_literal(target_schema)} AND SEQUENCE_NAME = {_sql_literal(name)}", ddl))
            posts.append(_count_check(f"sequence-{_safe_check_slug(name)}", f"ALL_SEQUENCES WHERE SEQUENCE_OWNER = {_sql_literal(target_schema)} AND SEQUENCE_NAME = {_sql_literal(name)}", 1))
        chunks.append(_sync_sequence_sql(target_schema, row, mappings[name]))

    target_identity = {(row.get("table_name"), row.get("column_name")) for row in right.get("identity-columns", [])}
    for row in sorted(source_identity_rows, key=lambda item: (item.get("table_name", ""), item.get("column_name", ""))):
        table_name, column_name = row.get("table_name"), row.get("column_name")
        if not isinstance(table_name, str) or not baseline._schema_filter(config, source_schema, table_name):
            continue
        if (table_name, column_name) not in target_identity and table_name in target_tables and (table_name, column_name) in target_columns:
            raise baseline.BaselineError(f"identity column {table_name}.{column_name} needs a reviewed conversion migration")
        chunks.append(_sync_identity_sql(target_schema, row))
        posts.append(_count_check(
            f"identity-{_safe_check_slug(table_name + '-' + str(column_name))}",
            f"ALL_TAB_IDENTITY_COLS WHERE OWNER = {_sql_literal(target_schema)} AND TABLE_NAME = {_sql_literal(table_name)} AND COLUMN_NAME = {_sql_literal(str(column_name))}", 1,
        ))

    steps = {"001-structure-delta.sql": "\n".join(chunks)} if chunks else {}
    return steps, {"preconditions": preconditions, "postconditions": posts}


def _grant_bundle(config: Mapping, source_schema: str, target_schema: str, source: Mapping, target: Mapping) -> tuple[dict[str, str], dict]:
    source_rows = baseline._grant_rows(config, source.get("sections", {}).get("object-grants", []), source_schema)
    target_rows = baseline._grant_rows(config, target.get("sections", {}).get("object-grants", []), target_schema)
    target_map = {(row.get("object_name"), row.get("grantee"), row.get("privilege")): row for row in target_rows}
    grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in source_rows:
        key = (row.get("object_name"), row.get("grantee"), row.get("privilege"))
        present = target_map.get(key)
        source_option = str(row.get("grantable", "NO")).upper()
        target_option = str((present or {}).get("grantable", "NO")).upper()
        if present is None or (source_option == "YES" and target_option != "YES"):
            if source_option not in {"YES", "NO"}:
                raise baseline.BaselineError(f"object grant {key} has an invalid grantable flag")
            keep_option = config.get("grants", {}).get("keepGrantOptions", True)
            grouped[(str(key[0]), str(key[1]), "YES" if source_option == "YES" and keep_option else "NO")].add(str(key[2]).upper())
    statements: list[str] = []
    checks: list[dict] = []
    for (object_name, grantee, grantable), privileges in sorted(grouped.items()):
        if any(re.fullmatch(r"[A-Z][A-Z0-9 ]*", value, re.ASCII) is None for value in privileges):
            raise baseline.BaselineError(f"grant on {object_name} contains an unsafe privilege")
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
    source_units = baseline._source_units(source, config, source_schema)
    target_units = baseline._source_units(target, config, target_schema)
    source_settings = baseline._settings_map(source, config, source_schema)
    target_settings = baseline._settings_map(target, config, target_schema)
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
        if isinstance(row.get("name"), str) and baseline._schema_filter(config, source_schema, row["name"])
    }
    target_views = {
        row["name"]: row for row in target.get("sections", {}).get("baseline-views", [])
        if isinstance(row.get("name"), str) and baseline._schema_filter(config, target_schema, row["name"])
    }
    for name, row in sorted(source_views.items()):
        source_ddl = row.get("ddl")
        if not isinstance(source_ddl, str):
            raise baseline.BaselineError(f"stored view metadata DDL for {name} is missing")
        target_row = target_views.get(name, {})
        mapped_ddl = _replace_create_header(source_ddl, "VIEW", source_schema, target_schema)
        if target_row.get("text") == row.get("text") and target_row.get("ddl") == mapped_ddl:
            continue
        ddl = source_ddl
        if not isinstance(ddl, str) or not re.match(
            r"(?is)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?(?:(?:NON)?EDITIONABLE\s+)?VIEW\b", ddl,
        ):
            raise baseline.BaselineError(f"stored view metadata DDL for {name} is not a CREATE VIEW statement")
        ddl = mapped_ddl
        steps[f"{sequence:03d}-view-{_safe_check_slug(name)}.sql"] = _clob_ddl_step(f"VIEW {name}", _ddl_without_terminator(ddl))
        sequence += 1
        postconditions.append(_count_check(
            f"view-{_safe_check_slug(name)}",
            f"ALL_VIEWS WHERE OWNER = {_sql_literal(target_schema)} AND VIEW_NAME = {_sql_literal(name)}", 1,
        ))
        postconditions.append(_view_validity_check(target_schema, name))
    if selected:
        steps[f"{sequence:03d}-compile-all.sql"] = _compile_all_step(target_schema, config.get("prefixes", []))
    return steps, {"preconditions": [], "postconditions": postconditions}


def _migration_bytes(steps: Mapping[str, str], checks: Mapping, readme: str) -> dict[str, bytes]:
    output = {name: (sql.rstrip("\n") + "\n").encode("utf-8") for name, sql in steps.items()}
    output["checks.json"] = baseline._json_bytes({
        "schemaVersion": 1,
        "preconditions": list(checks["preconditions"]),
        "postconditions": list(checks["postconditions"]),
    })
    output["README.md"] = (readme.rstrip("\n") + "\n").encode("utf-8")
    for name, source in output.items():
        if name.endswith(".sql"):
            validate_sql_only(source.decode("utf-8"))
    return output


def _bundle_readme(
    name: str,
    source_environment: str,
    source_schema: str,
    target_environment: str,
    target_schema: str,
    provenance: Sequence[Mapping[str, str]],
    *,
    has_data: bool,
) -> str:
    categories = tuple(dict.fromkeys(str(item["kind"]) for item in provenance))
    mapping = [
        "| Category | Original SQL file | Bundled SQL file | SHA-256 |",
        "| --- | --- | --- | --- |",
    ]
    mapping.extend(
        f"| {item['kind']} | `{item['originalName']}` | `{item['generatedName']}` | `{item['sqlSha256']}` |"
        for item in provenance
    )
    readme = (
        f"# Baseline Bundle: {name}\n\n"
        f"Source: {source_environment} / {source_schema}. Target: {target_environment} / {target_schema}.\n\n"
        "Included categories, in execution order: " + ", ".join(categories) + ".\n\n"
        "## Ordered SQL provenance\n\n"
        + "\n".join(mapping)
        + "\n\n## Review and checks\n\n"
        "Review every generated SQL step and `checks.json`. Evaluate the declared preconditions before applying and the postconditions after applying with the existing migration verification workflow. Keep this folder immutable after any write attempt; use a new revision for a correction.\n\n"
        "## Oracle DDL recovery\n\n"
        "Oracle DDL can commit implicitly. If a DDL step or later check fails, earlier DDL may remain in place. Stop, inspect the target, and use a reviewed compensating migration or DBA recovery; do not edit or replay an attempted folder."
    )
    if has_data:
        readme += (
            "\n\n## Separate reference data\n\n"
            "The `baseline-data` folder is installed separately after this DDL bundle. Review and apply it only after the DDL migration is verified; its natural-key, foreign-key, and DML checks remain independent."
        )
    return readme


def _snapshot_keys(catalog: Mapping, config: Mapping, schema: str) -> tuple[tuple[str, str], ...]:
    key_types = {"tables": "TABLE", "views": "VIEW", "sequences": "SEQUENCE", "indexes": "INDEX", "triggers": "TRIGGER"}
    keys = set()
    for section, object_type in key_types.items():
        for row in catalog.get("sections", {}).get(section, []):
            name = row.get("name")
            if isinstance(name, str) and baseline._schema_filter(config, schema, name):
                keys.add((name, object_type))
    for row in catalog.get("sections", {}).get("stored-code", []):
        name, object_type = row.get("name"), row.get("type")
        if isinstance(name, str) and object_type in baseline.SOURCE_TYPES and baseline._schema_filter(config, schema, name):
            keys.add((name, object_type))
    return tuple(sorted(keys))


# Imported last: this module reads baseline.<name> only when it runs, so baseline can bind the names from the
# build modules at its own end without a load-time cycle.
from . import baseline
