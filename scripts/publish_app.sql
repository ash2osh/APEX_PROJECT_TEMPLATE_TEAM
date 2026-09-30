-- Arguments: target parsing schema, target environment, expected session user,
-- absolute APEXlang source directory, absolute deployment descriptor, app ID,
-- and the live state the Builder drift guard approved ('-' skips that re-check).
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
DEFINE application_source = '&4'
DEFINE deployment_file = '&5'
DEFINE expected_app_id = '&6'
DEFINE expected_live_state = '&7'
SET ENCODING UTF-8
SET HEADING OFF
SET FEEDBACK OFF
SET ECHO OFF
SET VERIFY OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

DECLARE
  v_target_schema VARCHAR2(128) := UPPER('&&target_schema');
  v_environment   VARCHAR2(32) := LOWER('&&db_environment');
  v_expected_user VARCHAR2(128) := UPPER('&&expected_user');
  v_session_user  VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');
  -- Same production marker as scripts/db_targets.py, applied to each name.
  c_production_marker CONSTANT VARCHAR2(256) :=
    '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])';
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

  IF (REGEXP_LIKE(SYS_CONTEXT('USERENV', 'DB_NAME'), c_production_marker, 'i')
      OR REGEXP_LIKE(SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'), c_production_marker, 'i')
      OR REGEXP_LIKE(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), c_production_marker, 'i'))
     AND v_environment != 'production' THEN
    RAISE_APPLICATION_ERROR(-20002,
      'Database/service identity resembles production but the selected target is not production');
  END IF;
END;
/

-- The drift guard ran in an earlier session. Re-read the live revision in this
-- import session so a Builder save or teammate import since then is refused
-- instead of overwritten.
DECLARE
  v_expected VARCHAR2(1024) := '&&expected_live_state';
  v_observed VARCHAR2(1024);
  -- The characters Python's str.rstrip() removes; the drift guard's token is
  -- built from a version stripped that way.
  c_python_whitespace CONSTANT VARCHAR2(200) := UNISTR(
    '\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
    || '\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A'
    || '\2028\2029\202F\205F\3000');
BEGIN
  IF v_expected != '-' THEN
    SELECT CASE
             WHEN COUNT(*) = 0 THEN 'ABSENT'
             ELSE 'P|' || NVL(TO_CHAR(MAX(last_updated_on), 'YYYY-MM-DD"T"HH24:MI:SS'), 'NONE')
                  || '|' || RAWTOHEX(UTL_I18N.STRING_TO_RAW(
                       REGEXP_REPLACE(MAX(version), '[' || c_python_whitespace || ']+$'), 'AL32UTF8'))
           END
      INTO v_observed
      FROM apex_applications
     WHERE application_id = TO_NUMBER('&&expected_app_id');
    IF v_observed != v_expected THEN
      RAISE_APPLICATION_ERROR(-20016,
        'Live application changed after the Builder drift check; export and reconcile before publishing');
    END IF;
  END IF;
END;
/

apex import -input "&&application_source" -deployment "&&deployment_file"

DECLARE
  v_application_count PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_application_count
  FROM apex_applications
  WHERE application_id = TO_NUMBER('&&expected_app_id');
  IF v_application_count != 1 THEN
    RAISE_APPLICATION_ERROR(-20015,
      'Imported APEX application is not visible: ' || '&&expected_app_id');
  END IF;
END;
/

PROMPT APEX_IMPORT_VERIFIED:&&expected_app_id
EXIT SUCCESS COMMIT
