-- APEX 26.2 scoped action adapter. The caller supplies hex-encoded text so
-- SQLcl substitution cannot turn a workspace/static ID into executable SQL.
DEFINE action_name = '&1'
DEFINE app_id = '&2'
DEFINE static_id_hex = '&3'
DEFINE expected_status_hex = '&4'
DEFINE workspace_user_hex = '&5'
DEFINE workspace_hex = '&6'
DEFINE target_user_hex = '&7'
DEFINE target_schema_hex = '&8'
DEFINE workspace_id = '&9'
DEFINE session_user_hex = '&10'
DEFINE current_schema_hex = '&11'
DEFINE db_unique_name_hex = '&12'
DEFINE container_id_hex = '&13'
DEFINE container_name_hex = '&14'
DEFINE edition_hex = '&15'
DEFINE server_host_hex = '&16'
DEFINE service_name_hex = '&17'
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK
DECLARE
  c_action CONSTANT VARCHAR2(20) := UPPER('&&action_name');
  c_app_id CONSTANT NUMBER := TO_NUMBER('&&app_id');
  c_static_id CONSTANT VARCHAR2(255) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&static_id_hex'),'AL32UTF8');
  c_expected_status CONSTANT VARCHAR2(30) := UPPER(UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&expected_status_hex'),'AL32UTF8'));
  c_workspace_user CONSTANT VARCHAR2(255) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&workspace_user_hex'),'AL32UTF8');
  c_workspace CONSTANT VARCHAR2(255) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&workspace_hex'),'AL32UTF8');
  c_target_user CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&target_user_hex'),'AL32UTF8');
  c_target_schema CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&target_schema_hex'),'AL32UTF8');
  c_workspace_id CONSTANT NUMBER := TO_NUMBER('&&workspace_id');
  c_session_user CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&session_user_hex'),'AL32UTF8');
  c_current_schema CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&current_schema_hex'),'AL32UTF8');
  c_db_unique_name CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&db_unique_name_hex'),'AL32UTF8');
  c_container_id CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&container_id_hex'),'AL32UTF8');
  c_container_name CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&container_name_hex'),'AL32UTF8');
  c_edition CONSTANT VARCHAR2(128) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&edition_hex'),'AL32UTF8');
  c_server_host CONSTANT VARCHAR2(255) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&server_host_hex'),'AL32UTF8');
  c_service_name CONSTANT VARCHAR2(255) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&service_name_hex'),'AL32UTF8');
  v_count PLS_INTEGER;
  v_workspace_id NUMBER;
  v_page_id NUMBER;
  v_owner VARCHAR2(128);
  v_app_workspace VARCHAR2(255);
  v_locked_by VARCHAR2(255);
  v_status VARCHAR2(30);
  v_after_status VARCHAR2(30);
  v_release VARCHAR2(100);
  v_started TIMESTAMP WITH TIME ZONE := SYSTIMESTAMP;
  v_action_started BOOLEAN := FALSE;
  v_json_initialized BOOLEAN := FALSE;
  v_session_id NUMBER;
  v_session_count PLS_INTEGER;
  v_run_log_count PLS_INTEGER := 0;
  v_log_id VARCHAR2(40);
  v_log_status_code VARCHAR2(100);
  v_log_status VARCHAR2(255);
  v_log_started VARCHAR2(100);
  v_log_ended VARCHAR2(100);
  v_log_successful_rows NUMBER;
  v_log_error_rows NUMBER;
  v_json CLOB;
  v_offset PLS_INTEGER := 1;
  v_chunk VARCHAR2(32767);
  PROCEDURE refuse(p_code VARCHAR2, p_message VARCHAR2) IS
  BEGIN
    DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_REFUSED:'||p_code);
    RAISE_APPLICATION_ERROR(-20081,p_message);
  END;
BEGIN
  IF c_action NOT IN ('ENABLE','DISABLE','RUN-ONCE') THEN
    refuse('AUTOMATION','unsupported explicit action');
  END IF;
  IF NVL(SYS_CONTEXT('USERENV','SESSION_USER'),CHR(0)) <> c_target_user
     OR NVL(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),CHR(0)) <> c_target_schema
     OR NVL(SYS_CONTEXT('USERENV','SESSION_USER'),CHR(0)) <> c_session_user
     OR NVL(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),CHR(0)) <> c_current_schema
     OR NVL(SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'),CHR(0)) <> c_db_unique_name
     OR NVL(SYS_CONTEXT('USERENV','CON_ID'),CHR(0)) <> c_container_id
     OR NVL(SYS_CONTEXT('USERENV','CON_NAME'),CHR(0)) <> c_container_name
     OR NVL(SYS_CONTEXT('USERENV','CURRENT_EDITION_NAME'),CHR(0)) <> c_edition
     OR NVL(SYS_CONTEXT('USERENV','SERVER_HOST'),CHR(0)) <> c_server_host
     OR NVL(SYS_CONTEXT('USERENV','SERVICE_NAME'),CHR(0)) <> c_service_name THEN
    refuse('TARGET_IDENTITY','database identity changed after confirmation');
  END IF;
  SELECT version_no INTO v_release FROM apex_release;
  IF NOT REGEXP_LIKE(v_release,'^26[.]2([.][[:digit:]]+)*$') THEN
    refuse('TARGET_IDENTITY','automation action requires APEX 26.2');
  END IF;
  v_workspace_id := apex_util.find_security_group_id(c_workspace);
  IF v_workspace_id IS NULL OR v_workspace_id <> c_workspace_id THEN
    refuse('WORKSPACE','workspace identity changed after confirmation');
  END IF;
  apex_util.set_security_group_id(v_workspace_id);
  IF NV('FLOW_SECURITY_GROUP_ID') <> v_workspace_id THEN
    refuse('WORKSPACE','workspace context could not be established');
  END IF;
  SELECT COUNT(*) INTO v_count
    FROM apex_workspaces
   WHERE workspace_id = v_workspace_id AND workspace = c_workspace;
  IF v_count <> 1 THEN
    refuse('WORKSPACE','workspace scope is unavailable');
  END IF;
  SELECT COUNT(*), MAX(owner), MAX(workspace), MAX(locked_by)
    INTO v_count, v_owner, v_app_workspace, v_locked_by
    FROM apex_applications
   WHERE application_id = c_app_id;
  IF v_count <> 1 OR v_owner <> c_target_schema OR v_app_workspace <> c_workspace THEN
    refuse('APPLICATION','application scope is absent or changed');
  END IF;
  IF v_locked_by IS NOT NULL THEN
    refuse('APPLICATION_LOCKED','application has a native Builder lock');
  END IF;
  SELECT MIN(page_id) INTO v_page_id
    FROM apex_application_pages
   WHERE application_id = c_app_id AND page_id > 0;
  IF v_page_id IS NULL THEN
    refuse('SESSION','application has no regular page for an APEX runtime session');
  END IF;
  SELECT COUNT(*), MAX(polling_status_code)
    INTO v_count, v_status
    FROM apex_appl_automations
   WHERE application_id = c_app_id AND workspace = c_workspace AND static_id = c_static_id;
  IF v_count <> 1 OR v_status IS NULL OR UPPER(v_status) <> c_expected_status THEN
    refuse('AUTOMATION','selected automation or schedule status changed');
  END IF;
  IF c_action = 'ENABLE' THEN
    SELECT COUNT(*) INTO v_count FROM user_sys_privs WHERE privilege = 'CREATE JOB';
    IF v_count <> 1 THEN
      refuse('PRIVILEGE','CREATE JOB must be granted to the application parsing schema to enable scheduled automations');
    END IF;
  END IF;
  SELECT COUNT(*) INTO v_count
    FROM apex_workspace_apex_users
   WHERE workspace_id = v_workspace_id AND UPPER(user_name) = UPPER(c_workspace_user)
     AND (is_admin = 'Yes' OR is_application_developer = 'Yes');
  IF v_count <> 1 THEN
    refuse('SESSION','configured APEX_WORKSPACE_USERNAME is not an existing workspace developer');
  END IF;
  BEGIN
    apex_session.create_session(
      p_app_id => c_app_id,
      p_page_id => v_page_id,
      p_username => c_workspace_user
    );
    IF V('APP_ID') <> TO_CHAR(c_app_id,'TM9')
       OR NV('FLOW_SECURITY_GROUP_ID') <> v_workspace_id
       OR V('APP_SESSION') IS NULL THEN
      refuse('SESSION','APEX application session scope failed');
    END IF;
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE = -20081 THEN RAISE; END IF;
      DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_REFUSED:SESSION');
      RAISE;
  END;

  v_started := SYSTIMESTAMP;
  v_action_started := TRUE;
  IF c_action = 'ENABLE' THEN
    apex_automation.enable(p_application_id => c_app_id, p_static_id => c_static_id);
  ELSIF c_action = 'DISABLE' THEN
    apex_automation.disable(p_application_id => c_app_id, p_static_id => c_static_id);
  ELSE
    apex_automation.execute(
      p_application_id => c_app_id,
      p_static_id => c_static_id,
      p_run_in_background => FALSE
    );
  END IF;
  COMMIT;

  SELECT COUNT(*), MAX(polling_status_code)
    INTO v_count, v_after_status
    FROM apex_appl_automations
   WHERE application_id = c_app_id AND workspace = c_workspace AND static_id = c_static_id;
  IF v_count <> 1 OR v_after_status IS NULL THEN
    RAISE_APPLICATION_ERROR(-20082,'selected automation readback is unavailable');
  END IF;
  IF c_action = 'RUN-ONCE' THEN
    SELECT COUNT(*) INTO v_run_log_count
      FROM apex_automation_log
     WHERE workspace_id = v_workspace_id
       AND application_id = c_app_id
       AND automation_static_id = c_static_id
       AND start_timestamp >= v_started;
    IF v_run_log_count = 1 THEN
      SELECT TO_CHAR(id,'TM9'), status_code, status,
             TO_CHAR(SYS_EXTRACT_UTC(start_timestamp),'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"'),
             CASE WHEN end_timestamp IS NULL THEN NULL ELSE TO_CHAR(SYS_EXTRACT_UTC(end_timestamp),'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"') END,
             successful_row_count, error_row_count
        INTO v_log_id, v_log_status_code, v_log_status, v_log_started, v_log_ended,
             v_log_successful_rows, v_log_error_rows
        FROM apex_automation_log
       WHERE workspace_id = v_workspace_id
         AND application_id = c_app_id
         AND automation_static_id = c_static_id
         AND start_timestamp >= v_started;
    END IF;
  END IF;

  v_session_id := TO_NUMBER(V('APP_SESSION'));
  apex_session.delete_session(p_session_id => v_session_id);
  COMMIT;
  SELECT COUNT(*) INTO v_session_count FROM apex_workspace_sessions
   WHERE workspace_id = v_workspace_id AND apex_session_id = v_session_id;
  IF v_session_count <> 0 THEN
    RAISE_APPLICATION_ERROR(-20083,'APEX session cleanup could not be verified');
  END IF;
  DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_SESSION_DELETED');

  apex_json.initialize_clob_output;
  v_json_initialized := TRUE;
  apex_json.open_object;
  apex_json.write('schemaVersion',1);
  apex_json.write('action',c_action);
  apex_json.write('appId',c_app_id);
  apex_json.write('workspace',c_workspace);
  apex_json.write('workspaceId',TO_CHAR(v_workspace_id,'TM9'));
  apex_json.write('staticId',c_static_id);
  apex_json.write('beforeStatus',v_status);
  apex_json.write('afterStatus',v_after_status);
  apex_json.write('apiReturned',TRUE);
  apex_json.write('startedAt',TO_CHAR(SYS_EXTRACT_UTC(v_started),'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"'));
  apex_json.write('runLogCount',v_run_log_count);
  apex_json.write('sessionDeleted',TRUE);
  IF v_run_log_count = 1 THEN
    apex_json.open_object('runLog');
    apex_json.write('id',v_log_id);
    apex_json.write('applicationId',c_app_id);
    apex_json.write('workspaceId',TO_CHAR(v_workspace_id,'TM9'));
    apex_json.write('workspace',c_workspace);
    apex_json.write('staticId',c_static_id);
    apex_json.write('statusCode',v_log_status_code);
    apex_json.write('status',v_log_status);
    apex_json.write('startedAt',v_log_started);
    apex_json.write('endedAt',v_log_ended);
    apex_json.write('successfulRows',v_log_successful_rows);
    apex_json.write('errorRows',v_log_error_rows);
    apex_json.close_object;
  ELSE
    apex_json.write('runLog',v_log_status,p_write_null=>TRUE);
  END IF;
  apex_json.close_object;
  v_json := apex_json.get_clob_output;
  DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_PAYLOAD_BEGIN');
  WHILE v_offset <= DBMS_LOB.GETLENGTH(v_json) LOOP
    v_chunk := DBMS_LOB.SUBSTR(v_json,8000,v_offset);
    DBMS_OUTPUT.PUT_LINE(v_chunk);
    v_offset := v_offset + LENGTH2(v_chunk);
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_PAYLOAD_END');
  apex_json.free_output;
  v_json_initialized := FALSE;
  DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_VERIFIED');
EXCEPTION
  WHEN OTHERS THEN
    IF v_json_initialized THEN apex_json.free_output; END IF;
    IF v_action_started THEN
      DBMS_OUTPUT.PUT_LINE('APEX_AUTOMATION_ACTION_UNKNOWN');
    END IF;
    RAISE;
END;
/
UNDEFINE action_name app_id static_id_hex expected_status_hex workspace_user_hex workspace_hex target_user_hex target_schema_hex workspace_id session_user_hex current_schema_hex db_unique_name_hex container_id_hex container_name_hex edition_hex server_host_hex service_name_hex
