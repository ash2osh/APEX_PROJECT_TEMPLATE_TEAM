"""Controller metadata and persistent migration mutex abstraction."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from .config import Target


class MigrationStoreError(RuntimeError):
    pass


class MigrationSetupRequired(MigrationStoreError):
    pass


class MigrationMutexHeld(MigrationStoreError):
    def __init__(self, message: str, *, owner_token: str | None = None):
        super().__init__(message)
        self.owner_token = owner_token


class MigrationStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if self.root.exists() and self.root.is_symlink():
            raise MigrationStoreError("migration store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "migration-store.json"
        self.lock_path = self.root / "migration-store.lock"
        if not self.path.exists():
            self._write({"version": 1, "meta": None, "mutex": None, "history": {}, "attempts": {}, "inventories": {}, "observations": []})

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationStoreError("migration metadata is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise MigrationStoreError("unsupported migration metadata version")
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
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                self.data = outer._read()
                return self.data

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    outer._write(self.data)
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
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
        with self._locked() as data:
            identity = {"state_key": self._key(store_target), "project_id": store_target.project, "schema_set_digest": schema_set_digest}
            if data.get("meta") is not None and data["meta"] != identity:
                raise MigrationStoreError("metadata store belongs to a different project/schema set")
            data["meta"] = identity
            if data.get("mutex") is None:
                data["mutex"] = {"owner_token": None, "worker_identity": None, "host": None, "acquired_at": None}

    def _require(self, data: dict[str, Any], target: Target) -> dict[str, Any]:
        mutex = data.get("mutex")
        meta = data.get("meta")
        if not isinstance(meta, dict) or meta.get("state_key") != self._key(target) or not isinstance(mutex, dict):
            raise MigrationSetupRequired("migration metadata is not bootstrapped for this target")
        return mutex

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

    def record_attempt_start(self, store_target: Target, attempt_id: str, migration_id: str, checksum: str, run_token: str) -> None:
        self._assert_nonproduction(store_target)
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("attempt start requires current migration mutex owner")
            data["attempts"][attempt_id] = {
                "attempt_id": attempt_id, "migration_id": migration_id,
                "checksum": checksum, "state": "RUNNING", "run_token": run_token,
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
        self._assert_nonproduction(store_target)
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if run_token is not None and mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("history write requires current migration mutex owner")
            sequence = len(data["history"]) + 1
            data["history"][migration_id] = {
                "status": "APPLIED", "checksum": checksum, "target": target,
                "dependencies": list(dependencies), "source_commit": source_commit,
                "sequence": sequence, "applied_at": "now", "applied_by": applied_by,
                "run_token": run_token, "attempt_id": attempt_id,
                "observation": dict(observation),
            }
            if attempt_id and attempt_id in data["attempts"]:
                data["attempts"][attempt_id].update(state="APPLIED", finished_at="now")
            data["observations"].append({"sequence": sequence, "migration_id": migration_id, "attempt_id": attempt_id, **dict(observation)})

    def read_history(self, store_target: Target) -> dict[str, Any]:
        with self._locked() as data:
            self._require(data, store_target)
            return json.loads(json.dumps(data["history"]))

    def read_state(self, store_target: Target) -> dict[str, Any]:
        with self._locked() as data:
            mutex = self._require(data, store_target)
            return {"mutex": dict(mutex), "attempts": json.loads(json.dumps(data["attempts"])), "observations": json.loads(json.dumps(data["observations"]))}

    def export_history(self, store_target: Target, out: str | Path) -> None:
        envelope = {
            "version": 1,
            "target_state_key": store_target.state_key,
            "history": self.read_history(store_target),
            "observations": self.read_state(store_target)["observations"],
        }
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")

    def recover(self, store_target: Target, run_token: str | None, evidence: str | Path) -> None:
        self._assert_nonproduction(store_target)
        if not Path(evidence).exists():
            raise MigrationStoreError("worker termination evidence is required")
        with self._locked() as data:
            mutex = self._require(data, store_target)
            if mutex.get("owner_token") is not None and mutex.get("owner_token") != run_token:
                raise MigrationMutexHeld("recovery token does not match migration mutex owner", owner_token=mutex.get("owner_token"))
            mutex.update(owner_token=None, worker_identity=None, host=None, acquired_at=None)
