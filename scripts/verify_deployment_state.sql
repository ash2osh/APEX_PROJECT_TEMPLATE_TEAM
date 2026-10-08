-- Called by export_apps.sql inside its before/after revision observations.
-- The generated deployment file separately supplies the nonpublic checksum salt.
SET VERIFY OFF
SPOOL .apex-deployment-state.json
SELECT JSON_OBJECT(
  'subscription' VALUE JSON_OBJECT('masterApplicationIds' VALUE
    (SELECT JSON_ARRAYAGG(master_application_id ORDER BY master_application_id RETURNING CLOB)
       FROM apex_subscribed_components WHERE application_id = &&app_id) FORMAT JSON),
  'workspace' VALUE JSON_OBJECT('name' VALUE workspace),
  'app' VALUE JSON_OBJECT(
    'id' VALUE application_id,
    'name' VALUE application_name,
    'databaseSession' VALUE JSON_OBJECT('parsingSchema' VALUE owner),
    'runtime' VALUE JSON_OBJECT(
      'debugging' VALUE CASE debugging WHEN 'Allowed' THEN 'true' WHEN 'Not Allowed' THEN 'false' END FORMAT JSON,
      'logging' VALUE CASE logging WHEN 'Yes' THEN 'true' WHEN 'No' THEN 'false' END FORMAT JSON
    )
  )
) FROM apex_applications WHERE application_id = &&app_id;
SPOOL OFF
