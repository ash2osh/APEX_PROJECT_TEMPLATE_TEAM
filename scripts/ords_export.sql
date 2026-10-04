-- Export one schema's ORDS (REST) definition with SQLcl's REST export schema.
-- Export only: it never enables REST, imports generated SQL, changes ORDS
-- configuration, creates metadata, grants privileges or commits. Arguments
-- are supplied by the validated shell/PowerShell wrappers: schema,
-- environment, expected session user, and a SQLcl-safe spool schema.
--
-- The wrapper (scripts/ords_export.py verify) judges the transcript and the
-- spool files; SQLcl's exit status and a non-empty file prove nothing.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
DEFINE spool_schema = '&4'
SET ENCODING UTF-8
SET PAGESIZE 0
SET HEADING OFF
SET LINESIZE 32767
SET LONG 100000000
SET LONGCHUNKSIZE 100000000
SET TRIMSPOOL ON
SET FEEDBACK OFF
SET ECHO OFF
SET VERIFY OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

@@verify_ords_access.sql

DEFINE inventory_label = 'before'
@@ords_inventory.sql

-- SQLcl's REST export schema always passes p_include_oauth => TRUE, so an
-- export would carry OAuth client definitions. This template exports no OAuth
-- clients, secrets, tokens or credentials: refuse before exporting anything.
DECLARE
  v_clients PLS_INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_clients FROM user_ords_clients;
  IF v_clients > 0 THEN
    RAISE_APPLICATION_ERROR(-20064,
      'Schema ' || UPPER('&&target_schema') || ' owns ' || v_clients
      || ' ORDS OAuth client(s). SQLcl''s REST export schema always includes OAuth clients,'
      || ' and this template exports none, so nothing was exported');
  END IF;
END;
/

COLUMN export_script NEW_VALUE export_script NOPRINT
SELECT CASE WHEN COUNT(*) > 0 THEN 'ords_export_run.sql' ELSE 'ords_export_skip.sql' END AS export_script
FROM user_ords_schemas
WHERE UPPER(status) = 'ENABLED';
@@&&export_script

DEFINE inventory_label = 'after'
@@ords_inventory.sql

PROMPT ORDS_EXPORT_COMPLETE:&&target_schema
SET DEFINE OFF
EXIT SUCCESS ROLLBACK
