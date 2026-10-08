-- Read-only connection and database identity check used by team doctor.
SET DEFINE ON
SET VERIFY OFF
SET ECHO OFF
-- Only the identity line is wanted: no column heading (SQLcl prints it in ANSI
-- bold when output is redirected) and no "PL/SQL procedure successfully completed".
SET HEADING OFF
SET FEEDBACK OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
DEFINE doctor_profile = '&4'
DEFINE doctor_username_hex = '&5'
DEFINE doctor_app_ids = '&6'
@@verify_db_access.sql
-- The same identity driver also serves table/code profiles. Dynamic SQL keeps
-- those profiles independent of APEX installation and privileges.
DECLARE
  v_release VARCHAR2(100);
  v_username VARCHAR2(255);
BEGIN
  IF '&&doctor_profile' = 'apex' THEN
    EXECUTE IMMEDIATE 'SELECT version_no FROM apex_release' INTO v_release;
    IF v_release IS NULL OR NOT REGEXP_LIKE(v_release, '^26[.]2([.][[:digit:]]+)*$') THEN
      RAISE_APPLICATION_ERROR(-20018,
        'This template requires APEX 26.2; found ' || NVL(v_release, '(unknown)')
        || '. Use the matching template branch.');
    END IF;
    DBMS_OUTPUT.PUT_LINE('APEX_RELEASE_VERIFIED:26.2');
    IF NOT REGEXP_LIKE('&&doctor_username_hex', '^([0-9A-F]{2})+$') THEN
      RAISE_APPLICATION_ERROR(-20064, 'Set APEX_WORKSPACE_USERNAME explicitly');
    END IF;
    v_username := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&doctor_username_hex'), 'AL32UTF8');
    -- Dynamic SQL keeps table/code-only profiles independent of APEX. IDs
    -- travel as a bound comma list, never as executable SQL or an IN clause.
    EXECUTE IMMEDIATE q'~DECLARE
      v_schema VARCHAR2(128) := :schema_name;
      v_username VARCHAR2(255) := :workspace_user;
      v_ids VARCHAR2(32767) := :app_ids;
      n_workspaces PLS_INTEGER := 0;
      n_users PLS_INTEGER;
      v_canonical VARCHAR2(255);
    BEGIN
      FOR w IN (SELECT DISTINCT workspace FROM apex_applications
        WHERE owner=v_schema AND INSTR(','||v_ids||',', ','||TO_CHAR(application_id)||',')>0) LOOP
        n_workspaces := n_workspaces+1;
        apex_util.set_security_group_id(apex_util.find_security_group_id(w.workspace));
        SELECT COUNT(*), MAX(user_name) INTO n_users, v_canonical FROM apex_workspace_apex_users
          WHERE workspace_name=w.workspace AND UPPER(user_name)=UPPER(v_username)
          AND (is_admin='Yes' OR is_application_developer='Yes');
        IF n_users<>1 THEN
          RAISE_APPLICATION_ERROR(-20064, 'APEX_WORKSPACE_USERNAME is not a developer/admin in workspace '||w.workspace);
        END IF;
        DBMS_OUTPUT.PUT_LINE('APEX_WORKSPACE_USER_VERIFIED:'||w.workspace||':'||v_canonical);
      END LOOP;
      IF n_workspaces=0 THEN
        RAISE_APPLICATION_ERROR(-20073, 'Workspace user check unavailable: no configured APEX app is visible in this schema');
      END IF;
      DBMS_OUTPUT.PUT_LINE('APEX_WORKSPACE_USERS_VERIFIED');
    END;~' USING UPPER('&&target_schema'), v_username, '&&doctor_app_ids';
  END IF;
END;
/
PROMPT SQLcl connection and database identity checks passed.
PROMPT APEX_DOCTOR_VERIFIED:&&expected_user
EXIT SUCCESS ROLLBACK
