#!/usr/bin/env python3
"""Install a validated migration directory without replacing a destination."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path


class DirectoryInstallError(ValueError):
    """Raised when a staged directory cannot be installed safely."""


_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x00000004
_AT_FDCWD = -100
_RENAME_UNAVAILABLE_ERRNOS = {errno.EINVAL, errno.ENOSYS}
for _error_name in ("ENOTSUP", "EOPNOTSUPP"):
    _error_number = getattr(errno, _error_name, None)
    if _error_number is not None:
        _RENAME_UNAVAILABLE_ERRNOS.add(_error_number)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & reparse_flag)


def _is_symlink_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata)


def _validated_path(path: Path, *, allow_missing_leaf: bool, label: str) -> Path:
    """Return an absolute path after rejecting link/reparse components."""
    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise DirectoryInstallError(f"{label} path is invalid") from exc
    if absolute == Path(absolute.anchor):
        raise DirectoryInstallError(f"{label} cannot be a filesystem root")

    current = Path(absolute.anchor)
    components = absolute.parts[1:]
    for index, component in enumerate(components):
        current = current / component
        is_leaf = index == len(components) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            if is_leaf and allow_missing_leaf:
                return absolute
            raise DirectoryInstallError(f"{label} path component is missing: {current}") from exc
        except OSError as exc:
            raise DirectoryInstallError(f"{label} path cannot be inspected: {current}: {exc}") from exc

        if _is_symlink_or_reparse(metadata):
            raise DirectoryInstallError(f"{label} path contains a symbolic link or reparse point: {current}")
        if not is_leaf and not stat.S_ISDIR(metadata.st_mode):
            raise DirectoryInstallError(f"{label} parent is not a directory: {current}")
        if is_leaf:
            if allow_missing_leaf:
                raise DirectoryInstallError(f"destination already exists: {absolute}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise DirectoryInstallError(f"{label} is not a directory: {absolute}")

    return absolute


def _filesystem_id(path: Path) -> int:
    return path.stat().st_dev


def _rename_linux_noreplace(stage: Path, destination: Path) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE), with no emulated fallback."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise DirectoryInstallError("Linux renameat2(RENAME_NOREPLACE) is unavailable") from exc

    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(stage),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), os.fspath(destination))


def _rename_darwin_noreplace(stage: Path, destination: Path) -> None:
    """Use Darwin renamex_np(RENAME_EXCL), with no emulated fallback."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = libc.renamex_np
    except (AttributeError, OSError) as exc:
        raise DirectoryInstallError("Darwin renamex_np(RENAME_EXCL) is unavailable") from exc

    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = renamex_np(
        os.fsencode(stage),
        os.fsencode(destination),
        _DARWIN_RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), os.fspath(destination))


def _rename_windows_noreplace(stage: Path, destination: Path) -> None:
    """Use Python's Windows os.rename, which refuses an existing destination."""
    os.rename(stage, destination)


def install_directory_exclusive(stage: Path, destination: Path) -> None:
    """Atomically rename ``stage`` to an absent ``destination`` on one filesystem.

    Linux uses ``renameat2(RENAME_NOREPLACE)``, Darwin uses
    ``renamex_np(RENAME_EXCL)``, and Windows uses ``os.rename``. Unsupported
    platforms or filesystems refuse rather than emulate exclusivity with a
    racy check. The destination parent must already exist. Neither the stage
    nor the final path may contain a symbolic link or Windows reparse point.
    """
    stage_path = _validated_path(Path(stage), allow_missing_leaf=False, label="stage")
    destination_path = _validated_path(Path(destination), allow_missing_leaf=True, label="destination")
    if destination_path == stage_path or destination_path.is_relative_to(stage_path):
        raise DirectoryInstallError("destination must not be the stage directory or a child of it")

    try:
        stage_device = _filesystem_id(stage_path)
        destination_device = _filesystem_id(destination_path.parent)
    except OSError as exc:
        raise DirectoryInstallError(f"cannot verify stage and destination filesystem: {exc}") from exc
    if stage_device != destination_device:
        raise DirectoryInstallError("stage and destination parent must be on the same filesystem")

    try:
        if sys.platform == "linux":
            _rename_linux_noreplace(stage_path, destination_path)
        elif sys.platform == "darwin":
            _rename_darwin_noreplace(stage_path, destination_path)
        elif sys.platform == "win32":
            _rename_windows_noreplace(stage_path, destination_path)
        else:
            raise DirectoryInstallError(
                f"exclusive directory rename is unavailable on platform {sys.platform}"
            )
    except DirectoryInstallError:
        raise
    except OSError as exc:
        if exc.errno in _RENAME_UNAVAILABLE_ERRNOS:
            raise DirectoryInstallError(
                "exclusive directory rename primitive is unavailable on this filesystem"
            ) from exc
        if exc.errno == errno.EXDEV:
            raise DirectoryInstallError("stage and destination parent must be on the same filesystem") from exc
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise DirectoryInstallError(f"destination became occupied during installation: {destination_path}") from exc
        raise DirectoryInstallError(f"exclusive directory installation failed: {exc}") from exc
