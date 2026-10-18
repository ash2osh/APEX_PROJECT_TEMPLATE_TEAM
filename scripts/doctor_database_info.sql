-- Read-only database information for doctor, after its target identity guard.
-- Keep the internal software version separate from the full release and COMPATIBLE.
SET SERVEROUTPUT ON
SET LINESIZE 32767
DECLARE
  v_version       VARCHAR2(128);
  v_full_version  VARCHAR2(128);
  v_compatibility VARCHAR2(128);
BEGIN
  DBMS_UTILITY.DB_VERSION(v_version, v_compatibility);
  SELECT MAX(version_full) INTO v_full_version
    FROM product_component_version
   WHERE product LIKE 'Oracle%Database%';
  IF v_full_version IS NULL OR v_compatibility IS NULL
     OR NOT REGEXP_LIKE(v_full_version, '^[0-9]+([.][0-9]+)*$')
     OR NOT REGEXP_LIKE(v_compatibility, '^[0-9]+([.][0-9]+)*$') THEN
    RAISE_APPLICATION_ERROR(-20031, 'Database version/compatibility detection is unknown');
  END IF;
  DBMS_OUTPUT.PUT_LINE('Database version: ' || v_full_version
    || '; internal version: ' || v_version || '; COMPATIBLE: ' || v_compatibility);
  DBMS_OUTPUT.PUT_LINE('DATABASE_VERSION_VERIFIED:' || v_full_version || ':' || v_compatibility);
END;
/
