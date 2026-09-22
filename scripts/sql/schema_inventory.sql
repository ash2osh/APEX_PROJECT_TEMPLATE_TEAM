-- Read-only, framed schema inventory.  The client reconstructs every
-- DBMS_METADATA definition from UTF-8 chunks and hashes the complete bytes;
-- no bounded prefix is accepted as canonical evidence.
BEGIN
  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', FALSE);
  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SEGMENT_ATTRIBUTES', FALSE);
  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);
  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'TABLESPACE', FALSE);
EXCEPTION
  WHEN OTHERS THEN
    RAISE;
END;
/

-- TEAM_READONLY_INVENTORY_BLOCK: this block only reads dictionary metadata and
-- emits framed DBMS_OUTPUT rows.  The production SQL guard recognizes this
-- exact token set and still refuses all DML/DDL or dynamic SQL blocks.
SET SERVEROUTPUT ON SIZE UNLIMITED FORMAT WORD_WRAPPED
DECLARE
  v_definition CLOB;
  v_piece VARCHAR2(400);
  v_total PLS_INTEGER;
  v_count PLS_INTEGER;
  v_type VARCHAR2(30);
BEGIN
  SELECT COUNT(*) INTO v_count FROM (
    SELECT 1
      FROM all_objects
     WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
       AND object_type IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                           'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                           'TYPE', 'TYPE BODY', 'SYNONYM')
       AND object_name NOT LIKE 'ISEQ$$_%'
       AND object_name NOT LIKE 'SYS_%'
       AND object_name NOT LIKE 'BIN$%'
    UNION ALL
    SELECT 1 FROM all_constraints
     WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
       AND constraint_name NOT LIKE 'BIN$%'
       AND table_name NOT LIKE 'BIN$%'
    UNION ALL
    SELECT 1 FROM all_tab_privs
     WHERE table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
    UNION ALL
    SELECT 1 FROM all_col_privs
     WHERE table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
  );
  DBMS_OUTPUT.PUT_LINE('TEAM_INVENTORY_BEGIN|version=2|topology=__TOPOLOGY__|count=' || v_count);

  FOR o IN (
    SELECT owner,
           CASE object_type
             WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
             WHEN 'TYPE BODY' THEN 'TYPE_BODY'
             ELSE object_type
           END AS object_type,
           object_name,
           status,
           'DDL' AS kind,
           TO_CLOB(NULL) AS definition
      FROM all_objects
     WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
       AND object_type IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                           'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                           'TYPE', 'TYPE BODY', 'SYNONYM')
       AND object_name NOT LIKE 'ISEQ$$_%'
       AND object_name NOT LIKE 'SYS_%'
       AND object_name NOT LIKE 'BIN$%'
    UNION ALL
    SELECT c.owner,
           'CONSTRAINT',
           c.constraint_name || '@' || c.table_name,
           c.status,
           'TEXT',
           TO_CLOB(
             'TYPE=' || c.constraint_type ||
             '|DELETE_RULE=' || NVL(c.delete_rule, '') ||
             '|R_OWNER=' || NVL(c.r_owner, '') ||
             '|R_CONSTRAINT=' || NVL(c.r_constraint_name, '') ||
             '|SEARCH=' || NVL(c.search_condition_vc, '') ||
             '|COLUMNS=' || NVL((
               SELECT LISTAGG(cc.column_name, ',') WITHIN GROUP (ORDER BY cc.position)
                 FROM all_cons_columns cc
                WHERE cc.owner = c.owner
                  AND cc.constraint_name = c.constraint_name
                  AND cc.table_name = c.table_name
             ), '')
           )
      FROM all_constraints c
     WHERE c.owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
       AND c.constraint_name NOT LIKE 'BIN$%'
       AND c.table_name NOT LIKE 'BIN$%'
    UNION ALL
    SELECT p.table_schema,
           'GRANT',
           p.table_name || '@' || p.grantee || '@' || p.privilege,
           'VALID',
           'TEXT',
           TO_CLOB('GRANTABLE=' || NVL(p.grantable, '') || '|HIERARCHY=' || NVL(p.hierarchy, ''))
      FROM all_tab_privs p
     WHERE p.table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
    UNION ALL
    SELECT p.table_schema,
           'GRANT',
           p.table_name || '@' || p.grantee || '@' || p.privilege || '@' || p.column_name,
           'VALID',
           'TEXT',
           TO_CLOB('GRANTABLE=' || NVL(p.grantable, ''))
      FROM all_col_privs p
     WHERE p.table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
     ORDER BY 1, 2, 3
  ) LOOP
    v_type := o.object_type;
    IF o.kind = 'DDL' THEN
      v_definition := DBMS_METADATA.GET_DDL(v_type, o.object_name, o.owner);
    ELSE
      v_definition := o.definition;
    END IF;
    IF v_definition IS NULL THEN
      RAISE_APPLICATION_ERROR(-20071, 'TEAM_INVENTORY_DEFINITION_MISSING');
    END IF;
    v_total := GREATEST(1, CEIL(DBMS_LOB.GETLENGTH(v_definition) / 400));
    FOR part IN 1..v_total LOOP
      v_piece := DBMS_LOB.SUBSTR(v_definition, 400, (part - 1) * 400 + 1);
      DBMS_OUTPUT.PUT_LINE(
        'TEAM_INVENTORY_CHUNK|' || o.owner || '|' || o.object_type || '|' || o.object_name ||
        '|' || o.status || '|' || part || '|' || v_total || '|' ||
        UTL_RAW.CAST_TO_VARCHAR2(UTL_ENCODE.BASE64_ENCODE(UTL_RAW.CAST_TO_RAW(v_piece)))
      );
    END LOOP;
  END LOOP;

  FOR unsupported IN (
    SELECT owner, object_type, object_name
      FROM all_objects
     WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
       AND object_type NOT IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                               'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                               'TYPE', 'TYPE BODY', 'SYNONYM')
       AND object_name NOT LIKE 'ISEQ$$_%'
       AND object_name NOT LIKE 'SYS_%'
       AND object_name NOT LIKE 'BIN$%'
     ORDER BY owner, object_type, object_name
  ) LOOP
    DBMS_OUTPUT.PUT_LINE('TEAM_INVENTORY_UNSUPPORTED|' || unsupported.owner || '|' || unsupported.object_type || '|' || unsupported.object_name);
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('TEAM_INVENTORY_END|count=' || v_count);
END;
/
