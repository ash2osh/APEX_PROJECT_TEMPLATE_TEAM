#!/usr/bin/env python3
"""Kernel-backed local writer leases for database release targets."""

from __future__ import annotations

import contextlib
import contextvars
import errno
import functools
import hashlib
import json
import os
import stat
import sys
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path


SCOPE_FIELDS = {"dbUniqueName", "containerId", "schema", "edition"}
STAGING_COORDINATION_SCOPE = {
    "dbUniqueName": "__STAGING_COORDINATION__",
    "containerId": "__CHECKOUT__",
    "schema": "__ALL_SCHEMAS__",
    "edition": "__ALL_EDITIONS__",
}


class ReleaseOwnershipError(ValueError):
    """A local target writer lease could not be acquired or verified."""


class TargetWriterBusy(ReleaseOwnershipError):
    """Another local process currently owns this target scope."""


def pending_rehearsal_reset(repo_root: Path) -> tuple[Path, str] | None:
    """Return an incomplete reset archive that staging writers must not cross."""
    root = _root(repo_root)
    base = root / "scratch" / "rehearsal-reset"
    try:
        metadata = base.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return base, f"cannot inspect rehearsal-reset archives: {error}"
    if base.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        return base, "rehearsal-reset archive root is unsafe"
    for marker in sorted(base.glob(".restore-*.json")):
        if marker.is_symlink():
            return marker, "restore intent marker is a symbolic link"
        try:
            document = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return marker, f"restore intent marker cannot be read: {error}"
        if not isinstance(document, Mapping) or document.get("state") != "complete":
            operation_id = document.get("operationId", "unknown") if isinstance(document, Mapping) else "unknown"
            return marker, f"staging restore operation {operation_id} is pending"
    for archive in sorted(base.iterdir()):
        if archive.name.startswith(".restore-"):
            continue
        if archive.is_symlink() or not archive.is_dir():
            continue
        plan = archive / "reset-plan.json"
        legacy = archive / "reset-record.json"
        candidate = plan if plan.exists() or plan.is_symlink() else legacy
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if candidate.is_symlink():
            return archive, "reset plan is a symbolic link"
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return archive, f"reset plan cannot be read: {error}"
        if not isinstance(document, Mapping):
            return archive, "reset plan is malformed"
        complete = document.get("state") == "complete" or document.get("complete") is True
        if not complete:
            reason = "legacy incomplete reset needs manual reconciliation" if candidate == legacy else "reset archive is incomplete"
            return archive, reason
    return None


def assert_no_pending_rehearsal_reset(repo_root: Path) -> None:
    pending = pending_rehearsal_reset(repo_root)
    if pending is not None:
        archive, reason = pending
        raise ReleaseOwnershipError(
            f"staging is blocked by pending rehearsal reset at {archive}: {reason}; "
            "resume that exact reset or reconcile its archive before another staging operation"
        )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReleaseOwnershipError("target writer lease metadata is not JSON-safe") from error


def _scope_record(scope: Mapping) -> dict[str, str]:
    if not isinstance(scope, Mapping) or set(scope) != SCOPE_FIELDS:
        raise ReleaseOwnershipError("target writer scope must identify database, container, schema, and edition")
    result = {}
    for field in sorted(SCOPE_FIELDS):
        value = scope.get(field)
        if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
            raise ReleaseOwnershipError(f"target writer scope {field} must be a nonempty string")
        result[field] = value
    return result


def scope_from_observed_identity(identity: Mapping[str, str]) -> dict[str, str]:
    """Project a validated database observation onto its physical writer scope."""
    return {
        "dbUniqueName": identity["db_unique_name"].upper(),
        "containerId": identity["container_id"],
        "schema": identity["current_schema"].upper(),
        "edition": identity["edition"].upper(),
    }


def _root(repo_root: Path) -> Path:
    requested = Path(repo_root)
    if requested.is_symlink():
        raise ReleaseOwnershipError("repository root cannot be a symlink")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise ReleaseOwnershipError(f"repository root is unavailable: {error}") from error
    if not root.is_dir():
        raise ReleaseOwnershipError("repository root must be a directory")
    return root


def target_writer_lock_path(repo_root: Path, scope: Mapping) -> Path:
    """Return the deterministic ignored lock path for one target scope."""
    root = _root(repo_root)
    clean_scope = _scope_record(scope)
    scope_digest = hashlib.sha256(_canonical_bytes(clean_scope)).hexdigest()[:32]
    return root / "releases" / f".target-writer-{scope_digest}.lock"


def _ensure_private_directory(root: Path) -> Path:
    directory = root / "releases"
    created = False
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        try:
            directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise ReleaseOwnershipError(f"cannot create release ownership directory: {error}") from error
        try:
            metadata = directory.lstat()
        except OSError as error:
            raise ReleaseOwnershipError(f"release ownership directory is unavailable: {error}") from error
    except OSError as error:
        raise ReleaseOwnershipError(f"release ownership directory is unavailable: {error}") from error
    if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ReleaseOwnershipError("release ownership directory must be an ordinary directory")
    if created and os.name != "nt":
        try:
            directory.chmod(0o700)
        except OSError as error:
            raise ReleaseOwnershipError(f"cannot secure release ownership directory: {error}") from error
    return directory


class TargetWriterLease:
    """An exclusive kernel lock held for the lifetime of a target write."""

    def __init__(self, repo_root: Path, target_scope: Mapping, execution_identity: Mapping):
        self.root = _root(repo_root)
        self.target_scope = _scope_record(target_scope)
        if not isinstance(execution_identity, Mapping):
            raise ReleaseOwnershipError("target writer execution identity must be an object")
        self.execution_identity = dict(execution_identity)
        _canonical_bytes(self.execution_identity)
        self.token = str(uuid.uuid4())
        self.path = target_writer_lock_path(self.root, self.target_scope)
        self._fd: int | None = None

    def holds(self, repo_root: Path, target_scope: Mapping) -> bool:
        """Check that a caller-provided lease is active for this checkout and target."""
        return self._fd is not None and self.root == _root(repo_root) and self.target_scope == _scope_record(target_scope)

    def acquire(self) -> TargetWriterLease:
        if self._fd is not None:
            raise ReleaseOwnershipError("target writer lease is already acquired")
        _ensure_private_directory(self.root)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EMLINK}:
                raise ReleaseOwnershipError("target writer lock cannot be a symlink") from error
            raise ReleaseOwnershipError(f"cannot open target writer lock: {error}") from error
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise ReleaseOwnershipError("target writer lock must be an ordinary file")
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            self._lock(fd)
            self._fd = fd
            payload = _canonical_bytes({
                "schemaVersion": 1,
                "token": self.token,
                "pid": os.getpid(),
                "targetScope": self.target_scope,
                "executionIdentity": self.execution_identity,
                "acquiredAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            }) + b"\n"
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while recording target writer lease")
                view = view[written:]
            os.fsync(fd)
            return self
        except OSError as error:
            self._close(fd, unlock=self._fd is not None)
            self._fd = None
            raise ReleaseOwnershipError(f"cannot record target writer lease: {error}") from error
        except BaseException:
            self._close(fd, unlock=self._fd is not None)
            self._fd = None
            raise

    @staticmethod
    def _lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EPERM}:
                    raise TargetWriterBusy("another local run owns this target scope") from error
                raise ReleaseOwnershipError(f"cannot acquire target writer lock: {error}") from error
            return
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise TargetWriterBusy("another local run owns this target scope") from error
            raise ReleaseOwnershipError(f"cannot acquire target writer lock: {error}") from error

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def _close(self, fd: int, *, unlock: bool) -> None:
        try:
            if unlock:
                self._unlock(fd)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        ownership_changed = False
        read_error: Exception | None = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 1024 * 1024)
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, Mapping) or record.get("token") != self.token:
                ownership_changed = True
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            read_error = error
        try:
            self._unlock(fd)
        except OSError as error:
            read_error = read_error or error
        finally:
            os.close(fd)
        if ownership_changed:
            raise ReleaseOwnershipError("target writer lease ownership metadata changed; leave it for reconciliation")
        if read_error is not None:
            raise ReleaseOwnershipError(f"target writer lease metadata or kernel lock could not be verified: {read_error}") from read_error

    def __enter__(self) -> TargetWriterLease:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


@contextlib.contextmanager
def target_writer_lease(
    repo_root: Path,
    target_scope: Mapping,
    execution_identity: Mapping,
) -> Iterator[TargetWriterLease]:
    lease = TargetWriterLease(repo_root, target_scope, execution_identity)
    with lease:
        yield lease


class StagingCoordinationLease(TargetWriterLease):
    """One checkout-local claim shared by staging rollouts, migrations, and reset."""

    def __init__(self, repo_root: Path, execution_identity: Mapping):
        super().__init__(repo_root, STAGING_COORDINATION_SCOPE, execution_identity)
        self.path = self.root / "releases" / ".staging-coordination.lock"

    def holds(self, repo_root: Path) -> bool:
        """Check that this process still holds the staging claim for this checkout."""
        return self._fd is not None and self.root == _root(repo_root)


_active_staging_rollout: contextvars.ContextVar[StagingCoordinationLease | None] = contextvars.ContextVar(
    "active_staging_rollout", default=None,
)


def coordinate_staging_rollout(default_root: Path):
    """Hold the checkout claim across all mutating rollout steps and pauses."""
    def decorate(operation):
        @functools.wraps(operation)
        def coordinated(manifest_path, environment, **options):
            if environment != "staging" or options.get("dry_run") or options.get("prepare_only"):
                return operation(manifest_path, environment, **options)
            root = Path(options.get("repo_root", default_root))
            try:
                with staging_coordination_lease(root, {"purpose": "staging-rollout"}) as lease:
                    token = _active_staging_rollout.set(lease)
                    try:
                        return operation(manifest_path, environment, **options)
                    finally:
                        _active_staging_rollout.reset(token)
            except ReleaseOwnershipError as error:
                print(f"rollout error: staging coordination refused: {error}", file=sys.stderr)
                return 2
        return coordinated
    return decorate


def nested_staging_migration(repo_root: Path, environment: str) -> dict[str, StagingCoordinationLease]:
    """Pass the active claim only to a migration invoked from this staging rollout."""
    lease = _active_staging_rollout.get()
    if environment == "staging" and lease is not None and lease.holds(repo_root):
        return {"staging_lease": lease}
    return {}


@contextlib.contextmanager
def staging_coordination_lease(
    repo_root: Path,
    execution_identity: Mapping,
    *,
    allow_pending_reset: bool = False,
) -> Iterator[StagingCoordinationLease]:
    """Serialize cooperating staging commands with reset and refuse interrupted archives."""
    lease = StagingCoordinationLease(repo_root, execution_identity)
    with lease:
        if not allow_pending_reset:
            assert_no_pending_rehearsal_reset(lease.root)
        yield lease
