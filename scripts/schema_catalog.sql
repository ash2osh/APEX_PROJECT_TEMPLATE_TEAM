-- Read-only catalog capture. Arguments are phase plus UTF-8 values encoded as hex.
-- The SQLcl wrapper accepts only hex tokens, so object names cannot inject SQL.
SET DEFINE ON
SET ENCODING UTF-8
DEFINE catalog_phase = '&1'
DEFINE target_schema_hex = '&2'
DEFINE expected_user_hex = '&3'
DEFINE selected_keys_hex = '&4'
SET LONG 2000000000
SET LONGCHUNKSIZE 32767
SET SERVEROUTPUT ON SIZE UNLIMITED
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

PROMPT CATALOG_PAYLOAD_BEGIN:&catalog_phase
DECLARE
  c_target_schema CONSTANT VARCHAR2(128) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&target_schema_hex'));
  c_expected_user CONSTANT VARCHAR2(128) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&expected_user_hex'));
  c_selected_json CONSTANT CLOB := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('&&selected_keys_hex'));
  c_phase CONSTANT VARCHAR2(16) := '&&catalog_phase';
  l_payload JSON_OBJECT_T := JSON_OBJECT_T();
  l_identity JSON_OBJECT_T := JSON_OBJECT_T();
  l_coverage JSON_OBJECT_T := JSON_OBJECT_T();
  l_objects JSON_ARRAY_T := JSON_ARRAY_T();
  l_before JSON_ARRAY_T := JSON_ARRAY_T();
  l_after JSON_ARRAY_T := JSON_ARRAY_T();
  l_definitions JSON_ARRAY_T := JSON_ARRAY_T();
  l_synonyms JSON_ARRAY_T := JSON_ARRAY_T();
  l_grants JSON_ARRAY_T := JSON_ARRAY_T();
  l_errors JSON_ARRAY_T := JSON_ARRAY_T();
  l_privileges JSON_ARRAY_T := JSON_ARRAY_T();
  l_catalogs JSON_ARRAY_T := JSON_ARRAY_T();
  l_unsupported JSON_ARRAY_T := JSON_ARRAY_T();
  l_path VARCHAR2(32);
  l_owner_complete BOOLEAN := FALSE;
  l_metadata_role BOOLEAN := FALSE;
  l_found PLS_INTEGER := 0;
  l_started_at VARCHAR2(40) := TO_CHAR(SYSTIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"');
  l_completed_at VARCHAR2(40);
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
  l_owner VARCHAR2(128);
  l_name VARCHAR2(128);
  l_type VARCHAR2(128);
  l_metadata_type VARCHAR2(128);
  l_status VARCHAR2(16);
  l_last_ddl VARCHAR2(32);

  PROCEDURE add_inventory(p_array IN OUT NOCOPY JSON_ARRAY_T) IS
  BEGIN
    FOR item IN (
      SELECT o.owner, o.object_name, o.object_type, o.subobject_name, o.status, o.object_id, o.data_object_id,
             o.timestamp AS object_timestamp,
             TO_CHAR(o.last_ddl_time, 'YYYY-MM-DD"T"HH24:MI:SS') AS last_ddl_time,
             CASE WHEN o.object_type = 'SEQUENCE' AND ids.sequence_name IS NOT NULL THEN 'YES' ELSE 'NO' END AS identity_sequence,
             ids.identity_table_name
        FROM all_objects o
        LEFT JOIN (
          SELECT owner,
                 sequence_name,
                 MIN(table_name) AS identity_table_name
            FROM all_tab_identity_cols
           WHERE owner = c_target_schema
             AND sequence_name IS NOT NULL
           GROUP BY owner, sequence_name
        ) ids
          ON ids.owner = o.owner
         AND ids.sequence_name = o.object_name
         AND o.object_type = 'SEQUENCE'
       WHERE o.owner = c_target_schema
       ORDER BY o.object_name, o.object_type, o.subobject_name
    ) LOOP
      DECLARE
        row_value JSON_OBJECT_T := JSON_OBJECT_T();
      BEGIN
        row_value.put('owner', item.owner);
        row_value.put('name', item.object_name);
        row_value.put('type', item.object_type);
        IF item.subobject_name IS NOT NULL THEN row_value.put('subobject_name', item.subobject_name); END IF;
        row_value.put('status', item.status);
        row_value.put('object_id', item.object_id);
        IF item.data_object_id IS NOT NULL THEN row_value.put('data_object_id', item.data_object_id); END IF;
        row_value.put('object_timestamp', item.object_timestamp);
        row_value.put('last_ddl_time', item.last_ddl_time);
        row_value.put('identity_sequence', item.identity_sequence = 'YES');
        IF item.identity_table_name IS NOT NULL THEN row_value.put('identity_table_name', item.identity_table_name); END IF;
        p_array.append(row_value);
      END;
    END LOOP;
  END;

  PROCEDURE append_synonym(
    p_owner VARCHAR2,
    p_name VARCHAR2,
    p_table_owner VARCHAR2,
    p_table_name VARCHAR2,
    p_db_link VARCHAR2
  ) IS
    row_value JSON_OBJECT_T := JSON_OBJECT_T();
  BEGIN
    row_value.put('owner', p_owner);
    row_value.put('name', p_name);
    row_value.put('table_owner', p_table_owner);
    row_value.put('table_name', p_table_name);
    IF p_db_link IS NOT NULL THEN row_value.put('db_link', p_db_link); END IF;
    l_synonyms.append(row_value);
  END;

  PROCEDURE append_select_grant(
    p_owner VARCHAR2,
    p_table_name VARCHAR2,
    p_grantee VARCHAR2,
    p_privilege VARCHAR2
  ) IS
    row_value JSON_OBJECT_T := JSON_OBJECT_T();
  BEGIN
    row_value.put('owner', p_owner);
    row_value.put('table_name', p_table_name);
    row_value.put('grantee', p_grantee);
    row_value.put('privilege', p_privilege);
    l_grants.append(row_value);
  END;

  PROCEDURE add_dependency_catalogs IS
  BEGIN
    -- Owner sessions can see their own private synonyms and PUBLIC synonyms
    -- through ALL_* views. A validated dictionary reader uses DBA_* so that
    -- CURRENT_SCHEMA does not accidentally limit evidence to SESSION_USER.
    IF l_path = 'METADATA_PRIVILEGE' THEN
      FOR item IN (
        SELECT owner, synonym_name, table_owner, table_name, db_link
          FROM dba_synonyms
         WHERE owner IN (c_target_schema, 'PUBLIC')
           AND table_owner IS NOT NULL
           AND table_name IS NOT NULL
         ORDER BY owner, synonym_name
      ) LOOP
        append_synonym(item.owner, item.synonym_name, item.table_owner, item.table_name, item.db_link);
      END LOOP;
      FOR item IN (
        SELECT DISTINCT privilege_row.owner, privilege_row.table_name, privilege_row.grantee, privilege_row.privilege
          FROM dba_tab_privs privilege_row
         WHERE privilege_row.grantee IN (c_target_schema, 'PUBLIC')
           AND privilege_row.privilege = 'SELECT'
           AND EXISTS (
             SELECT 1
               FROM dba_synonyms synonym_row
              WHERE synonym_row.owner IN (c_target_schema, 'PUBLIC')
                AND synonym_row.table_owner = privilege_row.owner
                AND synonym_row.table_name = privilege_row.table_name
           )
         ORDER BY privilege_row.owner, privilege_row.table_name, privilege_row.grantee
      ) LOOP
        append_select_grant(item.owner, item.table_name, item.grantee, item.privilege);
      END LOOP;
    ELSE
      FOR item IN (
        SELECT owner, synonym_name, table_owner, table_name, db_link
          FROM all_synonyms
         WHERE owner IN (c_target_schema, 'PUBLIC')
           AND table_owner IS NOT NULL
           AND table_name IS NOT NULL
         ORDER BY owner, synonym_name
      ) LOOP
        append_synonym(item.owner, item.synonym_name, item.table_owner, item.table_name, item.db_link);
      END LOOP;
      FOR item IN (
        SELECT DISTINCT privilege_row.table_schema owner, privilege_row.table_name, privilege_row.grantee, privilege_row.privilege
          FROM all_tab_privs privilege_row
         WHERE privilege_row.grantee IN (c_target_schema, 'PUBLIC')
           AND privilege_row.privilege = 'SELECT'
           AND EXISTS (
             SELECT 1
               FROM all_synonyms synonym_row
              WHERE synonym_row.owner IN (c_target_schema, 'PUBLIC')
                AND synonym_row.table_owner = privilege_row.table_schema
                AND synonym_row.table_name = privilege_row.table_name
           )
         ORDER BY privilege_row.table_schema, privilege_row.table_name, privilege_row.grantee
      ) LOOP
        append_select_grant(item.owner, item.table_name, item.grantee, item.privilege);
      END LOOP;
    END IF;
  END;

  PROCEDURE add_definition(p_owner VARCHAR2, p_name VARCHAR2, p_type VARCHAR2) IS
    l_ddl_local CLOB;
    l_definition_local JSON_OBJECT_T;
    l_attributes_local JSON_OBJECT_T;
    l_columns_local JSON_ARRAY_T;
    l_identity_columns JSON_ARRAY_T;
    l_dependents_local JSON_ARRAY_T;
    l_child_local JSON_OBJECT_T;
    l_found_local PLS_INTEGER;
    l_valid VARCHAR2(16);
  BEGIN
    IF p_type = 'CONSTRAINT' THEN
      SELECT COUNT(*), NVL(MAX('VALID'), 'VALID'),
             CASE WHEN MAX(constraint_type) = 'R' THEN 'REF_CONSTRAINT'
                  ELSE 'CONSTRAINT' END
        INTO l_found_local, l_valid, l_metadata_type
        FROM all_constraints
       WHERE owner = p_owner AND constraint_name = p_name;
    ELSE
      SELECT COUNT(*), MAX(status) INTO l_found_local, l_valid
        FROM all_objects
       WHERE owner = p_owner AND object_name = p_name AND object_type = p_type;
      l_metadata_type := REPLACE(p_type, ' ', '_');
    END IF;
    IF l_found_local = 0 THEN
      RETURN;
    END IF;
    BEGIN
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SEGMENT_ATTRIBUTES', FALSE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'TABLESPACE', FALSE);
      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', FALSE);
      l_ddl_local := DBMS_METADATA.GET_DDL(l_metadata_type, p_name, p_owner);
      IF l_ddl_local IS NULL THEN
        RAISE_APPLICATION_ERROR(-20991, 'DBMS_METADATA returned an empty definition');
      END IF;
      SELECT JSON_OBJECT(
               'owner' VALUE p_owner,
               'name' VALUE p_name,
               'type' VALUE p_type,
               'valid' VALUE CASE WHEN l_valid = 'VALID' THEN 'true' ELSE 'false' END FORMAT JSON,
               'raw_ddl' VALUE l_ddl_local,
               'attributes' VALUE '{}' FORMAT JSON,
               'dependents' VALUE '[]' FORMAT JSON
               RETURNING CLOB
             )
        INTO l_raw FROM dual;
      l_definition_local := JSON_OBJECT_T(l_raw);
      l_attributes_local := JSON_OBJECT_T();
      l_dependents_local := JSON_ARRAY_T();

      IF p_type = 'TABLE' THEN
        SELECT JSON_OBJECT(
                 'temporary' VALUE temporary,
                 'partitioned' VALUE partitioned,
                 'iot_type' VALUE iot_type,
                 'nested' VALUE nested,
                 'secondary' VALUE secondary,
                 'compression' VALUE compression,
                 'logging' VALUE logging
                 RETURNING CLOB
               )
          INTO l_raw FROM all_tables
         WHERE owner = p_owner AND table_name = p_name;
        l_attributes_local := JSON_OBJECT_T(l_raw);
        l_columns_local := JSON_ARRAY_T();
        FOR col IN (
          SELECT column_name, data_type, data_length, data_precision, data_scale,
                 char_used, char_length, nullable, column_id, hidden_column,
                 virtual_column, identity_column
            FROM all_tab_cols
           WHERE owner = p_owner AND table_name = p_name
           ORDER BY column_id, internal_column_id
        ) LOOP
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('name', col.column_name);
          l_child_local.put('data_type', col.data_type);
          l_child_local.put('data_length', col.data_length);
          IF col.data_precision IS NOT NULL THEN l_child_local.put('data_precision', col.data_precision); END IF;
          IF col.data_scale IS NOT NULL THEN l_child_local.put('data_scale', col.data_scale); END IF;
          IF col.char_used IS NOT NULL THEN l_child_local.put('char_used', col.char_used); END IF;
          IF col.char_length IS NOT NULL THEN l_child_local.put('char_length', col.char_length); END IF;
          l_child_local.put('nullable', col.nullable);
          l_child_local.put('column_id', col.column_id);
          l_child_local.put('hidden', col.hidden_column);
          l_child_local.put('virtual', col.virtual_column);
          l_child_local.put('identity', col.identity_column);
          l_columns_local.append(l_child_local);
        END LOOP;
        l_attributes_local.put('columns', l_columns_local);
        l_identity_columns := JSON_ARRAY_T();
        FOR identity_col IN (
          SELECT column_name, generation_type, sequence_name, identity_options
            FROM all_tab_identity_cols
           WHERE owner = p_owner AND table_name = p_name
           ORDER BY column_name
        ) LOOP
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('column_name', identity_col.column_name);
          l_child_local.put('generation_type', identity_col.generation_type);
          l_child_local.put('sequence_name', identity_col.sequence_name);
          l_child_local.put('identity_options', identity_col.identity_options);
          l_identity_columns.append(l_child_local);
        END LOOP;
        l_attributes_local.put('identity_columns', l_identity_columns);

        FOR child_row IN (
          SELECT constraint_name AS object_name, 'CONSTRAINT' AS object_type FROM all_constraints
           WHERE owner = p_owner AND table_name = p_name
          UNION ALL
          SELECT index_name, 'INDEX' FROM all_indexes WHERE owner = p_owner AND table_name = p_name
          UNION ALL
          SELECT trigger_name, 'TRIGGER' FROM all_triggers WHERE owner = p_owner AND table_name = p_name
          ORDER BY 2, 1
        ) LOOP
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('owner', p_owner);
          l_child_local.put('name', child_row.object_name);
          l_child_local.put('type', child_row.object_type);
          l_dependents_local.append(l_child_local);
          add_definition(p_owner, child_row.object_name, child_row.object_type);
        END LOOP;
      ELSIF p_type = 'PACKAGE' THEN
        SELECT COUNT(*) INTO l_found_local FROM all_objects
         WHERE owner = p_owner AND object_name = p_name AND object_type = 'PACKAGE BODY';
        IF l_found_local > 0 THEN
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('owner', p_owner); l_child_local.put('name', p_name); l_child_local.put('type', 'PACKAGE BODY');
          l_dependents_local.append(l_child_local);
          add_definition(p_owner, p_name, 'PACKAGE BODY');
        END IF;
      ELSIF p_type = 'TYPE' THEN
        SELECT COUNT(*) INTO l_found_local FROM all_objects
         WHERE owner = p_owner AND object_name = p_name AND object_type = 'TYPE BODY';
        IF l_found_local > 0 THEN
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('owner', p_owner); l_child_local.put('name', p_name); l_child_local.put('type', 'TYPE BODY');
          l_dependents_local.append(l_child_local);
          add_definition(p_owner, p_name, 'TYPE BODY');
        END IF;
      ELSIF p_type = 'CONSTRAINT' THEN
        SELECT JSON_OBJECT(
                 'constraint_type' VALUE constraint_type,
                 'status' VALUE status,
                 'validated' VALUE validated,
                 'deferrable' VALUE deferrable,
                 'deferred' VALUE deferred,
                 'generated' VALUE generated,
                 'delete_rule' VALUE delete_rule,
                 'rely' VALUE rely,
                 'table_name' VALUE table_name
                 RETURNING CLOB
               )
          INTO l_raw FROM all_constraints
         WHERE owner = p_owner AND constraint_name = p_name;
        l_attributes_local := JSON_OBJECT_T(l_raw);
        l_columns_local := JSON_ARRAY_T();
        FOR col IN (
          SELECT column_name, position FROM all_cons_columns
           WHERE owner = p_owner AND constraint_name = p_name
           ORDER BY position
        ) LOOP
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('name', col.column_name);
          l_child_local.put('position', col.position);
          l_columns_local.append(l_child_local);
        END LOOP;
        l_attributes_local.put('columns', l_columns_local);
      ELSIF p_type = 'INDEX' THEN
        SELECT JSON_OBJECT(
                 'uniqueness' VALUE uniqueness,
                 'index_type' VALUE index_type,
                 'visibility' VALUE visibility,
                 'status' VALUE status,
                 'partitioned' VALUE partitioned,
                 'compression' VALUE compression,
                 'generated' VALUE generated,
                 'table_name' VALUE table_name
                 RETURNING CLOB
               )
          INTO l_raw FROM all_indexes
         WHERE owner = p_owner AND index_name = p_name;
        l_attributes_local := JSON_OBJECT_T(l_raw);
        l_columns_local := JSON_ARRAY_T();
        FOR col IN (
          SELECT column_name, column_position, descend FROM all_ind_columns
           WHERE index_owner = p_owner AND index_name = p_name
           ORDER BY column_position
        ) LOOP
          l_child_local := JSON_OBJECT_T();
          l_child_local.put('name', col.column_name);
          l_child_local.put('position', col.column_position);
          l_child_local.put('descend', col.descend);
          l_columns_local.append(l_child_local);
        END LOOP;
        l_attributes_local.put('columns', l_columns_local);
      ELSIF p_type = 'SEQUENCE' THEN
        SELECT JSON_OBJECT(
                 'increment_by' VALUE increment_by,
                 'min_value' VALUE min_value,
                 'max_value' VALUE max_value,
                 'cycle_flag' VALUE cycle_flag,
                 'order_flag' VALUE order_flag,
                 'cache_size' VALUE cache_size
                 RETURNING CLOB
               )
          INTO l_raw FROM all_sequences
         WHERE sequence_owner = p_owner AND sequence_name = p_name;
        l_attributes_local := JSON_OBJECT_T(l_raw);
      ELSIF p_type = 'VIEW' THEN
        SELECT JSON_OBJECT('text_length' VALUE text_length RETURNING CLOB)
          INTO l_raw FROM all_views
         WHERE owner = p_owner AND view_name = p_name;
        l_attributes_local := JSON_OBJECT_T(l_raw);
      ELSIF p_type = 'TRIGGER' THEN
        SELECT JSON_OBJECT(
                 'status' VALUE status,
                 'trigger_type' VALUE trigger_type,
                 'triggering_event' VALUE triggering_event,
                 'base_object_type' VALUE base_object_type,
                 'table_name' VALUE table_name
                 RETURNING CLOB
               )
          INTO l_raw FROM all_triggers
         WHERE owner = p_owner AND trigger_name = p_name;
        l_attributes_local := JSON_OBJECT_T(l_raw);
      END IF;
      l_definition_local.put('attributes', l_attributes_local);
      l_definition_local.put('dependents', l_dependents_local);
      l_definitions.append(l_definition_local);
    EXCEPTION
      WHEN OTHERS THEN
        DECLARE
          error_value JSON_OBJECT_T := JSON_OBJECT_T();
        BEGIN
          error_value.put('owner', p_owner);
          error_value.put('name', p_name);
          error_value.put('type', p_type);
          error_value.put('error', SQLERRM);
          l_errors.append(error_value);
        END;
    END;
  END;

  PROCEDURE cleanup_temp_lob(p_lob IN OUT NOCOPY CLOB) IS
  BEGIN
    IF p_lob IS NOT NULL THEN
      IF DBMS_LOB.ISTEMPORARY(p_lob) = 1 THEN
        DBMS_LOB.FREETEMPORARY(p_lob);
      END IF;
    END IF;
  EXCEPTION
    WHEN OTHERS THEN
      NULL;
  END cleanup_temp_lob;

  PROCEDURE cleanup_temp_blob(p_lob IN OUT NOCOPY BLOB) IS
  BEGIN
    IF p_lob IS NOT NULL THEN
      IF DBMS_LOB.ISTEMPORARY(p_lob) = 1 THEN
        DBMS_LOB.FREETEMPORARY(p_lob);
      END IF;
    END IF;
  EXCEPTION
    WHEN OTHERS THEN
      NULL;
  END cleanup_temp_blob;

BEGIN
  -- SQLcl's SERVEROUTPUT SIZE UNLIMITED still leaves a 1,000,000-byte buffer
  -- (ORU-10027), and a selected definition can be larger than that.
  DBMS_OUTPUT.ENABLE(NULL);
  IF SYS_CONTEXT('USERENV', 'SESSION_USER') != c_expected_user THEN
    RAISE_APPLICATION_ERROR(-20980, 'SQLcl session user did not match selected target');
  END IF;
  IF SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') != c_target_schema THEN
    RAISE_APPLICATION_ERROR(-20981, 'SQLcl current schema did not match selected target');
  END IF;

  SELECT COUNT(*) INTO l_found FROM all_users WHERE username = c_target_schema;
  IF l_found = 0 THEN
    RAISE_APPLICATION_ERROR(-20982, 'Selected owner is not visible');
  END IF;
  l_owner_complete := SYS_CONTEXT('USERENV', 'SESSION_USER') = c_target_schema;
  IF NOT l_owner_complete THEN
    FOR privilege_row IN (SELECT privilege FROM session_privs WHERE privilege = 'SELECT ANY DICTIONARY') LOOP
      l_privileges.append(privilege_row.privilege);
      l_metadata_role := TRUE;
    END LOOP;
    FOR role_row IN (SELECT role FROM session_roles WHERE role = 'SELECT_CATALOG_ROLE') LOOP
      l_privileges.append(role_row.role);
      l_metadata_role := TRUE;
    END LOOP;
    l_owner_complete := l_metadata_role;
  END IF;
  IF l_owner_complete AND SYS_CONTEXT('USERENV', 'SESSION_USER') = c_target_schema THEN
    l_path := 'OWNER_SESSION';
  ELSIF l_owner_complete THEN
    l_path := 'METADATA_PRIVILEGE';
  ELSE
    l_path := 'PARTIAL_ALL_OBJECTS';
  END IF;

  l_catalogs.append('ALL_OBJECTS');
  SELECT COUNT(*) INTO l_found FROM all_tables WHERE owner = c_target_schema;
  l_catalogs.append('ALL_TABLES');
  SELECT COUNT(*) INTO l_found FROM all_tab_columns WHERE owner = c_target_schema;
  l_catalogs.append('ALL_TAB_COLUMNS');
  SELECT COUNT(*) INTO l_found FROM all_tab_cols WHERE owner = c_target_schema;
  l_catalogs.append('ALL_TAB_COLS');
  SELECT COUNT(*) INTO l_found FROM all_tab_identity_cols WHERE owner = c_target_schema;
  l_catalogs.append('ALL_TAB_IDENTITY_COLS');
  SELECT COUNT(*) INTO l_found FROM all_views WHERE owner = c_target_schema;
  l_catalogs.append('ALL_VIEWS');
  SELECT COUNT(*) INTO l_found FROM all_sequences WHERE sequence_owner = c_target_schema;
  l_catalogs.append('ALL_SEQUENCES');
  SELECT COUNT(*) INTO l_found FROM all_constraints WHERE owner = c_target_schema;
  l_catalogs.append('ALL_CONSTRAINTS');
  SELECT COUNT(*) INTO l_found FROM all_cons_columns WHERE owner = c_target_schema;
  l_catalogs.append('ALL_CONS_COLUMNS');
  SELECT COUNT(*) INTO l_found FROM all_indexes WHERE owner = c_target_schema;
  l_catalogs.append('ALL_INDEXES');
  SELECT COUNT(*) INTO l_found FROM all_ind_columns WHERE index_owner = c_target_schema;
  l_catalogs.append('ALL_IND_COLUMNS');
  SELECT COUNT(*) INTO l_found FROM all_triggers WHERE owner = c_target_schema;
  l_catalogs.append('ALL_TRIGGERS');

  l_identity.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));
  l_identity.put('current_schema', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'));
  l_identity.put('db_name', SYS_CONTEXT('USERENV', 'DB_NAME'));
  l_identity.put('db_unique_name', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'));
  l_identity.put('service_name', NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>'));
  l_identity.put('container_id', NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0'));
  l_identity.put('container_name', NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB'));
  l_identity.put('edition', NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>'));
  l_identity.put('database_version', TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE));

  IF c_phase = 'inventory' THEN
    add_inventory(l_objects);
  ELSE
    add_dependency_catalogs();
    l_catalogs.append('SYNONYMS');
    l_catalogs.append('OBJECT_GRANTS');
    add_inventory(l_before);
    FOR selected_row IN (
      SELECT object_name, object_type
        FROM JSON_TABLE(c_selected_json, '$[*]' COLUMNS (
          object_name VARCHAR2(128) PATH '$.name',
          object_type VARCHAR2(128) PATH '$.type'
        ))
    ) LOOP
      add_definition(c_target_schema, selected_row.object_name, selected_row.object_type);
    END LOOP;
    add_inventory(l_after);
  END IF;

  l_coverage.put('ownerComplete', l_owner_complete);
  l_coverage.put('path', l_path);
  l_coverage.put('metadataReadable', l_owner_complete);
  l_coverage.put('privileges', l_privileges);
  l_coverage.put('catalogs', l_catalogs);
  l_coverage.put('unsupported', l_unsupported);
  l_completed_at := TO_CHAR(SYSTIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.FF3"Z"');

  l_payload.put('schemaVersion', 2);
  l_payload.put('phase', c_phase);
  l_payload.put('complete', l_owner_complete);
  l_payload.put('coverage', l_coverage);
  l_payload.put('identity', l_identity);
  l_payload.put('started_at', l_started_at);
  l_payload.put('completed_at', l_completed_at);
  IF c_phase = 'inventory' THEN
    l_payload.put('objects', l_objects);
  ELSE
    l_payload.put('before', l_before);
    l_payload.put('after', l_after);
    l_payload.put('definitions', l_definitions);
    l_payload.put('metadataErrors', l_errors);
    l_payload.put('synonyms', l_synonyms);
    l_payload.put('grants', l_grants);
  END IF;

  l_raw := l_payload.to_clob();
  DBMS_LOB.CREATETEMPORARY(l_blob, TRUE);
  DBMS_LOB.CONVERTTOBLOB(
    dest_lob     => l_blob,
    src_clob     => l_raw,
    amount       => DBMS_LOB.LOBMAXSIZE,
    dest_offset  => l_dest,
    src_offset   => l_src,
    blob_csid    => NLS_CHARSET_ID('AL32UTF8'),
    lang_context => l_ctx,
    warning      => l_warning
  );
  IF l_warning != 0 THEN
    RAISE_APPLICATION_ERROR(-20992, 'Unicode conversion warning during catalog compression');
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
  cleanup_temp_blob(l_gzip);
  cleanup_temp_blob(l_blob);
  cleanup_temp_lob(l_raw);
EXCEPTION
  WHEN OTHERS THEN
    cleanup_temp_blob(l_gzip);
    cleanup_temp_blob(l_blob);
    cleanup_temp_lob(l_raw);
    ROLLBACK;
    RAISE;
END;
/
PROMPT CATALOG_PAYLOAD_END:&catalog_phase
PROMPT CATALOG_VERIFIED:&catalog_phase
SET DEFINE OFF
