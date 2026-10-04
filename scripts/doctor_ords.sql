-- Read-only identity and ORDS capability check used by team doctor for the
-- ORDS profile. Stricter than doctor.sql: the session user must be the REST
-- schema owner (see verify_ords_access.sql).
SET DEFINE ON
SET VERIFY OFF
SET ECHO OFF
SET HEADING OFF
SET FEEDBACK OFF
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
@@verify_ords_access.sql
PROMPT SQLcl connection, database identity and ORDS checks passed.
PROMPT APEX_DOCTOR_VERIFIED:&&expected_user
EXIT SUCCESS ROLLBACK
