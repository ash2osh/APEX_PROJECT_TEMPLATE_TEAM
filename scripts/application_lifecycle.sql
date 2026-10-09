-- APEX 26.2 public-view adapter; caller starts a read-only transaction.
DEFINE lifecycle_workspace_hex = '&1'
DEFINE lifecycle_app_id = '&2'
DECLARE
  v_workspace VARCHAR2(255) := UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&lifecycle_workspace_hex'),'AL32UTF8');
  v_app NUMBER := TO_NUMBER('&&lifecycle_app_id');
  v_workspace_id NUMBER;
  v_release VARCHAR2(100);
  v_count NUMBER;
  v_present BOOLEAN;
  v_owner VARCHAR2(128);
  v_app_workspace VARCHAR2(255);
  v_started VARCHAR2(100) := TO_CHAR(SYS_EXTRACT_UTC(SYSTIMESTAMP),'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"');
  v_json CLOB;
  v_offset PLS_INTEGER := 1;
  v_chunk VARCHAR2(32767);
BEGIN
  IF SYS_CONTEXT('USERENV','SESSION_USER')<>UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&lifecycle_user_hex'),'AL32UTF8') OR SYS_CONTEXT('USERENV','CURRENT_SCHEMA')<>UTL_I18N.RAW_TO_CHAR(HEXTORAW('&&lifecycle_schema_hex'),'AL32UTF8') THEN RAISE_APPLICATION_ERROR(-20001,'Lifecycle database identity mismatch'); END IF;
  SELECT version_no INTO v_release FROM apex_release;
  IF NOT REGEXP_LIKE(v_release,'^26[.]2([.][[:digit:]]+)*$') THEN RAISE_APPLICATION_ERROR(-20018,'Lifecycle adapter requires APEX 26.2'); END IF;
  v_workspace_id := apex_util.find_security_group_id(v_workspace);
  IF v_workspace_id IS NULL OR v_workspace_id<=0 THEN RAISE_APPLICATION_ERROR(-20073,'Lifecycle workspace is not visible'); END IF;
  apex_util.set_security_group_id(v_workspace_id);
  IF NV('FLOW_SECURITY_GROUP_ID')<>v_workspace_id THEN RAISE_APPLICATION_ERROR(-20073,'Lifecycle workspace context failed'); END IF;
  SELECT COUNT(*) INTO v_count FROM apex_workspaces WHERE workspace_id=v_workspace_id AND workspace=v_workspace;
  IF v_count<>1 THEN RAISE_APPLICATION_ERROR(-20073,'Lifecycle workspace scope failed'); END IF;
  SELECT COUNT(*), MAX(owner), MAX(workspace) INTO v_count,v_owner,v_app_workspace FROM apex_applications WHERE application_id=v_app;
  IF v_count>1 OR (v_count=1 AND (v_owner<>SYS_CONTEXT('USERENV','CURRENT_SCHEMA') OR v_app_workspace<>v_workspace)) THEN RAISE_APPLICATION_ERROR(-20073,'Lifecycle application scope mismatch'); END IF;
  v_present:=v_count=1;
  apex_json.initialize_clob_output;
  apex_json.open_object;
  apex_json.write('schemaVersion',1);
  apex_json.open_object('identity');
  apex_json.write('session_user',SYS_CONTEXT('USERENV','SESSION_USER'));
  apex_json.write('current_schema',SYS_CONTEXT('USERENV','CURRENT_SCHEMA'));
  apex_json.write('db_unique_name',SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'));
  apex_json.write('container_id',SYS_CONTEXT('USERENV','CON_ID'));
  apex_json.write('container_name',SYS_CONTEXT('USERENV','CON_NAME'));
  apex_json.write('edition',SYS_CONTEXT('USERENV','CURRENT_EDITION_NAME'));
  apex_json.write('server_host',SYS_CONTEXT('USERENV','SERVER_HOST'));
  apex_json.write('service_name',SYS_CONTEXT('USERENV','SERVICE_NAME'));
  apex_json.close_object;
  apex_json.write('release',v_release); apex_json.write('workspace',v_workspace);
  apex_json.write('workspaceId',TO_CHAR(v_workspace_id,'TM9')); apex_json.write('appId',v_app); apex_json.write('applicationPresent',v_present);
  apex_json.write('startedAt',v_started);
  apex_json.open_array('automations');
  FOR r IN (SELECT static_id,polling_status_code FROM apex_appl_automations WHERE application_id=v_app AND workspace=v_workspace ORDER BY static_id) LOOP
    apex_json.open_object; apex_json.write('staticId',r.static_id); apex_json.write('status',r.polling_status_code); apex_json.close_object;
  END LOOP;
  apex_json.close_array;
  apex_json.open_array('workflows');
  FOR r IN (SELECT workflow_id,workflow_def_id,workflow_def_static_id,state_code FROM apex_workflows WHERE application_id=v_app AND workspace_id=v_workspace_id ORDER BY workflow_id) LOOP
    apex_json.open_object; apex_json.write('instanceId',TO_CHAR(r.workflow_id,'TM9')); apex_json.write('definitionId',TO_CHAR(r.workflow_def_id,'TM9'));
    apex_json.write('staticId',r.workflow_def_static_id); apex_json.write('state',r.state_code); apex_json.write('terminal',r.state_code IN ('COMPLETED','TERMINATED','FAULTED')); apex_json.close_object;
  END LOOP;
  apex_json.close_array;
  apex_json.open_array('tasks');
  FOR r IN (SELECT task_id,task_def_id,task_def_static_id,state_code FROM apex_tasks WHERE application_id=v_app AND workspace_id=v_workspace_id ORDER BY task_id) LOOP
    apex_json.open_object; apex_json.write('instanceId',TO_CHAR(r.task_id,'TM9')); apex_json.write('definitionId',TO_CHAR(r.task_def_id,'TM9'));
    apex_json.write('staticId',r.task_def_static_id); apex_json.write('state',r.state_code); apex_json.write('terminal',r.state_code IN ('COMPLETED','CANCELED','ERRORED','EXPIRED','FAILED')); apex_json.close_object;
  END LOOP;
  apex_json.close_array;
  apex_json.open_object('coverage');
  apex_json.write('application',TRUE); apex_json.write('workspace',TRUE); apex_json.write('automations',TRUE); apex_json.write('workflows',TRUE); apex_json.write('tasks',TRUE);
  apex_json.close_object;
  apex_json.write('completedAt',TO_CHAR(SYS_EXTRACT_UTC(SYSTIMESTAMP),'YYYY-MM-DD"T"HH24:MI:SS.FF6"Z"'));
  apex_json.close_object;
  v_json:=apex_json.get_clob_output;
  DBMS_OUTPUT.PUT_LINE('LIFECYCLE_PAYLOAD_BEGIN');
  WHILE v_offset<=DBMS_LOB.GETLENGTH(v_json) LOOP
    v_chunk:=DBMS_LOB.SUBSTR(v_json,8000,v_offset);
    DBMS_OUTPUT.PUT_LINE(v_chunk);
    v_offset:=v_offset+LENGTH2(v_chunk);
  END LOOP;
  DBMS_OUTPUT.PUT_LINE('LIFECYCLE_PAYLOAD_END');
  apex_json.free_output;
  DBMS_OUTPUT.PUT_LINE('LIFECYCLE_VERIFIED');
EXCEPTION WHEN OTHERS THEN
  apex_json.free_output;
  RAISE;
END;
/
