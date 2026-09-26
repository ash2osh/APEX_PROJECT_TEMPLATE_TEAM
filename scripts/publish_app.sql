-- Arguments: target parsing schema, target environment, expected session user,
-- and explicit deployment descriptor path relative to the application source.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
DEFINE deployment_file = '&4'
SET ENCODING UTF-8
SET HEADING OFF
SET FEEDBACK OFF
SET ECHO OFF
SET VERIFY OFF
WHENEVER SQLERROR EXIT SQL.SQLCODE
WHENEVER OSERROR EXIT FAILURE

DECLARE
  v_target_schema VARCHAR2(128) := UPPER('&&target_schema');
  v_environment   VARCHAR2(32) := LOWER('&&db_environment');
  v_expected_user VARCHAR2(128) := UPPER('&&expected_user');
  v_session_user  VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');
  v_database_id   VARCHAR2(512) := SYS_CONTEXT('USERENV', 'DB_NAME') || '.'
                                   || SYS_CONTEXT('USERENV', 'SERVICE_NAME');
  v_schema_count  PLS_INTEGER;
BEGIN
  IF v_session_user != v_expected_user THEN
    RAISE_APPLICATION_ERROR(-20001,
      'Expected session user ' || v_expected_user || ' but found ' || v_session_user);
  END IF;

  SELECT COUNT(*) INTO v_schema_count
  FROM all_users
  WHERE username = v_target_schema;
  IF v_schema_count = 0 THEN
    RAISE_APPLICATION_ERROR(-20014,
      'Target parsing schema does not exist or is not visible: ' || v_target_schema);
  END IF;

  IF REGEXP_LIKE(v_database_id,
       '(^|[^[:alnum:]])(prod|prd|production|live)[[:digit:]]*([^[:alnum:]]|$)', 'i')
     AND v_environment != 'production' THEN
    RAISE_APPLICATION_ERROR(-20002,
      'Database/service identity resembles production but the selected target is not production');
  END IF;
END;
/

apex import -input . -deployment &&deployment_file
exit
