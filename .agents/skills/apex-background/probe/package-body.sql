SET DEFINE OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK

CREATE OR REPLACE PACKAGE BODY apex_bg_probe AS
  -- APEX_BG_PROBE_OWNER_V1: ownership marker checked by uninstall.sql.
  PROCEDURE log_value(p_context VARCHAR2, p_name VARCHAR2, p_value VARCHAR2, p_error VARCHAR2 DEFAULT NULL) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
  BEGIN
    INSERT INTO apex_bg_probe_log (run_id, context_name, probe_name, probe_value, probe_error)
    VALUES (current_run, p_context, p_name, SUBSTR(p_value, 1, 4000), SUBSTR(p_error, 1, 4000));
    COMMIT;
  END log_value;

  PROCEDURE start_run(p_label IN VARCHAR2) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
  BEGIN
    INSERT INTO apex_bg_probe_run (label) VALUES (p_label);
    COMMIT;
  END start_run;

  FUNCTION current_run RETURN NUMBER IS
    l_run NUMBER;
  BEGIN
    SELECT run_id INTO l_run
      FROM apex_bg_probe_run
     WHERE run_state = 'RUNNING';
    RETURN l_run;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      raise_application_error(-20002, 'APEX_BG_PROBE.start_run must run before capture');
  END current_run;

  PROCEDURE complete_run IS
    PRAGMA AUTONOMOUS_TRANSACTION;
    l_run_id NUMBER;
  BEGIN
    SELECT run_id INTO l_run_id
      FROM apex_bg_probe_run
     WHERE run_state = 'RUNNING';

    UPDATE apex_bg_probe_run
       SET run_state = 'COMPLETE'
     WHERE run_id = l_run_id;
    COMMIT;
  END complete_run;

  -- Each probe is evaluated separately so one unavailable API cannot hide the rest.
  PROCEDURE probe(p_context VARCHAR2, p_name VARCHAR2, p_expression VARCHAR2) IS
    l_value VARCHAR2(4000);
  BEGIN
    EXECUTE IMMEDIATE 'BEGIN :v := ' || p_expression || '; END;' USING OUT l_value;
    log_value(p_context, p_name, NVL(l_value, '<null>'));
  EXCEPTION
    WHEN OTHERS THEN
      log_value(p_context, p_name, NULL, SQLERRM);
  END probe;

  PROCEDURE capture_builtin_substitutions(p_context VARCHAR2) IS
    TYPE t_names IS TABLE OF VARCHAR2(64);
    l_names t_names := t_names(
      'APEX_FILES', 'APEX$ROW_NUM', 'APEX$ROW_SELECTOR', 'APEX$ROW_STATUS',
      'APP_ID', 'APP_ALIAS',
      'APP_AJAX_X01', 'APP_AJAX_X02', 'APP_AJAX_X03', 'APP_AJAX_X04', 'APP_AJAX_X05',
      'APP_AJAX_X06', 'APP_AJAX_X07', 'APP_AJAX_X08', 'APP_AJAX_X09', 'APP_AJAX_X10',
      'APP_BUILDER_SESSION', 'APP_DATE_TIME_FORMAT', 'APP_FILES', 'APP_NLS_DATE_FORMAT',
      'APP_NLS_TIMESTAMP_FORMAT', 'APP_NLS_TIMESTAMP_TZ_FORMAT', 'APP_PAGE_ALIAS', 'APP_PAGE_ID',
      'APP_REGION_DOM_ID', 'APP_REGION_ID', 'APP_REGION_STATIC_ID', 'APP_REQUEST_DATA_HASH',
      'APP_SESSION', 'SESSION', 'APP_SESSION_VISIBLE', 'APP_TEXT$PROBE_MESSAGE',
      'APP_TEXT$PROBE_MESSAGE$EN', 'APP_TITLE', 'APP_UNIQUE_PAGE_ID', 'APP_USER',
      'AUTHENTICATED_URL_PREFIX', 'BROWSER_LANGUAGE', 'CURRENT_PARENT_TAB_TEXT', 'DEBUG',
      'HOME_LINK', 'LOGIN_URL', 'LOGOUT_URL', 'MAIN_APP_ID', 'PUBLIC_URL_PREFIX', 'REQUEST',
      'WORKSPACE_FILES', 'WORKSPACE_ID',
      'APP_IMAGES', 'IMAGE_PREFIX', 'THEME_DB_IMAGES', 'THEME_IMAGES', 'WORKSPACE_IMAGE'
    );
    l_token VARCHAR2(128);
  BEGIN
    FOR i IN 1 .. l_names.COUNT LOOP
      l_token := '&' || l_names(i) || '.';
      probe(
        p_context,
        'DO_SUBSTITUTIONS(' || l_token || ')',
        'apex_application.do_substitutions(''' || l_token || ''')'
      );
    END LOOP;
  END capture_builtin_substitutions;

  PROCEDURE capture(p_context IN VARCHAR2) IS
  BEGIN
    probe(p_context, 'V(APP_ID)', q'[v('APP_ID')]');
    probe(p_context, 'V(APP_PAGE_ID)', q'[v('APP_PAGE_ID')]');
    probe(p_context, 'V(APP_SESSION)', q'[v('APP_SESSION')]');
    probe(p_context, 'V(APP_USER)', q'[v('APP_USER')]');
    probe(p_context, 'V(APP_ALIAS)', q'[v('APP_ALIAS')]');
    probe(p_context, 'V(WORKSPACE_ID)', q'[v('WORKSPACE_ID')]');
    probe(p_context, 'V(PROBE_APP_ITEM)', q'[v('PROBE_APP_ITEM')]');
    probe(p_context, 'V(P1_PROBE_ITEM)', q'[v('P1_PROBE_ITEM')]');
    probe(p_context, 'APEX_APPLICATION.G_FLOW_ID', 'apex_application.g_flow_id');
    probe(p_context, 'APEX_APPLICATION.G_FLOW_STEP_ID', 'apex_application.g_flow_step_id');
    probe(p_context, 'APEX_APPLICATION.G_INSTANCE', 'apex_application.g_instance');
    probe(p_context, 'APEX_APPLICATION.G_USER', 'apex_application.g_user');
    probe(p_context, 'SYS_CONTEXT(APEX$SESSION,APP_SESSION)', q'[sys_context('APEX$SESSION', 'APP_SESSION')]');
    probe(p_context, 'SYS_CONTEXT(APEX$SESSION,APP_USER)', q'[sys_context('APEX$SESSION', 'APP_USER')]');
    probe(p_context, 'SYS_CONTEXT(APEX$SESSION,WORKSPACE_ID)', q'[sys_context('APEX$SESSION', 'WORKSPACE_ID')]');
    capture_builtin_substitutions(p_context);
    probe(p_context, 'DO_SUBSTITUTIONS(&PROBE_SUBST.)', q'[apex_application.do_substitutions('&PROBE_SUBST.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&PROBE_APP_ITEM.)', q'[apex_application.do_substitutions('&PROBE_APP_ITEM.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&P1_PROBE_ITEM.)', q'[apex_application.do_substitutions('&P1_PROBE_ITEM.')]');
    probe(p_context, 'APEX_APPLICATION.G_FLOW_SCHEMA_OWNER', 'apex_application.g_flow_schema_owner');
    probe(p_context, 'APEX_APPLICATION.G_PROXY_SERVER', 'apex_application.g_proxy_server');
    probe(p_context, 'APEX_APPLICATION.G_SYSDATE', q'[to_char(apex_application.g_sysdate, 'YYYY-MM-DD')]');
    probe(p_context, 'V(SYSDATE_YYYYMMDD)', q'[v('SYSDATE_YYYYMMDD')]');
    probe(p_context, 'USERENV SESSION_USER', q'[sys_context('USERENV', 'SESSION_USER')]');
    probe(p_context, 'USERENV CURRENT_SCHEMA', q'[sys_context('USERENV', 'CURRENT_SCHEMA')]');
    probe(p_context, 'USERENV MODULE', q'[sys_context('USERENV', 'MODULE')]');
    probe(p_context, 'USERENV ACTION', q'[sys_context('USERENV', 'ACTION')]');
    probe(p_context, 'USERENV CLIENT_IDENTIFIER', q'[sys_context('USERENV', 'CLIENT_IDENTIFIER')]');
    probe(p_context, 'USERENV CLIENT_INFO', q'[sys_context('USERENV', 'CLIENT_INFO')]');
    probe(p_context, 'USERENV BG_JOB_ID', q'[sys_context('USERENV', 'BG_JOB_ID')]');
    probe(p_context, 'USERENV SID', q'[sys_context('USERENV', 'SID')]');
    probe(p_context, 'SESSIONTIMEZONE', 'sessiontimezone');
  END capture;

  PROCEDURE capture_bind(p_context IN VARCHAR2, p_name IN VARCHAR2, p_value IN VARCHAR2) IS
  BEGIN
    log_value(p_context, 'BIND :' || p_name, NVL(p_value, '<null>'));
  END capture_bind;

  PROCEDURE record_background_execution(p_context IN VARCHAR2) IS
    l_execution apex_background_process.t_execution;
  BEGIN
    l_execution := apex_background_process.get_current_execution;
    log_value(p_context, 'BACKGROUND_EXECUTION_ID', TO_CHAR(l_execution.id));
    log_value(p_context, 'BACKGROUND_EXECUTION_STATE', l_execution.state);
  END record_background_execution;

  PROCEDURE record_context_complete(p_context IN VARCHAR2) IS
  BEGIN
    log_value(p_context, 'CONTEXT_COMPLETE', 'OK');
  END record_context_complete;

  PROCEDURE record_issue(p_name IN VARCHAR2, p_detail IN VARCHAR2) IS
  BEGIN
    log_value('FINISH', p_name, NULL, p_detail);
  END record_issue;
END apex_bg_probe;
/

DECLARE
  l_error_count PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO l_error_count
    FROM user_errors
   WHERE name = 'APEX_BG_PROBE'
     AND type = 'PACKAGE BODY'
     AND attribute = 'ERROR';
  IF l_error_count > 0 THEN
    raise_application_error(-20006, 'APEX_BG_PROBE package body has compilation errors');
  END IF;
END;
/
PROMPT APEX_BG_PROBE_PACKAGE_BODY_READY
