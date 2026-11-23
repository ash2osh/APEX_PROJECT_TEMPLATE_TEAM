"""Shared, cross-checkout generation records for staging release proofs.

The configured registry lives outside a project checkout. It records the current
restore generation for each physical database/container pair. A proof is useful
for promotion only while every generation it captured is still current.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import release_ownership


REGISTRY_ENV = "APEX_RELEASE_PROOF_REGISTRY"
REGISTRY_SCHEMA_VERSION = 1
TARGET_FIELDS = {"dbUniqueName", "containerId", "status", "generationId", "operationId", "updatedAt"}
GENERATION_FIELDS = {"dbUniqueName", "containerId", "generationId"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.ASCII)


class ReleaseProofGenerationError(ValueError):
    """The trusted current-generation registry is missing, stale, or unsafe."""


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise ReleaseProofGenerationError(f"{label} must be a canonical UUID")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as error:
        raise ReleaseProofGenerationError(f"{label} must be a canonical UUID") from error
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ReleaseProofGenerationError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseProofGenerationError(f"{label} must be a UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ReleaseProofGenerationError(f"{label} must be a UTC timestamp")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _object_no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseProofGenerationError(f"registry contains duplicate key {key!r}")
        result[key] = value
    return result


def registry_path(value: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the required absolute shared registry path; never guess a checkout-local default."""
    env = os.environ if environ is None else environ
    supplied = value if value is not None else env.get(REGISTRY_ENV)
    if isinstance(supplied, Path):
        raw = str(supplied)
    elif isinstance(supplied, str):
        raw = supplied.strip()
    else:
        raw = ""
    if not raw:
        raise ReleaseProofGenerationError(
            f"{REGISTRY_ENV} must name the trusted shared proof-generation file"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ReleaseProofGenerationError(f"{REGISTRY_ENV} must be an absolute path")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise ReleaseProofGenerationError(f"shared proof-generation directory is unavailable: {error}") from error
    if not parent.is_dir():
        raise ReleaseProofGenerationError("shared proof-generation parent must be a directory")
    resolved = parent / path.name
    try:
        metadata = resolved.lstat()
    except FileNotFoundError:
        return resolved
    except OSError as error:
        raise ReleaseProofGenerationError(f"shared proof-generation file is unavailable: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ReleaseProofGenerationError("shared proof-generation path must be an ordinary file, not a link")
    return resolved


def _identity_key(identity: Mapping[str, object]) -> tuple[str, str]:
    if not isinstance(identity, Mapping):
        raise ReleaseProofGenerationError("observed staging identity is missing")
    db_name = identity.get("db_unique_name", identity.get("dbUniqueName"))
    container_id = identity.get("container_id", identity.get("containerId"))
    if not isinstance(db_name, str) or not db_name.strip() or db_name != db_name.strip():
        raise ReleaseProofGenerationError("observed staging db_unique_name is missing")
    if not isinstance(container_id, str) or not container_id.strip() or container_id != container_id.strip():
        raise ReleaseProofGenerationError("observed staging container_id is missing")
    return db_name.upper(), container_id


def _scope_key(scope: Mapping[str, object]) -> tuple[str, str]:
    if not isinstance(scope, Mapping):
        raise ReleaseProofGenerationError("stage report target scope is malformed")
    db_name = scope.get("dbUniqueName")
    container_id = scope.get("containerId")
    if not isinstance(db_name, str) or not db_name.strip() or db_name != db_name.strip():
        raise ReleaseProofGenerationError("stage report target dbUniqueName is missing")
    if not isinstance(container_id, str) or not container_id.strip() or container_id != container_id.strip():
        raise ReleaseProofGenerationError("stage report target containerId is missing")
    return db_name.upper(), container_id


def _target_order(row: Mapping[str, object]) -> tuple[str, str]:
    return str(row["dbUniqueName"]), str(row["containerId"])


def _validate_target(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != TARGET_FIELDS:
        raise ReleaseProofGenerationError("shared proof-generation target record is malformed")
    db_name, container_id = row.get("dbUniqueName"), row.get("containerId")
    if not isinstance(db_name, str) or not db_name or db_name != db_name.strip() or db_name != db_name.upper():
        raise ReleaseProofGenerationError("shared proof-generation dbUniqueName is invalid")
    if not isinstance(container_id, str) or not container_id or container_id != container_id.strip():
        raise ReleaseProofGenerationError("shared proof-generation containerId is invalid")
    status = row.get("status")
    if status not in {"ready", "restoring"}:
        raise ReleaseProofGenerationError("shared proof-generation status is invalid")
    generation = row.get("generationId")
    if status == "ready":
        _uuid(generation, "shared proof-generation generationId")
    elif generation is not None:
        raise ReleaseProofGenerationError("a restoring target cannot have a current generation")
    operation = _uuid(row.get("operationId"), "shared proof-generation operationId")
    updated = _timestamp(row.get("updatedAt"), "shared proof-generation updatedAt")
    return {
        "dbUniqueName": db_name, "containerId": container_id, "status": status,
        "generationId": generation, "operationId": operation, "updatedAt": updated,
    }


def _load(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise ReleaseProofGenerationError(
            "shared proof-generation registry is missing; activate the restored staging baseline first"
        ) from error
    except OSError as error:
        raise ReleaseProofGenerationError(f"cannot read shared proof-generation registry: {error}") from error
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseProofGenerationError(f"shared proof-generation registry is invalid JSON: {error}") from error
    if not isinstance(document, Mapping) or set(document) != {"schemaVersion", "targets"}:
        raise ReleaseProofGenerationError("shared proof-generation registry has unsupported fields")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != REGISTRY_SCHEMA_VERSION:
        raise ReleaseProofGenerationError("shared proof-generation registry schemaVersion is unsupported")
    if not isinstance(document["targets"], list):
        raise ReleaseProofGenerationError("shared proof-generation targets must be an array")
    targets = [_validate_target(row) for row in document["targets"]]
    keys = [_target_order(row) for row in targets]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ReleaseProofGenerationError("shared proof-generation targets must be uniquely sorted")
    return {"schemaVersion": REGISTRY_SCHEMA_VERSION, "targets": targets}


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Use an OS file lock so a process crash releases it without stale-lock guessing."""
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    created = False
    try:
        try:
            fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o660)
            created = True
        except FileExistsError:
            fd = os.open(lock_path, flags)
    except OSError as error:
        raise ReleaseProofGenerationError(f"cannot open shared proof-generation lock: {error}") from error
    acquired = False
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ReleaseProofGenerationError("shared proof-generation lock must be an ordinary file")
        if os.name != "nt" and created and stat.S_IMODE(metadata.st_mode) != 0o660:
            os.fchmod(fd, 0o660)
        try:
            release_ownership.TargetWriterLease._lock(fd)
        except release_ownership.ReleaseOwnershipError as error:
            raise ReleaseProofGenerationError(f"cannot lock shared proof-generation registry: {error}") from error
        acquired = True
        yield
    except OSError as error:
        raise ReleaseProofGenerationError(f"cannot update shared proof-generation registry: {error}") from error
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                release_ownership.TargetWriterLease._unlock(fd)
        os.close(fd)


def _write(path: Path, document: Mapping[str, object]) -> None:
    directory = path.parent
    try:
        prior = path.lstat()
    except FileNotFoundError:
        prior = None
    except OSError as error:
        raise ReleaseProofGenerationError(f"cannot inspect shared proof-generation file: {error}") from error
    if prior is not None and (stat.S_ISLNK(prior.st_mode) or not stat.S_ISREG(prior.st_mode)):
        raise ReleaseProofGenerationError("shared proof-generation path must be an ordinary file, not a link")
    fd = -1
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
        if os.name != "nt":
            os.fchmod(fd, stat.S_IMODE(prior.st_mode) if prior is not None else 0o660)
            if prior is not None and os.fstat(fd).st_gid != prior.st_gid:
                try:
                    os.fchown(fd, -1, prior.st_gid)
                except OSError as error:
                    raise ReleaseProofGenerationError(
                        "cannot preserve shared proof-generation group ownership; provision the shared directory "
                        "with the operator group, setgid, and a default ACL"
                    ) from error
                if os.fstat(fd).st_gid != prior.st_gid:
                    raise ReleaseProofGenerationError("shared proof-generation group ownership changed unexpectedly")
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write((json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as error:
        raise ReleaseProofGenerationError(f"cannot atomically update shared proof-generation registry: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _read_for_update(path: Path) -> dict:
    if not path.exists():
        return {"schemaVersion": REGISTRY_SCHEMA_VERSION, "targets": []}
    return _load(path)


def _update_target(path: Path, key: tuple[str, str], update) -> dict:
    path = registry_path(path)
    with _locked(path):
        document = _read_for_update(path)
        targets = list(document["targets"])
        index = next((position for position, row in enumerate(targets) if _target_order(row) == key), None)
        current = targets[index] if index is not None else None
        changed = update(current)
        if changed == current:
            return changed
        if changed is current:
            return current
        if index is None:
            targets.append(changed)
        else:
            targets[index] = changed
        targets.sort(key=_target_order)
        result = {"schemaVersion": REGISTRY_SCHEMA_VERSION, "targets": targets}
        _write(path, result)
        return changed


def initialize_baseline(path: str | Path | None, identity: Mapping[str, object], *, operation_id: str | None = None) -> dict:
    """Activate the first generation for an already verified staging baseline, without replacing one."""
    resolved = registry_path(path)
    key = _identity_key(identity)
    operation = _uuid(operation_id or str(uuid.uuid4()), "baseline operationId")

    def update(current):
        if current is not None:
            raise ReleaseProofGenerationError(
                "this staging target already has a proof-generation record; initialization never overwrites it"
            )
        return {
            "dbUniqueName": key[0], "containerId": key[1], "status": "ready",
            "generationId": str(uuid.uuid4()), "operationId": operation, "updatedAt": _now(),
        }

    return _update_target(resolved, key, update)


def begin_restore(path: str | Path | None, identity: Mapping[str, object], operation_id: str) -> dict:
    """Invalidate old proofs before a staging restore starts."""
    resolved = registry_path(path)
    key = _identity_key(identity)
    operation = _uuid(operation_id, "restore operationId")

    def update(current):
        if current is not None:
            if current["status"] == "restoring":
                if current["operationId"] == operation:
                    return current
                raise ReleaseProofGenerationError("another restore is already in progress for this DB/container")
            if current["operationId"] == operation:
                raise ReleaseProofGenerationError("restore operation was already completed; use a new operation ID")
        return {
            "dbUniqueName": key[0], "containerId": key[1], "status": "restoring",
            "generationId": None, "operationId": operation, "updatedAt": _now(),
        }

    return _update_target(resolved, key, update)


def complete_restore(path: str | Path | None, identity: Mapping[str, object], operation_id: str) -> dict:
    """Publish a fresh proof generation after the restore and its local archive complete."""
    resolved = registry_path(path)
    key = _identity_key(identity)
    operation = _uuid(operation_id, "restore operationId")

    def update(current):
        if current is None or current["operationId"] != operation:
            raise ReleaseProofGenerationError("no matching pending restore exists for this DB/container and operation ID")
        if current["status"] == "ready":
            return current
        return {
            "dbUniqueName": key[0], "containerId": key[1], "status": "ready",
            "generationId": str(uuid.uuid4()), "operationId": operation, "updatedAt": _now(),
        }

    return _update_target(resolved, key, update)


def _generation_rows(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ReleaseProofGenerationError("stage report restoreGenerations must be an array")
    rows = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != GENERATION_FIELDS:
            raise ReleaseProofGenerationError("stage report restore generation is malformed")
        db_name, container_id = row.get("dbUniqueName"), row.get("containerId")
        if not isinstance(db_name, str) or not db_name or db_name != db_name.strip() or db_name != db_name.upper():
            raise ReleaseProofGenerationError("stage report restore generation dbUniqueName is invalid")
        if not isinstance(container_id, str) or not container_id or container_id != container_id.strip():
            raise ReleaseProofGenerationError("stage report restore generation containerId is invalid")
        generation = _uuid(row.get("generationId"), "stage report generationId")
        rows.append({"dbUniqueName": db_name, "containerId": container_id, "generationId": generation})
    keys = [(row["dbUniqueName"], row["containerId"]) for row in rows]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ReleaseProofGenerationError("stage report restore generations must be uniquely sorted")
    return rows


def capture_generations(path: str | Path | None, target_scopes: Sequence[Mapping[str, object]]) -> list[dict]:
    """Capture ready generation identifiers for every unique physical target in a staging rollout."""
    if not isinstance(target_scopes, Sequence) or isinstance(target_scopes, (str, bytes)) or not target_scopes:
        raise ReleaseProofGenerationError("staging rollout has no target scopes for restore-generation proof")
    keys = sorted({_scope_key(scope) for scope in target_scopes})
    resolved = registry_path(path)
    document = _load(resolved)
    records = {_target_order(row): row for row in document["targets"]}
    result = []
    for key in keys:
        current = records.get(key)
        if current is None:
            raise ReleaseProofGenerationError(
                f"no trusted restore generation is registered for staging target {key[0]}/{key[1]}; "
                "initialize the verified baseline or complete rehearsal-reset"
            )
        if current["status"] != "ready":
            raise ReleaseProofGenerationError(
                f"staging target {key[0]}/{key[1]} is marked restoring; finish rehearsal-reset before proving it"
            )
        result.append({
            "dbUniqueName": current["dbUniqueName"],
            "containerId": current["containerId"],
            "generationId": current["generationId"],
        })
    return result


def assert_current_generations(
    path: str | Path | None,
    captured: object,
    target_scopes: Sequence[Mapping[str, object]] | None = None,
) -> list[dict]:
    """Refuse missing, restoring, wrong-target, or superseded generations."""
    rows = _generation_rows(captured)
    scopes = target_scopes if target_scopes is not None else rows
    if target_scopes is None:
        # Generation rows are also physical target identities, which is sufficient to re-read current values.
        scopes = [{"dbUniqueName": row["dbUniqueName"], "containerId": row["containerId"]} for row in rows]
    current = capture_generations(path, scopes)
    if rows != current:
        raise ReleaseProofGenerationError(
            "staging restore generation changed after proof capture; the report is historical and cannot promote"
        )
    return current
