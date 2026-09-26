-- Read-only connection and database identity check used by team doctor.
SET DEFINE ON
SET VERIFY OFF
SET ECHO OFF
WHENEVER SQLERROR EXIT SQL.SQLCODE
WHENEVER OSERROR EXIT FAILURE
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
@@verify_db_access.sql
PROMPT SQLcl connection and database identity checks passed.
EXIT SUCCESS ROLLBACK
