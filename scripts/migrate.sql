-- Arguments: target schema, environment, expected session user, migration path.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE db_environment = '&2'
DEFINE expected_user = '&3'
DEFINE migration_file = '&4'
SET ENCODING UTF-8
WHENEVER SQLERROR EXIT SQL.SQLCODE
WHENEVER OSERROR EXIT FAILURE

@@verify_db_access.sql
ALTER SESSION SET CURRENT_SCHEMA = &&target_schema;
@@&&migration_file

EXIT
