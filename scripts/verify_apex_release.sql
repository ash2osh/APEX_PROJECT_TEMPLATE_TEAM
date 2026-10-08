-- APEX-only read-only preflight. Never include this in schema or ORDS drivers.
DECLARE
  v_release VARCHAR2(100);
BEGIN
  SELECT version_no INTO v_release FROM apex_release;
  IF v_release IS NULL OR NOT REGEXP_LIKE(v_release, '^26[.]2([.][[:digit:]]+)*$') THEN
    RAISE_APPLICATION_ERROR(-20018,
      'This template requires APEX 26.2; found ' || NVL(v_release, '(unknown)')
      || '. Use the matching template branch.');
  END IF;
  DBMS_OUTPUT.PUT_LINE('APEX_RELEASE_VERIFIED:26.2');
END;
/
