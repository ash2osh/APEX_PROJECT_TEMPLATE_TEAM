-- Export the connected schema's ORDS definition twice, back to back. The
-- wrapper requires both spool files to be identical, which detects metadata
-- that changed while the export ran. Included by ords_export.sql; the caller
-- defines spool_schema. SPOOL does not create directories: the wrapper does.
PROMPT ORDS_EXPORT_MODE:run
SPOOL database/&&spool_schema/ords/schema.sql
REST export schema
SPOOL OFF
SPOOL verify/&&spool_schema/schema.sql
REST export schema
SPOOL OFF
