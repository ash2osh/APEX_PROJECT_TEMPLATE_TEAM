-- Shared ORDS identity and capability check. The calling script must define
-- target_schema, db_environment and expected_user, as verify_db_access.sql
-- requires.
--
-- ORDS metadata is read and authorized for the actual login user, so the
-- session must be the REST schema owner itself. ALTER SESSION SET
-- CURRENT_SCHEMA does not change SESSION_USER and is not a substitute, so
-- unlike the table and code profiles this check never accepts a different
-- session user for the target schema.
--
-- Read-only: it selects from the data dictionary and calls one ORDS version
-- function. It never enables REST or changes ORDS metadata.
@@verify_db_access.sql

DECLARE
  v_target_schema VARCHAR2(128) := UPPER('&&target_schema');
  v_session_user  VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');
  v_version       VARCHAR2(256);
  v_count         PLS_INTEGER;
BEGIN
  IF v_session_user != v_target_schema THEN
    RAISE_APPLICATION_ERROR(-20061,
      'ORDS export must authenticate as the REST schema owner ' || v_target_schema
      || ' but the session user is ' || v_session_user
      || ' (ALTER SESSION SET CURRENT_SCHEMA is not a substitute)');
  END IF;

  BEGIN
    EXECUTE IMMEDIATE 'SELECT ords.installed_version FROM dual' INTO v_version;
  EXCEPTION
    WHEN OTHERS THEN
      RAISE_APPLICATION_ERROR(-20062,
        'ORDS is not installed in this database, or its metadata is not visible to '
        || v_session_user || ': ' || SUBSTR(SQLERRM, 1, 400));
  END;

  -- SQLcl's REST export schema calls ORDS_METADATA.ORDS_EXPORT.EXPORT_SCHEMA.
  -- An ORDS release without it cannot produce this export.
  SELECT COUNT(*) INTO v_count
  FROM all_procedures
  WHERE owner = 'ORDS_METADATA'
    AND object_name = 'ORDS_EXPORT'
    AND procedure_name = 'EXPORT_SCHEMA';
  IF v_count = 0 THEN
    RAISE_APPLICATION_ERROR(-20063,
      'Unsupported ORDS release ' || v_version
      || ': ORDS_METADATA.ORDS_EXPORT.EXPORT_SCHEMA is not available to ' || v_session_user
      || '. Upgrade ORDS (Oracle documents the schema export API from ORDS 25.1)');
  END IF;

  DBMS_OUTPUT.PUT_LINE('ORDS version: ' || v_version);
END;
/
PROMPT ORDS_ACCESS_VERIFIED:&&target_schema
