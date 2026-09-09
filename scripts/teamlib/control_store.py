"""Shared app-target registry and mutex abstraction.

The production adapter executes the same state transitions through the
controller profile.  The JSON implementation here is deliberately strict and
durable so the public workflow can be tested without pretending that a local
process lock is a database mutex.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any
from collections.abc import Iterable

from .config import Target
from .sqlcl import SqlclError, run_sqlcl
from .sql_text import b64_sql as _b64_sql, row_lines as _row_lines, sql_literal as _sql_literal


class ControlStoreError(RuntimeError):
    """Base class for controller-store failures."""


class SetupRequired(ControlStoreError):
    pass


class MutexHeld(ControlStoreError):
    def __init__(self, message: str, *, owner_token: str | None = None, recovery_role: str | None = None):
        super().__init__(message)
        self.owner_token = owner_token
        self.recovery_role = recovery_role


class TargetUncertain(ControlStoreError):
    pass


class ControllerError(ControlStoreError):
    pass


@dataclass(frozen=True)
class SyncState:
    target_key: str
    owner_token: str | None
    checkout_uuid: str | None
    host: str | None
    acquired_by_user: str | None
    acquired_at: str | None
    generation: int
    is_uncertain: bool


@dataclass(frozen=True)
class RegistryEntry:
    target_key: str
    checkout_uuid: str
    host: str
    registered_by_user: str
    registered_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target_key(value: Target | str) -> str:
    return value.physical_key if isinstance(value, Target) else value


def _assert_nonproduction(target: Target) -> None:
    if target.environment == "production":
        raise ControlStoreError("production control-store writes are refused")


class ControlStore:
    """File-backed controller store used by tests and offline simulations."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        if self.root.exists() and self.root.is_symlink():
            raise ControlStoreError("control store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "control-store.json"
        self.lock_path = self.root / "control-store.lock"
        if not self.path.exists():
            self._write({"version": 1, "mutexes": {}, "registry": {}, "transfers": []})

    def _locked(self):
        class _Lock:
            def __init__(self, outer: ControlStore):
                self.outer = outer
                self.handle = None
                self.data: dict[str, Any] | None = None

            def __enter__(self):
                self.outer.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = self.outer.lock_path.open("a+")
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                elif msvcrt is not None:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                self.data = self.outer._read()
                return self.data

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None and self.data is not None:
                    self.outer._write(self.data)
                if self.handle is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                        elif msvcrt is not None:
                            self.handle.seek(0)
                            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                    finally:
                        self.handle.close()
                return False

        return _Lock(self)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlStoreError("control store is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ControlStoreError("unsupported control store version")
        data.setdefault("mutexes", {})
        data.setdefault("registry", {})
        data.setdefault("transfers", [])
        return data

    def _write(self, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True, indent=2) + "\n"
        fd, name = tempfile.mkstemp(prefix=".control-store-", dir=str(self.root))
        temp_path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise ControlStoreError("could not persist control store") from exc

    def setup_state(self, targets: Iterable[Target]) -> None:
        with self._locked() as data:
            for target in targets:
                _assert_nonproduction(target)
                key = _target_key(target)
                if not key:
                    raise ControlStoreError("target key is required")
                data["mutexes"].setdefault(
                    key,
                    {
                        "target_key": key,
                        "owner_token": None,
                        "checkout_uuid": None,
                        "host": None,
                        "acquired_by_user": None,
                        "acquired_at": None,
                        "generation": 1,
                        "is_uncertain": False,
                    },
                )

    def _require_mutex(self, data: dict[str, Any], key: str) -> dict[str, Any]:
        mutex = data["mutexes"].get(key)
        if not isinstance(mutex, dict):
            raise SetupRequired(f"target {key} has not been bootstrapped; run setup-state")
        return mutex

    def register_app(
        self,
        target: Target,
        checkout_uuid: str,
        host: str,
        registered_by_user: str,
        *,
        transfer_from: str | None = None,
        capture_recovery_id: str | None = None,
    ) -> RegistryEntry:
        _assert_nonproduction(target)
        if not checkout_uuid or not host or not registered_by_user:
            raise ControlStoreError("checkout UUID, host and user are required")
        key = target.physical_key
        with self._locked() as data:
            self._require_mutex(data, key)
            registry = data["registry"].setdefault(key, {})
            if target.ownership_mode == "single":
                other = next((value for uuid, value in registry.items() if uuid != checkout_uuid), None)
                if other is not None and transfer_from != other["checkout_uuid"]:
                    raise MutexHeld("single-owner target already has a registered checkout")
                if other is not None:
                    if not capture_recovery_id:
                        raise ControlStoreError("single-owner transfer requires a retained capture")
                    data["transfers"].append(
                        {
                            "transfer_id": os.urandom(8).hex(),
                            "target_key": key,
                            "old_checkout_uuid": transfer_from,
                            "new_checkout_uuid": checkout_uuid,
                            "actor": registered_by_user,
                            "capture_recovery_id": capture_recovery_id,
                            "transferred_at": _now(),
                        }
                    )
                    registry.pop(transfer_from, None)
            value = registry.setdefault(
                checkout_uuid,
                {
                    "target_key": key,
                    "checkout_uuid": checkout_uuid,
                    "host": host,
                    "registered_by_user": registered_by_user,
                    "registered_at": _now(),
                },
            )
            # Re-registration is idempotent and never touches mutex state.
            return RegistryEntry(**value)

    def list_registry(self, target: Target | str) -> list[RegistryEntry]:
        key = _target_key(target)
        with self._locked() as data:
            values = data["registry"].get(key, {})
            return [RegistryEntry(**value) for value in values.values()]

    def acquire_app(
        self,
        target_key: str,
        run_token: str,
        checkout_uuid: str,
        host: str,
        acquired_by_user: str,
        *,
        recovery_role: str | None = None,
    ) -> SyncState:
        if not all((target_key, run_token, checkout_uuid, host, acquired_by_user)):
            raise ControlStoreError("mutex acquisition fields are required")
        with self._locked() as data:
            mutex = self._require_mutex(data, target_key)
            if mutex.get("owner_token") is not None:
                raise MutexHeld(
                    f"app target is held by run token {mutex['owner_token']}; recovery owner: {recovery_role or 'configured recovery owners'}",
                    owner_token=mutex["owner_token"], recovery_role=recovery_role,
                )
            if mutex.get("is_uncertain"):
                raise TargetUncertain("app target is uncertain; recover-app-lock is required")
            mutex.update(
                owner_token=run_token,
                checkout_uuid=checkout_uuid,
                host=host,
                acquired_by_user=acquired_by_user,
                acquired_at=_now(),
            )
            return self._sync(mutex)

    def mark_payload_starting(self, target_key: str, run_token: str) -> SyncState:
        with self._locked() as data:
            mutex = self._require_mutex(data, target_key)
            if mutex.get("owner_token") != run_token:
                raise MutexHeld("payload-start transition requires the current owner token")
            mutex["is_uncertain"] = True
            return self._sync(mutex)

    def release_app(self, target_key: str, run_token: str, *, confirmed_success: bool = False) -> SyncState:
        with self._locked() as data:
            mutex = self._require_mutex(data, target_key)
            if mutex.get("owner_token") != run_token:
                raise MutexHeld("mutex release token does not match current owner")
            mutex.update(owner_token=None, checkout_uuid=None, host=None, acquired_by_user=None, acquired_at=None)
            if confirmed_success:
                mutex["is_uncertain"] = False
                mutex["generation"] = int(mutex.get("generation", 1)) + 1
            return self._sync(mutex)

    def recover_app_lock(
        self,
        target_key: str,
        *,
        evidence: str | Path,
        run_token: str | None,
    ) -> SyncState:
        evidence_path = Path(evidence)
        if not evidence_path.exists() or evidence_path.is_symlink():
            raise ControlStoreError("worker-termination and retained-capture evidence is required")
        with self._locked() as data:
            mutex = self._require_mutex(data, target_key)
            owner = mutex.get("owner_token")
            if owner is not None and run_token != owner:
                raise MutexHeld("recovery run token does not match the held target", owner_token=owner)
            if owner is None and not mutex.get("is_uncertain"):
                raise ControlStoreError("target is neither held nor uncertain")
            mutex.update(owner_token=None, checkout_uuid=None, host=None, acquired_by_user=None, acquired_at=None)
            mutex["is_uncertain"] = False
            mutex["generation"] = int(mutex.get("generation", 1)) + 1
            return self._sync(mutex)

    def read_app_sync_state(self, target: Target | str) -> SyncState:
        key = _target_key(target)
        with self._locked() as data:
            mutex = self._require_mutex(data, key)
            return self._sync(mutex)

    @staticmethod
    def _sync(mutex: dict[str, Any]) -> SyncState:
        return SyncState(
            target_key=mutex["target_key"],
            owner_token=mutex.get("owner_token"),
            checkout_uuid=mutex.get("checkout_uuid"),
            host=mutex.get("host"),
            acquired_by_user=mutex.get("acquired_by_user"),
            acquired_at=mutex.get("acquired_at"),
            generation=int(mutex.get("generation", 1)),
            is_uncertain=bool(mutex.get("is_uncertain", False)),
        )

    def validate_controller(self, metadata_target: Target, contract: dict[str, Any]) -> None:
        if not isinstance(contract, dict) or contract.get("version") != 1:
            raise ControllerError("controller contract version is invalid")
        controllers = contract.get("controllers")
        if not isinstance(controllers, dict):
            raise ControllerError("controller contract has no controllers")
        entry = controllers.get(metadata_target.instance_id)
        if not isinstance(entry, dict):
            raise ControllerError(f"no tracked metadata controller for instance {metadata_target.instance_id}")
        expected = {
            "metadata_schema": metadata_target.current_schema,
            "expected_user": metadata_target.session_user,
            "current_schema": metadata_target.current_schema,
        }
        for key, value in expected.items():
            if entry.get(key) != value:
                raise ControllerError(f"metadata controller mismatch for {key}")


_CONTROL_BOOTSTRAP_SQL = r"""
DECLARE
  PROCEDURE create_if_missing(p_sql CLOB) IS
  BEGIN
    EXECUTE IMMEDIATE p_sql;
  EXCEPTION
    WHEN OTHERS THEN
      IF SQLCODE != -955 THEN RAISE; END IF;
  END;
BEGIN
  create_if_missing(q'[CREATE TABLE TEAM_CONTROL_META (
    version_number NUMBER(10) NOT NULL,
    project_id VARCHAR2(128) NOT NULL,
    schema_set_digest VARCHAR2(64),
    installed_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT team_control_meta_pk PRIMARY KEY (version_number, project_id)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_APP_REGISTRY (
    target_key VARCHAR2(64) NOT NULL,
    checkout_uuid VARCHAR2(128) NOT NULL,
    host VARCHAR2(512) NOT NULL,
    registered_by_user VARCHAR2(256) NOT NULL,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT team_app_registry_pk PRIMARY KEY (target_key, checkout_uuid)
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_APP_MUTEX (
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
  )]');
  create_if_missing(q'[CREATE TABLE TEAM_APP_TRANSFER (
    transfer_id VARCHAR2(64) NOT NULL,
    target_key VARCHAR2(64) NOT NULL,
    old_checkout_uuid VARCHAR2(128) NOT NULL,
    new_checkout_uuid VARCHAR2(128) NOT NULL,
    actor VARCHAR2(256) NOT NULL,
    capture_recovery_id VARCHAR2(128) NOT NULL,
    transferred_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT team_app_transfer_pk PRIMARY KEY (transfer_id)
  )]');
  COMMIT;
END;
/
"""


class SqlControlStore:
    """Oracle-owned app registry and mutex implementation.

    ``ControlStore`` is retained as an offline/test backend.  This class is
    the online controller adapter and deliberately keeps all metadata SQL on
    the isolated METADATA target; application payload sessions never receive
    its credentials or tokens.
    """

    def __init__(self, metadata_target: Target, *, runner=run_sqlcl, work_root: str | Path | None = None):
        if metadata_target.alias is not None or metadata_target.app_id is not None:
            raise ControlStoreError("controller target must not be bound to an APEX application")
        self.target = metadata_target
        self.runner = runner
        self.work_root = Path(work_root) if work_root is not None else Path("scratch") / "metadata"

    @staticmethod
    def _assert_nonproduction(target: Target) -> None:
        if target.environment == "production":
            raise ControlStoreError("production control-store writes are refused")

    def _run(self, operation: str, payload: str):
        work = self.work_root / f"control-{uuid.uuid4().hex}"
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
                    detail += ": " + Path(match.group(1)).read_text(encoding="utf-8", errors="replace")[-4000:]
                except OSError:
                    pass
            raise ControlStoreError(detail) from exc

    def _check_target(self, target: Target) -> str:
        if target.instance_id != self.target.instance_id:
            raise ControlStoreError("application and metadata targets are different database instances")
        return target.physical_key

    def setup_state(self, targets: Iterable[Target]) -> None:
        target_list = list(targets)
        if any(target.environment == "production" for target in target_list):
            raise ControlStoreError("production control-store writes are refused")
        keys = [self._check_target(target) for target in target_list]
        seed = "\n".join(
            f"MERGE INTO TEAM_APP_MUTEX d USING (SELECT {_sql_literal(key)} target_key FROM dual) s "
            "ON (d.target_key = s.target_key) WHEN NOT MATCHED THEN "
            "INSERT (target_key, generation, is_uncertain) VALUES (s.target_key, 1, 0);"
            for key in sorted(set(keys))
        )
        payload = _CONTROL_BOOTSTRAP_SQL + f"""
MERGE INTO TEAM_CONTROL_META d
USING (SELECT 1 version_number, {_sql_literal(self.target.project)} project_id FROM dual) s
   ON (d.version_number = s.version_number AND d.project_id = s.project_id)
WHEN NOT MATCHED THEN INSERT (version_number, project_id) VALUES (1, s.project_id);
{seed}
COMMIT;
"""
        self._run("write", payload)

    def register_app(
        self,
        target: Target,
        checkout_uuid: str,
        host: str,
        registered_by_user: str,
        *,
        transfer_from: str | None = None,
        capture_recovery_id: str | None = None,
    ) -> RegistryEntry:
        self._assert_nonproduction(target)
        key = self._check_target(target)
        if not all((checkout_uuid, host, registered_by_user)):
            raise ControlStoreError("checkout UUID, host and user are required")
        if transfer_from is not None and (target.ownership_mode != "single" or not capture_recovery_id):
            raise ControlStoreError("single-owner transfer requires a retained capture")
        transfer_sql = ""
        if target.ownership_mode == "single":
            transfer_sql = f"""
DECLARE
  v_other VARCHAR2(128);
  v_count NUMBER;
BEGIN
  -- The physical mutex row serializes roster changes for this application;
  -- the check and the write therefore share one transaction and cannot race.
  BEGIN
    SELECT target_key INTO v_other FROM TEAM_APP_MUTEX
     WHERE target_key = {_sql_literal(key)} FOR UPDATE NOWAIT;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN RAISE_APPLICATION_ERROR(-20004, 'CONTROL_SETUP_REQUIRED');
  END;
  SELECT MIN(checkout_uuid) INTO v_other FROM TEAM_APP_REGISTRY
   WHERE target_key = {_sql_literal(key)}
     AND checkout_uuid <> {_sql_literal(checkout_uuid)}
     AND ({_sql_literal(transfer_from or '')} IS NULL OR checkout_uuid <> {_sql_literal(transfer_from or '')});
  IF v_other IS NOT NULL THEN
    RAISE_APPLICATION_ERROR(-20010, 'SINGLE_OWNER_HELD:' || v_other);
  END IF;
"""
            if transfer_from is not None:
                transfer_sql += f"""
  SELECT COUNT(*) INTO v_count FROM TEAM_APP_REGISTRY
   WHERE target_key = {_sql_literal(key)} AND checkout_uuid = {_sql_literal(transfer_from)};
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20011, 'TRANSFER_SOURCE_MISSING'); END IF;
  DELETE FROM TEAM_APP_REGISTRY
   WHERE target_key = {_sql_literal(key)} AND checkout_uuid = {_sql_literal(transfer_from)};
  INSERT INTO TEAM_APP_TRANSFER
    (transfer_id, target_key, old_checkout_uuid, new_checkout_uuid, actor, capture_recovery_id)
  VALUES ({_sql_literal(uuid.uuid4().hex)}, {_sql_literal(key)}, {_sql_literal(transfer_from)},
          {_sql_literal(checkout_uuid)}, {_sql_literal(registered_by_user)}, {_sql_literal(capture_recovery_id)});
"""
            transfer_sql += "END;\n/"
        payload = f"""
{transfer_sql}
MERGE INTO TEAM_APP_REGISTRY d
USING (SELECT {_sql_literal(key)} target_key, {_sql_literal(checkout_uuid)} checkout_uuid,
              {_sql_literal(host)} host, {_sql_literal(registered_by_user)} registered_by_user FROM dual) s
   ON (d.target_key = s.target_key AND d.checkout_uuid = s.checkout_uuid)
WHEN MATCHED THEN UPDATE SET d.host = s.host, d.registered_by_user = s.registered_by_user
WHEN NOT MATCHED THEN INSERT (target_key, checkout_uuid, host, registered_by_user)
VALUES (s.target_key, s.checkout_uuid, s.host, s.registered_by_user);
MERGE INTO TEAM_APP_MUTEX d USING (SELECT {_sql_literal(key)} target_key FROM dual) s
   ON (d.target_key = s.target_key)
WHEN NOT MATCHED THEN INSERT (target_key, generation, is_uncertain) VALUES (s.target_key, 1, 0);
COMMIT;
"""
        try:
            self._run("write", payload)
        except ControlStoreError as exc:
            text = str(exc)
            if "CONTROL_SETUP_REQUIRED" in text:
                raise SetupRequired(f"target {key} has not been bootstrapped; run setup-state") from exc
            if "SINGLE_OWNER_HELD:" in text or "ORA-00054" in text:
                raise MutexHeld("single-owner target already has a registered checkout") from exc
            if "TRANSFER_SOURCE_MISSING" in text:
                raise ControlStoreError("single-owner transfer source is not registered") from exc
            raise
        entries = self.list_registry(target)
        for entry in entries:
            if entry.checkout_uuid == checkout_uuid:
                return entry
        raise ControlStoreError("controller did not return the registered checkout")

    def list_registry(self, target: Target | str) -> list[RegistryEntry]:
        key = target if isinstance(target, str) else self._check_target(target)
        payload = "SELECT 'TEAM_REGISTRY|' || " + " || '|' || ".join(
            [_b64_sql("target_key"), _b64_sql("checkout_uuid"), _b64_sql("host"), _b64_sql("registered_by_user"), _b64_sql("TO_CHAR(registered_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM')")]
        ) + f" FROM TEAM_APP_REGISTRY WHERE target_key = {_sql_literal(key)} ORDER BY checkout_uuid;"
        result = self._run("read", payload)
        rows = _row_lines(getattr(result, "stdout", ""), "TEAM_REGISTRY|")
        return [RegistryEntry(*row) for row in rows if len(row) == 5]

    def acquire_app(self, target_key: str, run_token: str, checkout_uuid: str, host: str, acquired_by_user: str, *, recovery_role: str | None = None) -> SyncState:
        if not all((target_key, run_token, checkout_uuid, host, acquired_by_user)):
            raise ControlStoreError("mutex acquisition fields are required")
        payload = f"""
DECLARE v_owner VARCHAR2(128); v_uncertain NUMBER;
BEGIN
  BEGIN
    SELECT owner_token, is_uncertain INTO v_owner, v_uncertain FROM TEAM_APP_MUTEX
     WHERE target_key = {_sql_literal(target_key)} FOR UPDATE NOWAIT;
  EXCEPTION WHEN NO_DATA_FOUND THEN RAISE_APPLICATION_ERROR(-20004, 'CONTROL_SETUP_REQUIRED'); END;
  IF v_owner IS NOT NULL THEN RAISE_APPLICATION_ERROR(-20001, 'MUTEX_HELD:' || v_owner); END IF;
  IF v_uncertain = 1 THEN RAISE_APPLICATION_ERROR(-20005, 'TARGET_UNCERTAIN'); END IF;
  UPDATE TEAM_APP_MUTEX SET owner_token = {_sql_literal(run_token)}, checkout_uuid = {_sql_literal(checkout_uuid)},
      host = {_sql_literal(host)}, acquired_by_user = {_sql_literal(acquired_by_user)}, acquired_at = SYSTIMESTAMP
   WHERE target_key = {_sql_literal(target_key)};
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except ControlStoreError as exc:
            text = str(exc)
            if "CONTROL_SETUP_REQUIRED" in text:
                raise SetupRequired(f"target {target_key} has not been bootstrapped; run setup-state") from exc
            if "TARGET_UNCERTAIN" in text:
                raise TargetUncertain("app target is uncertain; recover-app-lock is required") from exc
            if "MUTEX_HELD:" in text or "ORA-00054" in text:
                owner = text.split("MUTEX_HELD:", 1)[1].split()[0] if "MUTEX_HELD:" in text else None
                raise MutexHeld(
                    f"app target is held by {owner or 'another worker'}; recovery owner: {recovery_role or 'configured recovery owners'}",
                    owner_token=owner, recovery_role=recovery_role,
                ) from exc
            raise

    def mark_payload_starting(self, target_key: str, run_token: str) -> SyncState:
        payload = f"""
DECLARE v_count NUMBER;
BEGIN
  UPDATE TEAM_APP_MUTEX SET is_uncertain = 1
   WHERE target_key = {_sql_literal(target_key)} AND owner_token = {_sql_literal(run_token)};
  v_count := SQL%ROWCOUNT;
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'MUTEX_TOKEN_MISMATCH'); END IF;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except ControlStoreError as exc:
            if "MUTEX_TOKEN_MISMATCH" in str(exc):
                raise MutexHeld("payload-start transition requires the current app mutex owner") from exc
            raise
        return self.read_app_sync_state(target_key)

    def _check_owner(self, target_key: str, run_token: str) -> None:
        state = self.read_app_sync_state(target_key)
        if state.owner_token != run_token:
            raise MutexHeld("operation requires the current app mutex owner")

    def release_app(self, target_key: str, run_token: str, *, confirmed_success: bool = False) -> SyncState:
        if confirmed_success:
            assignment = "is_uncertain = 0, generation = generation + 1"
        else:
            assignment = "is_uncertain = is_uncertain"
        payload = f"""
DECLARE v_count NUMBER;
BEGIN
  UPDATE TEAM_APP_MUTEX SET owner_token = NULL, checkout_uuid = NULL, host = NULL,
      acquired_by_user = NULL, acquired_at = NULL, {assignment}
   WHERE target_key = {_sql_literal(target_key)} AND owner_token = {_sql_literal(run_token)};
  v_count := SQL%ROWCOUNT;
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'MUTEX_TOKEN_MISMATCH'); END IF;
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except ControlStoreError as exc:
            if "MUTEX_TOKEN_MISMATCH" in str(exc):
                raise MutexHeld("mutex release token does not match current owner") from exc
            raise
        return self.read_app_sync_state(target_key)

    def recover_app_lock(self, target_key: str, *, evidence: str | Path, run_token: str | None) -> SyncState:
        evidence_path = Path(evidence)
        if evidence_path.is_symlink() or not evidence_path.is_file() or not evidence_path.read_text(encoding="utf-8", errors="ignore").strip():
            raise ControlStoreError("worker-termination and retained-capture evidence is required")
        predicate = "owner_token IS NOT NULL OR is_uncertain = 1" if run_token is None else f"owner_token = {_sql_literal(run_token)}"
        payload = f"""
DECLARE v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count FROM TEAM_APP_MUTEX WHERE target_key = {_sql_literal(target_key)} AND ({predicate});
  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20002, 'RECOVERY_TOKEN_MISMATCH'); END IF;
  UPDATE TEAM_APP_MUTEX SET owner_token = NULL, checkout_uuid = NULL, host = NULL,
      acquired_by_user = NULL, acquired_at = NULL, is_uncertain = 0, generation = generation + 1
   WHERE target_key = {_sql_literal(target_key)} AND ({predicate});
  COMMIT;
END;
/
"""
        try:
            self._run("write", payload)
        except ControlStoreError as exc:
            if "RECOVERY_TOKEN_MISMATCH" in str(exc):
                raise MutexHeld("recovery token does not match the held app target") from exc
            raise
        return self.read_app_sync_state(target_key)

    def read_app_sync_state(self, target: Target | str) -> SyncState:
        key = target if isinstance(target, str) else self._check_target(target)
        payload = "SELECT 'TEAM_MUTEX|' || " + " || '|' || ".join(
            [_b64_sql("target_key"), _b64_sql("owner_token"), _b64_sql("checkout_uuid"), _b64_sql("host"), _b64_sql("acquired_by_user"), _b64_sql("TO_CHAR(acquired_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM')"), _b64_sql("TO_CHAR(generation)"), _b64_sql("TO_CHAR(is_uncertain)")]
        ) + f" FROM TEAM_APP_MUTEX WHERE target_key = {_sql_literal(key)};"
        result = self._run("read", payload)
        rows = _row_lines(getattr(result, "stdout", ""), "TEAM_MUTEX|")
        if len(rows) != 1 or len(rows[0]) != 8:
            raise SetupRequired(f"target {key} has not been bootstrapped; run setup-state")
        row = rows[0]
        return SyncState(row[0], row[1] or None, row[2] or None, row[3] or None, row[4] or None, row[5] or None, int(row[6]), row[7] == "1")
