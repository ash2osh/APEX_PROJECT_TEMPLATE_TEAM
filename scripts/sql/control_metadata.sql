-- Controller-owned metadata schema.  This file is executed only through the
-- verified METADATA write profile by setup-state; payload profiles must not
-- have privileges on these objects.
CREATE TABLE TEAM_CONTROL_META (
    version_number NUMBER(10) NOT NULL,
    project_id VARCHAR2(128) NOT NULL,
    schema_set_digest VARCHAR2(64),
    installed_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT team_control_meta_pk PRIMARY KEY (version_number, project_id)
);

CREATE TABLE TEAM_APP_REGISTRY (
    target_key VARCHAR2(64) NOT NULL,
    checkout_uuid VARCHAR2(128) NOT NULL,
    host VARCHAR2(512) NOT NULL,
    registered_by_user VARCHAR2(256) NOT NULL,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT team_app_registry_pk PRIMARY KEY (target_key, checkout_uuid)
);

CREATE TABLE TEAM_APP_MUTEX (
    target_key VARCHAR2(64) NOT NULL,
    owner_token VARCHAR2(128),
    checkout_uuid VARCHAR2(128),
    host VARCHAR2(512),
    acquired_by_user VARCHAR2(256),
    acquired_at TIMESTAMP WITH TIME ZONE,
    generation NUMBER(19) DEFAULT 1 NOT NULL,
    is_uncertain NUMBER(1) DEFAULT 0 NOT NULL,
    CONSTRAINT team_app_mutex_pk PRIMARY KEY (target_key),
    CONSTRAINT team_app_mutex_uncertain_ck CHECK (is_uncertain IN (0, 1)),
    CONSTRAINT team_app_mutex_generation_ck CHECK (generation > 0)
);

CREATE TABLE TEAM_APP_TRANSFER (
    transfer_id VARCHAR2(64) NOT NULL,
    target_key VARCHAR2(64) NOT NULL,
    old_checkout_uuid VARCHAR2(128) NOT NULL,
    new_checkout_uuid VARCHAR2(128) NOT NULL,
    actor VARCHAR2(256) NOT NULL,
    capture_recovery_id VARCHAR2(128) NOT NULL,
    transferred_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT team_app_transfer_pk PRIMARY KEY (transfer_id)
);
