-- Read-only Builder timestamp lookup. Arguments: app id and expected session user.
SET DEFINE ON
DEFINE application_id = '&1'
DEFINE expected_user = '&2'
SET ENCODING UTF-8
SET PAGESIZE 0
SET HEADING OFF
SET FEEDBACK OFF
SET ECHO OFF
SET VERIFY OFF
SET TRIMSPOOL ON
WHENEVER SQLERROR EXIT SQL.SQLCODE
WHENEVER OSERROR EXIT FAILURE

DECLARE
  v_expected_user VARCHAR2(128) := UPPER('&&expected_user');
  v_session_user  VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');
BEGIN
  IF v_expected_user != '-' AND v_session_user != v_expected_user THEN
    RAISE_APPLICATION_ERROR(-20001,
      'Expected session user ' || v_expected_user || ' but found ' || v_session_user);
  END IF;
END;
/

SELECT NVL(TO_CHAR(MAX(last_updated_on), 'YYYY-MM-DD"T"HH24:MI:SS'), 'NOT_FOUND')
       || '|' || TO_CHAR(SYSDATE, 'YYYY-MM-DD"T"HH24:MI:SS')
FROM apex_applications
WHERE application_id = &&application_id;

EXIT
