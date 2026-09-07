-- Controller-owned migration metadata.  Execute only through METADATA.
CREATE TABLE TEAM_MIGRATION_META (
    version_number NUMBER(10) NOT NULL,
    project_id VARCHAR2(128) NOT NULL,
    schema_set_digest VARCHAR2(64) NOT NULL,
    CONSTRAINT team_migration_meta_pk PRIMARY KEY (version_number, project_id)
);

CREATE TABLE TEAM_MIGRATION_MUTEX (
    singleton_id NUMBER(1) NOT NULL,
    owner_token VARCHAR2(128),
    worker_identity VARCHAR2(256),
    host VARCHAR2(512),
    acquired_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT team_migration_mutex_pk PRIMARY KEY (singleton_id),
    CONSTRAINT team_migration_mutex_singleton_ck CHECK (singleton_id = 1)
);

CREATE TABLE TEAM_MIGRATION_HISTORY (
    id VARCHAR2(128) NOT NULL,
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
    CONSTRAINT team_migration_history_pk PRIMARY KEY (id),
    CONSTRAINT team_migration_history_target_ck CHECK (target IN ('tables', 'code'))
);

CREATE TABLE TEAM_MIGRATION_ATTEMPT (
    attempt_id VARCHAR2(128) NOT NULL,
    migration_id VARCHAR2(128) NOT NULL,
    checksum VARCHAR2(64) NOT NULL,
    state VARCHAR2(16) NOT NULL,
    run_token VARCHAR2(64) NOT NULL,
    worker_identity VARCHAR2(256) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    diagnostic_digest VARCHAR2(64),
    CONSTRAINT team_migration_attempt_pk PRIMARY KEY (attempt_id),
    CONSTRAINT team_migration_attempt_state_ck CHECK (state IN ('RUNNING','APPLIED','FAILED','UNKNOWN','RECOVERED'))
);

CREATE TABLE TEAM_MIGRATION_INVENTORY (
    inventory_digest VARCHAR2(64) NOT NULL,
    manifest_json CLOB NOT NULL,
    schema_set_digest VARCHAR2(64) NOT NULL,
    normalizer_version VARCHAR2(32) NOT NULL,
    coverage_version VARCHAR2(32) NOT NULL,
    CONSTRAINT team_migration_inventory_pk PRIMARY KEY (inventory_digest)
);

CREATE TABLE TEAM_MIGRATION_OBSERVATION (
    sequence_number NUMBER(19) NOT NULL,
    migration_id VARCHAR2(128),
    attempt_id VARCHAR2(128),
    predecessor_sequence NUMBER(19),
    before_digest VARCHAR2(64) NOT NULL,
    after_digest VARCHAR2(64) NOT NULL,
    evidence_digest VARCHAR2(64) NOT NULL,
    CONSTRAINT team_migration_observation_pk PRIMARY KEY (sequence_number)
);
