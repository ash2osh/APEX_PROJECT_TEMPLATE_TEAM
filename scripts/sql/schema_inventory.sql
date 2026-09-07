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

WITH inventory_objects AS (
  SELECT owner,
         CASE object_type
           WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
           WHEN 'TYPE BODY' THEN 'TYPE_BODY'
           ELSE object_type
         END AS object_type,
         object_name
    FROM all_objects
   WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
     AND object_type IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                         'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                         'TYPE', 'TYPE BODY', 'SYNONYM')
     AND object_name NOT LIKE 'ISEQ$$_%'
     AND object_name NOT LIKE 'SYS_%'
  UNION ALL
  SELECT owner, 'CONSTRAINT', constraint_name || '@' || table_name
    FROM all_constraints
   WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
  UNION ALL
  SELECT table_schema, 'GRANT', table_name || '@' || grantee || '@' || privilege
    FROM all_tab_privs
   WHERE table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
  UNION ALL
  SELECT table_schema, 'GRANT', table_name || '@' || grantee || '@' || privilege || '@' || column_name
    FROM all_col_privs
   WHERE table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
)
SELECT 'TEAM_INVENTORY_BEGIN|version=2|topology=__TOPOLOGY__|count=' || COUNT(*)
  FROM inventory_objects;

WITH inventory_objects AS (
  SELECT owner,
         CASE object_type
           WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
           WHEN 'TYPE BODY' THEN 'TYPE_BODY'
           ELSE object_type
         END AS object_type,
         object_name,
         status,
         DBMS_METADATA.GET_DDL(
           CASE object_type
             WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY'
             WHEN 'TYPE BODY' THEN 'TYPE_BODY'
             ELSE object_type
           END,
           object_name,
           owner
         ) AS definition
    FROM all_objects
   WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
     AND object_type IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                         'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                         'TYPE', 'TYPE BODY', 'SYNONYM')
     AND object_name NOT LIKE 'ISEQ$$_%'
     AND object_name NOT LIKE 'SYS_%'
  UNION ALL
  SELECT c.owner,
         'CONSTRAINT',
         c.constraint_name || '@' || c.table_name,
         c.status,
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
  UNION ALL
  SELECT p.table_schema,
         'GRANT',
         p.table_name || '@' || p.grantee || '@' || p.privilege,
         'VALID',
         TO_CLOB('GRANTABLE=' || NVL(p.grantable, '') ||
                 '|HIERARCHY=' || NVL(p.hierarchy, ''))
    FROM all_tab_privs p
   WHERE p.table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
  UNION ALL
  SELECT p.table_schema,
         'GRANT',
         p.table_name || '@' || p.grantee || '@' || p.privilege || '@' || p.column_name,
         'VALID',
         TO_CLOB('GRANTABLE=' || NVL(p.grantable, ''))
    FROM all_col_privs p
   WHERE p.table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
), chunk_numbers AS (
  SELECT LEVEL AS part
    FROM dual
  CONNECT BY LEVEL <= NVL((
    SELECT MAX(CEIL(DBMS_LOB.GETLENGTH(definition) / 400))
      FROM inventory_objects
  ), 1)
), chunks AS (
  SELECT o.owner, o.object_type, o.object_name, o.status,
         p.part, CEIL(DBMS_LOB.GETLENGTH(o.definition) / 400) AS total,
         DBMS_LOB.SUBSTR(o.definition, 400, (p.part - 1) * 400 + 1) AS piece
    FROM inventory_objects o
    CROSS JOIN chunk_numbers p
   WHERE p.part <= CEIL(DBMS_LOB.GETLENGTH(o.definition) / 400)
)
SELECT 'TEAM_INVENTORY_CHUNK|' || owner || '|' || object_type || '|' || object_name ||
       '|' || status || '|' || part || '|' || total || '|' ||
       UTL_RAW.CAST_TO_VARCHAR2(
         UTL_ENCODE.BASE64_ENCODE(UTL_RAW.CAST_TO_RAW(piece))
       )
  FROM chunks
 ORDER BY owner, object_type, object_name, part;

WITH inventory_objects AS (
  SELECT owner, object_type, object_name
    FROM all_objects
   WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
     AND object_type NOT IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                             'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                             'TYPE', 'TYPE BODY', 'SYNONYM')
     AND object_name NOT LIKE 'ISEQ$$_%'
     AND object_name NOT LIKE 'SYS_%'
)
SELECT 'TEAM_INVENTORY_UNSUPPORTED|' || owner || '|' || object_type || '|' || object_name
  FROM inventory_objects
 ORDER BY owner, object_type, object_name;

SELECT 'TEAM_INVENTORY_END|count=' || COUNT(*)
  FROM (
    SELECT owner, object_name
      FROM all_objects
     WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
       AND object_type IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY',
                           'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE',
                           'TYPE', 'TYPE BODY', 'SYNONYM')
       AND object_name NOT LIKE 'ISEQ$$_%'
       AND object_name NOT LIKE 'SYS_%'
    UNION ALL
    SELECT owner, constraint_name || '@' || table_name FROM all_constraints
     WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
    UNION ALL
    SELECT table_schema, table_name || '@' || grantee || '@' || privilege FROM all_tab_privs
     WHERE table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
    UNION ALL
    SELECT table_schema, table_name || '@' || grantee || '@' || privilege || '@' || column_name FROM all_col_privs
     WHERE table_schema IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
  );
