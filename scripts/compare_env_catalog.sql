-- Read-only, paged catalog capture used by the existing compare_schema command.
-- Arguments are UTF-8 values encoded as hex so names cannot inject SQL.
SET DEFINE ON
SET ENCODING UTF-8
DEFINE compare_schema_hex = '&1'
DEFINE compare_user_hex = '&2'
DEFINE compare_environment_hex = '&3'
DEFINE compare_dba = '&4'
DEFINE compare_sections_hex = '&5'
DEFINE compare_baseline_internal = '&6'
DEFINE compare_baseline_prefixes_hex = '&7'
DEFINE compare_baseline_excluded_hex = '&8'
DEFINE compare_baseline_data_tables_hex = '&9'
SET LONG 2000000000
SET LONGCHUNKSIZE 32767
SET SERVEROUTPUT ON SIZE UNLIMITED
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

PROMPT CATALOG_PAYLOAD_BEGIN:compare-env
DECLARE
  c_schema CONSTANT VARCHAR2(128) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_schema_hex'));
  c_expected_user CONSTANT VARCHAR2(128) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_user_hex'));
  c_environment CONSTANT VARCHAR2(16) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_environment_hex'));
  c_dba_mode CONSTANT BOOLEAN := '&&compare_dba' = '1';
  c_baseline_internal CONSTANT BOOLEAN := '&&compare_baseline_internal' = '1';
  c_selected_json CONSTANT CLOB := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_sections_hex'));
  c_baseline_prefixes_json CONSTANT CLOB := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_baseline_prefixes_hex'));
  c_baseline_excluded_json CONSTANT CLOB := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_baseline_excluded_hex'));
  c_baseline_data_tables_json CONSTANT CLOB := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&compare_baseline_data_tables_hex'));
  c_page_size CONSTANT PLS_INTEGER := 500;
  c_max_rows CONSTANT PLS_INTEGER := 100000;
  l_payload JSON_OBJECT_T := JSON_OBJECT_T();
  l_identity JSON_OBJECT_T := JSON_OBJECT_T();
  l_coverage JSON_OBJECT_T := JSON_OBJECT_T();
  l_coverage_sections JSON_OBJECT_T := JSON_OBJECT_T();
  l_sections JSON_OBJECT_T := JSON_OBJECT_T();
  l_unavailable JSON_OBJECT_T := JSON_OBJECT_T();
  l_requested JSON_ARRAY_T := JSON_ARRAY_T.parse(c_selected_json);
  l_rows JSON_ARRAY_T;
  l_pages JSON_ARRAY_T;
  l_page JSON_OBJECT_T;
  l_row_json CLOB;
  l_raw CLOB;
  l_blob BLOB;
  l_gzip BLOB;
  l_dest INTEGER := 1;
  l_src INTEGER := 1;
  l_ctx INTEGER := 0;
  l_warning INTEGER;
  l_pos PLS_INTEGER;
  l_piece RAW(12000);
  l_chunk VARCHAR2(32767);
  l_offset PLS_INTEGER;
  l_page_count PLS_INTEGER;
  l_row_count PLS_INTEGER;
  l_total PLS_INTEGER;
  l_started_at VARCHAR2(40) := TO_CHAR(SYSTIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"');
  l_completed_at VARCHAR2(40);
  l_db_version VARCHAR2(128);
  l_apex_version VARCHAR2(128);
  l_ords_count PLS_INTEGER;
  l_cursor SYS_REFCURSOR;
  l_view_name VARCHAR2(128);
  l_view_status VARCHAR2(16);
  l_view_ddl CLOB;
  l_view_lines PLS_INTEGER;
  l_view_hash NUMBER;
  l_apex_version_available BOOLEAN := TRUE;
  l_line_start PLS_INTEGER;
  l_line_end PLS_INTEGER;
  l_line_length PLS_INTEGER;
  l_line VARCHAR2(32767);
  l_is_selected BOOLEAN;

  FUNCTION selected(p_name VARCHAR2) RETURN BOOLEAN IS
  BEGIN
    FOR i IN 0 .. l_requested.get_size - 1 LOOP
      IF l_requested.get_string(i) = p_name THEN RETURN TRUE; END IF;
    END LOOP;
    RETURN FALSE;
  END;

  PROCEDURE fingerprint(p_text CLOB, p_line_count OUT PLS_INTEGER, p_hash_sum OUT NUMBER) IS
    position_value PLS_INTEGER := 1;
    text_length PLS_INTEGER := DBMS_LOB.GETLENGTH(p_text);
    next_line PLS_INTEGER;
    line_length PLS_INTEGER;
    current_line VARCHAR2(32767);
  BEGIN
    p_line_count := 0;
    p_hash_sum := 0;
    WHILE position_value <= text_length LOOP
      next_line := DBMS_LOB.INSTR(p_text, CHR(10), position_value);
      IF next_line = 0 THEN
        line_length := text_length - position_value + 1;
      ELSE
        line_length := next_line - position_value;
      END IF;
      IF line_length > 32767 THEN
        RAISE_APPLICATION_ERROR(-20998, 'compare-env source line exceeds the hash input limit');
      END IF;
      IF line_length > 0 THEN
        current_line := DBMS_LOB.SUBSTR(p_text, line_length, position_value);
      ELSE
        current_line := NULL;
      END IF;
      p_line_count := p_line_count + 1;
      IF p_line_count > 1 AND line_length > 0 THEN
        p_hash_sum := p_hash_sum + ORA_HASH(current_line);
      END IF;
      EXIT WHEN next_line = 0;
      position_value := next_line + 1;
    END LOOP;
  END;

  PROCEDURE fingerprint_source(
    p_name VARCHAR2,
    p_type VARCHAR2,
    p_line_count OUT PLS_INTEGER,
    p_hash_sum OUT NUMBER
  ) IS
    source_cursor SYS_REFCURSOR;
    source_line PLS_INTEGER;
    source_text VARCHAR2(32767);
    line_hash NUMBER;
  BEGIN
    p_line_count := 0;
    p_hash_sum := 0;
    OPEN source_cursor FOR
      'SELECT line, text FROM all_source WHERE owner = :1 AND name = :2 AND type = :3 ORDER BY line'
      USING c_schema, p_name, p_type;
    LOOP
      FETCH source_cursor INTO source_line, source_text;
      EXIT WHEN source_cursor%NOTFOUND;
      p_line_count := p_line_count + 1;
      IF source_line > 1 THEN
        SELECT ORA_HASH(source_text) INTO line_hash FROM dual;
        p_hash_sum := p_hash_sum + line_hash;
      END IF;
    END LOOP;
    CLOSE source_cursor;
  EXCEPTION
    WHEN OTHERS THEN
      IF source_cursor%ISOPEN THEN CLOSE source_cursor; END IF;
      RAISE;
  END;

  PROCEDURE append_rows(
    p_section VARCHAR2,
    p_query VARCHAR2,
    p_rows IN OUT NOCOPY JSON_ARRAY_T,
    p_pages IN OUT NOCOPY JSON_ARRAY_T
  ) IS
  BEGIN
    l_offset := 0;
    l_total := 0;
    LOOP
      l_page_count := 0;
      IF p_section IN ('baseline-source', 'baseline-settings', 'object-grants') THEN
        OPEN l_cursor FOR p_query USING
          c_schema, c_baseline_prefixes_json, c_baseline_prefixes_json,
          c_baseline_excluded_json, c_baseline_excluded_json, l_offset, c_page_size;
      ELSE
        OPEN l_cursor FOR p_query USING c_schema, l_offset, c_page_size;
      END IF;
      LOOP
        FETCH l_cursor INTO l_row_json;
        EXIT WHEN l_cursor%NOTFOUND;
        IF l_total >= c_max_rows THEN
          CLOSE l_cursor;
          RAISE_APPLICATION_ERROR(-20995, 'compare-env ' || p_section || ' exceeded its 100000-row cap');
        END IF;
        l_page_count := l_page_count + 1;
        l_total := l_total + 1;
        p_rows.append(JSON_OBJECT_T(l_row_json));
      END LOOP;
      CLOSE l_cursor;
      p_pages.append(l_page_count);
      EXIT WHEN l_page_count < c_page_size;
      l_offset := l_offset + c_page_size;
    END LOOP;
  EXCEPTION
    WHEN OTHERS THEN
      IF l_cursor%ISOPEN THEN CLOSE l_cursor; END IF;
      RAISE;
  END;

  PROCEDURE add_query_section(p_name VARCHAR2, p_query VARCHAR2) IS
  BEGIN
    IF NOT selected(p_name) THEN RETURN; END IF;
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    append_rows(p_name, p_query, l_rows, l_pages);
    l_sections.put(p_name, l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', TRUE);
    l_page.put('pages', l_pages);
    l_coverage_sections.put(p_name, l_page);
  END;

  PROCEDURE add_unavailable(p_name VARCHAR2, p_reason VARCHAR2) IS
  BEGIN
    l_sections.put(p_name, JSON_ARRAY_T());
    l_unavailable.put(p_name, p_reason);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', FALSE);
    l_page.put('reason', p_reason);
    l_page.put('pages', JSON_ARRAY_T.parse('[0]'));
    l_coverage_sections.put(p_name, l_page);
  END;

  PROCEDURE add_baseline_data IS
    specs JSON_ARRAY_T;
    spec JSON_OBJECT_T;
    excluded_columns JSON_ARRAY_T;
    key_columns JSON_ARRAY_T;
    label_columns JSON_ARRAY_T;
    expected_identity_element JSON_ELEMENT_T;
    expected_identity JSON_OBJECT_T;
    table_payload JSON_OBJECT_T;
    column_payload JSON_OBJECT_T;
    column_rows JSON_ARRAY_T;
    table_rows JSON_ARRAY_T;
    table_pages JSON_ARRAY_T;
    column_names JSON_ARRAY_T;
    order_list VARCHAR2(32767);
    dynamic_sql VARCHAR2(32767);
    l_table_name VARCHAR2(128);
    column_name VARCHAR2(128);
    excluded BOOLEAN;
    row_limit PLS_INTEGER;
    total_rows PLS_INTEGER;
    base_table_count PLS_INTEGER;
    offset_rows PLS_INTEGER;
    page_rows PLS_INTEGER;
    actual_identity_column VARCHAR2(128);
    actual_identity_generation VARCHAR2(30);
    identity_found BOOLEAN;
  BEGIN
    IF NOT selected('baseline-data') THEN RETURN; END IF;
    specs := JSON_ARRAY_T.parse(c_baseline_data_tables_json);
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    IF specs.get_size > c_max_rows THEN
      RAISE_APPLICATION_ERROR(-20995, 'baseline-data configured table list exceeded its 100000-table cap');
    END IF;

    FOR spec_index IN 0 .. specs.get_size - 1 LOOP
      spec := TREAT(specs.get(spec_index) AS JSON_OBJECT_T);
      l_table_name := spec.get_string('name');
      excluded_columns := spec.get_array('excludeColumns');
      key_columns := spec.get_array('keyColumns');
      label_columns := spec.get_array('labelColumns');
      row_limit := spec.get_number('rowLimit');
      IF l_table_name IS NULL OR row_limit IS NULL OR row_limit < 1 OR row_limit > c_max_rows THEN
        RAISE_APPLICATION_ERROR(-20994, 'baseline-data table configuration is malformed');
      END IF;
      SELECT COUNT(*) INTO base_table_count
        FROM all_tables
       WHERE owner = c_schema AND table_name = l_table_name;
      IF base_table_count != 1 THEN
        RAISE_APPLICATION_ERROR(-20994, 'baseline-data allow-list entry is missing or is not a table: ' || l_table_name);
      END IF;
      table_payload := JSON_OBJECT_T();
      table_payload.put('name', l_table_name);
      table_payload.put('rowLimit', row_limit);
      column_payload := JSON_OBJECT_T();
      column_rows := JSON_ARRAY_T();
      column_names := JSON_ARRAY_T();
      FOR c IN (
        SELECT column_name, data_type, data_length, data_precision, data_scale,
               char_used, char_length, nullable, column_id
          FROM all_tab_cols c
         WHERE c.owner = c_schema AND c.table_name = l_table_name
           AND c.hidden_column = 'NO' AND c.virtual_column = 'NO'
         ORDER BY c.column_id
      ) LOOP
        excluded := FALSE;
        IF excluded_columns IS NOT NULL THEN
          FOR exclude_index IN 0 .. excluded_columns.get_size - 1 LOOP
            IF UPPER(excluded_columns.get_string(exclude_index)) = UPPER(c.column_name) THEN
              excluded := TRUE;
              EXIT;
            END IF;
          END LOOP;
        END IF;
        IF NOT excluded THEN
          column_names.append(c.column_name);
          column_payload := JSON_OBJECT_T();
          column_payload.put('name', c.column_name);
          column_payload.put('data_type', c.data_type);
          column_payload.put('data_length', c.data_length);
          IF c.data_precision IS NULL THEN column_payload.put_null('data_precision'); ELSE column_payload.put('data_precision', c.data_precision); END IF;
          IF c.data_scale IS NULL THEN column_payload.put_null('data_scale'); ELSE column_payload.put('data_scale', c.data_scale); END IF;
          IF c.char_used IS NULL THEN column_payload.put_null('char_used'); ELSE column_payload.put('char_used', c.char_used); END IF;
          IF c.char_length IS NULL THEN column_payload.put_null('char_length'); ELSE column_payload.put('char_length', c.char_length); END IF;
          column_payload.put('nullable', c.nullable);
          column_payload.put('position', c.column_id);
          column_rows.append(column_payload);
        END IF;
      END LOOP;

      IF column_names.get_size = 0 THEN
        RAISE_APPLICATION_ERROR(-20994, 'baseline-data table ' || l_table_name || ' is missing or has no exportable columns');
      END IF;
      FOR required_index IN 0 .. key_columns.get_size - 1 LOOP
        excluded := TRUE;
        FOR column_index IN 0 .. column_names.get_size - 1 LOOP
          IF UPPER(column_names.get_string(column_index)) = UPPER(key_columns.get_string(required_index)) THEN excluded := FALSE; EXIT; END IF;
        END LOOP;
        IF excluded THEN RAISE_APPLICATION_ERROR(-20994, 'baseline-data natural key column is unavailable in ' || l_table_name); END IF;
      END LOOP;
      FOR required_index IN 0 .. label_columns.get_size - 1 LOOP
        excluded := TRUE;
        FOR column_index IN 0 .. column_names.get_size - 1 LOOP
          IF UPPER(column_names.get_string(column_index)) = UPPER(label_columns.get_string(required_index)) THEN excluded := FALSE; EXIT; END IF;
        END LOOP;
        IF excluded THEN RAISE_APPLICATION_ERROR(-20994, 'baseline-data label column is unavailable in ' || l_table_name); END IF;
      END LOOP;

      order_list := NULL;
      FOR key_index IN 0 .. key_columns.get_size - 1 LOOP
        IF order_list IS NOT NULL THEN order_list := order_list || ', '; END IF;
        order_list := order_list || 't.' || DBMS_ASSERT.ENQUOTE_NAME(key_columns.get_string(key_index), FALSE);
      END LOOP;
      dynamic_sql := 'SELECT COUNT(*) FROM ' || DBMS_ASSERT.ENQUOTE_NAME(c_schema, FALSE) || '.' || DBMS_ASSERT.ENQUOTE_NAME(l_table_name, FALSE);
      EXECUTE IMMEDIATE dynamic_sql INTO total_rows;
      IF total_rows > row_limit THEN
        RAISE_APPLICATION_ERROR(-20995, 'baseline-data table ' || l_table_name || ' reached rowLimit ' || row_limit || '; no partial export was emitted');
      END IF;

      actual_identity_column := NULL;
      actual_identity_generation := NULL;
      identity_found := FALSE;
      BEGIN
        EXECUTE IMMEDIATE 'SELECT column_name, generation_type FROM all_tab_identity_cols WHERE owner = :1 AND table_name = :2'
          INTO actual_identity_column, actual_identity_generation USING c_schema, l_table_name;
        identity_found := TRUE;
      EXCEPTION
        WHEN NO_DATA_FOUND THEN NULL;
      END;
      expected_identity_element := spec.get('identity');
      IF expected_identity_element IS NOT NULL AND NOT expected_identity_element.is_null THEN
        expected_identity := TREAT(expected_identity_element AS JSON_OBJECT_T);
        IF NOT identity_found OR UPPER(expected_identity.get_string('column')) != UPPER(actual_identity_column)
           OR UPPER(REPLACE(expected_identity.get_string('generationType'), ' ', '')) != UPPER(REPLACE(actual_identity_generation, ' ', '')) THEN
          RAISE_APPLICATION_ERROR(-20994, 'baseline-data identity configuration does not match ' || l_table_name);
        END IF;
        table_payload.put('identity', expected_identity);
      ELSE
        IF identity_found THEN
          RAISE_APPLICATION_ERROR(-20994, 'baseline-data identity configuration is null but ' || l_table_name || ' has an identity column');
        END IF;
        table_payload.put_null('identity');
      END IF;

      table_payload.put('columns', column_rows);
      table_payload.put('rowCount', total_rows);
      table_rows := JSON_ARRAY_T();
      table_pages := JSON_ARRAY_T();
      offset_rows := 0;
      LOOP
        page_rows := 0;
        dynamic_sql := 'SELECT JSON_OBJECT(';
        FOR column_index IN 0 .. column_names.get_size - 1 LOOP
          IF column_index > 0 THEN dynamic_sql := dynamic_sql || ', '; END IF;
          column_name := column_names.get_string(column_index);
          dynamic_sql := dynamic_sql || 'KEY ''' || REPLACE(column_name, '''', '''''') || ''' VALUE t.' || DBMS_ASSERT.ENQUOTE_NAME(column_name, FALSE);
        END LOOP;
        dynamic_sql := dynamic_sql || ' NULL ON NULL RETURNING CLOB) FROM ' || DBMS_ASSERT.ENQUOTE_NAME(c_schema, FALSE) || '.' || DBMS_ASSERT.ENQUOTE_NAME(l_table_name, FALSE)
          || ' t ORDER BY ' || order_list || ' OFFSET :1 ROWS FETCH NEXT :2 ROWS ONLY';
        OPEN l_cursor FOR dynamic_sql USING offset_rows, c_page_size;
        LOOP
          FETCH l_cursor INTO l_row_json;
          EXIT WHEN l_cursor%NOTFOUND;
          page_rows := page_rows + 1;
          table_rows.append(JSON_OBJECT_T(l_row_json));
        END LOOP;
        CLOSE l_cursor;
        table_pages.append(page_rows);
        EXIT WHEN page_rows < c_page_size;
        offset_rows := offset_rows + c_page_size;
      END LOOP;
      IF table_rows.get_size != total_rows THEN
        RAISE_APPLICATION_ERROR(-20994, 'baseline-data table ' || l_table_name || ' row count changed during paged export');
      END IF;
      table_payload.put('rows', table_rows);
      table_payload.put('pages', table_pages);
      table_payload.put('complete', TRUE);
      l_rows.append(table_payload);
    END LOOP;

    IF l_rows.get_size = 0 THEN
      l_pages.append(0);
    ELSE
      FOR page_index IN 0 .. TRUNC((l_rows.get_size - 1) / c_page_size) LOOP
        l_pages.append(LEAST(c_page_size, l_rows.get_size - page_index * c_page_size));
      END LOOP;
      IF MOD(l_rows.get_size, c_page_size) = 0 THEN l_pages.append(0); END IF;
    END IF;
    l_sections.put('baseline-data', l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', TRUE);
    l_page.put('pages', l_pages);
    l_coverage_sections.put('baseline-data', l_page);
  EXCEPTION
    WHEN OTHERS THEN
      IF l_cursor%ISOPEN THEN CLOSE l_cursor; END IF;
      RAISE;
  END;

  PROCEDURE add_views IS
  BEGIN
    IF NOT selected('views') THEN RETURN; END IF;
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    l_offset := 0;
    l_total := 0;
    LOOP
      l_page_count := 0;
      FOR view_row IN (
        SELECT v.view_name, o.status
          FROM all_views v
          JOIN all_objects o ON o.owner = v.owner AND o.object_name = v.view_name AND o.object_type = 'VIEW'
         WHERE v.owner = c_schema
         ORDER BY v.view_name
         OFFSET l_offset ROWS FETCH NEXT c_page_size ROWS ONLY
      ) LOOP
        IF l_total >= c_max_rows THEN
          RAISE_APPLICATION_ERROR(-20995, 'compare-env views exceeded its 100000-row cap');
        END IF;
        l_view_ddl := DBMS_METADATA.GET_DDL('VIEW', view_row.view_name, c_schema);
        fingerprint(l_view_ddl, l_view_lines, l_view_hash);
        l_page := JSON_OBJECT_T();
        l_page.put('name', view_row.view_name);
        l_page.put('status', view_row.status);
        l_page.put('line_count', l_view_lines);
        l_page.put('source_hash', l_view_hash);
        l_rows.append(l_page);
        l_page_count := l_page_count + 1;
        l_total := l_total + 1;
      END LOOP;
      l_pages.append(l_page_count);
      EXIT WHEN l_page_count < c_page_size;
      l_offset := l_offset + c_page_size;
    END LOOP;
    l_sections.put('views', l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', TRUE);
    l_page.put('pages', l_pages);
    l_coverage_sections.put('views', l_page);
  EXCEPTION
    WHEN OTHERS THEN
      RAISE;
  END;

  PROCEDURE add_baseline_views IS
    view_cursor INTEGER;
    view_execute INTEGER;
    view_offset INTEGER;
    view_piece_length INTEGER;
    view_piece VARCHAR2(32767);
    view_text CLOB;
    view_ddl CLOB;
    view_json JSON_OBJECT_T;
  BEGIN
    IF NOT selected('baseline-views') THEN RETURN; END IF;
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    l_offset := 0;
    l_total := 0;
    LOOP
      l_page_count := 0;
      FOR view_row IN (
        SELECT v.view_name
          FROM all_views v
         WHERE v.owner = c_schema
           AND (DBMS_LOB.GETLENGTH(c_baseline_prefixes_json) <= 2 OR EXISTS (
             SELECT 1 FROM JSON_TABLE(c_baseline_prefixes_json, '$[*]' COLUMNS (prefix VARCHAR2(128) PATH '$')) p
              WHERE SUBSTR(v.view_name, 1, LENGTH(p.prefix)) = UPPER(p.prefix)))
           AND (DBMS_LOB.GETLENGTH(c_baseline_excluded_json) <= 2 OR NOT EXISTS (
             SELECT 1 FROM JSON_TABLE(c_baseline_excluded_json, '$[*]' COLUMNS (object_name VARCHAR2(128) PATH '$')) e
              WHERE v.view_name = UPPER(e.object_name)))
         ORDER BY v.view_name OFFSET l_offset ROWS FETCH NEXT c_page_size ROWS ONLY
      ) LOOP
        IF l_total >= c_max_rows THEN
          RAISE_APPLICATION_ERROR(-20995, 'compare-env baseline-views exceeded its 100000-row cap');
        END IF;
        DBMS_LOB.CREATETEMPORARY(view_text, TRUE);
        view_cursor := DBMS_SQL.OPEN_CURSOR;
        DBMS_SQL.PARSE(view_cursor,
          'SELECT text FROM all_views WHERE owner = :owner AND view_name = :name', DBMS_SQL.NATIVE);
        DBMS_SQL.BIND_VARIABLE(view_cursor, ':owner', c_schema);
        DBMS_SQL.BIND_VARIABLE(view_cursor, ':name', view_row.view_name);
        DBMS_SQL.DEFINE_COLUMN_LONG(view_cursor, 1);
        view_execute := DBMS_SQL.EXECUTE(view_cursor);
        IF DBMS_SQL.FETCH_ROWS(view_cursor) = 0 THEN
          RAISE_APPLICATION_ERROR(-20996, 'compare-env could not read ALL_VIEWS.TEXT for ' || view_row.view_name);
        END IF;
        view_offset := 0;
        LOOP
          DBMS_SQL.COLUMN_VALUE_LONG(view_cursor, 1, 32767, view_offset, view_piece, view_piece_length);
          EXIT WHEN view_piece_length = 0;
          DBMS_LOB.WRITEAPPEND(view_text, LENGTH(view_piece), view_piece);
          view_offset := view_offset + view_piece_length;
        END LOOP;
        DBMS_SQL.CLOSE_CURSOR(view_cursor);
        view_cursor := NULL;
        view_ddl := DBMS_METADATA.GET_DDL('VIEW', view_row.view_name, c_schema);
        view_json := JSON_OBJECT_T();
        view_json.put('name', view_row.view_name);
        view_json.put('text', view_text);
        view_json.put('ddl', view_ddl);
        l_rows.append(view_json);
        DBMS_LOB.FREETEMPORARY(view_text);
        l_page_count := l_page_count + 1;
        l_total := l_total + 1;
      END LOOP;
      l_pages.append(l_page_count);
      EXIT WHEN l_page_count < c_page_size;
      l_offset := l_offset + c_page_size;
    END LOOP;
    l_sections.put('baseline-views', l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', TRUE);
    l_page.put('pages', l_pages);
    l_coverage_sections.put('baseline-views', l_page);
  EXCEPTION
    WHEN OTHERS THEN
      IF view_cursor IS NOT NULL AND DBMS_SQL.IS_OPEN(view_cursor) THEN DBMS_SQL.CLOSE_CURSOR(view_cursor); END IF;
      IF DBMS_LOB.ISTEMPORARY(view_text) = 1 THEN DBMS_LOB.FREETEMPORARY(view_text); END IF;
      RAISE;
  END;

  PROCEDURE add_triggers IS
    line_count_value PLS_INTEGER;
    hash_sum_value NUMBER;
  BEGIN
    IF NOT selected('triggers') THEN RETURN; END IF;
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    l_offset := 0;
    l_total := 0;
    LOOP
      l_page_count := 0;
      FOR trigger_row IN (
        SELECT t.trigger_name, t.table_name, t.trigger_type, t.triggering_event,
            t.base_object_type, t.status
          FROM all_triggers t WHERE t.owner = c_schema
         ORDER BY t.trigger_name
         OFFSET l_offset ROWS FETCH NEXT c_page_size ROWS ONLY
      ) LOOP
        IF l_total >= c_max_rows THEN
          RAISE_APPLICATION_ERROR(-20995, 'compare-env triggers exceeded its 100000-row cap');
        END IF;
        fingerprint_source(trigger_row.trigger_name, 'TRIGGER', line_count_value, hash_sum_value);
        l_page := JSON_OBJECT_T();
        l_page.put('name', trigger_row.trigger_name);
        l_page.put('table_name', trigger_row.table_name);
        l_page.put('trigger_type', trigger_row.trigger_type);
        l_page.put('triggering_event', trigger_row.triggering_event);
        l_page.put('base_object_type', trigger_row.base_object_type);
        l_page.put('status', trigger_row.status);
        l_page.put('line_count', line_count_value);
        l_page.put('source_hash', hash_sum_value);
        l_rows.append(l_page);
        l_page_count := l_page_count + 1;
        l_total := l_total + 1;
      END LOOP;
      l_pages.append(l_page_count);
      EXIT WHEN l_page_count < c_page_size;
      l_offset := l_offset + c_page_size;
    END LOOP;
    l_sections.put('triggers', l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', TRUE);
    l_page.put('pages', l_pages);
    l_coverage_sections.put('triggers', l_page);
  END;

  PROCEDURE add_stored_code IS
    line_count_value PLS_INTEGER;
    hash_sum_value NUMBER;
  BEGIN
    IF NOT selected('stored-code') THEN RETURN; END IF;
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    l_offset := 0;
    l_total := 0;
    LOOP
      l_page_count := 0;
      FOR code_row IN (
        SELECT s.name, s.type, MAX(o.status) status
          FROM all_source s
          JOIN all_objects o ON o.owner = s.owner AND o.object_name = s.name AND o.object_type = s.type
         WHERE s.owner = c_schema
           AND s.type IN ('FUNCTION','PROCEDURE','PACKAGE','PACKAGE BODY','TYPE','TYPE BODY')
         GROUP BY s.name, s.type
         ORDER BY s.type, s.name
         OFFSET l_offset ROWS FETCH NEXT c_page_size ROWS ONLY
      ) LOOP
        IF l_total >= c_max_rows THEN
          RAISE_APPLICATION_ERROR(-20995, 'compare-env stored-code exceeded its 100000-row cap');
        END IF;
        fingerprint_source(code_row.name, code_row.type, line_count_value, hash_sum_value);
        l_page := JSON_OBJECT_T();
        l_page.put('name', code_row.name);
        l_page.put('type', code_row.type);
        l_page.put('status', code_row.status);
        l_page.put('line_count', line_count_value);
        l_page.put('source_hash', hash_sum_value);
        l_rows.append(l_page);
        l_page_count := l_page_count + 1;
        l_total := l_total + 1;
      END LOOP;
      l_pages.append(l_page_count);
      EXIT WHEN l_page_count < c_page_size;
      l_offset := l_offset + c_page_size;
    END LOOP;
    l_sections.put('stored-code', l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', TRUE);
    l_page.put('pages', l_pages);
    l_coverage_sections.put('stored-code', l_page);
  END;

BEGIN
  DBMS_OUTPUT.ENABLE(NULL);
  IF NOT c_dba_mode AND SYS_CONTEXT('USERENV', 'SESSION_USER') != c_expected_user THEN
    RAISE_APPLICATION_ERROR(-20980, 'SQLcl session user did not match selected compare-env target');
  END IF;
  IF SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') != c_schema THEN
    RAISE_APPLICATION_ERROR(-20981, 'SQLcl current schema did not match selected compare-env schema');
  END IF;
  IF c_schema IS NULL OR c_environment NOT IN ('dev', 'staging', 'prod') THEN
    RAISE_APPLICATION_ERROR(-20982, 'compare-env target identity is invalid');
  END IF;

  l_identity.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));
  l_identity.put('current_schema', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'));
  l_identity.put('db_name', SYS_CONTEXT('USERENV', 'DB_NAME'));
  l_identity.put('db_unique_name', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'));
  l_identity.put('service_name', SYS_CONTEXT('USERENV', 'SERVICE_NAME'));
  l_identity.put('container_id', SYS_CONTEXT('USERENV', 'CON_ID'));
  l_identity.put('container_name', SYS_CONTEXT('USERENV', 'CON_NAME'));
  l_identity.put('edition', SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'));
  SELECT MAX(version) INTO l_db_version
    FROM product_component_version
   WHERE product LIKE 'Oracle Database%';
  l_identity.put('database_version', l_db_version);

  add_query_section('tables', q'~
    SELECT JSON_OBJECT('name' VALUE t.table_name, 'temporary' VALUE t.temporary,
      'partitioned' VALUE t.partitioned, 'iot_type' VALUE t.iot_type,
      'nested' VALUE t.nested, 'secondary' VALUE t.secondary,
      'compression' VALUE t.compression, 'logging' VALUE t.logging RETURNING CLOB) row_json
      FROM all_tables t WHERE t.owner = :scope
     ORDER BY t.table_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('columns', q'~
    SELECT JSON_OBJECT('table_name' VALUE c.table_name, 'name' VALUE c.column_name,
      'data_type' VALUE c.data_type, 'data_type_mod' VALUE c.data_type_mod,
      'data_type_owner' VALUE c.data_type_owner, 'data_length' VALUE c.data_length,
      'data_precision' VALUE c.data_precision, 'data_scale' VALUE c.data_scale,
      'char_used' VALUE c.char_used, 'char_length' VALUE c.char_length,
      'nullable' VALUE c.nullable, 'hidden' VALUE CASE WHEN c.hidden_column = 'YES' THEN 'Y' ELSE 'N' END,
      'virtual' VALUE CASE WHEN c.virtual_column = 'YES' THEN 'Y' ELSE 'N' END RETURNING CLOB) row_json
      FROM all_tab_cols c WHERE c.owner = :scope
     ORDER BY c.table_name, c.column_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('constraints', q'~
    SELECT JSON_OBJECT('name' VALUE c.constraint_name, 'table_name' VALUE c.table_name,
      'constraint_type' VALUE c.constraint_type,
      'columns' VALUE COALESCE((SELECT JSON_ARRAYAGG(cc.column_name ORDER BY cc.position RETURNING CLOB)
        FROM all_cons_columns cc WHERE cc.owner = c.owner AND cc.constraint_name = c.constraint_name), TO_CLOB('[]')) FORMAT JSON,
      'referenced_table' VALUE CASE WHEN c.r_constraint_name IS NULL THEN NULL ELSE
        CASE WHEN c.r_owner = c_schema THEN '<CONFIGURED_SCHEMA>' ELSE c.r_owner END || '.' ||
        (SELECT r.table_name FROM all_constraints r WHERE r.owner = c.r_owner AND r.constraint_name = c.r_constraint_name) END,
      'referenced_columns' VALUE COALESCE((SELECT JSON_ARRAYAGG(rc.column_name ORDER BY rc.position RETURNING CLOB)
        FROM all_cons_columns rc WHERE rc.owner = c.r_owner AND rc.constraint_name = c.r_constraint_name), TO_CLOB('[]')) FORMAT JSON,
      'condition' VALUE c.search_condition_vc,
      'condition_truncated' VALUE CASE WHEN LENGTHB(c.search_condition_vc) >= 4000 THEN 'Y' ELSE 'N' END,
      'status' VALUE c.status, 'validated' VALUE c.validated,
      'delete_rule' VALUE c.delete_rule, 'deferrable' VALUE c.deferrable, 'deferred' VALUE c.deferred,
      'rely' VALUE c.rely RETURNING CLOB) row_json
      FROM all_constraints c WHERE c.owner = :scope
     ORDER BY c.table_name, c.constraint_type, c.constraint_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('indexes', q'~
    SELECT JSON_OBJECT('name' VALUE i.index_name, 'table_name' VALUE i.table_name,
      'uniqueness' VALUE i.uniqueness,
      'columns' VALUE COALESCE((SELECT JSON_ARRAYAGG(JSON_OBJECT('name' VALUE ic.column_name,
        'position' VALUE ic.column_position, 'descend' VALUE ic.descend RETURNING CLOB)
        ORDER BY ic.column_position RETURNING CLOB) FROM all_ind_columns ic
        WHERE ic.index_owner = i.owner AND ic.index_name = i.index_name), TO_CLOB('[]')) FORMAT JSON,
      'index_type' VALUE i.index_type, 'visibility' VALUE i.visibility, 'status' VALUE i.status RETURNING CLOB) row_json
      FROM all_indexes i WHERE i.owner = :scope
     ORDER BY i.index_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_triggers;

  add_query_section('sequences', q'~
    SELECT JSON_OBJECT('name' VALUE s.sequence_name, 'increment_by' VALUE s.increment_by,
      'min_value' VALUE s.min_value, 'max_value' VALUE s.max_value, 'cycle_flag' VALUE s.cycle_flag,
      'order_flag' VALUE s.order_flag, 'cache_size' VALUE s.cache_size RETURNING CLOB) row_json
      FROM all_sequences s WHERE s.sequence_owner = :scope
     ORDER BY s.sequence_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('synonyms', q'~
    SELECT JSON_OBJECT('owner' VALUE s.owner, 'name' VALUE s.synonym_name,
      'table_owner' VALUE s.table_owner, 'table_name' VALUE s.table_name, 'db_link' VALUE s.db_link RETURNING CLOB) row_json
      FROM all_synonyms s WHERE s.owner = :scope OR s.owner = 'PUBLIC'
     ORDER BY s.owner, s.synonym_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_views;

  add_stored_code;

  IF c_baseline_internal THEN
    add_query_section('baseline-source', q'~
      SELECT JSON_OBJECT('name' VALUE s.name, 'type' VALUE s.type, 'line' VALUE s.line,
        'text' VALUE s.text NULL ON NULL RETURNING CLOB) row_json
        FROM all_source s
       WHERE s.owner = :scope
         AND s.type IN ('PACKAGE','PACKAGE BODY','TRIGGER','FUNCTION','PROCEDURE','TYPE','TYPE BODY')
         AND (DBMS_LOB.GETLENGTH(:baseline_prefixes) <= 2 OR EXISTS (
           SELECT 1 FROM JSON_TABLE(:baseline_prefixes, '$[*]' COLUMNS (prefix VARCHAR2(128) PATH '$')) p
            WHERE SUBSTR(s.name, 1, LENGTH(p.prefix)) = UPPER(p.prefix)))
         AND (DBMS_LOB.GETLENGTH(:baseline_excluded) <= 2 OR NOT EXISTS (
           SELECT 1 FROM JSON_TABLE(:baseline_excluded, '$[*]' COLUMNS (object_name VARCHAR2(128) PATH '$')) e
            WHERE s.name = UPPER(e.object_name)))
       ORDER BY s.type, s.name, s.line OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

    add_query_section('baseline-settings', q'~
      SELECT JSON_OBJECT('name' VALUE p.name, 'type' VALUE p.type,
        'plsql_optimize_level' VALUE p.plsql_optimize_level,
        'plsql_code_type' VALUE p.plsql_code_type, 'plsql_debug' VALUE p.plsql_debug,
        'plsql_warnings' VALUE p.plsql_warnings, 'nls_length_semantics' VALUE p.nls_length_semantics,
        'plsql_ccflags' VALUE p.plsql_ccflags, 'plscope_settings' VALUE p.plscope_settings RETURNING CLOB) row_json
       FROM all_plsql_object_settings p
       WHERE p.owner = :scope
         AND (DBMS_LOB.GETLENGTH(:baseline_prefixes) <= 2 OR EXISTS (
           SELECT 1 FROM JSON_TABLE(:baseline_prefixes, '$[*]' COLUMNS (prefix VARCHAR2(128) PATH '$')) x
            WHERE SUBSTR(p.name, 1, LENGTH(x.prefix)) = UPPER(x.prefix)))
         AND (DBMS_LOB.GETLENGTH(:baseline_excluded) <= 2 OR NOT EXISTS (
           SELECT 1 FROM JSON_TABLE(:baseline_excluded, '$[*]' COLUMNS (object_name VARCHAR2(128) PATH '$')) x
            WHERE p.name = UPPER(x.object_name)))
       ORDER BY p.type, p.name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

    add_baseline_views;
    add_baseline_data;
  END IF;

  add_query_section('invalid-objects', q'~
    SELECT JSON_OBJECT('name' VALUE o.object_name, 'type' VALUE o.object_type, 'status' VALUE o.status RETURNING CLOB) row_json
      FROM all_objects o WHERE o.owner = :scope AND o.status = 'INVALID' AND o.subobject_name IS NULL
     ORDER BY o.object_type, o.object_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('identity-columns', q'~
    SELECT JSON_OBJECT('table_name' VALUE i.table_name, 'column_name' VALUE i.column_name,
      'generation_type' VALUE i.generation_type, 'identity_options' VALUE i.identity_options,
      'sequence_name' VALUE i.sequence_name RETURNING CLOB) row_json
      FROM all_tab_identity_cols i WHERE i.owner = :scope
     ORDER BY i.table_name, i.column_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('object-grants', q'~
    SELECT JSON_OBJECT('owner' VALUE p.table_schema, 'object_name' VALUE p.table_name,
      'grantee' VALUE p.grantee, 'privilege' VALUE p.privilege, 'grantable' VALUE p.grantable RETURNING CLOB) row_json
      FROM all_tab_privs p WHERE p.table_schema = :scope
       AND (DBMS_LOB.GETLENGTH(:baseline_prefixes) <= 2 OR EXISTS (
         SELECT 1 FROM JSON_TABLE(:baseline_prefixes, '$[*]' COLUMNS (prefix VARCHAR2(128) PATH '$')) x
          WHERE SUBSTR(p.table_name, 1, LENGTH(x.prefix)) = UPPER(x.prefix)))
       AND (DBMS_LOB.GETLENGTH(:baseline_excluded) <= 2 OR NOT EXISTS (
         SELECT 1 FROM JSON_TABLE(:baseline_excluded, '$[*]' COLUMNS (object_name VARCHAR2(128) PATH '$')) x
          WHERE p.table_name = UPPER(x.object_name)))
     ORDER BY p.table_name, p.grantee, p.privilege OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  add_query_section('java-mle', q'~
    SELECT JSON_OBJECT('name' VALUE o.object_name, 'type' VALUE o.object_type, 'status' VALUE o.status RETURNING CLOB) row_json
      FROM all_objects o WHERE o.owner = :scope
       AND o.object_type IN ('JAVA CLASS','JAVA RESOURCE','JAVA SOURCE','MLE MODULE','MLE ENVIRONMENT')
     ORDER BY o.object_type, o.object_name OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

  IF c_dba_mode THEN
    add_query_section('system-privileges', q'~
      SELECT JSON_OBJECT('grantee' VALUE p.grantee, 'privilege' VALUE p.privilege, 'admin_option' VALUE p.admin_option RETURNING CLOB) row_json
        FROM dba_sys_privs p WHERE p.grantee = :scope
       ORDER BY p.privilege OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');
    add_query_section('roles', q'~
      SELECT JSON_OBJECT('grantee' VALUE p.grantee, 'role' VALUE p.granted_role,
        'admin_option' VALUE p.admin_option, 'default_role' VALUE p.default_role RETURNING CLOB) row_json
        FROM dba_role_privs p WHERE p.grantee = :scope
       ORDER BY p.granted_role OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');
    add_query_section('network-aces', q'~
      WITH selected_schema AS (SELECT :scope schema_name FROM dual),
      selected_principals AS (
        SELECT schema_name principal FROM selected_schema
        UNION ALL
        SELECT p.granted_role FROM dba_role_privs p JOIN selected_schema s ON s.schema_name = p.grantee
        UNION ALL
        SELECT 'PUBLIC' FROM dual
      )
      SELECT JSON_OBJECT('host' VALUE a.host, 'lower_port' VALUE a.lower_port, 'upper_port' VALUE a.upper_port,
        'principal' VALUE a.principal, 'principal_type' VALUE a.principal_type, 'privilege' VALUE a.privilege,
        'grant_type' VALUE a.grant_type, 'inverted_principal' VALUE a.inverted_principal,
        'ace_order' VALUE a.ace_order,
        'start_date' VALUE TO_CHAR(a.start_date, 'YYYY-MM-DD"T"HH24:MI:SS TZH:TZM'),
        'end_date' VALUE TO_CHAR(a.end_date, 'YYYY-MM-DD"T"HH24:MI:SS TZH:TZM') RETURNING CLOB) row_json
        FROM dba_host_aces a JOIN selected_principals p ON p.principal = a.principal
       ORDER BY a.host, a.lower_port, a.upper_port, a.ace_order OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');

    BEGIN
      SELECT COUNT(*) INTO l_ords_count FROM dba_objects
       WHERE owner = 'ORDS_METADATA' AND object_type = 'TABLE'
         AND object_name IN ('ORDS_SCHEMAS','ORDS_MODULES','ORDS_TEMPLATES','ORDS_HANDLERS');
      IF l_ords_count < 4 THEN
        add_unavailable('ords', 'ORDS metadata catalog is not installed or readable through the DBA connection');
      ELSE
        add_query_section('ords', q'~
          SELECT row_json FROM (
          WITH selected_schema AS (SELECT :scope schema_name FROM dual)
          SELECT 'schema' kind, s.schema module_name, CAST(NULL AS VARCHAR2(4000)) uri_template,
              CAST(NULL AS VARCHAR2(30)) method,
              JSON_OBJECT('kind' VALUE 'schema', 'name' VALUE s.schema,
                'enabled' VALUE CASE WHEN UPPER(TO_CHAR(s.enabled)) IN ('YES','Y','TRUE','1') THEN 'true' ELSE 'false' END FORMAT JSON,
                'url_mapping_type' VALUE s.url_mapping_type, 'url_mapping_pattern' VALUE s.url_mapping_pattern,
                'auto_rest_auth' VALUE CASE WHEN UPPER(TO_CHAR(s.auto_rest_auth)) IN ('YES','Y','TRUE','1') THEN 'true' ELSE 'false' END FORMAT JSON RETURNING CLOB) row_json
              FROM ords_metadata.ords_schemas s JOIN selected_schema x ON x.schema_name = s.schema
            UNION ALL
            SELECT 'module', s.schema, CAST(NULL AS VARCHAR2(4000)), CAST(NULL AS VARCHAR2(30)),
              JSON_OBJECT('kind' VALUE 'module', 'name' VALUE m.name, 'module_name' VALUE m.name,
                'uri_prefix' VALUE m.uri_prefix, 'status' VALUE m.status, 'module_type' VALUE m.module_type RETURNING CLOB)
              FROM ords_metadata.ords_modules m JOIN ords_metadata.ords_schemas s ON s.id = m.schema_id
                JOIN selected_schema x ON x.schema_name = s.schema
            UNION ALL
            SELECT 'template', m.name, t.uri_template, CAST(NULL AS VARCHAR2(30)),
              JSON_OBJECT('kind' VALUE 'template', 'module_name' VALUE m.name, 'name' VALUE t.uri_template,
                'uri_template' VALUE t.uri_template, 'priority' VALUE t.priority, 'status' VALUE m.status RETURNING CLOB)
              FROM ords_metadata.ords_templates t JOIN ords_metadata.ords_modules m ON m.id = t.module_id
                JOIN ords_metadata.ords_schemas s ON s.id = m.schema_id
                JOIN selected_schema x ON x.schema_name = s.schema
            UNION ALL
            SELECT 'handler', m.name, t.uri_template, h.method,
              JSON_OBJECT('kind' VALUE 'handler', 'module_name' VALUE m.name, 'uri_template' VALUE t.uri_template,
                'method' VALUE h.method, 'source_type' VALUE h.source_type, 'items_per_page' VALUE h.items_per_page,
                'mime_type' VALUE h.mimetype, 'format' VALUE h.format, 'status' VALUE m.status RETURNING CLOB)
              FROM ords_metadata.ords_handlers h JOIN ords_metadata.ords_templates t ON t.id = h.template_id
                JOIN ords_metadata.ords_modules m ON m.id = t.module_id JOIN ords_metadata.ords_schemas s ON s.id = m.schema_id
                JOIN selected_schema x ON x.schema_name = s.schema
          ) ORDER BY kind, module_name, uri_template, method OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        add_unavailable('ords', 'ORDS catalog query is unavailable: ' || SQLERRM);
    END;

    add_query_section('installed-options', q'~
      SELECT row_json FROM (
        SELECT parameter name, 'OPTION' option_type, value, CAST(NULL AS VARCHAR2(30)) status,
          JSON_OBJECT('name' VALUE 'OPTION:' || parameter, 'value' VALUE value RETURNING CLOB) row_json
          FROM v$option WHERE parameter IN ('Java', 'Spatial')
        UNION ALL
        SELECT comp_id, 'COMPONENT', version, status,
          JSON_OBJECT('name' VALUE 'COMPONENT:' || comp_id, 'value' VALUE version, 'status' VALUE status RETURNING CLOB)
          FROM dba_registry WHERE comp_id IN ('JAVAVM', 'SDO')
      ) WHERE :scope IS NOT NULL ORDER BY name, option_type OFFSET :page_offset ROWS FETCH NEXT :page_size ROWS ONLY~');
  ELSE
    FOR section_name IN (SELECT 'system-privileges' name FROM dual UNION ALL SELECT 'roles' FROM dual UNION ALL SELECT 'network-aces' FROM dual UNION ALL SELECT 'ords' FROM dual UNION ALL SELECT 'installed-options' FROM dual) LOOP
      IF selected(section_name.name) THEN
        add_unavailable(section_name.name, 'not compared (no DBA connection)');
      END IF;
    END LOOP;
  END IF;

  IF selected('versions') THEN
    l_rows := JSON_ARRAY_T();
    l_pages := JSON_ARRAY_T();
    SELECT MAX(version) INTO l_db_version FROM product_component_version WHERE product LIKE 'Oracle Database%';
    BEGIN
    EXECUTE IMMEDIATE 'SELECT APEX_RELEASE.VERSION_NO FROM DUAL' INTO l_apex_version;
    EXCEPTION
      WHEN OTHERS THEN
        l_apex_version := NULL;
        l_apex_version_available := FALSE;
        l_unavailable.put('versions', 'APEX version is not installed or accessible');
    END;
    l_page := JSON_OBJECT_T();
    l_page.put('component', 'database');
    l_page.put('version', l_db_version);
    l_rows.append(l_page);
    l_page := JSON_OBJECT_T();
    l_page.put('component', 'apex');
    l_page.put('version', l_apex_version);
    l_rows.append(l_page);
    l_pages.append(2);
    l_sections.put('versions', l_rows);
    l_page := JSON_OBJECT_T();
    l_page.put('complete', l_apex_version_available);
    l_page.put('pages', l_pages);
    IF NOT l_apex_version_available THEN
      l_page.put('reason', 'APEX version is not installed or accessible');
    END IF;
    l_coverage_sections.put('versions', l_page);
  END IF;

  l_completed_at := TO_CHAR(SYSTIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"');
  l_identity.put('database_version', l_db_version);
  l_coverage.put('complete', l_unavailable.get_size = 0);
  l_coverage.put('pageSize', c_page_size);
  l_coverage.put('maxRows', c_max_rows);
  l_coverage.put('sections', l_coverage_sections);
  l_payload.put('schemaVersion', 2);
  l_payload.put('phase', 'compare-env');
  l_payload.put('environment', c_environment);
  l_payload.put('schema', c_schema);
  l_payload.put('identity', l_identity);
  l_payload.put('started_at', l_started_at);
  l_payload.put('completed_at', l_completed_at);
  l_payload.put('coverage', l_coverage);
  l_payload.put('sections', l_sections);
  l_payload.put('unavailable', l_unavailable);

  l_raw := l_payload.to_clob();
  IF DBMS_LOB.GETLENGTH(l_raw) > 128 * 1024 * 1024 THEN
    RAISE_APPLICATION_ERROR(-20996, 'compare-env catalog exceeded its 128 MiB payload cap');
  END IF;
  DBMS_LOB.CREATETEMPORARY(l_blob, TRUE);
  DBMS_LOB.CONVERTTOBLOB(
    dest_lob => l_blob, src_clob => l_raw, amount => DBMS_LOB.LOBMAXSIZE,
    dest_offset => l_dest, src_offset => l_src, blob_csid => NLS_CHARSET_ID('AL32UTF8'),
    lang_context => l_ctx, warning => l_warning
  );
  IF l_warning != 0 THEN RAISE_APPLICATION_ERROR(-20997, 'Unicode conversion warning during compare-env catalog compression'); END IF;
  IF DBMS_LOB.GETLENGTH(l_blob) > 128 * 1024 * 1024 THEN
    RAISE_APPLICATION_ERROR(-20996, 'compare-env catalog exceeded its 128 MiB UTF-8 payload cap');
  END IF;
  l_gzip := UTL_COMPRESS.LZ_COMPRESS(l_blob);
  DBMS_OUTPUT.PUT_LINE('CATALOG_ENCODING:gzip-base64-v1');
  l_pos := 1;
  WHILE l_pos <= DBMS_LOB.GETLENGTH(l_gzip) LOOP
    l_piece := DBMS_LOB.SUBSTR(l_gzip, 12000, l_pos);
    l_chunk := REPLACE(REPLACE(UTL_RAW.CAST_TO_VARCHAR2(UTL_ENCODE.BASE64_ENCODE(l_piece)), CHR(13), ''), CHR(10), '');
    DBMS_OUTPUT.PUT_LINE(l_chunk);
    l_pos := l_pos + UTL_RAW.LENGTH(l_piece);
  END LOOP;
  IF DBMS_LOB.ISTEMPORARY(l_gzip) = 1 THEN DBMS_LOB.FREETEMPORARY(l_gzip); END IF;
  IF DBMS_LOB.ISTEMPORARY(l_blob) = 1 THEN DBMS_LOB.FREETEMPORARY(l_blob); END IF;
  IF DBMS_LOB.ISTEMPORARY(l_raw) = 1 THEN DBMS_LOB.FREETEMPORARY(l_raw); END IF;
EXCEPTION
  WHEN OTHERS THEN
    IF l_cursor%ISOPEN THEN CLOSE l_cursor; END IF;
    IF l_gzip IS NOT NULL AND DBMS_LOB.ISTEMPORARY(l_gzip) = 1 THEN DBMS_LOB.FREETEMPORARY(l_gzip); END IF;
    IF l_blob IS NOT NULL AND DBMS_LOB.ISTEMPORARY(l_blob) = 1 THEN DBMS_LOB.FREETEMPORARY(l_blob); END IF;
    IF l_raw IS NOT NULL AND DBMS_LOB.ISTEMPORARY(l_raw) = 1 THEN DBMS_LOB.FREETEMPORARY(l_raw); END IF;
    ROLLBACK;
    RAISE;
END;
/
PROMPT CATALOG_PAYLOAD_END:compare-env
PROMPT CATALOG_VERIFIED:compare-env
SET DEFINE OFF
