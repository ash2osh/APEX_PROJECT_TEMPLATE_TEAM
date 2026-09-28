-- Migration-specific identity guard. The generic production policy remains
-- read-only; this driver is reached only after an explicit migration command
-- and target confirmation for staging/production.
SET SERVEROUTPUT ON SIZE UNLIMITED

DECLARE
  c_target_schema CONSTANT VARCHAR2(128) := UPPER('&&target_schema');
  c_target_environment CONSTANT VARCHAR2(16) := LOWER('&&target_environment');
  c_expected_user CONSTANT VARCHAR2(128) := UPPER('&&expected_user');
  l_session_user VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');
  l_database_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_NAME');
  l_db_unique_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME');
  l_service_name VARCHAR2(256) := SYS_CONTEXT('USERENV', 'SERVICE_NAME');
  l_owner_count PLS_INTEGER;
BEGIN
  IF l_session_user != c_expected_user THEN
    RAISE_APPLICATION_ERROR(-20980, 'Migration expected session user ' || c_expected_user || ' but found ' || l_session_user);
  END IF;
  IF c_target_environment NOT IN ('dev', 'staging', 'prod') THEN
    RAISE_APPLICATION_ERROR(-20981, 'Migration target environment is invalid');
  END IF;
  SELECT COUNT(*) INTO l_owner_count FROM all_users WHERE username = c_target_schema;
  IF l_owner_count != 1 THEN
    RAISE_APPLICATION_ERROR(-20982, 'Migration target schema is not visible: ' || c_target_schema);
  END IF;
  IF c_target_environment != 'prod' AND REGEXP_LIKE(
       NVL(l_db_unique_name, '') || '.' || NVL(l_service_name, ''),
       '(^|[^[:alnum:]])(prod|prd|production|live)[[:digit:]]*([^[:alnum:]]|$)', 'i') THEN
    RAISE_APPLICATION_ERROR(-20983, 'Target identity resembles production but the selected environment is not prod');
  END IF;
END;
/

ALTER SESSION SET CURRENT_SCHEMA = &&target_schema;

DECLARE
  c_target_schema CONSTANT VARCHAR2(128) := UPPER('&&target_schema');
  c_expected_user CONSTANT VARCHAR2(128) := UPPER('&&expected_user');
  l_identity JSON_OBJECT_T := JSON_OBJECT_T();
  l_json CLOB;
  l_chunk VARCHAR2(30000);
  l_offset PLS_INTEGER := 1;
BEGIN
  IF SYS_CONTEXT('USERENV', 'SESSION_USER') != c_expected_user THEN
    RAISE_APPLICATION_ERROR(-20984, 'Migration session identity changed after schema selection');
  END IF;
  IF SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') != c_target_schema THEN
    RAISE_APPLICATION_ERROR(-20985, 'Migration current schema did not resolve to the selected owner');
  END IF;
  l_identity.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));
  l_identity.put('current_schema', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'));
  l_identity.put('db_name', SYS_CONTEXT('USERENV', 'DB_NAME'));
  l_identity.put('db_unique_name', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'));
  l_identity.put('service_name', NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>'));
  l_identity.put('container_id', NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0'));
  l_identity.put('container_name', NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB'));
  l_identity.put('edition', NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>'));
  l_identity.put('database_version', TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE));
  l_json := l_identity.to_clob();
  DBMS_OUTPUT.PUT_LINE('MIGRATION_IDENTITY_BEGIN');
  WHILE l_offset <= DBMS_LOB.GETLENGTH(l_json) LOOP
    l_chunk := DBMS_LOB.SUBSTR(l_json, 30000, l_offset);
    DBMS_OUTPUT.PUT_LINE(l_chunk);
    l_offset := l_offset + LENGTH(l_chunk);
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('MIGRATION_IDENTITY_END');
  DBMS_OUTPUT.PUT_LINE('MIGRATION_IDENTITY_VERIFIED');
END;
/
