-- One authoritative inventory line of the connected schema's ORDS metadata,
-- read from the ORDS data dictionary views. The caller defines inventory_label
-- (before or after). The separator is a comma: SQLcl's statement splitter
-- cuts a SELECT at a semicolon inside a string literal (see backup_db.sql).
-- A view that cannot be read raises an error and ends the script: an
-- unavailable inventory is never reported as an empty schema.
SELECT 'ORDS_INVENTORY:&&inventory_label:'
       || 'schemas=' || (SELECT COUNT(*) FROM user_ords_schemas)
       || ',enabled=' || (SELECT COUNT(*) FROM user_ords_schemas WHERE UPPER(status) = 'ENABLED')
       || ',modules=' || (SELECT COUNT(*) FROM user_ords_modules)
       || ',templates=' || (SELECT COUNT(*) FROM user_ords_templates)
       || ',handlers=' || (SELECT COUNT(*) FROM user_ords_handlers)
       || ',parameters=' || (SELECT COUNT(*) FROM user_ords_parameters)
       || ',roles=' || (SELECT COUNT(*) FROM user_ords_roles)
       || ',privileges=' || (SELECT COUNT(*) FROM user_ords_privileges)
       || ',privilege_roles=' || (SELECT COUNT(*) FROM user_ords_privilege_roles)
       || ',privilege_modules=' || (SELECT COUNT(*) FROM user_ords_privilege_modules)
       || ',privilege_mappings=' || (SELECT COUNT(*) FROM user_ords_privilege_mappings)
       || ',enabled_objects=' || (SELECT COUNT(*) FROM user_ords_enabled_objects)
       || ',clients=' || (SELECT COUNT(*) FROM user_ords_clients)
FROM dual;
