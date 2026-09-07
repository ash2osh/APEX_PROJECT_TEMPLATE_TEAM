"""Shared app-target registry and mutex abstraction.

The production adapter executes the same state transitions through the
controller profile.  The JSON implementation here is deliberately strict and
durable so the public workflow can be tested without pretending that a local
process lock is a database mutex.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .config import Target


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
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                self.data = self.outer._read()
                return self.data

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None and self.data is not None:
                    self.outer._write(self.data)
                if self.handle is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
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
