-- APEX background probe objects. Run as the probe application's parsing schema.
-- This creates only APEX_BG_PROBE* objects and refuses existing object names.
SET DEFINE OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK

DECLARE
  l_count PLS_INTEGER;
BEGIN
  SELECT COUNT(*)
    INTO l_count
    FROM user_objects
   WHERE object_name IN ('APEX_BG_PROBE_RUN', 'APEX_BG_PROBE_LOG', 'APEX_BG_PROBE')
     AND object_type IN ('TABLE', 'PACKAGE', 'PACKAGE BODY');
  IF l_count > 0 THEN
    raise_application_error(-20001, 'APEX_BG_PROBE object names already exist; refusing to replace them');
  END IF;
END;
/

CREATE TABLE apex_bg_probe_run (
  run_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  label      VARCHAR2(200) NOT NULL,
  started_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

COMMENT ON TABLE apex_bg_probe_run IS 'APEX_BG_PROBE_OWNER_V1';

CREATE TABLE apex_bg_probe_log (
  log_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id       NUMBER NOT NULL REFERENCES apex_bg_probe_run,
  context_name VARCHAR2(60) NOT NULL,
  probe_name   VARCHAR2(128) NOT NULL,
  probe_value  VARCHAR2(4000),
  probe_error  VARCHAR2(4000),
  captured_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

COMMENT ON TABLE apex_bg_probe_log IS 'APEX_BG_PROBE_OWNER_V1';

CREATE OR REPLACE PACKAGE apex_bg_probe AUTHID DEFINER AS
  PROCEDURE start_run(p_label IN VARCHAR2);
  FUNCTION current_run RETURN NUMBER;
  PROCEDURE capture(p_context IN VARCHAR2);
  PROCEDURE capture_bind(p_context IN VARCHAR2, p_name IN VARCHAR2, p_value IN VARCHAR2);
END apex_bg_probe;
/

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
    SELECT MAX(run_id) INTO l_run FROM apex_bg_probe_run;
    IF l_run IS NULL THEN
      raise_application_error(-20002, 'APEX_BG_PROBE.start_run must run before capture');
    END IF;
    RETURN l_run;
  END current_run;

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
    probe(p_context, 'DO_SUBSTITUTIONS(&APP_ID.)', q'[apex_application.do_substitutions('&APP_ID.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&APP_USER.)', q'[apex_application.do_substitutions('&APP_USER.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&APP_NAME.)', q'[apex_application.do_substitutions('&APP_NAME.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&PROBE_SUBST.)', q'[apex_application.do_substitutions('&PROBE_SUBST.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&PROBE_APP_ITEM.)', q'[apex_application.do_substitutions('&PROBE_APP_ITEM.')]');
    probe(p_context, 'DO_SUBSTITUTIONS(&P1_PROBE_ITEM.)', q'[apex_application.do_substitutions('&P1_PROBE_ITEM.')]');
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
END apex_bg_probe;
/

PROMPT APEX_BG_PROBE_INSTALLED
EXIT SUCCESS COMMIT
