#!/usr/bin/env python3
"""Install a validated migration directory without replacing a destination."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class DirectoryInstallError(ValueError):
    """Raised when a staged directory cannot be installed safely."""


_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x00000004
_RENAME_UNAVAILABLE_ERRNOS = {errno.EINVAL, errno.ENOSYS}
for _error_name in ("ENOTSUP", "EOPNOTSUPP"):
    _error_number = getattr(errno, _error_name, None)
    if _error_number is not None:
        _RENAME_UNAVAILABLE_ERRNOS.add(_error_number)

_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_FILE_LIST_DIRECTORY = 0x00000001
_WINDOWS_FILE_TRAVERSE = 0x00000020
_WINDOWS_FILE_ADD_SUBDIRECTORY = 0x00000004
_WINDOWS_FILE_DELETE = 0x00010000
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_FILE_SHARE_ALL = 0x00000007
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_DIRECTORY_FILE = 0x00000001
_WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WINDOWS_FILE_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_OPEN = 1
_WINDOWS_OBJ_CASE_INSENSITIVE = 0x00000040
_WINDOWS_OBJ_DONT_REPARSE = 0x00001000
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_RENAME_INFORMATION_CLASS = 10
_WINDOWS_ERROR_FILE_EXISTS = 80
_WINDOWS_ERROR_ALREADY_EXISTS = 183
_WINDOWS_ERROR_NOT_SAME_DEVICE = 17
_WINDOWS_ERROR_UNSUPPORTED = {1, 50, 87, 120}
_WINDOWS_STATUS_REPARSE_POINT_ENCOUNTERED = ctypes.c_int32(0xC000050B).value


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _is_symlink_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(getattr(metadata, "st_mode", 0)) or _is_reparse_point(metadata)


@dataclass
class _PinnedEntry:
    path: Path
    parent_handles: list[object]
    leaf_name: str
    leaf_handle: object | None
    device_id: int
    windows: bool = False

    @property
    def parent_handle(self) -> object:
        return self.parent_handles[-1]


def _absolute_path(path: Path, *, label: str) -> Path:
    """Normalize a path lexically; filesystem checks use pinned handles below."""
    try:
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or "\x00" in raw_path:
            raise ValueError("path is not a valid string path")
        absolute = Path(os.path.abspath(raw_path))
    except (OSError, TypeError, ValueError) as exc:
        raise DirectoryInstallError(f"{label} path is invalid") from exc
    if absolute == Path(absolute.anchor):
        raise DirectoryInstallError(f"{label} cannot be a filesystem root")
    if not absolute.name or absolute.name in {".", ".."}:
        raise DirectoryInstallError(f"{label} path has no safe final component")
    return absolute


def _filesystem_id(handle: object) -> int:
    """Return the volume identifier from an already-open directory handle or path."""
    if isinstance(handle, (str, Path)):
        return int(Path(handle).stat().st_dev)
    if os.name == "nt":
        return int(_windows_file_information(handle).dwVolumeSerialNumber)
    return int(os.fstat(int(handle)).st_dev)


def _close_posix_handles(handles: list[object]) -> None:
    for handle in reversed(handles):
        try:
            os.close(int(handle))
        except OSError:
            pass


def _open_posix_directory_chain(path: Path, *, label: str) -> list[object]:
    """Open each directory component relative to its pinned parent, no-follow."""
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise DirectoryInstallError("safe descriptor-relative directory traversal is unavailable")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise DirectoryInstallError("safe no-follow directory traversal is unavailable")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    handles: list[object] = []
    try:
        root_handle = os.open(path.anchor, flags)
        handles.append(root_handle)
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=int(handles[-1]))
            try:
                metadata = os.fstat(child)
            except OSError:
                os.close(child)
                raise
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise DirectoryInstallError(f"{label} path component is not a directory: {component}")
            handles.append(child)
        return handles
    except DirectoryInstallError:
        _close_posix_handles(handles)
        raise
    except (OSError, ValueError) as exc:
        _close_posix_handles(handles)
        raise DirectoryInstallError(f"{label} parent path is missing, unsafe, or inaccessible: {exc}") from exc


@contextmanager
def _pin_posix_entry(path: Path, *, allow_missing_leaf: bool, label: str) -> Iterator[_PinnedEntry]:
    absolute = _absolute_path(path, label=label)
    parent_handles = _open_posix_directory_chain(absolute.parent, label=label)
    leaf_handle: object | None = None
    try:
        if allow_missing_leaf:
            try:
                os.stat(absolute.name, dir_fd=int(parent_handles[-1]), follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DirectoryInstallError(f"{label} destination cannot be inspected: {exc}") from exc
            else:
                raise DirectoryInstallError(f"destination already exists: {absolute}")
            try:
                device_id = _filesystem_id(parent_handles[-1])
            except OSError as exc:
                raise DirectoryInstallError(f"cannot verify {label} filesystem: {exc}") from exc
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                leaf_handle = os.open(absolute.name, flags, dir_fd=int(parent_handles[-1]))
            except OSError as exc:
                raise DirectoryInstallError(f"{label} is missing, unsafe, or not a directory: {absolute}: {exc}") from exc
            try:
                metadata = os.fstat(int(leaf_handle))
                device_id = _filesystem_id(leaf_handle)
            except OSError as exc:
                raise DirectoryInstallError(f"cannot verify {label} directory: {absolute}: {exc}") from exc
            if not stat.S_ISDIR(metadata.st_mode):
                raise DirectoryInstallError(f"{label} is not a directory: {absolute}")
        yield _PinnedEntry(absolute, parent_handles, absolute.name, leaf_handle, device_id)
    finally:
        if leaf_handle is not None:
            try:
                os.close(int(leaf_handle))
            except OSError:
                pass
        _close_posix_handles(parent_handles)


def _rename_linux_noreplace(
    stage_parent: object, stage_name: str, destination_parent: object, destination_name: str
) -> None:
    """Use renameat2(RENAME_NOREPLACE) relative to pinned directory descriptors."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise DirectoryInstallError("Linux renameat2(RENAME_NOREPLACE) is unavailable") from exc

    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        int(stage_parent),
        os.fsencode(stage_name),
        int(destination_parent),
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _rename_darwin_noreplace(
    stage_parent: object, stage_name: str, destination_parent: object, destination_name: str
) -> None:
    """Use renameatx_np(RENAME_EXCL) relative to pinned directory descriptors."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = getattr(libc, "renameatx_np", None)
        if renameatx_np is None:
            renameatx_np = getattr(libc, "renamex_np", None)
            if renameatx_np is None:
                raise AttributeError("renameatx_np")
    except (AttributeError, OSError) as exc:
        raise DirectoryInstallError("Darwin renameatx_np(RENAME_EXCL) is unavailable") from exc

    renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        int(stage_parent),
        os.fsencode(stage_name),
        int(destination_parent),
        os.fsencode(destination_name),
        _DARWIN_RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _WindowsFileTime),
        ("ftLastAccessTime", _WindowsFileTime),
        ("ftLastWriteTime", _WindowsFileTime),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _WindowsUnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _WindowsObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_WindowsUnicodeString)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _WindowsIoStatusUnion(ctypes.Union):
    _fields_ = [("Status", ctypes.c_int32), ("Pointer", ctypes.c_void_p)]


class _WindowsIoStatusBlock(ctypes.Structure):
    _anonymous_ = ("StatusOrPointer",)
    _fields_ = [("StatusOrPointer", _WindowsIoStatusUnion), ("Information", ctypes.c_size_t)]


class _WindowsFileRenameInfo(ctypes.Structure):
    # The Win32 union occupies four bytes; a DWORD preserves its ABI size.
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_wchar * 1),
    ]


def _windows_apis():
    try:
        win_dll = ctypes.WinDLL
    except AttributeError as exc:
        raise DirectoryInstallError("Windows handle-relative file APIs are unavailable") from exc
    try:
        kernel32 = win_dll("kernel32", use_last_error=True)
        ntdll = win_dll("ntdll")
    except OSError as exc:
        raise DirectoryInstallError("Windows handle-relative file APIs are unavailable") from exc

    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsObjectAttributes),
        ctypes.POINTER(_WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    ntdll.NtCreateFile.restype = ctypes.c_int32
    ntdll.NtSetInformationFile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    ntdll.NtSetInformationFile.restype = ctypes.c_int32
    ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_int32]
    ntdll.RtlNtStatusToDosError.restype = ctypes.c_uint32
    return kernel32, ntdll


def _windows_last_error(kernel32: object) -> OSError:
    error_code = ctypes.get_last_error()
    if error_code == _WINDOWS_ERROR_NOT_SAME_DEVICE:
        return OSError(errno.EXDEV, "stage and destination parent must be on the same filesystem")
    if error_code in {_WINDOWS_ERROR_FILE_EXISTS, _WINDOWS_ERROR_ALREADY_EXISTS}:
        return OSError(errno.EEXIST, "destination became occupied during installation")
    if error_code in _WINDOWS_ERROR_UNSUPPORTED:
        return DirectoryInstallError("handle-relative exclusive directory rename is unavailable on this filesystem")
    return ctypes.WinError(error_code)


def _windows_nt_error(ntdll: object, status: int) -> OSError:
    error_code = int(ntdll.RtlNtStatusToDosError(status))
    return ctypes.WinError(error_code)


def _windows_file_information(handle: object) -> _WindowsByHandleFileInformation:
    kernel32, _ = _windows_apis()
    information = _WindowsByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise _windows_last_error(kernel32)
    return information


def _windows_close_handles(handles: list[object]) -> None:
    try:
        kernel32, _ = _windows_apis()
    except DirectoryInstallError:
        return
    for handle in reversed(handles):
        try:
            kernel32.CloseHandle(handle)
        except (OSError, TypeError):
            pass


def _windows_open_root(anchor: str, *, label: str, add_subdirectory: bool = False) -> object:
    kernel32, _ = _windows_apis()
    desired_access = (
        _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_FILE_LIST_DIRECTORY
        | _WINDOWS_FILE_TRAVERSE
        | _WINDOWS_SYNCHRONIZE
    )
    if add_subdirectory:
        desired_access |= _WINDOWS_FILE_ADD_SUBDIRECTORY
    flags = _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    handle = kernel32.CreateFileW(
        anchor,
        desired_access,
        _WINDOWS_FILE_SHARE_ALL,
        None,
        _WINDOWS_OPEN_EXISTING,
        flags,
        None,
    )
    if handle == ctypes.c_void_p(-1).value or handle is None:
        raise DirectoryInstallError(f"{label} filesystem root cannot be opened: {_windows_last_error(kernel32)}")
    try:
        information = _windows_file_information(handle)
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    if not information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
        kernel32.CloseHandle(handle)
        raise DirectoryInstallError(f"{label} filesystem root is not a directory")
    if information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        kernel32.CloseHandle(handle)
        raise DirectoryInstallError(f"{label} filesystem root is a reparse point")
    return handle


def _windows_open_relative(
    parent_handle: object,
    name: str,
    *,
    label: str,
    directory_only: bool,
    rename_source: bool = False,
    add_subdirectory: bool = False,
) -> object:
    if not name or name in {".", ".."} or any(char in name for char in ("/", "\\", ":", "\x00")):
        raise DirectoryInstallError(f"{label} path component is invalid: {name!r}")
    kernel32, ntdll = _windows_apis()
    name_buffer = ctypes.create_unicode_buffer(name)
    name_length = len(name.encode("utf-16-le"))
    unicode_name = _WindowsUnicodeString(
        name_length,
        name_length + ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    attributes = _WindowsObjectAttributes(
        ctypes.sizeof(_WindowsObjectAttributes),
        parent_handle,
        ctypes.pointer(unicode_name),
        _WINDOWS_OBJ_CASE_INSENSITIVE | _WINDOWS_OBJ_DONT_REPARSE,
        None,
        None,
    )
    io_status = _WindowsIoStatusBlock()
    opened = ctypes.c_void_p()
    desired_access = _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_SYNCHRONIZE
    if directory_only:
        desired_access |= _WINDOWS_FILE_LIST_DIRECTORY | _WINDOWS_FILE_TRAVERSE
    if rename_source:
        desired_access |= _WINDOWS_FILE_DELETE
    if add_subdirectory:
        desired_access |= _WINDOWS_FILE_ADD_SUBDIRECTORY
    options = _WINDOWS_FILE_OPEN_REPARSE_POINT | _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
    if directory_only:
        options |= _WINDOWS_FILE_DIRECTORY_FILE
    status = ntdll.NtCreateFile(
        ctypes.byref(opened),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _WINDOWS_FILE_SHARE_ALL,
        _WINDOWS_FILE_OPEN,
        options,
        None,
        0,
    )
    if status < 0:
        if status == _WINDOWS_STATUS_REPARSE_POINT_ENCOUNTERED:
            raise DirectoryInstallError(f"{label} path contains a symbolic link or reparse point: {name}")
        raise _windows_nt_error(ntdll, status)
    handle = opened.value
    if handle is None:
        raise DirectoryInstallError(f"{label} path component could not be opened: {name}")
    try:
        information = _windows_file_information(handle)
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    if information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        kernel32.CloseHandle(handle)
        raise DirectoryInstallError(f"{label} path contains a symbolic link or reparse point: {name}")
    if directory_only and not information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
        kernel32.CloseHandle(handle)
        raise DirectoryInstallError(f"{label} parent is not a directory: {name}")
    return handle


def _open_windows_directory_chain(
    path: Path, *, label: str, add_subdirectory: bool = False
) -> list[object]:
    handles: list[object] = []
    try:
        components = path.parts[1:]
        handles.append(
            _windows_open_root(
                path.anchor,
                label=label,
                add_subdirectory=add_subdirectory and not components,
            )
        )
        for index, component in enumerate(components):
            handles.append(
                _windows_open_relative(
                    handles[-1],
                    component,
                    label=label,
                    directory_only=True,
                    add_subdirectory=add_subdirectory and index == len(components) - 1,
                )
            )
        return handles
    except DirectoryInstallError:
        _windows_close_handles(handles)
        raise
    except OSError as exc:
        _windows_close_handles(handles)
        raise DirectoryInstallError(f"{label} parent path is missing, unsafe, or inaccessible: {exc}") from exc


@contextmanager
def _pin_windows_entry(path: Path, *, allow_missing_leaf: bool, label: str) -> Iterator[_PinnedEntry]:
    absolute = _absolute_path(path, label=label)
    parent_handles = _open_windows_directory_chain(
        absolute.parent, label=label, add_subdirectory=allow_missing_leaf
    )
    leaf_handle: object | None = None
    try:
        if allow_missing_leaf:
            try:
                leaf_handle = _windows_open_relative(
                    parent_handles[-1], absolute.name, label=label, directory_only=False
                )
            except OSError as exc:
                if getattr(exc, "winerror", None) in {2, 3}:
                    leaf_handle = None
                else:
                    raise DirectoryInstallError(f"{label} destination cannot be inspected: {exc}") from exc
            if leaf_handle is not None:
                raise DirectoryInstallError(f"destination already exists: {absolute}")
            try:
                device_id = _filesystem_id(parent_handles[-1])
            except OSError as exc:
                raise DirectoryInstallError(f"cannot verify destination filesystem: {exc}") from exc
        else:
            try:
                leaf_handle = _windows_open_relative(
                    parent_handles[-1],
                    absolute.name,
                    label=label,
                    directory_only=True,
                    rename_source=True,
                )
            except DirectoryInstallError:
                raise
            except OSError as exc:
                raise DirectoryInstallError(
                    f"{label} is missing, unsafe, or not a directory: {absolute}: {exc}"
                ) from exc
            try:
                device_id = _filesystem_id(leaf_handle)
            except OSError as exc:
                raise DirectoryInstallError(f"cannot verify {label} filesystem: {exc}") from exc
        yield _PinnedEntry(absolute, parent_handles, absolute.name, leaf_handle, device_id, windows=True)
    finally:
        if leaf_handle is not None:
            try:
                kernel32, _ = _windows_apis()
                kernel32.CloseHandle(leaf_handle)
            except (DirectoryInstallError, OSError, TypeError):
                pass
        _windows_close_handles(parent_handles)


def _rename_windows_noreplace(stage_handle: object, destination_parent: object, destination_name: str) -> None:
    """Rename an opened directory handle relative to a pinned parent, without replacement."""
    _, ntdll = _windows_apis()
    if not destination_name or destination_name in {".", ".."} or any(
        char in destination_name for char in ("/", "\\", ":")
    ):
        raise DirectoryInstallError(f"destination path component is invalid: {destination_name!r}")

    encoded_name = destination_name.encode("utf-16-le")
    buffer_size = _WindowsFileRenameInfo.FileName.offset + len(encoded_name)
    rename_buffer = ctypes.create_string_buffer(buffer_size)
    rename_information = ctypes.cast(rename_buffer, ctypes.POINTER(_WindowsFileRenameInfo)).contents
    rename_information.ReplaceIfExists = 0
    rename_information.RootDirectory = destination_parent
    rename_information.FileNameLength = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(rename_buffer) + _WindowsFileRenameInfo.FileName.offset,
        encoded_name,
        len(encoded_name),
    )
    io_status = _WindowsIoStatusBlock()
    status = ntdll.NtSetInformationFile(
        stage_handle,
        ctypes.byref(io_status),
        ctypes.cast(rename_buffer, ctypes.c_void_p),
        buffer_size,
        _WINDOWS_FILE_RENAME_INFORMATION_CLASS,
    )
    if status < 0:
        raise _windows_nt_error(ntdll, status)


def _rename_error(exc: OSError, destination: Path) -> DirectoryInstallError:
    windows_error = getattr(exc, "winerror", None)
    if exc.errno in _RENAME_UNAVAILABLE_ERRNOS or windows_error in _WINDOWS_ERROR_UNSUPPORTED:
        return DirectoryInstallError("exclusive directory rename primitive is unavailable on this filesystem")
    if exc.errno == errno.EXDEV or windows_error == _WINDOWS_ERROR_NOT_SAME_DEVICE:
        return DirectoryInstallError("stage and destination parent must be on the same filesystem")
    if exc.errno in {errno.EEXIST, errno.ENOTEMPTY} or windows_error in {
        _WINDOWS_ERROR_FILE_EXISTS,
        _WINDOWS_ERROR_ALREADY_EXISTS,
    }:
        return DirectoryInstallError(f"destination became occupied during installation: {destination}")
    return DirectoryInstallError(f"exclusive directory installation failed: {exc}")


def install_directory_exclusive(stage: Path, destination: Path) -> None:
    """Atomically rename ``stage`` to an absent ``destination`` on one filesystem.

    Linux uses descriptor-relative ``renameat2(RENAME_NOREPLACE)``; Darwin uses
    ``renameatx_np(RENAME_EXCL)``; Windows renames the opened stage handle with
    ``NtSetInformationFile(FileRenameInformation)`` and a pinned destination
    parent. Every parent component is opened relative to the preceding pinned
    directory and rejects symbolic links/reparse points. Unsupported platforms
    or filesystems refuse rather than emulate exclusivity with a racy check.
    """
    stage_path = _absolute_path(Path(stage), label="stage")
    destination_path = _absolute_path(Path(destination), label="destination")
    if destination_path == stage_path or destination_path.is_relative_to(stage_path):
        raise DirectoryInstallError("destination must not be the stage directory or a child of it")

    if os.name == "nt":
        entry_context = _pin_windows_entry
    elif sys.platform in {"linux", "darwin", "win32"}:
        entry_context = _pin_posix_entry
    else:
        raise DirectoryInstallError(f"exclusive directory rename is unavailable on platform {sys.platform}")

    with entry_context(stage_path, allow_missing_leaf=False, label="stage") as stage_entry:
        with entry_context(destination_path, allow_missing_leaf=True, label="destination") as destination_entry:
            if stage_entry.device_id != destination_entry.device_id:
                raise DirectoryInstallError("stage and destination parent must be on the same filesystem")
            try:
                if sys.platform == "linux":
                    _rename_linux_noreplace(
                        stage_entry.parent_handle,
                        stage_entry.leaf_name,
                        destination_entry.parent_handle,
                        destination_entry.leaf_name,
                    )
                elif sys.platform == "darwin":
                    _rename_darwin_noreplace(
                        stage_entry.parent_handle,
                        stage_entry.leaf_name,
                        destination_entry.parent_handle,
                        destination_entry.leaf_name,
                    )
                elif sys.platform == "win32":
                    if stage_entry.leaf_handle is None:
                        raise DirectoryInstallError("stage directory handle is unavailable")
                    _rename_windows_noreplace(
                        stage_entry.leaf_handle,
                        destination_entry.parent_handle,
                        destination_entry.leaf_name,
                    )
                else:
                    raise DirectoryInstallError(f"exclusive directory rename is unavailable on platform {sys.platform}")
            except DirectoryInstallError:
                raise
            except OSError as exc:
                raise _rename_error(exc, destination_path) from exc
