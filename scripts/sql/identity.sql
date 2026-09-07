-- Read-only identity query included by the generated SQLcl driver.
-- The Python adapter verifies the two observations and compares them with the
-- tracked target contract before it accepts any payload result.
SET DEFINE OFF
SET HEADING OFF
SET FEEDBACK OFF
SELECT 'TEAM_IDENTITY|' ||
       'SESSION_USER=' || REPLACE(SYS_CONTEXT('USERENV','SESSION_USER'),'|','/') ||
       '|CURRENT_SCHEMA=' || REPLACE(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),'|','/') ||
       '|DB_NAME=' || REPLACE(SYS_CONTEXT('USERENV','DB_NAME'),'|','/') ||
       '|SERVICE=' || REPLACE(SYS_CONTEXT('USERENV','SERVICE_NAME'),'|','/') ||
       '|INSTANCE_ID=' ||
       REPLACE(SYS_CONTEXT('USERENV','INSTANCE_NAME'),'|','/') || '@' ||
       REPLACE(SYS_CONTEXT('USERENV','SERVER_HOST'),'|','/')
FROM dual;
