-- Arguments: target schema, target environment, expected SQLcl session user.
SET DEFINE ON
DEFINE target_schema = '&1'
DEFINE target_environment = '&2'
DEFINE expected_user = '&3'
SET ENCODING UTF-8
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK

@@verify_migration_access.sql
