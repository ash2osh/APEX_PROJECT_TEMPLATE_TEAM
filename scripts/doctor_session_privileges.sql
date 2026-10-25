-- Read-only summary of the connected login's enabled session privileges.
DECLARE
  TYPE t_privilege_list IS VARRAY(11) OF VARCHAR2(64);
  l_privileges t_privilege_list := t_privilege_list(
    'SELECT ANY DICTIONARY',
    'CREATE ANY PROCEDURE',
    'ALTER ANY PROCEDURE',
    'CREATE ANY TABLE',
    'ALTER ANY TABLE',
    'CREATE ANY INDEX',
    'CREATE ANY TRIGGER',
    'CREATE ANY SEQUENCE',
    'CREATE ANY VIEW',
    'CREATE ANY SYNONYM',
    'GRANT ANY OBJECT PRIVILEGE'
  );
  l_count PLS_INTEGER;
BEGIN
  FOR i IN 1 .. l_privileges.COUNT LOOP
    SELECT COUNT(*)
    INTO l_count
    FROM session_privs
    WHERE privilege = l_privileges(i);

    DBMS_OUTPUT.PUT_LINE(
      'SESSION_PRIVILEGE:' || l_privileges(i) || '=' ||
      CASE WHEN l_count > 0 THEN 'YES' ELSE 'NO' END
    );
  END LOOP;

  SELECT COUNT(*)
  INTO l_count
  FROM session_roles
  WHERE role = 'APEX_ADMINISTRATOR_ROLE';

  DBMS_OUTPUT.PUT_LINE(
    'SESSION_ROLE:APEX_ADMINISTRATOR_ROLE=' ||
    CASE WHEN l_count > 0 THEN 'YES' ELSE 'NO' END
  );
  DBMS_OUTPUT.PUT_LINE(
    'SESSION_MODE:ACTS_AS_SCHEMA ' || UPPER('&&target_schema') ||
    ' LOGIN ' || UPPER('&&expected_user')
  );
END;
/
