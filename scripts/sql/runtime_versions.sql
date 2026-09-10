-- Read-only prepared-runner version markers.
SET DEFINE OFF
SET HEADING OFF
SET FEEDBACK OFF
SET PAGESIZE 0
SELECT 'TEAM_RUNTIME|database|' || version_full
  FROM product_component_version
 WHERE product LIKE 'Oracle Database%';
SELECT 'TEAM_RUNTIME|apex|' || version_no
  FROM apex_release;
