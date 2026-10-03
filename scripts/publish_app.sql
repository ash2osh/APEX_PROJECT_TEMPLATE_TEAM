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
  c_production_marker CONSTANT VARCHAR2(256) :=
    '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])';
  c_non_production_marker CONSTANT VARCHAR2(64) := '(pre|non)[-_.]?(prod|prd)';
  v_schema_count  PLS_INTEGER;
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

  SELECT COUNT(*) INTO v_schema_count
  FROM all_users
  WHERE username = v_target_schema;
  IF v_schema_count = 0 THEN
    RAISE_APPLICATION_ERROR(-20014,
      'Target parsing schema does not exist or is not visible: ' || v_target_schema);
  END IF;

  IF (resembles_production(SYS_CONTEXT('USERENV', 'DB_NAME'))
      OR resembles_production(SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'))
      OR resembles_production(SYS_CONTEXT('USERENV', 'SERVICE_NAME')))
     AND v_environment != 'production' THEN
    RAISE_APPLICATION_ERROR(-20002,
      'Database/service identity resembles production but the selected target is not production');
  END IF;
END;
/

-- The drift guard ran in an earlier session. Re-read the live revision in this
-- import session so a Builder save or teammate import since then is refused
-- instead of overwritten. The token is built in PL/SQL, whose strings hold
-- 32767 bytes, so a long multibyte version still fits once hex-encoded.
DECLARE
  v_expected VARCHAR2(32767) := '&&expected_live_state';
  v_observed VARCHAR2(32767);
  v_count    PLS_INTEGER;
  v_updated  DATE;
  v_version  VARCHAR2(32767);
  -- The characters Python's str.rstrip() removes; the drift guard's token is
  -- built from a version stripped that way.
  c_python_whitespace CONSTANT VARCHAR2(200) := UNISTR(
    '\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680'
    || '\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A'
    || '\2028\2029\202F\205F\3000');
BEGIN
  IF v_expected != '-' THEN
    SELECT COUNT(*), MAX(last_updated_on), MAX(version)
      INTO v_count, v_updated, v_version
      FROM apex_applications
     WHERE application_id = TO_NUMBER('&&expected_app_id');
    IF v_count = 0 THEN
      v_observed := 'ABSENT';
    ELSE
      v_version := REGEXP_REPLACE(v_version, '[' || c_python_whitespace || ']+$');
      v_observed := 'P.' || NVL(TO_CHAR(v_updated, 'YYYY-MM-DD"T"HH24:MI:SS'), 'NONE') || '.';
      IF v_version IS NOT NULL THEN
        v_observed := v_observed || RAWTOHEX(UTL_I18N.STRING_TO_RAW(v_version, 'AL32UTF8'));
      END IF;
    END IF;
    IF v_observed != v_expected THEN
      RAISE_APPLICATION_ERROR(-20016,
        'Live application changed after the Builder drift check; export and reconcile before publishing');
    END IF;
  END IF;
END;
/

-- The live application must be parsed by the descriptor's schema, also with one
-- schema configured (only the multi-schema wrappers look it up beforehand); an
-- application that is not there yet (first import) is allowed.
DECLARE
  v_owner VARCHAR2(128);
BEGIN
  SELECT MAX(owner) INTO v_owner
  FROM apex_applications
  WHERE application_id = TO_NUMBER('&&expected_app_id');
  IF v_owner IS NOT NULL AND v_owner != UPPER('&&target_schema') THEN
    RAISE_APPLICATION_ERROR(-20017,
      'Application &&expected_app_id is parsed by ' || v_owner || ', not the descriptor''s '
      || UPPER('&&target_schema') || '; refusing to import');
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
