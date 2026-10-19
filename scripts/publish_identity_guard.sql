-- Bind a writer session to the identity observed before publication.
-- Arguments are UTF-8 hex values: DB unique name, service, container id,
-- container name, edition, server host, session user, current schema.
SET DEFINE ON
DEFINE expected_db_unique_name = '&1'
DEFINE expected_service_name = '&2'
DEFINE expected_container_id = '&3'
DEFINE expected_container_name = '&4'
DEFINE expected_edition = '&5'
DEFINE expected_server_host = '&6'
DEFINE expected_session_user = '&7'
DEFINE expected_current_schema = '&8'
SET SERVEROUTPUT ON
DECLARE
  FUNCTION expected_value(p_hex VARCHAR2) RETURN VARCHAR2 IS
  BEGIN
    RETURN UTL_I18N.RAW_TO_CHAR(HEXTORAW(p_hex), 'AL32UTF8');
  END;
  PROCEDURE require_equal(p_label VARCHAR2, p_context VARCHAR2, p_expected_hex VARCHAR2) IS
  BEGIN
    IF NVL(p_context, CHR(0)) <> NVL(expected_value(p_expected_hex), CHR(0)) THEN
      RAISE_APPLICATION_ERROR(-20081, 'Publication identity changed: ' || p_label);
    END IF;
  END;
BEGIN
  IF '&&expected_db_unique_name' <> '-' THEN
    require_equal('DB_UNIQUE_NAME', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'), '&&expected_db_unique_name');
    require_equal('SERVICE_NAME', SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '&&expected_service_name');
    require_equal('CON_ID', SYS_CONTEXT('USERENV', 'CON_ID'), '&&expected_container_id');
    require_equal('CON_NAME', SYS_CONTEXT('USERENV', 'CON_NAME'), '&&expected_container_name');
    require_equal('CURRENT_EDITION_NAME', SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '&&expected_edition');
    require_equal('SERVER_HOST', SYS_CONTEXT('USERENV', 'SERVER_HOST'), '&&expected_server_host');
    require_equal('SESSION_USER', SYS_CONTEXT('USERENV', 'SESSION_USER'), '&&expected_session_user');
    require_equal('CURRENT_SCHEMA', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'), '&&expected_current_schema');
  END IF;
  DBMS_OUTPUT.PUT_LINE('PUBLISH_IDENTITY_VERIFIED');
END;
/
