-- Read-only public Builder page-lock observation. Filename is a trusted literal.
SET VERIFY OFF
SET LONG 1000000
SET LONGCHUNKSIZE 1000000
SPOOL &1
SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
  'applicationId' VALUE application_id,
  'workspace' VALUE workspace,
  'pageId' VALUE page_id,
  'lockId' VALUE lock_id,
  'owner' VALUE locked_by,
  'comment' VALUE lock_comment,
  'lockedOn' VALUE TO_CHAR(locked_on, 'YYYY-MM-DD"T"HH24:MI:SS')
) ORDER BY page_id RETURNING CLOB), TO_CLOB('[]'))
FROM apex_application_locked_pages WHERE application_id = &&app_id;
SPOOL OFF
