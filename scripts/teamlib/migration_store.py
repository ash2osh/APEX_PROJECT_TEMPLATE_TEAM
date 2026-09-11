"""Controller metadata and persistent migration mutex abstraction."""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any
from collections.abc import Iterable, Mapping

from .config import Target
from .fingerprints import InventoryError, inventory_from_manifest
from .sqlcl import SqlclError, run_sqlcl
from .sql_text import (
    SqlTextError,
    b64_sql as _b64_sql,
    clob_builder as _clob_builder,
    row_lines as _row_lines,
    sql_literal as _sql_literal,
)


class MigrationStoreError(RuntimeError):
    pass


class MigrationSetupRequired(MigrationStoreError):
    pass


class MigrationMutexHeld(MigrationStoreError):
    def __init__(self, message: str, *, owner_token: str | None = None):
        super().__init__(message)
        self.owner_token = owner_token


def _validate_schema_set_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MigrationStoreError(
            "migration metadata bootstrap requires a lowercase schema-set SHA-256"
        )
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inventory_manifest(value: Any) -> tuple[dict[str, Any], str]:
    """Validate and canonicalize a complete inventory before storing it."""
    if isinstance(value, Mapping):
        manifest = dict(value)
    else:
        as_dict = getattr(value, "as_dict", None)
        manifest = as_dict() if callable(as_dict) else None
        if not isinstance(manifest, Mapping):
            raise MigrationStoreError("inventory evidence must include a complete manifest")
        manifest = dict(manifest)
    try:
        inventory = inventory_from_manifest(manifest)
    except InventoryError as exc:
        raise MigrationStoreError(f"inventory evidence is invalid: {exc}") from exc
    return inventory.as_dict(), inventory.digest


_MIGRATION_BOOTSTRAP_SQL = r"""
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
"""


class SqlMigrationStore:
    """Oracle-owned migration metadata implementation.

    The JSON :class:`MigrationStore` remains the deterministic unit-test
    backend.  This adapter is the online implementation: every transition is
    executed on the isolated METADATA target through ``run_sqlcl`` and every
    acquisition uses an Oracle ``FOR UPDATE NOWAIT`` transition.
    """

    def __init__(
        self,
        metadata_target: Target,
        *,
        runner=run_sqlcl,
        work_root: str | Path | None = None,
    ):
        if metadata_target.alias is not None or metadata_target.app_id is not None:
            raise MigrationStoreError("migration metadata target must not be bound to an APEX application")
        self.target = metadata_target
        self.runner = runner
        self.work_root = Path(work_root) if work_root is not None else Path("scratch") / "metadata"

    @staticmethod
    def _key(target: Target) -> str:
        return target.state_key

    @staticmethod
    def _assert_nonproduction(target: Target) -> None:
        if target.environment == "production":
            raise MigrationStoreError("production migration metadata writes are refused")

    def _run(self, operation: str, payload: str):
        work = self.work_root / f"migration-{uuid.uuid4().hex}"
        work.mkdir(parents=True, exist_ok=False)
        driver = work / "metadata.sql"
        driver.write_text(payload, encoding="utf-8", newline="\n")
        try:
            return self.runner(self.target, operation, driver, work)
        except SqlclError as exc:
            detail = str(exc)
            match = re.search(r"see (.+)$", detail)
            if match:
                try:
                    log_text = Path(match.group(1)).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    log_text = ""
                if log_text:
                    detail = f"{detail}: {log_text[-4000:]}"
            raise MigrationStoreError(detail) from exc

    def bootstrap(self, store_target: Target, *, schema_set_digest: str = "") -> None:
        self._assert_nonproduction(store_target)
        if store_target.state_key != self.target.state_key:
            raise MigrationStoreError("metadata target does not match the configured SQL controller")
        schema_set_digest = _validate_schema_set_digest(schema_set_digest)
        existing = self._existing_meta(store_target)
        if existing is not None:
            version, recorded = existing
            if version not in {1, 2}:
                raise MigrationStoreError("migration metadata version is unsupported")
            if recorded and recorded != schema_set_digest:
                raise MigrationStoreError("metadata store belongs to a different project/schema set")
        payload = _MIGRATION_BOOTSTRAP_SQL + f"""
DECLARE
  v_count NUMBER;
  v_version NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_META
   WHERE project_id = {_sql_literal(store_target.project)};
  IF v_count > 1 THEN
    RAISE_APPLICATION_ERROR(-20028, 'MIGRATION_META_PROJECT_DUPLICATE');
  ELSIF v_count = 0 THEN
    INSERT INTO TEAM_MIGRATION_META (version_number, project_id, schema_set_digest)
    VALUES (2, {_sql_literal(store_target.project)}, {_sql_literal(schema_set_digest)});
  ELSE
    SELECT version_number INTO v_version FROM TEAM_MIGRATION_META
     WHERE project_id = {_sql_literal(store_target.project)};
    IF v_version NOT IN (1, 2) THEN
      RAISE_APPLICATION_ERROR(-20029, 'MIGRATION_META_VERSION_INVALID');
    END IF;
    IF v_version = 1 THEN
      UPDATE TEAM_MIGRATION_META SET version_number = 2,
          schema_set_digest = {_sql_literal(schema_set_digest)}
       WHERE project_id = {_sql_literal(store_target.project)} AND version_number = 1;
    END IF;
  END IF;
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_META
   WHERE project_id = {_sql_literal(store_target.project)} AND version_number = 2;
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
"""
        self._run("write", payload)

    def _existing_meta(self, store_target: Target) -> tuple[int, str] | None:
        exists = self._read_rows(
            store_target,
            "SELECT 'TEAM_META_TABLE|' || "
            + _b64_sql("TO_CHAR(COUNT(*))")
            + " FROM user_tables WHERE table_name = 'TEAM_MIGRATION_META';",
            "TEAM_META_TABLE|",
        )
        if exists == [["0"]]:
            return None
        if exists != [["1"]]:
            raise MigrationStoreError("migration metadata table probe is malformed")
        rows = self._read_rows(
            store_target,
            "SELECT 'TEAM_META_ID|' || "
            + _b64_sql("TO_CHAR(version_number)")
            + " || '|' || "
            + _b64_sql("schema_set_digest")
            + " FROM TEAM_MIGRATION_META WHERE project_id = "
            + _sql_literal(store_target.project)
            + ";",
            "TEAM_META_ID|",
        )
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2:
            raise MigrationStoreError(
                "migration metadata project identity is missing or duplicated"
            )
        try:
            version = int(rows[0][0])
        except (TypeError, ValueError) as exc:
            raise MigrationStoreError("migration metadata version is malformed") from exc
        recorded = rows[0][1]
        if not isinstance(recorded, str):
            raise MigrationStoreError("migration metadata schema-set digest is malformed")
        if recorded and not re.fullmatch(r"[0-9a-f]{64}", recorded):
            raise MigrationStoreError("migration metadata schema-set digest is malformed")
        return version, recorded

    def _require_sql(self, target: Target) -> str:
        if target.state_key != self.target.state_key:
            raise MigrationStoreError("metadata target does not match the configured SQL controller")
        return _sql_literal(target.project)

    def record_inventory(
        self,
        store_target: Target,
        inventory: Any,
        *,
        run_token: str,
    ) -> str:
        """Persist an immutable complete inventory manifest and return its digest."""
        self._assert_nonproduction(store_target)
        self._require_sql(store_target)
        if not run_token:
            raise MigrationStoreError("inventory writes require a mutex token")
        manifest, digest = _inventory_manifest(inventory)
        # read_inventories reads this CLOB back in 900-character chunks and
        # base64-encodes each one through UTL_RAW/UTL_ENCODE, whose SQL results
        # are RAW and capped at 2000 bytes. ASCII escaping keeps 900 characters
        # at 900 bytes, matching the other two CLOBs this module writes. The
        # digest is computed from the Inventory, not these bytes, so escaping
        # does not change identity.
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        payload = f"""
DECLARE
  v_owned NUMBER;
  v_existing NUMBER;
  v_compare INTEGER;
  v_schema_set VARCHAR2(64);
  v_meta_schema_set VARCHAR2(64);
  v_normalizer VARCHAR2(32);
  v_coverage VARCHAR2(32);
  v_manifest CLOB;
BEGIN
{_clob_builder('v_manifest', manifest_json)}
  SELECT COUNT(*) INTO v_owned FROM TEAM_MIGRATION_MUTEX
   WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
  IF v_owned = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'INVENTORY_WRITE_REFUSED'); END IF;
  SELECT schema_set_digest INTO v_meta_schema_set
    FROM TEAM_MIGRATION_META
   WHERE version_number = 2 AND project_id = {_sql_literal(store_target.project)};
  IF v_meta_schema_set <> {_sql_literal(str(manifest['schema_set_digest']))} THEN
    RAISE_APPLICATION_ERROR(-20011, 'SCHEMA_SET_DIGEST_MISMATCH');
  END IF;
  SELECT COUNT(*) INTO v_existing FROM TEAM_MIGRATION_INVENTORY
   WHERE inventory_digest = {_sql_literal(digest)};
  IF v_existing = 0 THEN
    INSERT INTO TEAM_MIGRATION_INVENTORY
      (inventory_digest, manifest_json, schema_set_digest, normalizer_version, coverage_version)
    VALUES ({_sql_literal(digest)}, v_manifest,
            {_sql_literal(str(manifest['schema_set_digest']))},
            {_sql_literal(str(manifest['normalizer_version']))},
            {_sql_literal(str(manifest['coverage_version']))});
  ELSE
    SELECT DBMS_LOB.COMPARE(manifest_json, v_manifest),
           schema_set_digest, normalizer_version, coverage_version
      INTO v_compare, v_schema_set, v_normalizer, v_coverage
      FROM TEAM_MIGRATION_INVENTORY
     WHERE inventory_digest = {_sql_literal(digest)};
    IF NVL(v_compare, -1) <> 0
       OR v_schema_set <> {_sql_literal(str(manifest['schema_set_digest']))}
       OR v_normalizer <> {_sql_literal(str(manifest['normalizer_version']))}
       OR v_coverage <> {_sql_literal(str(manifest['coverage_version']))}
    THEN
      RAISE_APPLICATION_ERROR(-20007, 'INVENTORY_IMMUTABILITY_VIOLATION');
    END IF;
  END IF;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except MigrationStoreError as exc:
            if "INVENTORY_WRITE_REFUSED" in str(exc):
                raise MigrationMutexHeld("inventory write requires current migration mutex owner") from exc
            raise
        return digest

    def ensure_observation(self, store_target: Target, digest: str, *, run_token: str) -> None:
        """Create the sequence-zero observed frontier exactly once."""
        self._assert_nonproduction(store_target)
        self._require_sql(store_target)
        if not run_token or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MigrationStoreError("initial observation requires a valid inventory digest and mutex token")
        evidence = hashlib.sha256(f"initial:{digest}".encode("ascii")).hexdigest()
        payload = f"""
DECLARE
  v_owned NUMBER;
  v_observations NUMBER;
  v_history NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_owned FROM TEAM_MIGRATION_MUTEX
   WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
  IF v_owned = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'OBSERVATION_WRITE_REFUSED'); END IF;
  SELECT COUNT(*) INTO v_observations FROM TEAM_MIGRATION_OBSERVATION;
  SELECT COUNT(*) INTO v_history FROM TEAM_MIGRATION_HISTORY;
  IF v_observations = 0 AND v_history <> 0 THEN
    RAISE_APPLICATION_ERROR(-20008, 'OBSERVATION_BASELINE_REQUIRED');
  ELSIF v_observations = 0 THEN
    INSERT INTO TEAM_MIGRATION_OBSERVATION
      (sequence_number, migration_id, attempt_id, predecessor_sequence,
       before_digest, after_digest, evidence_digest)
    VALUES (0, NULL, NULL, NULL, {_sql_literal(digest)}, {_sql_literal(digest)}, {_sql_literal(evidence)});
  END IF;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except MigrationStoreError as exc:
            if "OBSERVATION_WRITE_REFUSED" in str(exc):
                raise MigrationMutexHeld("observed frontier write requires current migration mutex owner") from exc
            if "OBSERVATION_BASELINE_REQUIRED" in str(exc):
                raise MigrationStoreError("migration history exists without an observed baseline; run adoption before applying") from exc
            raise

    def validate_frontier(self, store_target: Target, digest: str) -> None:
        """Refuse a payload when live structure diverges from accepted evidence."""
        self._assert_nonproduction(store_target)
        self._require_sql(store_target)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MigrationStoreError("observed frontier digest is malformed")
        rows = self._read_rows(
            store_target,
            "SELECT 'TEAM_FRONTIER|' || " + _b64_sql("after_digest") +
            " FROM TEAM_MIGRATION_OBSERVATION WHERE sequence_number = (SELECT MAX(sequence_number) FROM TEAM_MIGRATION_OBSERVATION);",
            "TEAM_FRONTIER|",
        )
        if len(rows) != 1 or len(rows[0]) != 1:
            raise MigrationSetupRequired("migration metadata has no observed inventory frontier")
        if rows[0][0] != digest:
            raise MigrationStoreError(
                f"live inventory {digest} does not match accepted observed frontier {rows[0][0]}"
            )

    def validate_observation_chain(self, store_target: Target) -> None:
        state = self.read_state(store_target)
        observations = state.get("observations", [])
        for index, observation in enumerate(observations):
            if observation.get("sequence") != index:
                raise MigrationStoreError("observed migration chain has a sequence gap")
            expected_predecessor = None if index == 0 else index - 1
            if observation.get("predecessor_sequence") != expected_predecessor:
                raise MigrationStoreError("observed migration chain has an invalid predecessor")
            before = observation.get("before", "")
            after = observation.get("after", "")
            if not re.fullmatch(r"[0-9a-f]{64}", before) or not re.fullmatch(r"[0-9a-f]{64}", after):
                raise MigrationStoreError("observed migration chain contains an invalid digest")
            if index == 0:
                if observation.get("migration_id") is not None or observation.get("attempt_id") is not None or before != after:
                    raise MigrationStoreError("observed migration chain has an invalid initial frontier")
            elif before != observations[index - 1].get("after"):
                raise MigrationStoreError("observed migration chain is not continuous")

    def acquire(self, store_target: Target, run_token: str, worker_identity: str, host: str) -> None:
        self._assert_nonproduction(store_target)
        if not all((run_token, worker_identity, host)):
            raise MigrationStoreError("migration mutex fields are required")
        self._require_sql(store_target)
        payload = f"""
DECLARE
  v_owner TEAM_MIGRATION_MUTEX.owner_token%TYPE;
BEGIN
  BEGIN
    SELECT owner_token INTO v_owner FROM TEAM_MIGRATION_MUTEX
     WHERE singleton_id = 1 FOR UPDATE NOWAIT;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RAISE_APPLICATION_ERROR(-20004, 'METADATA_SETUP_REQUIRED');
  END;
  IF v_owner IS NOT NULL THEN
    RAISE_APPLICATION_ERROR(-20001, 'MUTEX_HELD:' || v_owner);
  END IF;
  UPDATE TEAM_MIGRATION_MUTEX
     SET owner_token = {_sql_literal(run_token)},
         worker_identity = {_sql_literal(worker_identity)},
         host = {_sql_literal(host)}, acquired_at = SYSTIMESTAMP
   WHERE singleton_id = 1;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except MigrationStoreError as exc:
            text = str(exc)
            if "METADATA_SETUP_REQUIRED" in text or "ORA-20004" in text:
                raise MigrationSetupRequired("migration metadata is not bootstrapped for this target") from exc
            if "MUTEX_HELD:" in text or "ORA-20001" in text or "ORA-00054" in text:
                owner = text.split("MUTEX_HELD:", 1)[1].split()[0] if "MUTEX_HELD:" in text else None
                raise MigrationMutexHeld(
                    f"migration mutex held by {owner or 'another worker'}; use the target recovery owner",
                    owner_token=owner,
                ) from exc
            raise

    def release(self, store_target: Target, run_token: str) -> None:
        self._assert_nonproduction(store_target)
        self._require_sql(store_target)
        payload = f"""
DECLARE
  v_count NUMBER;
  v_unresolved NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_MUTEX
   WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'MUTEX_TOKEN_MISMATCH'); END IF;
  SELECT COUNT(*) INTO v_unresolved FROM TEAM_MIGRATION_ATTEMPT
   WHERE state IN ('RUNNING','UNKNOWN','FAILED');
  IF v_unresolved > 0 THEN RAISE_APPLICATION_ERROR(-20003, 'UNRESOLVED_ATTEMPT'); END IF;
  UPDATE TEAM_MIGRATION_MUTEX SET owner_token = NULL, worker_identity = NULL,
      host = NULL, acquired_at = NULL WHERE singleton_id = 1;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except MigrationStoreError as exc:
            if "UNRESOLVED_ATTEMPT" in str(exc):
                raise MigrationMutexHeld("unresolved migration attempt prevents release", owner_token=run_token) from exc
            if "MUTEX_TOKEN_MISMATCH" in str(exc):
                raise MigrationMutexHeld("migration mutex token does not match current owner") from exc
            raise

    def record_attempt_start(
        self,
        store_target: Target,
        attempt_id: str,
        migration_id: str,
        checksum: str,
        run_token: str,
        *,
        action: str = "migrate",
        confirmation_digest: str = "",
    ) -> None:
        self._assert_nonproduction(store_target)
        self._require_sql(store_target)
        if action not in {"migrate", "undo", "redo"}:
            raise MigrationStoreError(f"unsupported migration action: {action}")
        if confirmation_digest and not re.fullmatch(r"[0-9a-f]{64}", confirmation_digest):
            raise MigrationStoreError("confirmation digest must be a lowercase SHA-256")
        payload = f"""
INSERT INTO TEAM_MIGRATION_ATTEMPT
  (attempt_id, migration_id, checksum, action, state, run_token, worker_identity, started_at, confirmation_digest)
SELECT {_sql_literal(attempt_id)}, {_sql_literal(migration_id)}, {_sql_literal(checksum)},
       {_sql_literal(action)}, 'RUNNING', {_sql_literal(run_token)}, worker_identity, SYSTIMESTAMP,
       NULLIF({_sql_literal(confirmation_digest)}, '')
  FROM TEAM_MIGRATION_MUTEX
 WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
DECLARE v_count NUMBER; BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_ATTEMPT WHERE attempt_id = {_sql_literal(attempt_id)};
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'MUTEX_TOKEN_MISMATCH'); END IF;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except MigrationStoreError as exc:
            if "MUTEX_TOKEN_MISMATCH" in str(exc):
                raise MigrationMutexHeld("attempt start requires current migration mutex owner") from exc
            raise

    def record_attempt_state(self, store_target: Target, attempt_id: str, run_token: str, state: str, diagnostic_digest: str = "") -> None:
        self._assert_nonproduction(store_target)
        if state not in {"RUNNING", "APPLIED", "FAILED", "UNKNOWN", "RECOVERED"}:
            raise MigrationStoreError(f"unsupported migration attempt state: {state}")
        self._require_sql(store_target)
        finished = "NULL" if state == "RUNNING" else "SYSTIMESTAMP"
        payload = f"""
DECLARE v_owned NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_owned FROM TEAM_MIGRATION_MUTEX
   WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
  IF v_owned = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'ATTEMPT_UPDATE_REFUSED'); END IF;
END;
/
UPDATE TEAM_MIGRATION_ATTEMPT
   SET state = {_sql_literal(state)}, finished_at = {finished},
       diagnostic_digest = NULLIF({_sql_literal(diagnostic_digest)}, '')
 WHERE attempt_id = {_sql_literal(attempt_id)}
   AND run_token = {_sql_literal(run_token)}
   AND EXISTS (SELECT 1 FROM TEAM_MIGRATION_MUTEX WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)});
DECLARE v_count NUMBER; BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_ATTEMPT WHERE attempt_id = {_sql_literal(attempt_id)} AND state = {_sql_literal(state)};
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'ATTEMPT_UPDATE_REFUSED'); END IF;
  COMMIT;
END;
/
"""
        self._run("write", payload)

    def record_event(
        self,
        store_target: Target,
        migration_id: str,
        checksum: str,
        target: str,
        dependencies: Iterable[tuple[str, str]],
        source_commit: str,
        applied_by: str,
        observation: Mapping[str, Any],
        *,
        operation: str,
        run_token: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self._assert_nonproduction(store_target)
        self._require_sql(store_target)
        if target not in {"tables", "code"}:
            raise MigrationStoreError("migration target must be tables or code")
        if operation not in {"up", "down"}:
            raise MigrationStoreError(f"unsupported migration event operation: {operation}")
        if not run_token:
            raise MigrationStoreError("SQL migration event writes require a mutex token")
        dependency_json = json.dumps(list(dependencies), sort_keys=True, separators=(",", ":"))
        observation_json = json.dumps(dict(observation), sort_keys=True, separators=(",", ":"))
        before = str(observation.get("before", ""))
        after = str(observation.get("after", ""))
        evidence = str(observation.get("evidence", observation.get("evidence_digest", "")))
        if not re.fullmatch(r"[0-9a-f]{64}", before) or not re.fullmatch(r"[0-9a-f]{64}", after):
            raise MigrationStoreError("migration event requires complete before and after inventory digests")
        payload = f"""
DECLARE
  v_sequence NUMBER;
  v_owned NUMBER;
  v_previous NUMBER;
  v_frontier VARCHAR2(64);
  v_inventory_count NUMBER;
  v_attempt_count NUMBER;
  v_current_operation VARCHAR2(4);
  v_dependencies CLOB;
  v_observation CLOB;
BEGIN
{_clob_builder('v_dependencies', dependency_json)}
{_clob_builder('v_observation', observation_json)}
  SELECT COUNT(*) INTO v_owned FROM TEAM_MIGRATION_MUTEX
   WHERE singleton_id = 1 AND owner_token = {_sql_literal(run_token)};
  IF v_owned = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'HISTORY_WRITE_REFUSED'); END IF;
  SELECT COUNT(DISTINCT inventory_digest) INTO v_inventory_count
    FROM TEAM_MIGRATION_INVENTORY
   WHERE inventory_digest IN ({_sql_literal(before)}, {_sql_literal(after)});
  IF v_inventory_count < 2 AND {_sql_literal(before)} <> {_sql_literal(after)} THEN
    RAISE_APPLICATION_ERROR(-20009, 'OBSERVATION_MANIFEST_MISSING');
  ELSIF v_inventory_count = 0 THEN
    RAISE_APPLICATION_ERROR(-20009, 'OBSERVATION_MANIFEST_MISSING');
  END IF;
  SELECT MAX(sequence_number), MAX(after_digest) KEEP (DENSE_RANK LAST ORDER BY sequence_number)
    INTO v_previous, v_frontier FROM TEAM_MIGRATION_OBSERVATION;
  IF v_previous IS NULL THEN RAISE_APPLICATION_ERROR(-20008, 'OBSERVATION_BASELINE_REQUIRED'); END IF;
  IF v_frontier <> {_sql_literal(before)} THEN
    RAISE_APPLICATION_ERROR(-20010, 'OBSERVATION_FRONTIER_MISMATCH');
  END IF;
  SELECT MAX(operation) KEEP (DENSE_RANK LAST ORDER BY applied_sequence)
    INTO v_current_operation
    FROM TEAM_MIGRATION_HISTORY WHERE id = {_sql_literal(migration_id)};
  IF {_sql_literal(operation)} = 'down' AND NVL(v_current_operation, 'none') <> 'up' THEN
    RAISE_APPLICATION_ERROR(-20031, 'MIGRATION_NOT_APPLIED');
  ELSIF {_sql_literal(operation)} = 'up' AND v_current_operation = 'up' THEN
    RAISE_APPLICATION_ERROR(-20032, 'MIGRATION_ALREADY_APPLIED');
  END IF;
  SELECT NVL(MAX(applied_sequence), 0) + 1 INTO v_sequence FROM TEAM_MIGRATION_HISTORY;
  INSERT INTO TEAM_MIGRATION_HISTORY
    (id, operation, checksum, target, dependencies_json, payload_manifest_json, source_commit,
     applied_sequence, applied_at, applied_by, run_token, attempt_id)
  VALUES ({_sql_literal(migration_id)}, {_sql_literal(operation)}, {_sql_literal(checksum)}, {_sql_literal(target)},
          v_dependencies, v_observation,
          {_sql_literal(source_commit)}, v_sequence, SYSTIMESTAMP, {_sql_literal(applied_by)},
          {_sql_literal(run_token)}, {_sql_literal(attempt_id or '')});
  UPDATE TEAM_MIGRATION_ATTEMPT SET state = 'APPLIED', finished_at = SYSTIMESTAMP
   WHERE attempt_id = {_sql_literal(attempt_id or '')} AND run_token = {_sql_literal(run_token)};
  SELECT COUNT(*) INTO v_attempt_count FROM TEAM_MIGRATION_ATTEMPT
   WHERE attempt_id = {_sql_literal(attempt_id or '')} AND run_token = {_sql_literal(run_token)} AND state = 'APPLIED';
  IF v_attempt_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'ATTEMPT_UPDATE_REFUSED'); END IF;
  INSERT INTO TEAM_MIGRATION_OBSERVATION
    (sequence_number, migration_id, attempt_id, predecessor_sequence, before_digest, after_digest, evidence_digest)
  VALUES (v_sequence, {_sql_literal(migration_id)}, {_sql_literal(attempt_id or '')},
          v_sequence - 1,
          {_sql_literal(before)}, {_sql_literal(after)}, {_sql_literal(evidence)});
  COMMIT;
END;
/
"""
        self._run("write", payload)

    def record_applied(
        self,
        store_target: Target,
        migration_id: str,
        checksum: str,
        target: str,
        dependencies: Iterable[tuple[str, str]],
        source_commit: str,
        applied_by: str,
        observation: Mapping[str, Any],
        *,
        run_token: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self.record_event(
            store_target, migration_id, checksum, target, dependencies, source_commit,
            applied_by, observation, operation="up", run_token=run_token, attempt_id=attempt_id,
        )

    def _read_rows(self, target: Target, payload: str, prefix: str) -> list[list[str]]:
        self._require_sql(target)
        result = self._run("read", payload)
        try:
            return _row_lines(getattr(result, "stdout", ""), prefix)
        except SqlTextError as exc:
            raise MigrationStoreError(str(exc)) from exc

    def read_history(self, store_target: Target) -> dict[str, Any]:
        fields = [
            "id", "operation", "checksum", "target", "source_commit",
            "applied_sequence", "applied_at", "applied_by", "run_token", "attempt_id",
        ]
        expressions = [
            _b64_sql("id"), _b64_sql("operation"), _b64_sql("checksum"), _b64_sql("target"),
            _b64_sql("source_commit"),
            _b64_sql("TO_CHAR(applied_sequence)"),
            _b64_sql("TO_CHAR(applied_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM')"),
            _b64_sql("applied_by"),
            _b64_sql("run_token"), _b64_sql("attempt_id"),
        ]
        payload = "SELECT 'TEAM_HISTORY|' || " + " || '|' || ".join(expressions) + " FROM TEAM_MIGRATION_HISTORY ORDER BY applied_sequence;"
        rows = self._read_rows(store_target, payload, "TEAM_HISTORY|")
        chunk_selects: list[str] = []
        for column, label in (("dependencies_json", "dependencies"), ("payload_manifest_json", "observation")):
            # Generate chunk numbers once, independently of each history row.
            # A correlated CONNECT BY under CROSS APPLY can re-enter the
            # hierarchy for every outer row on Oracle and never converge.
            chunk_numbers = (
                "(SELECT LEVEL part FROM dual CONNECT BY LEVEL <= "
                f"NVL((SELECT MAX(CEIL(DBMS_LOB.GETLENGTH({column}) / 900)) "
                f"FROM TEAM_MIGRATION_HISTORY), 1))"
            )
            chunk_selects.append(
                "SELECT 'TEAM_HISTORY_CLOB|' || "
                + " || '|' || ".join(
                    [
                        _b64_sql("id"),
                        _b64_sql("TO_CHAR(applied_sequence)"),
                        _b64_sql(_sql_literal(label)),
                        _b64_sql("TO_CHAR(part)"),
                        _b64_sql("TO_CHAR(total)"),
                        _b64_sql("chunk"),
                    ]
                )
                + f" FROM (SELECT h.id, h.applied_sequence, c.part, GREATEST(1, CEIL(DBMS_LOB.GETLENGTH(h.{column}) / 900)) total, "
                  f"DBMS_LOB.SUBSTR(h.{column}, 900, (c.part - 1) * 900 + 1) chunk "
                  f"FROM TEAM_MIGRATION_HISTORY h CROSS JOIN {chunk_numbers} c "
                  f"WHERE c.part <= GREATEST(1, CEIL(DBMS_LOB.GETLENGTH(h.{column}) / 900)))"
            )
        clob_rows = self._read_rows(
            store_target,
            " UNION ALL ".join(chunk_selects),
            "TEAM_HISTORY_CLOB|",
        )
        clob_parts: dict[tuple[int, str], dict[int, str]] = {}
        clob_totals: dict[tuple[int, str], int] = {}
        for row in clob_rows:
            if len(row) != 6:
                raise MigrationStoreError("malformed migration history CLOB row")
            _, raw_sequence, field, raw_part, raw_total, chunk = row
            if field not in {"dependencies", "observation"}:
                raise MigrationStoreError("unknown migration history CLOB field")
            try:
                sequence = int(raw_sequence)
                part = int(raw_part)
                total = int(raw_total)
            except ValueError as exc:
                raise MigrationStoreError("migration history CLOB numbering is malformed") from exc
            key = (sequence, field)
            if part < 1 or total < part or part in clob_parts.setdefault(key, {}):
                raise MigrationStoreError("migration history CLOB chunks are inconsistent")
            if key in clob_totals and clob_totals[key] != total:
                raise MigrationStoreError("migration history CLOB totals are inconsistent")
            clob_totals[key] = total
            clob_parts[key][part] = chunk

        def clob_value(sequence: int, field: str) -> str:
            key = (sequence, field)
            parts = clob_parts.get(key)
            total = clob_totals.get(key)
            if not parts or total is None or set(parts) != set(range(1, total + 1)):
                raise MigrationStoreError(f"migration history CLOB is incomplete: {sequence}/{field}")
            return "".join(parts[index] for index in range(1, total + 1))

        events: list[dict[str, Any]] = []
        for row in rows:
            if len(row) != len(fields):
                raise MigrationStoreError("malformed migration history row")
            values = dict(zip(fields, row, strict=True))
            try:
                sequence = int(values["applied_sequence"])
            except ValueError as exc:
                raise MigrationStoreError("migration history sequence is malformed") from exc
            if values["operation"] not in {"up", "down"}:
                raise MigrationStoreError("migration history operation is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", values["checksum"]):
                raise MigrationStoreError("migration history checksum is invalid")
            if values["target"] not in {"tables", "code"}:
                raise MigrationStoreError("migration history target is invalid")
            try:
                dependencies = json.loads(clob_value(sequence, "dependencies") or "[]")
                observation = json.loads(clob_value(sequence, "observation") or "{}")
            except json.JSONDecodeError as exc:
                raise MigrationStoreError("migration history dependency JSON is malformed") from exc
            if not isinstance(dependencies, list) or not isinstance(observation, dict):
                raise MigrationStoreError("migration history JSON payload is malformed")
            events.append({
                "id": values["id"], "operation": values["operation"],
                "status": "APPLIED" if values["operation"] == "up" else "REVERTED",
                "checksum": values["checksum"], "target": values["target"],
                "dependencies": dependencies, "source_commit": values["source_commit"],
                "sequence": sequence, "applied_sequence": sequence, "applied_at": values["applied_at"],
                "applied_by": values["applied_by"],
                "run_token": values["run_token"] or None, "attempt_id": values["attempt_id"] or None,
                "observation": observation,
            })
        events.sort(key=lambda event: event["sequence"])
        _validate_history_events(events)
        return _collapse_history(events)

    def read_inventories(self, store_target: Target) -> dict[str, dict[str, Any]]:
        """Read and verify every immutable complete inventory manifest."""
        rows = self._read_rows(
            store_target,
            "SELECT 'TEAM_INVENTORY|' || " + " || '|' || ".join(
                [
                    _b64_sql("inventory_digest"), _b64_sql("schema_set_digest"),
                    _b64_sql("normalizer_version"), _b64_sql("coverage_version"),
                ]
            ) + " FROM TEAM_MIGRATION_INVENTORY ORDER BY inventory_digest;",
            "TEAM_INVENTORY|",
        )
        chunk_limit = (
            "(SELECT LEVEL part FROM dual CONNECT BY LEVEL <= "
            "NVL((SELECT MAX(CEIL(DBMS_LOB.GETLENGTH(manifest_json) / 900)) "
            "FROM TEAM_MIGRATION_INVENTORY), 1))"
        )
        chunks = self._read_rows(
            store_target,
            "SELECT 'TEAM_INVENTORY_CLOB|' || " + " || '|' || ".join(
                [
                    _b64_sql("inventory_digest"), _b64_sql("TO_CHAR(part)"),
                    _b64_sql("TO_CHAR(total)"), _b64_sql("chunk"),
                ]
            ) + f" FROM (SELECT i.inventory_digest, c.part, "
              f"GREATEST(1, CEIL(DBMS_LOB.GETLENGTH(i.manifest_json) / 900)) total, "
              f"DBMS_LOB.SUBSTR(i.manifest_json, 900, (c.part - 1) * 900 + 1) chunk "
              f"FROM TEAM_MIGRATION_INVENTORY i CROSS JOIN {chunk_limit} c "
              "WHERE c.part <= GREATEST(1, CEIL(DBMS_LOB.GETLENGTH(i.manifest_json) / 900)))",
            "TEAM_INVENTORY_CLOB|",
        )
        parts: dict[str, dict[int, str]] = {}
        totals: dict[str, int] = {}
        for row in chunks:
            if len(row) != 4:
                raise MigrationStoreError("malformed migration inventory CLOB row")
            digest, raw_part, raw_total, chunk = row
            try:
                part, total = int(raw_part), int(raw_total)
            except ValueError as exc:
                raise MigrationStoreError("migration inventory CLOB numbering is malformed") from exc
            if part < 1 or total < part or part in parts.setdefault(digest, {}):
                raise MigrationStoreError("migration inventory CLOB chunks are inconsistent")
            if digest in totals and totals[digest] != total:
                raise MigrationStoreError("migration inventory CLOB totals are inconsistent")
            totals[digest] = total
            parts[digest][part] = chunk

        inventories: dict[str, dict[str, Any]] = {}
        for row in rows:
            if len(row) != 4:
                raise MigrationStoreError("malformed migration inventory row")
            digest, schema_set, normalizer, coverage = row
            total = totals.get(digest)
            chunks_for_digest = parts.get(digest, {})
            if total is None or set(chunks_for_digest) != set(range(1, total + 1)):
                raise MigrationStoreError(f"migration inventory CLOB is incomplete: {digest}")
            try:
                manifest = json.loads("".join(chunks_for_digest[index] for index in range(1, total + 1)))
            except json.JSONDecodeError as exc:
                raise MigrationStoreError("migration inventory manifest JSON is malformed") from exc
            if not isinstance(manifest, Mapping):
                raise MigrationStoreError("migration inventory manifest is not an object")
            canonical, verified_digest = _inventory_manifest(manifest)
            if verified_digest != digest or canonical["schema_set_digest"] != schema_set or canonical["normalizer_version"] != normalizer or canonical["coverage_version"] != coverage:
                raise MigrationStoreError("migration inventory scalar fields do not match its manifest")
            inventories[digest] = canonical
        return inventories

    def read_state(self, store_target: Target) -> dict[str, Any]:
        mutex_rows = self._read_rows(
            store_target,
            "SELECT 'TEAM_MUTEX|' || " + " || '|' || ".join(
                [
                    _b64_sql("owner_token"), _b64_sql("worker_identity"), _b64_sql("host"),
                    _b64_sql("TO_CHAR(acquired_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM')"),
                ]
            ) + " FROM TEAM_MIGRATION_MUTEX WHERE singleton_id = 1;",
            "TEAM_MUTEX|",
        )
        if len(mutex_rows) != 1 or len(mutex_rows[0]) != 4:
            raise MigrationSetupRequired("migration metadata mutex is absent")
        attempts = self._read_rows(
            store_target,
            "SELECT 'TEAM_ATTEMPT|' || " + " || '|' || ".join(
                [
                    _b64_sql("attempt_id"), _b64_sql("migration_id"), _b64_sql("action"), _b64_sql("state"),
                    _b64_sql("run_token"), _b64_sql("worker_identity"),
                    _b64_sql("TO_CHAR(started_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM')"),
                    _b64_sql("TO_CHAR(finished_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM')"),
                    _b64_sql("diagnostic_digest"), _b64_sql("confirmation_digest"),
                ]
            ) + " FROM TEAM_MIGRATION_ATTEMPT ORDER BY started_at;",
            "TEAM_ATTEMPT|",
        )
        observations = self._read_rows(
            store_target,
            "SELECT 'TEAM_OBSERVATION|' || " + " || '|' || ".join(
                [
                    _b64_sql("TO_CHAR(sequence_number)"), _b64_sql("migration_id"),
                    _b64_sql("attempt_id"), _b64_sql("TO_CHAR(predecessor_sequence)"),
                    _b64_sql("before_digest"), _b64_sql("after_digest"), _b64_sql("evidence_digest"),
                ]
            ) + " FROM TEAM_MIGRATION_OBSERVATION ORDER BY sequence_number;",
            "TEAM_OBSERVATION|",
        )
        return {
            "mutex": {
                "owner_token": mutex_rows[0][0] or None, "worker_identity": mutex_rows[0][1] or None,
                "host": mutex_rows[0][2] or None, "acquired_at": mutex_rows[0][3] or None,
            },
            "attempts": {
                row[0]: {
                    "attempt_id": row[0], "migration_id": row[1], "action": row[2], "state": row[3],
                    "run_token": row[4], "worker_identity": row[5], "started_at": row[6],
                    "finished_at": row[7] or None, "diagnostic_digest": row[8] or "",
                    "confirmation_digest": row[9] or "",
                }
                for row in attempts if len(row) == 10
            },
            "observations": [
                {
                    "sequence": int(row[0]), "migration_id": row[1] or None,
                    "attempt_id": row[2] or None,
                    "predecessor_sequence": int(row[3]) if row[3] else None,
                    "before": row[4], "after": row[5], "evidence": row[6],
                }
                for row in observations if len(row) == 7
            ],
        }

    def export_history(self, store_target: Target, out: str | Path) -> None:
        envelope = {
            "version": 2,
            "target_state_key": store_target.state_key,
            "history": self.read_history(store_target),
            "observations": self.read_state(store_target)["observations"],
            "inventories": self.read_inventories(store_target),
        }
        destination = Path(out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def recover(
        self,
        store_target: Target,
        run_token: str | None,
        evidence: str | Path,
        *,
        attempt_id: str | None = None,
    ) -> None:
        self._assert_nonproduction(store_target)
        evidence_path = Path(evidence)
        if evidence_path.is_symlink() or not evidence_path.is_file() or not evidence_path.read_text(encoding="utf-8", errors="ignore").strip():
            raise MigrationStoreError("worker termination evidence is required")
        self._require_sql(store_target)
        predicate = "owner_token IS NOT NULL" if run_token is None else f"owner_token = {_sql_literal(run_token)}"
        if attempt_id is not None and run_token is None:
            raise MigrationStoreError("--attempt requires a run token")
        attempt_recovery = ""
        attempt_check = ""
        if attempt_id is not None:
            attempt_check = f"""
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_ATTEMPT
   WHERE attempt_id = {_sql_literal(attempt_id)} AND run_token = {_sql_literal(run_token or '')}
     AND state IN ('RUNNING','UNKNOWN','FAILED');
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'ATTEMPT_TOKEN_MISMATCH'); END IF;
"""
            attempt_recovery = (
                f"UPDATE TEAM_MIGRATION_ATTEMPT SET state = 'RECOVERED', finished_at = SYSTIMESTAMP "
                f"WHERE attempt_id = {_sql_literal(attempt_id)} AND run_token = {_sql_literal(run_token or '')};"
            )
        payload = f"""
DECLARE v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_MIGRATION_MUTEX WHERE singleton_id = 1 AND {predicate};
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'MUTEX_TOKEN_MISMATCH'); END IF;
  {attempt_check}
  {attempt_recovery}
  UPDATE TEAM_MIGRATION_MUTEX SET owner_token = NULL, worker_identity = NULL,
      host = NULL, acquired_at = NULL WHERE singleton_id = 1 AND {predicate};
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except MigrationStoreError as exc:
            if "MUTEX_TOKEN_MISMATCH" in str(exc) or "ATTEMPT_TOKEN_MISMATCH" in str(exc):
                raise MigrationMutexHeld("recovery token does not match migration mutex owner") from exc
            raise


def _history_sequence(event: Mapping[str, Any], label: str) -> int:
    value = event.get("applied_sequence", event.get("sequence"))
    if type(value) is not int or value < 1:
        raise MigrationStoreError(f"migration history sequence is malformed: {label}")
    return value


def _validate_history_events(events: Any) -> None:
    if not isinstance(events, list):
        raise MigrationStoreError("migration history must be an event list")
    last_sequence = 0
    current: dict[str, str] = {}
    for index, raw in enumerate(events):
        if not isinstance(raw, Mapping):
            raise MigrationStoreError(f"migration history event {index} is malformed")
        migration_id = raw.get("id", raw.get("migration_id"))
        if not isinstance(migration_id, str) or not migration_id:
            raise MigrationStoreError(f"migration history event {index} has no migration ID")
        operation = raw.get("operation")
        if operation not in {"up", "down"}:
            raise MigrationStoreError(f"migration history event has invalid operation: {migration_id}")
        sequence = _history_sequence(raw, migration_id)
        if sequence != last_sequence + 1:
            raise MigrationStoreError("migration history sequence is not contiguous")
        status = "APPLIED" if operation == "up" else "REVERTED"
        prior = current.get(migration_id, "")
        if operation == "down" and prior != "APPLIED":
            raise MigrationStoreError(f"migration history contains a down event before up: {migration_id}")
        if operation == "up" and prior == "APPLIED":
            raise MigrationStoreError(f"migration history contains duplicate applied event: {migration_id}")
        if raw.get("status", status) != status:
            raise MigrationStoreError(f"migration history event status disagrees with operation: {migration_id}")
        current[migration_id] = status
        last_sequence = sequence


def _collapse_history(events: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    _validate_history_events(events)
    collapsed: dict[str, dict[str, Any]] = {}
    for raw in events:
        event = dict(raw)
        migration_id = str(event.get("id", event.get("migration_id")))
        sequence = _history_sequence(event, migration_id)
        event["id"] = migration_id
        event["status"] = "APPLIED" if event.get("operation") == "up" else "REVERTED"
        event["sequence"] = sequence
        event["applied_sequence"] = sequence
        collapsed[migration_id] = event
    return collapsed


class MigrationStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if self.root.exists() and self.root.is_symlink():
            raise MigrationStoreError("migration store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "migration-store.json"
        self.lock_path = self.root / "migration-store.lock"
        if not self.path.exists():
            self._write({"version": 2, "meta": None, "mutex": None, "history": [], "attempts": {}, "inventories": {}, "observations": []})
        else:
            self._upgrade_v1_if_needed()

    @property
    def _backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".v1-backup")

    def _upgrade_v1_if_needed(self) -> None:
        try:
            raw = self.path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationStoreError("migration metadata is unreadable") from exc
        if not isinstance(value, dict):
            raise MigrationStoreError("migration metadata must contain an object")
        if value.get("version") == 2:
            if not isinstance(value.get("history"), list):
                raise MigrationStoreError("version-two migration history must be an event list")
            _validate_history_events(value["history"])
            return
        if value.get("version") != 1:
            raise MigrationStoreError("unsupported migration metadata version")
        history = value.get("history")
        attempts = value.get("attempts")
        if not isinstance(history, dict) or not isinstance(attempts, dict):
            raise MigrationStoreError("version-one migration metadata is incomplete")
        events: list[dict[str, Any]] = []
        for index, (migration_id, raw_entry) in enumerate(sorted(history.items()), start=1):
            if not isinstance(raw_entry, Mapping) or raw_entry.get("status") != "APPLIED":
                raise MigrationStoreError("version-one history contains a non-applied entry")
            event = dict(raw_entry)
            event.pop("status", None)
            sequence = event.get("applied_sequence", event.get("sequence", index))
            if type(sequence) is not int or sequence < 1:
                raise MigrationStoreError(f"version-one history sequence is malformed: {migration_id}")
            event.update({
                "id": migration_id,
                "operation": "up",
                "status": "APPLIED",
                "sequence": sequence,
                "applied_sequence": sequence,
            })
            events.append(event)
        events.sort(key=lambda item: item["applied_sequence"])
        _validate_history_events(events)
        upgraded: dict[str, Any] = {
            "version": 2,
            "meta": value.get("meta"),
            "mutex": value.get("mutex"),
            "history": events,
            "attempts": json.loads(json.dumps(attempts)),
            "inventories": json.loads(json.dumps(value.get("inventories", {}))),
            "observations": json.loads(json.dumps(value.get("observations", []))),
        }
        for attempt in upgraded["attempts"].values():
            if isinstance(attempt, dict):
                attempt.setdefault("action", "migrate")
                attempt.setdefault("confirmation_digest", "")
        backup = self._backup_path
        if backup.exists():
            try:
                if backup.read_bytes() != raw:
                    raise MigrationStoreError("version-one migration backup conflicts with source bytes")
            except OSError as exc:
                raise MigrationStoreError("version-one migration backup is unreadable") from exc
        else:
            try:
                with backup.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise MigrationStoreError("could not preserve version-one migration metadata") from exc
        self._write(upgraded)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationStoreError("migration metadata is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != 2:
            raise MigrationStoreError("unsupported migration metadata version")
        _validate_history_events(data.get("history"))
        return data

    def _write(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        fd, name = tempfile.mkstemp(prefix=".migration-store-", dir=str(self.root))
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise MigrationStoreError("could not persist migration metadata") from exc

    def _locked(self):
        outer = self

        class Lock:
            def __enter__(self):
                self.handle = outer.lock_path.open("a+")
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                elif msvcrt is not None:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                self.data = outer._read()
                return self.data

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    outer._write(self.data)
                try:
                    if fcntl is not None:
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        self.handle.seek(0)
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    self.handle.close()
                return False

        return Lock()

    @staticmethod
    def _key(target: Target) -> str:
        return target.state_key

    @staticmethod
    def _assert_nonproduction(target: Target) -> None:
        if target.environment == "production":
            raise MigrationStoreError("production migration metadata writes are refused")

    def bootstrap(self, store_target: Target, *, schema_set_digest: str = "") -> None:
        self._assert_nonproduction(store_target)
        schema_set_digest = _validate_schema_set_digest(schema_set_digest)
        with self._locked() as data:
            meta = data.get("meta")
            if meta is not None:
                if not isinstance(meta, Mapping):
                    raise MigrationStoreError("migration metadata identity is malformed")
                if (
                    meta.get("state_key") != self._key(store_target)
                    or meta.get("project_id") != store_target.project
                ):
                    raise MigrationStoreError(
                        "metadata store belongs to a different project/schema set"
                    )
                recorded = meta.get("schema_set_digest")
                if recorded == "":
                    if not self._backup_path.exists():
                        raise MigrationStoreError("metadata store has an unowned schema set")
                else:
                    if not isinstance(recorded, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", recorded
                    ):
                        raise MigrationStoreError("migration metadata identity is malformed")
                    if recorded != schema_set_digest:
                        raise MigrationStoreError(
                            "metadata store belongs to a different project/schema set"
                        )
            identity = {
                "state_key": self._key(store_target),
                "project_id": store_target.project,
                "schema_set_digest": schema_set_digest,
            }
            data["meta"] = identity
            if data.get("mutex") is None:
                data["mutex"] = {"owner_token": None, "worker_identity": None, "host": None, "acquired_at": None}

    def _require(self, data: dict[str, Any], target: Target) -> dict[str, Any]:
        mutex = data.get("mutex")
        meta = data.get("meta")
        if not isinstance(meta, dict) or meta.get("state_key") != self._key(target) or not isinstance(mutex, dict):
            raise MigrationSetupRequired("migration metadata is not bootstrapped for this target")
        return mutex

    def record_inventory(self, store_target: Target, inventory: Any, *, run_token: str) -> str:
        self._assert_nonproduction(store_target)
        manifest, digest = _inventory_manifest(inventory)
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("inventory write requires current migration mutex owner")
            meta = data.get("meta") or {}
            expected_schema_set = str(meta.get("schema_set_digest", ""))
            if expected_schema_set and manifest.get("schema_set_digest") != expected_schema_set:
                raise MigrationStoreError("inventory schema-set digest does not match metadata bootstrap")
            existing = data.setdefault("inventories", {}).get(digest)
            if existing is not None and existing != manifest:
                raise MigrationStoreError("inventory manifest is immutable")
            data.setdefault("inventories", {})[digest] = manifest
        return digest

    def ensure_observation(self, store_target: Target, digest: str, *, run_token: str) -> None:
        self._assert_nonproduction(store_target)
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("observed frontier write requires current migration mutex owner")
            observations = data.setdefault("observations", [])
            if not observations:
                if data.get("history"):
                    raise MigrationStoreError("migration history exists without an observed baseline; run adoption before applying")
                evidence = hashlib.sha256(f"initial:{digest}".encode("ascii")).hexdigest()
                observations.append({
                    "sequence": 0, "migration_id": None, "attempt_id": None,
                    "predecessor_sequence": None, "before": digest, "after": digest,
                    "evidence": evidence,
                })

    def validate_frontier(self, store_target: Target, digest: str) -> None:
        with self._locked() as data:
            self._require(data, store_target)
            observations = data.get("observations", [])
            if not observations:
                raise MigrationSetupRequired("migration metadata has no observed inventory frontier")
            if observations[-1].get("after") != digest:
                raise MigrationStoreError(
                    f"live inventory {digest} does not match accepted observed frontier {observations[-1].get('after')}"
                )

    def validate_observation_chain(self, store_target: Target) -> None:
        state = self.read_state(store_target)
        observations = state.get("observations", [])
        for index, observation in enumerate(observations):
            if observation.get("sequence") != index:
                raise MigrationStoreError("observed migration chain has a sequence gap")
            expected_predecessor = None if index == 0 else index - 1
            if observation.get("predecessor_sequence") != expected_predecessor:
                raise MigrationStoreError("observed migration chain has an invalid predecessor")
            before = observation.get("before", "")
            after = observation.get("after", "")
            if not re.fullmatch(r"[0-9a-f]{64}", before) or not re.fullmatch(r"[0-9a-f]{64}", after):
                raise MigrationStoreError("observed migration chain contains an invalid digest")
            if index == 0:
                if observation.get("migration_id") is not None or observation.get("attempt_id") is not None or before != after:
                    raise MigrationStoreError("observed migration chain has an invalid initial frontier")
            elif before != observations[index - 1].get("after"):
                raise MigrationStoreError("observed migration chain is not continuous")

    def acquire(self, store_target: Target, run_token: str, worker_identity: str, host: str) -> None:
        self._assert_nonproduction(store_target)
        if not all((run_token, worker_identity, host)):
            raise MigrationStoreError("migration mutex fields are required")
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") is not None:
                raise MigrationMutexHeld(f"migration mutex held by {mutex['owner_token']}; use the target recovery owner", owner_token=mutex["owner_token"])
            mutex.update(owner_token=run_token, worker_identity=worker_identity, host=host, acquired_at="now")

    def release(self, store_target: Target, run_token: str) -> None:
        self._assert_nonproduction(store_target)
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("migration mutex token does not match current owner", owner_token=mutex.get("owner_token"))
            unresolved = [attempt_id for attempt_id, attempt in data.get("attempts", {}).items() if attempt.get("state") in {"RUNNING", "UNKNOWN", "FAILED"}]
            if unresolved:
                raise MigrationMutexHeld("unresolved migration attempt prevents release")
            mutex.update(owner_token=None, worker_identity=None, host=None, acquired_at=None)

    def record_attempt_start(
        self,
        store_target: Target,
        attempt_id: str,
        migration_id: str,
        checksum: str,
        run_token: str,
        *,
        action: str = "migrate",
        confirmation_digest: str = "",
    ) -> None:
        self._assert_nonproduction(store_target)
        if action not in {"migrate", "undo", "redo"}:
            raise MigrationStoreError(f"unsupported migration action: {action}")
        if confirmation_digest and not re.fullmatch(r"[0-9a-f]{64}", confirmation_digest):
            raise MigrationStoreError("confirmation digest must be a lowercase SHA-256")
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("attempt start requires current migration mutex owner")
            if attempt_id in data["attempts"]:
                raise MigrationStoreError("migration attempt already exists")
            data["attempts"][attempt_id] = {
                "attempt_id": attempt_id, "migration_id": migration_id,
                "checksum": checksum, "action": action,
                "confirmation_digest": confirmation_digest,
                "state": "RUNNING", "run_token": run_token,
                "started_at": "now", "finished_at": None,
            }

    def record_attempt_state(self, store_target: Target, attempt_id: str, run_token: str, state: str, diagnostic_digest: str = "") -> None:
        self._assert_nonproduction(store_target)
        if state not in {"RUNNING", "APPLIED", "FAILED", "UNKNOWN", "RECOVERED"}:
            raise MigrationStoreError(f"unsupported migration attempt state: {state}")
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("attempt state update requires current migration mutex owner")
            attempt = data["attempts"].get(attempt_id)
            if not isinstance(attempt, dict):
                raise MigrationStoreError("migration attempt does not exist")
            attempt.update(state=state, finished_at="now" if state != "RUNNING" else None, diagnostic_digest=diagnostic_digest)

    def _record_event_locked(
        self,
        data: dict[str, Any],
        store_target: Target,
        migration_id: str,
        checksum: str,
        target: str,
        dependencies: Iterable[tuple[str, str]],
        source_commit: str,
        applied_by: str,
        observation: Mapping[str, Any],
        *,
        operation: str,
        run_token: str | None,
        attempt_id: str | None,
    ) -> None:
        if operation not in {"up", "down"}:
            raise MigrationStoreError(f"unsupported migration event operation: {operation}")
        before = str(observation.get("before", ""))
        after = str(observation.get("after", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", before) or not re.fullmatch(r"[0-9a-f]{64}", after):
            raise MigrationStoreError("migration event requires complete before and after inventory digests")
        mutex = self._require(data, store_target)
        if run_token is not None and mutex.get("owner_token") != run_token:
            raise MigrationMutexHeld("history write requires current migration mutex owner")
        if before not in data.get("inventories", {}) or after not in data.get("inventories", {}):
            raise MigrationStoreError("migration event references an unrecorded inventory manifest")
        observations = data.setdefault("observations", [])
        if not observations:
            raise MigrationSetupRequired("migration metadata has no observed inventory frontier")
        if observations[-1].get("after") != before:
            raise MigrationStoreError("live inventory does not match accepted observed frontier")
        _validate_history_events(data["history"])
        current = _collapse_history(data["history"]).get(migration_id)
        current_status = current.get("status") if isinstance(current, Mapping) else ""
        if operation == "down" and current_status != "APPLIED":
            raise MigrationStoreError(f"cannot record down event before an applied event: {migration_id}")
        if operation == "up" and current_status == "APPLIED":
            raise MigrationStoreError(f"migration is already currently APPLIED: {migration_id}")
        sequence = (data["history"][-1].get("applied_sequence", 0) if data["history"] else 0) + 1
        event = {
            "id": migration_id,
            "operation": operation,
            "status": "APPLIED" if operation == "up" else "REVERTED",
            "checksum": checksum,
            "target": target,
            "dependencies": list(dependencies),
            "source_commit": source_commit,
            "sequence": sequence,
            "applied_sequence": sequence,
            "applied_at": "now",
            "applied_by": applied_by,
            "run_token": run_token,
            "attempt_id": attempt_id,
            "observation": dict(observation),
        }
        data["history"].append(event)
        if attempt_id and attempt_id in data["attempts"]:
            data["attempts"][attempt_id].update(state="APPLIED", finished_at="now")
        observations.append({
            "sequence": len(observations),
            "migration_id": migration_id,
            "attempt_id": attempt_id,
            "predecessor_sequence": len(observations) - 1,
            **dict(observation),
        })

    def record_event(
        self,
        store_target: Target,
        migration_id: str,
        checksum: str,
        target: str,
        dependencies: Iterable[tuple[str, str]],
        source_commit: str,
        applied_by: str,
        observation: Mapping[str, Any],
        *,
        operation: str,
        run_token: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self._assert_nonproduction(store_target)
        with self._locked() as data:
            self._record_event_locked(
                data, store_target, migration_id, checksum, target, dependencies,
                source_commit, applied_by, observation,
                operation=operation, run_token=run_token, attempt_id=attempt_id,
            )

    def record_applied(
        self,
        store_target: Target,
        migration_id: str,
        checksum: str,
        target: str,
        dependencies: Iterable[tuple[str, str]],
        source_commit: str,
        applied_by: str,
        observation: Mapping[str, Any],
        *,
        run_token: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self.record_event(
            store_target, migration_id, checksum, target, dependencies, source_commit,
            applied_by, observation, operation="up", run_token=run_token, attempt_id=attempt_id,
        )

    def read_history(self, store_target: Target) -> dict[str, Any]:
        with self._locked() as data:
            self._require(data, store_target)
            return json.loads(json.dumps(_collapse_history(data["history"])))

    def read_state(self, store_target: Target) -> dict[str, Any]:
        with self._locked() as data:
            mutex = self._require(data, store_target)
            return {"mutex": dict(mutex), "attempts": json.loads(json.dumps(data["attempts"])), "observations": json.loads(json.dumps(data["observations"]))}

    def export_history(self, store_target: Target, out: str | Path) -> None:
        envelope = {
            "version": 2,
            "target_state_key": store_target.state_key,
            "history": self.read_history(store_target),
            "observations": self.read_state(store_target)["observations"],
            "inventories": self.read_inventories(store_target),
        }
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def read_inventories(self, store_target: Target) -> dict[str, dict[str, Any]]:
        with self._locked() as data:
            self._require(data, store_target)
            return json.loads(json.dumps(data.get("inventories", {})))

    def recover(
        self,
        store_target: Target,
        run_token: str | None,
        evidence: str | Path,
        *,
        attempt_id: str | None = None,
    ) -> None:
        self._assert_nonproduction(store_target)
        evidence_path = Path(evidence)
        if evidence_path.is_symlink() or not evidence_path.is_file() or not evidence_path.read_text(encoding="utf-8", errors="ignore").strip():
            raise MigrationStoreError("worker termination evidence is required")
        if attempt_id is not None and run_token is None:
            raise MigrationStoreError("--attempt requires a run token")
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") is not None and mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("recovery token does not match migration mutex owner", owner_token=mutex.get("owner_token"))
            if attempt_id is not None:
                attempt = data.get("attempts", {}).get(attempt_id)
                if not isinstance(attempt, dict) or attempt.get("run_token") != run_token or attempt.get("state") not in {"RUNNING", "UNKNOWN", "FAILED"}:
                    raise MigrationMutexHeld("recovery attempt does not match the selected run", owner_token=mutex.get("owner_token"))
                attempt.update(state="RECOVERED", finished_at="now")
            mutex.update(owner_token=None, worker_identity=None, host=None, acquired_at=None)
