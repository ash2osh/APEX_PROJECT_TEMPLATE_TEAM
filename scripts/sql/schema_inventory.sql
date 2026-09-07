-- Read-only inventory request. The selected SQLcl adapter wraps this with
-- begin/count/end markers and verifies the target identity before accepting it.
SELECT 'TEAM_INVENTORY|' || owner || '|' ||
       CASE object_type WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY' WHEN 'TYPE BODY' THEN 'TYPE_BODY' ELSE object_type END ||
       '|' || object_name || '|' || status || '|' ||
       RAWTOHEX(STANDARD_HASH(
           DBMS_LOB.SUBSTR(DBMS_METADATA.GET_DDL(
               CASE object_type WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY' WHEN 'TYPE BODY' THEN 'TYPE_BODY' ELSE object_type END,
               object_name, owner), 3000, 1)
           || TO_CHAR(DBMS_LOB.GETLENGTH(DBMS_METADATA.GET_DDL(
               CASE object_type WHEN 'PACKAGE BODY' THEN 'PACKAGE_BODY' WHEN 'TYPE BODY' THEN 'TYPE_BODY' ELSE object_type END,
               object_name, owner))), 'SHA256'))
  FROM all_objects
 WHERE owner IN ('__TABLES_SCHEMA__', '__CODE_SCHEMA__')
   AND object_type IN ('TABLE', 'INDEX', 'VIEW', 'PACKAGE', 'PACKAGE BODY', 'PROCEDURE', 'FUNCTION', 'TRIGGER', 'SEQUENCE', 'TYPE', 'TYPE BODY', 'SYNONYM')
   AND object_name NOT LIKE 'ISEQ$$_%'
   AND object_name NOT LIKE 'SYS_%'
 ORDER BY owner, object_type, object_name;
