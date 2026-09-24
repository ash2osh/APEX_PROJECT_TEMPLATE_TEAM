SET HEADING OFF
SET FEEDBACK OFF
SET PAGESIZE 0
SET LINESIZE 32767
SET TRIMSPOOL ON
SELECT 'TEAM_PRIV|SYSTEM|' || privilege
  FROM SESSION_PRIVS
 ORDER BY privilege;
SELECT 'TEAM_PRIV|ROLE|' || role
  FROM SESSION_ROLES
 ORDER BY role;
-- Grantee and whether the object's owner is Oracle-maintained: grants Oracle
-- itself makes to PUBLIC on its own objects (EXECUTE on DBMS_METADATA and
-- friends) are present on every account and are what read-only work uses.
SELECT 'TEAM_PRIV|OBJECT|' || p.privilege || '|' || NVL(p.type, 'UNKNOWN') || '|'
       || p.grantee || '|' || NVL(u.oracle_maintained, 'N')
  FROM ALL_TAB_PRIVS_RECD p
  LEFT JOIN ALL_USERS u ON u.username = p.owner
 ORDER BY p.privilege, p.type, p.grantee;
SELECT 'TEAM_PRIV|COLUMN|' || p.privilege || '|' || p.grantee || '|' || NVL(u.oracle_maintained, 'N')
  FROM ALL_COL_PRIVS_RECD p
  LEFT JOIN ALL_USERS u ON u.username = p.owner
 ORDER BY p.privilege, p.grantee;
SELECT 'TEAM_PRIV|OWNED|' || object_type
  FROM USER_OBJECTS
 WHERE object_type <> 'SYNONYM'
 ORDER BY object_type;
