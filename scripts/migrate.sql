-- Arguments: target schema, environment, expected session user.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
SET ENCODING UTF-8
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

@@verify_db_access.sql
ALTER SESSION SET CURRENT_SCHEMA = &&target_schema;
