-- Read-only phase preflight. Arguments: parsing schema, workspace, phase.
SET DEFINE ON
SET HEADING OFF
SET FEEDBACK OFF
SET VERIFY OFF
SET PAGESIZE 0
SET TRIMSPOOL ON
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
DEFINE target_schema = '&1'
DEFINE target_workspace = '&2'
DEFINE target_phase = '&3'

DECLARE
  l_workspace_count PLS_INTEGER;
  l_workspace_id NUMBER;
  l_current_workspace_id NUMBER;
  l_app_count PLS_INTEGER;
  l_alias_count PLS_INTEGER;
  l_probe_name_count PLS_INTEGER;
  l_invalid_object_count PLS_INTEGER;
  l_probe_marker_count PLS_INTEGER;
  l_package_marker_count PLS_INTEGER;
  l_run_table_count PLS_INTEGER;
  l_log_table_count PLS_INTEGER;
  l_package_count PLS_INTEGER;
  l_package_body_count PLS_INTEGER;
  l_run_marker_count PLS_INTEGER;
  l_log_marker_count PLS_INTEGER;
  l_active_run_count PLS_INTEGER;
BEGIN
  IF UPPER(SYS_CONTEXT('USERENV', 'SESSION_USER')) <> UPPER('&&target_schema')
     OR UPPER(SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')) <> UPPER('&&target_schema') THEN
    raise_application_error(-20010, 'Connected user/current schema does not match the parsing schema');
  END IF;

  l_workspace_id := apex_util.find_security_group_id(p_workspace => '&&target_workspace');
  IF l_workspace_id IS NULL OR l_workspace_id = 0 THEN
    raise_application_error(-20011, 'The requested APEX workspace is missing or ambiguous');
  END IF;
  apex_util.set_security_group_id(p_security_group_id => l_workspace_id);
  l_current_workspace_id := NVL(NV('FLOW_SECURITY_GROUP_ID'), 0);
  IF l_current_workspace_id <> l_workspace_id THEN
    raise_application_error(-20011, 'The current APEX security group does not match the requested workspace');
  END IF;

  SELECT COUNT(*) INTO l_workspace_count
    FROM apex_workspaces
   WHERE workspace_id = l_workspace_id
     AND UPPER(workspace) = UPPER('&&target_workspace');
  IF l_workspace_count <> 1 THEN
    raise_application_error(-20011, 'The requested APEX workspace is missing or ambiguous');
  END IF;

  SELECT COUNT(*) INTO l_app_count
    FROM apex_applications
   WHERE application_id = 9901;
  SELECT COUNT(*) INTO l_probe_name_count
    FROM user_objects
   WHERE object_name IN ('APEX_BG_PROBE_RUN', 'APEX_BG_PROBE_LOG', 'APEX_BG_PROBE');
  SELECT COUNT(*) INTO l_invalid_object_count
    FROM user_objects
   WHERE object_name IN ('APEX_BG_PROBE_RUN', 'APEX_BG_PROBE_LOG', 'APEX_BG_PROBE')
     AND status <> 'VALID';
  SELECT COUNT(*) INTO l_run_table_count FROM user_tables WHERE table_name = 'APEX_BG_PROBE_RUN';
  SELECT COUNT(*) INTO l_log_table_count FROM user_tables WHERE table_name = 'APEX_BG_PROBE_LOG';
  SELECT COUNT(*) INTO l_package_count
    FROM user_objects WHERE object_name = 'APEX_BG_PROBE' AND object_type = 'PACKAGE';
  SELECT COUNT(*) INTO l_package_body_count
    FROM user_objects WHERE object_name = 'APEX_BG_PROBE' AND object_type = 'PACKAGE BODY';
  SELECT COUNT(*) INTO l_probe_marker_count
    FROM user_tab_comments
   WHERE table_name IN ('APEX_BG_PROBE_RUN', 'APEX_BG_PROBE_LOG')
     AND comments = 'APEX_BG_PROBE_OWNER_V1';
  SELECT COUNT(*) INTO l_package_marker_count
    FROM user_source
   WHERE name = 'APEX_BG_PROBE'
     AND type = 'PACKAGE BODY'
     AND INSTR(UPPER(text), 'APEX_BG_PROBE_OWNER_V1') > 0;
  IF UPPER('&&target_phase') = 'INSTALL' THEN
    IF l_app_count <> 0 THEN
      raise_application_error(-20012, 'Application ID 9901 is already in use; refusing to overwrite it');
    END IF;
    SELECT COUNT(*) INTO l_alias_count
      FROM apex_applications
     WHERE UPPER(alias) = 'APEX-BG-PROBE'
       AND UPPER(workspace) = UPPER('&&target_workspace');
    IF l_alias_count <> 0 THEN
      raise_application_error(-20018, 'The requested workspace already contains alias APEX-BG-PROBE');
    END IF;
    IF l_probe_name_count <> l_run_table_count + l_log_table_count + l_package_count + l_package_body_count THEN
      raise_application_error(-20019, 'Probe object names are used by an unexpected object type');
    END IF;
    IF l_probe_name_count <> 0 AND (
         l_probe_name_count <> 4
         OR l_run_table_count <> 1
         OR l_log_table_count <> 1
         OR l_package_count <> 1
         OR l_package_body_count <> 1
         OR l_probe_marker_count <> 2
         OR l_package_marker_count = 0
         OR l_invalid_object_count > 0
       ) THEN
      raise_application_error(-20019, 'Probe objects are partial or ownership markers do not match; refusing install resume');
    END IF;
  ELSE
    IF UPPER('&&target_phase') = 'UNINSTALL' THEN
      IF l_app_count = 1 THEN
        SELECT COUNT(*) INTO l_app_count
          FROM apex_applications
         WHERE application_id = 9901
           AND UPPER(alias) = 'APEX-BG-PROBE'
           AND UPPER(workspace) = UPPER('&&target_workspace')
           AND UPPER(owner) = UPPER('&&target_schema');
        IF l_app_count <> 1 THEN
          raise_application_error(-20013, 'Application 9901 does not match the expected alias, workspace, and parsing schema');
        END IF;
      END IF;
      SELECT COUNT(*) INTO l_run_marker_count
        FROM user_tab_comments
       WHERE table_name = 'APEX_BG_PROBE_RUN'
         AND comments = 'APEX_BG_PROBE_OWNER_V1';
      SELECT COUNT(*) INTO l_log_marker_count
        FROM user_tab_comments
       WHERE table_name = 'APEX_BG_PROBE_LOG'
         AND comments = 'APEX_BG_PROBE_OWNER_V1';
      IF l_run_table_count = 1 AND l_run_marker_count <> 1 THEN
        raise_application_error(-20014, 'Probe run table ownership marker does not match');
      END IF;
      IF l_log_table_count = 1 AND l_log_marker_count <> 1 THEN
        raise_application_error(-20014, 'Probe log table ownership marker does not match');
      END IF;
      IF l_package_count + l_package_body_count > 0 AND
         (l_package_count <> 1 OR l_package_body_count <> 1 OR l_package_marker_count = 0) THEN
        raise_application_error(-20014, 'Probe package ownership marker does not match');
      END IF;
      IF l_probe_name_count <> l_run_table_count + l_log_table_count + l_package_count + l_package_body_count THEN
        raise_application_error(-20019, 'Probe object names are used by an unexpected object type');
      END IF;
    ELSE
      SELECT COUNT(*) INTO l_app_count
        FROM apex_applications
       WHERE application_id = 9901
         AND UPPER(alias) = 'APEX-BG-PROBE'
         AND UPPER(workspace) = UPPER('&&target_workspace')
         AND UPPER(owner) = UPPER('&&target_schema');
      IF l_app_count <> 1 THEN
        raise_application_error(-20013, 'Application 9901 does not match the expected alias, workspace, and parsing schema');
      END IF;
      IF l_run_table_count <> 1 OR l_log_table_count <> 1 OR l_package_count <> 1
         OR l_package_body_count <> 1 OR l_probe_marker_count <> 2 OR l_package_marker_count = 0
         OR l_invalid_object_count > 0 THEN
        raise_application_error(-20014, 'Probe object ownership markers do not match');
      END IF;
    END IF;

    IF l_run_table_count = 1 THEN
      EXECUTE IMMEDIATE
        'SELECT COUNT(*) FROM apex_bg_probe_run WHERE run_state = ''RUNNING'''
        INTO l_active_run_count;
      IF UPPER('&&target_phase') = 'START' AND l_active_run_count <> 0 THEN
        raise_application_error(-20015, 'A probe run is already active');
      ELSIF UPPER('&&target_phase') = 'FINISH' AND l_active_run_count <> 1 THEN
        raise_application_error(-20016, 'Exactly one active probe run is required to finish');
      END IF;
    END IF;
  END IF;
END;
/

SELECT 'APEX_BG_PROBE_TARGET:'
       || UPPER(SYS_CONTEXT('USERENV', 'SESSION_USER')) || ':'
       || UPPER(SYS_CONTEXT('USERENV', 'DB_NAME')) || ':'
       || UPPER(SYS_CONTEXT('USERENV', 'CON_NAME')) || ':'
       || UPPER('&&target_workspace')
  FROM dual;
SELECT 'APEX_BG_PROBE_OBJECTS:' || CASE WHEN COUNT(*) = 0 THEN 'FRESH' ELSE 'OWNED' END
  FROM user_objects
 WHERE object_name IN ('APEX_BG_PROBE_RUN', 'APEX_BG_PROBE_LOG', 'APEX_BG_PROBE')
   AND object_type IN ('TABLE', 'PACKAGE', 'PACKAGE BODY');
PROMPT APEX_BG_PROBE_PREFLIGHT_OK
EXIT SUCCESS ROLLBACK
