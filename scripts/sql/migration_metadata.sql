-- Controller-owned migration metadata. Execute only through METADATA.
--
-- This file is the single canonical source for the migration-metadata
-- bootstrap: teamlib.migration_store.SqlMigrationStore.bootstrap() builds its
-- runtime payload from this exact text, substituting the two placeholder
-- tokens below with sql_literal()-quoted values. Change the bootstrap
-- contract here or in migration_store.py and update both together;
-- scripts/tests/test_sql_metadata_store.py fails if they diverge.
--
-- To run this file by hand for diagnosis or recovery, replace both
-- placeholders with your own quoted string literals, e.g. __PROJECT_ID_LITERAL__
-- becomes 'team-template' and __SCHEMA_SET_DIGEST_LITERAL__ becomes a quoted
-- lowercase 64-character schema-set SHA-256.

-- Bootstrap identity contract: a fresh project row records the supplied
-- lowercase schema-set SHA-256 at version 2; an upgraded version-1 row may
-- fill only its empty digest; a non-empty version-2 digest is immutable and
-- any mismatch must be refused before metadata writes begin.
DECLARE
  PROCEDURE create_if_missing(p_sql CLOB) IS
  BEGIN
    EXECUTE IMMEDIATE p_sql;
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE != -955 THEN RAISE; END IF;
  END;
  v_count NUMBER;
  v_pk_count NUMBER;
  v_sequence_pk NUMBER;
  v_id_pk NUMBER;
  v_pk_name VARCHAR2(128);
  v_condition VARCHAR2(4000);
  v_duplicate NUMBER;
BEGIN
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_META (
    version_number NUMBER(10) NOT NULL,
    project_id VARCHAR2(128) NOT NULL,
    schema_set_digest VARCHAR2(64) NOT NULL,
    CONSTRAINT team_migration_meta_pk PRIMARY KEY (version_number, project_id)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_MUTEX (
    singleton_id NUMBER(1) NOT NULL,
    owner_token VARCHAR2(128),
    worker_identity VARCHAR2(256),
    host VARCHAR2(512),
    acquired_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT team_migration_mutex_pk PRIMARY KEY (singleton_id),
    CONSTRAINT team_migration_mutex_singleton_ck CHECK (singleton_id = 1)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_HISTORY (
    id VARCHAR2(128) NOT NULL,
    operation VARCHAR2(4) NOT NULL,
    checksum VARCHAR2(64) NOT NULL,
    target VARCHAR2(16) NOT NULL,
    dependencies_json CLOB NOT NULL,
    payload_manifest_json CLOB NOT NULL,
    source_commit VARCHAR2(128) NOT NULL,
    applied_sequence NUMBER(19) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL,
    applied_by VARCHAR2(256) NOT NULL,
    run_token VARCHAR2(128),
    attempt_id VARCHAR2(128),
    CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence),
    CONSTRAINT team_migration_history_operation_ck CHECK (operation IN ('up','down')),
    CONSTRAINT team_migration_history_target_ck CHECK (target IN ('tables', 'code'))
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_ATTEMPT (
    attempt_id VARCHAR2(128) NOT NULL,
    migration_id VARCHAR2(128) NOT NULL,
    checksum VARCHAR2(64) NOT NULL,
    action VARCHAR2(16) NOT NULL,
    state VARCHAR2(16) NOT NULL,
    run_token VARCHAR2(128) NOT NULL,
    worker_identity VARCHAR2(256) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    diagnostic_digest VARCHAR2(64),
    confirmation_digest VARCHAR2(64),
    CONSTRAINT team_migration_attempt_pk PRIMARY KEY (attempt_id),
    CONSTRAINT team_migration_attempt_action_ck CHECK (action IN ('migrate','undo','redo')),
    CONSTRAINT team_migration_attempt_state_ck CHECK (state IN ('RUNNING','APPLIED','FAILED','UNKNOWN','RECOVERED'))
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_INVENTORY (
    inventory_digest VARCHAR2(64) NOT NULL,
    manifest_json CLOB NOT NULL,
    schema_set_digest VARCHAR2(64) NOT NULL,
    normalizer_version VARCHAR2(32) NOT NULL,
    coverage_version VARCHAR2(32) NOT NULL,
    CONSTRAINT team_migration_inventory_pk PRIMARY KEY (inventory_digest)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_MIGRATION_OBSERVATION (
    sequence_number NUMBER(19) NOT NULL,
    migration_id VARCHAR2(128),
    attempt_id VARCHAR2(128),
    predecessor_sequence NUMBER(19),
    before_digest VARCHAR2(64) NOT NULL,
    after_digest VARCHAR2(64) NOT NULL,
    evidence_digest VARCHAR2(64) NOT NULL,
    CONSTRAINT team_migration_observation_pk PRIMARY KEY (sequence_number)
  )]');

  -- Existing v1 history receives its direction before constraints are checked.
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND column_name = 'OPERATION';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD (operation VARCHAR2(4))';
  END IF;
  UPDATE TEAM_MIGRATION_HISTORY SET operation = 'up' WHERE operation IS NULL;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_HISTORY
   WHERE operation IS NULL OR operation NOT IN ('up', 'down');
  IF v_count <> 0 THEN
    RAISE_APPLICATION_ERROR(-20021, 'MIGRATION_HISTORY_OPERATION_INVALID');
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND column_name = 'OPERATION'
     AND data_type = 'VARCHAR2' AND data_length = 4 AND nullable = 'N';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY MODIFY (operation VARCHAR2(4) NOT NULL)';
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_constraints
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_name = 'TEAM_MIGRATION_HISTORY_OPERATION_CK';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD CONSTRAINT team_migration_history_operation_ck CHECK (operation IN (''up'',''down''))';
  ELSE
    SELECT DBMS_LOB.SUBSTR(search_condition, 4000, 1) INTO v_condition
      FROM user_constraints
     WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_name = 'TEAM_MIGRATION_HISTORY_OPERATION_CK';
    IF REGEXP_REPLACE(UPPER(v_condition), '[[:space:]]', '') <> 'OPERATIONIN(''UP'',''DOWN'')' THEN
      RAISE_APPLICATION_ERROR(-20022, 'MIGRATION_HISTORY_OPERATION_CONSTRAINT_INVALID');
    END IF;
  END IF;

  SELECT COUNT(*) - COUNT(DISTINCT applied_sequence) INTO v_duplicate FROM TEAM_MIGRATION_HISTORY;
  IF v_duplicate <> 0 THEN
    RAISE_APPLICATION_ERROR(-20023, 'MIGRATION_HISTORY_SEQUENCE_DUPLICATE');
  END IF;
  SELECT COUNT(*) INTO v_pk_count FROM user_constraints
   WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_type = 'P';
  SELECT COUNT(*) INTO v_sequence_pk
    FROM user_constraints c JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
   WHERE c.table_name = 'TEAM_MIGRATION_HISTORY' AND c.constraint_type = 'P'
     AND cc.column_name = 'APPLIED_SEQUENCE';
  SELECT COUNT(*) INTO v_id_pk
    FROM user_constraints c JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name
   WHERE c.table_name = 'TEAM_MIGRATION_HISTORY' AND c.constraint_type = 'P'
     AND cc.column_name = 'ID';
  IF v_pk_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence)';
  ELSIF v_sequence_pk = 0 THEN
    IF v_pk_count <> 1 OR v_id_pk <> 1 THEN
      RAISE_APPLICATION_ERROR(-20024, 'MIGRATION_HISTORY_PRIMARY_KEY_INVALID');
    END IF;
    SELECT constraint_name INTO v_pk_name FROM user_constraints
     WHERE table_name = 'TEAM_MIGRATION_HISTORY' AND constraint_type = 'P';
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY DROP CONSTRAINT ' || v_pk_name;
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_HISTORY ADD CONSTRAINT team_migration_history_pk PRIMARY KEY (applied_sequence)';
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_indexes WHERE index_name = 'TEAM_MIGRATION_HISTORY_ID_IX';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'CREATE INDEX team_migration_history_id_ix ON TEAM_MIGRATION_HISTORY (id, applied_sequence)';
  END IF;

  -- Existing v1 attempts are backfilled before action is constrained.
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'ACTION';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT ADD (action VARCHAR2(16))';
  END IF;
  UPDATE TEAM_MIGRATION_ATTEMPT SET action = 'migrate' WHERE action IS NULL;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_ATTEMPT
   WHERE action IS NULL OR action NOT IN ('migrate', 'undo', 'redo');
  IF v_count <> 0 THEN
    RAISE_APPLICATION_ERROR(-20025, 'MIGRATION_ATTEMPT_ACTION_INVALID');
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'ACTION'
     AND data_type = 'VARCHAR2' AND data_length = 16 AND nullable = 'N';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT MODIFY (action VARCHAR2(16) NOT NULL)';
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_constraints
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND constraint_name = 'TEAM_MIGRATION_ATTEMPT_ACTION_CK';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT ADD CONSTRAINT team_migration_attempt_action_ck CHECK (action IN (''migrate'',''undo'',''redo''))';
  ELSE
    SELECT DBMS_LOB.SUBSTR(search_condition, 4000, 1) INTO v_condition
      FROM user_constraints
     WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND constraint_name = 'TEAM_MIGRATION_ATTEMPT_ACTION_CK';
    IF REGEXP_REPLACE(UPPER(v_condition), '[[:space:]]', '') <> 'ACTIONIN(''MIGRATE'',''UNDO'',''REDO'')' THEN
      RAISE_APPLICATION_ERROR(-20026, 'MIGRATION_ATTEMPT_ACTION_CONSTRAINT_INVALID');
    END IF;
  END IF;
  SELECT COUNT(*) INTO v_count FROM user_tab_columns
   WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'CONFIRMATION_DIGEST';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE TEAM_MIGRATION_ATTEMPT ADD (confirmation_digest VARCHAR2(64))';
  ELSE
    SELECT COUNT(*) INTO v_count FROM user_tab_columns
     WHERE table_name = 'TEAM_MIGRATION_ATTEMPT' AND column_name = 'CONFIRMATION_DIGEST'
       AND data_type = 'VARCHAR2' AND data_length = 64 AND nullable = 'Y';
    IF v_count = 0 THEN
      RAISE_APPLICATION_ERROR(-20027, 'MIGRATION_ATTEMPT_CONFIRMATION_COLUMN_INVALID');
    END IF;
  END IF;
  COMMIT;
END;
/

DECLARE
  v_count NUMBER;
  v_version NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_META
   WHERE project_id = __PROJECT_ID_LITERAL__;
  IF v_count > 1 THEN
    RAISE_APPLICATION_ERROR(-20028, 'MIGRATION_META_PROJECT_DUPLICATE');
  ELSIF v_count = 0 THEN
    INSERT INTO TEAM_MIGRATION_META (version_number, project_id, schema_set_digest)
    VALUES (2, __PROJECT_ID_LITERAL__, __SCHEMA_SET_DIGEST_LITERAL__);
  ELSE
    SELECT version_number INTO v_version FROM TEAM_MIGRATION_META
     WHERE project_id = __PROJECT_ID_LITERAL__;
    IF v_version NOT IN (1, 2) THEN
      RAISE_APPLICATION_ERROR(-20029, 'MIGRATION_META_VERSION_INVALID');
    END IF;
    IF v_version = 1 THEN
      UPDATE TEAM_MIGRATION_META SET version_number = 2,
          schema_set_digest = __SCHEMA_SET_DIGEST_LITERAL__
       WHERE project_id = __PROJECT_ID_LITERAL__ AND version_number = 1;
    END IF;
  END IF;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_META
   WHERE project_id = __PROJECT_ID_LITERAL__ AND version_number = 2;
  IF v_count <> 1 THEN
    RAISE_APPLICATION_ERROR(-20030, 'MIGRATION_META_VERSION_NOT_TWO');
  END IF;
END;
/
MERGE INTO TEAM_MIGRATION_MUTEX d
USING (SELECT 1 singleton_id FROM dual) s
   ON (d.singleton_id = s.singleton_id)
WHEN NOT MATCHED THEN INSERT (singleton_id, owner_token, worker_identity, host)
VALUES (1, NULL, NULL, NULL);
COMMIT;
