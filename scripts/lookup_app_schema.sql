-- Read-only lookup of an application's parsing schema. Arguments are supplied
-- by the validated wrappers: schema, app id, environment, expected session
-- user. The identity check runs first, so the lookup only ever reads through a
-- verified session.
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

SELECT 'APEX_APP_SCHEMA:' || &&app_id || ':' || NVL(MAX(owner), 'NOT_FOUND')
FROM apex_applications
WHERE application_id = &&app_id;

SET DEFINE OFF
EXIT SUCCESS ROLLBACK
