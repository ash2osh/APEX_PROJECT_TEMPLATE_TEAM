-- Remove APEX background probe objects only after their ownership markers match.
WHENEVER SQLERROR EXIT FAILURE ROLLBACK

DECLARE
  l_run_comment  USER_TAB_COMMENTS.COMMENTS%TYPE;
  l_log_comment  USER_TAB_COMMENTS.COMMENTS%TYPE;
  l_package_mark PLS_INTEGER;
BEGIN
  SELECT comments INTO l_run_comment
    FROM USER_TAB_COMMENTS
   WHERE table_name = 'APEX_BG_PROBE_RUN';
  SELECT comments INTO l_log_comment
    FROM USER_TAB_COMMENTS
   WHERE table_name = 'APEX_BG_PROBE_LOG';
  SELECT COUNT(*) INTO l_package_mark
    FROM USER_SOURCE
   WHERE name = 'APEX_BG_PROBE'
     AND type = 'PACKAGE BODY'
     AND INSTR(UPPER(text), 'APEX_BG_PROBE_OWNER_V1') > 0;
  IF l_run_comment <> 'APEX_BG_PROBE_OWNER_V1'
     OR l_log_comment <> 'APEX_BG_PROBE_OWNER_V1'
     OR l_package_mark = 0 THEN
    raise_application_error(-20003, 'APEX_BG_PROBE ownership check failed; refusing to drop objects');
  END IF;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    raise_application_error(-20003, 'APEX_BG_PROBE ownership markers are missing; refusing to drop objects');
END;
/

DROP PACKAGE apex_bg_probe;
DROP TABLE apex_bg_probe_log PURGE;
DROP TABLE apex_bg_probe_run PURGE;
PROMPT APEX_BG_PROBE_UNINSTALLED
EXIT SUCCESS COMMIT
