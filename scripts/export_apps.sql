-- Export one APEX application as APEXlang. Arguments are supplied by the
-- validated shell/PowerShell wrappers: schema, app id, environment,
-- expected session user.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE app_id = '&2'
DEFINE db_environment = '&3'
DEFINE expected_user = '&4'
SET ENCODING UTF-8
SET HEADING OFF
SET FEEDBACK OFF
SET ECHO OFF
SET VERIFY OFF
SET PAGESIZE 0
SET TRIMSPOOL ON
SET LINESIZE 32767
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

@@verify_db_access.sql

-- Save the live Builder revision in database time before reading APEX source.
SPOOL .apex-export-before.txt
SELECT CASE
         WHEN COUNT(*) = 0 THEN 'NOT_FOUND'
         -- APEX leaves last_updated_on NULL on import; the app still exists.
         WHEN MAX(last_updated_on) IS NULL THEN 'NO_TIMESTAMP'
         ELSE TO_CHAR(MAX(last_updated_on), 'YYYY-MM-DD"T"HH24:MI:SS')
       END || '|' || TO_CHAR(SYSDATE, 'YYYY-MM-DD"T"HH24:MI:SS')
       -- The version is last because it is free text and may contain '|'.
       || '|' || MAX(version)
FROM apex_applications
WHERE application_id = &&app_id;
SPOOL OFF

-- -dir is the parent directory. SQLcl creates the application-alias child.
apex export -applicationid &&app_id -exptype APEXLANG -overwrite-files -dir apps/&&target_schema

-- Refuse to publish a local export marker if Builder changed during the export.
SPOOL .apex-export-after.txt
SELECT CASE
         WHEN COUNT(*) = 0 THEN 'NOT_FOUND'
         -- APEX leaves last_updated_on NULL on import; the app still exists.
         WHEN MAX(last_updated_on) IS NULL THEN 'NO_TIMESTAMP'
         ELSE TO_CHAR(MAX(last_updated_on), 'YYYY-MM-DD"T"HH24:MI:SS')
       END || '|' || TO_CHAR(SYSDATE, 'YYYY-MM-DD"T"HH24:MI:SS')
       -- The version is last because it is free text and may contain '|'.
       || '|' || MAX(version)
FROM apex_applications
WHERE application_id = &&app_id;
SPOOL OFF

SET DEFINE OFF
EXIT SUCCESS ROLLBACK
