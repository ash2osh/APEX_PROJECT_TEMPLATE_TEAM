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
-- Every role granted to the account or PUBLIC, enabled or not: a non-default
-- role is absent from SESSION_ROLES and its privileges from the queries below,
-- yet SET ROLE can enable it later.
SELECT 'TEAM_PRIV|GRANTED|' || granted_role
  FROM USER_ROLE_PRIVS
 ORDER BY granted_role;
-- Grantee, whether the owner is Oracle-maintained, the object name and whether
-- it is a temporary table: Oracle's own grants to PUBLIC (EXECUTE on
-- DBMS_METADATA and friends, DML on session-private temporary tables) are on
-- every account; the audit accepts only that named baseline.
SELECT 'TEAM_PRIV|OBJECT|' || p.privilege || '|' || NVL(p.type, 'UNKNOWN') || '|'
       || p.grantee || '|' || NVL(u.oracle_maintained, 'N') || '|' || p.table_name || '|'
       || NVL(t.temporary, 'N')
  FROM ALL_TAB_PRIVS_RECD p
  LEFT JOIN ALL_USERS u ON u.username = p.owner
  LEFT JOIN ALL_TABLES t ON t.owner = p.owner AND t.table_name = p.table_name
 ORDER BY p.privilege, p.type, p.grantee, p.table_name;
SELECT 'TEAM_PRIV|COLUMN|' || privilege
  FROM ALL_COL_PRIVS_RECD
 ORDER BY privilege;
SELECT 'TEAM_PRIV|OWNED|' || object_type
  FROM USER_OBJECTS
 WHERE object_type <> 'SYNONYM'
 ORDER BY object_type;
