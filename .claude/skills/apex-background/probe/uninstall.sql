-- Remove only probe objects whose individual ownership markers match.
-- Missing objects are accepted so a previously partial uninstall can resume.
WHENEVER SQLERROR EXIT FAILURE ROLLBACK

DECLARE
  l_count PLS_INTEGER;

  PROCEDURE require_owned_table(p_table VARCHAR2) IS
    l_exists PLS_INTEGER;
    l_marked PLS_INTEGER;
  BEGIN
    SELECT COUNT(*) INTO l_exists FROM user_tables WHERE table_name = p_table;
    SELECT COUNT(*) INTO l_marked
      FROM user_tab_comments
     WHERE table_name = p_table
       AND comments = 'APEX_BG_PROBE_OWNER_V1';
    IF l_exists <> l_marked THEN
      raise_application_error(-20003, 'Ownership marker missing or invalid for ' || p_table);
    END IF;
  END require_owned_table;
BEGIN
  require_owned_table('APEX_BG_PROBE_RUN');
  require_owned_table('APEX_BG_PROBE_LOG');

  SELECT COUNT(*) INTO l_count
    FROM user_objects
   WHERE object_name = 'APEX_BG_PROBE'
     AND object_type IN ('PACKAGE', 'PACKAGE BODY');
  IF l_count > 0 THEN
    DECLARE
      l_specs PLS_INTEGER;
      l_bodies PLS_INTEGER;
      l_marked PLS_INTEGER;
    BEGIN
      SELECT COUNT(*) INTO l_specs
        FROM user_objects WHERE object_name = 'APEX_BG_PROBE' AND object_type = 'PACKAGE';
      SELECT COUNT(*) INTO l_bodies
        FROM user_objects WHERE object_name = 'APEX_BG_PROBE' AND object_type = 'PACKAGE BODY';
      SELECT COUNT(*) INTO l_marked
        FROM user_source
       WHERE name = 'APEX_BG_PROBE'
         AND type = 'PACKAGE BODY'
         AND INSTR(UPPER(text), 'APEX_BG_PROBE_OWNER_V1') > 0;
      IF l_specs <> 1 OR l_bodies <> 1 OR l_marked = 0 THEN
        raise_application_error(-20003, 'APEX_BG_PROBE package ownership marker is missing or invalid');
      END IF;
    END;
  END IF;
END;
/

DECLARE
  l_count PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO l_count
    FROM user_objects
   WHERE object_name = 'APEX_BG_PROBE'
     AND object_type = 'PACKAGE';
  IF l_count = 1 THEN
    EXECUTE IMMEDIATE 'DROP PACKAGE apex_bg_probe';
  END IF;

  SELECT COUNT(*) INTO l_count FROM user_tables WHERE table_name = 'APEX_BG_PROBE_LOG';
  IF l_count = 1 THEN
    EXECUTE IMMEDIATE 'DROP TABLE apex_bg_probe_log PURGE';
  END IF;

  SELECT COUNT(*) INTO l_count FROM user_tables WHERE table_name = 'APEX_BG_PROBE_RUN';
  IF l_count = 1 THEN
    EXECUTE IMMEDIATE 'DROP TABLE apex_bg_probe_run PURGE';
  END IF;
END;
/
PROMPT APEX_BG_PROBE_UNINSTALLED
EXIT SUCCESS COMMIT
