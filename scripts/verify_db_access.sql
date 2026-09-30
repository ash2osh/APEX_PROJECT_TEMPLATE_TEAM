-- Shared identity check. The calling script must define target_schema,
-- db_environment, and expected_user.
--
-- This script confirms *who and where* the session is. It does not audit
-- privileges. Production safety in this template is an instruction to the
-- client, not a privilege gate: run SELECT statements only, never DML or DDL.
SET SERVEROUTPUT ON

SELECT 'SQLcl target: session_user=' || SYS_CONTEXT('USERENV', 'SESSION_USER')
       || ', current_schema=' || SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
       || ', db_name=' || SYS_CONTEXT('USERENV', 'DB_NAME')
       || ', db_unique_name=' || SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')
       || ', service=' || SYS_CONTEXT('USERENV', 'SERVICE_NAME')
FROM DUAL;

DECLARE
  v_target_schema     VARCHAR2(128) := UPPER('&&target_schema');
  v_environment       VARCHAR2(32)  := LOWER('&&db_environment');
  v_expected_user     VARCHAR2(128) := UPPER('&&expected_user');
  v_session_user      VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');
  v_current_schema    VARCHAR2(128) := SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA');
  c_production_marker CONSTANT VARCHAR2(256) :=
    '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])';
  c_non_production_marker CONSTANT VARCHAR2(64) := '(pre|non)[-_.]?(prod|prd)';
  v_count             PLS_INTEGER;
  -- Same production marker as scripts/db_targets.py, applied to each name
  -- after removing pre-production words such as PREPROD and NON-PROD.
  FUNCTION resembles_production(p_name VARCHAR2) RETURN BOOLEAN IS
  BEGIN
    RETURN REGEXP_LIKE(REGEXP_REPLACE(p_name, c_non_production_marker, ' ', 1, 0, 'i'),
                       c_production_marker, 'i');
  END;
BEGIN
  IF v_session_user != v_expected_user THEN
    RAISE_APPLICATION_ERROR(-20001,
      'Expected session user ' || v_expected_user || ' but found ' || v_session_user);
  END IF;

  SELECT COUNT(*) INTO v_count
  FROM all_users
  WHERE username = v_target_schema;
  IF v_count = 0 THEN
    RAISE_APPLICATION_ERROR(-20014,
      'Target schema does not exist or is not visible: ' || v_target_schema);
  END IF;

  IF (resembles_production(SYS_CONTEXT('USERENV', 'DB_NAME'))
      OR resembles_production(SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'))
      OR resembles_production(SYS_CONTEXT('USERENV', 'SERVICE_NAME')))
     AND v_environment != 'production' THEN
    RAISE_APPLICATION_ERROR(-20002,
      'Database/service identity resembles production; ask the user to classify it before continuing');
  END IF;

  IF v_expected_user = v_target_schema AND v_current_schema != v_target_schema THEN
    RAISE_APPLICATION_ERROR(-20009,
      'Expected current schema ' || v_target_schema || ' but found ' || v_current_schema);
  END IF;

  IF v_environment = 'production' THEN
    DBMS_OUTPUT.PUT_LINE('****************************************************************');
    DBMS_OUTPUT.PUT_LINE('*  PRODUCTION SESSION - READ ONLY                              *');
    DBMS_OUTPUT.PUT_LINE('*                                                              *');
    DBMS_OUTPUT.PUT_LINE('*  Run SELECT statements only.                                 *');
    DBMS_OUTPUT.PUT_LINE('*  Do NOT run INSERT, UPDATE, DELETE, MERGE, or any other DML. *');
    DBMS_OUTPUT.PUT_LINE('*  Do NOT run CREATE, ALTER, DROP, TRUNCATE, or any other DDL. *');
    DBMS_OUTPUT.PUT_LINE('*  Do NOT COMMIT. Prepare changes for an approved deployment.  *');
    DBMS_OUTPUT.PUT_LINE('*                                                              *');
    DBMS_OUTPUT.PUT_LINE('*  This is not enforced by the database. It is your contract.  *');
    DBMS_OUTPUT.PUT_LINE('****************************************************************');
  END IF;
END;
/
