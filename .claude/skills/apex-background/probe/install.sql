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
  run_state  VARCHAR2(8) DEFAULT 'RUNNING' NOT NULL
             CONSTRAINT apex_bg_probe_run_state_ck CHECK (run_state IN ('RUNNING', 'COMPLETE')),
  active_slot NUMBER GENERATED ALWAYS AS
              (CASE WHEN run_state = 'RUNNING' THEN 1 END) VIRTUAL,
  started_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

ALTER TABLE apex_bg_probe_run
  ADD CONSTRAINT apex_bg_probe_one_active_uq UNIQUE (active_slot);

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
  PROCEDURE complete_run;
  PROCEDURE capture(p_context IN VARCHAR2);
  PROCEDURE capture_bind(p_context IN VARCHAR2, p_name IN VARCHAR2, p_value IN VARCHAR2);
  PROCEDURE record_background_execution(p_context IN VARCHAR2);
  PROCEDURE record_context_complete(p_context IN VARCHAR2);
  PROCEDURE record_issue(p_name IN VARCHAR2, p_detail IN VARCHAR2);
END apex_bg_probe;
/

@@package-body.sql

PROMPT APEX_BG_PROBE_INSTALLED
EXIT SUCCESS COMMIT
