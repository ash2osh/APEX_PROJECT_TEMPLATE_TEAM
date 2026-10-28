#!/usr/bin/env python3
"""Kernel-backed local writer leases for feature release targets."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path


SCOPE_FIELDS = {"dbUniqueName", "containerId", "schema", "edition"}


class ReleaseOwnershipError(ValueError):
    """A local target writer lease could not be acquired or verified."""


class TargetWriterBusy(ReleaseOwnershipError):
    """Another local process currently owns this target scope."""


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
        except Exception:
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
