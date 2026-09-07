"""Journaled, file-level application of reconciled source trees."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from typing import Any, Mapping

from .trees import Tree, TreeError, _validate_tree_paths, assert_source_clean, read_export_tree


class PatchError(RuntimeError):
    """Raised when a source precondition, write or recovery is unsafe."""


_ABSENT = object()


def _git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise PatchError("Git is unavailable") from exc
    if result.returncode != 0:
        raise PatchError("could not resolve current HEAD")
    return result.stdout.decode("ascii").strip()


def _tree_at_worktree(repo: Path, alias: str) -> dict[str, bytes]:
    root = repo / "apps" / alias
    if not root.exists():
        return {}
    try:
        return read_export_tree(root)
    except TreeError as exc:
        raise PatchError(str(exc)) from exc


def _current_file(path: Path):
    if not path.exists():
        return _ABSENT
    if path.is_symlink() or not path.is_file():
        raise PatchError(f"source path is not a regular file: {path}")
    return path.read_bytes()


def _encode(value: object) -> str | None:
    if value is _ABSENT or value is None:
        return None
    if not isinstance(value, bytes):
        raise PatchError("journal preimages must be bytes or absent")
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any):
    if value is None:
        return _ABSENT
    if not isinstance(value, str):
        raise PatchError("malformed journal preimage")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise PatchError("malformed journal preimage") from exc


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PatchError(f"could not write patch journal: {path}") from exc


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise PatchError(f"refusing to write through symlink: {path}")
    temp = path.with_name(f".{path.name}.team-tmp-{uuid.uuid4().hex}")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise PatchError(f"could not atomically write source path: {path}") from exc


def _lock(repo: Path):
    class _Lock:
        def __init__(self):
            self.handle = None

        def __enter__(self):
            lock_path = repo / ".sync-state" / "patch.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = lock_path.open("a+")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type, exc, tb):
            if self.handle is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
            return False

    return _Lock()


def _load_journal(repo: Path, operation_id: str) -> tuple[Path, dict[str, Any]]:
    if not operation_id or "/" in operation_id or "\\" in operation_id or operation_id in {".", ".."}:
        raise PatchError("unsafe patch operation ID")
    path = repo / ".sync-state" / "journals" / f"{operation_id}.json"
    if path.is_symlink() or not path.is_file():
        raise PatchError(f"patch journal not found: {operation_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchError("patch journal is unreadable") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files"), list):
        raise PatchError("patch journal is malformed")
    return path, data


def _verify_operation_paths(repo: Path, alias: str, files: list[dict[str, Any]]) -> list[tuple[str, object, object]]:
    source_root = repo / "apps" / alias
    result = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "before", "after"}:
            raise PatchError("malformed patch file entry")
        path = item["path"]
        if not isinstance(path, str) or path.startswith("/") or "\\" in path or path.startswith("../") or "/../" in path:
            raise PatchError(f"unsafe patch path: {path!r}")
        before = _decode(item["before"])
        after = _decode(item["after"])
        result.append((path, before, after))
    return result


def _assert_current(path: Path, expected: object, context: str) -> None:
    actual = _current_file(path)
    if expected is _ABSENT:
        if actual is not _ABSENT:
            raise PatchError(f"{context}: expected {path} to be absent")
    elif actual is _ABSENT or actual != expected:
        raise PatchError(f"{context}: source preimage changed at {path}")


def _apply_one(path: Path, value: object) -> None:
    if value is _ABSENT:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise PatchError(f"refusing to delete non-regular source path: {path}")
            path.unlink()
        return
    _write_bytes(path, value)


def apply_tree(
    repo: str | Path,
    alias: str,
    expected_head: str,
    before: Tree,
    after: Tree,
    recovery_id: str,
) -> None:
    """Apply only the exact owned-file delta after all preconditions pass."""
    repo_path = Path(repo)
    if not repo_path.is_dir() or not recovery_id:
        raise PatchError("repository and recovery ID are required")
    try:
        _validate_tree_paths(before)
        _validate_tree_paths(after)
    except TreeError as exc:
        raise PatchError(str(exc)) from exc
    with _lock(repo_path):
        if _git_head(repo_path) != expected_head:
            raise PatchError("HEAD changed before patch application")
        try:
            assert_source_clean(repo_path, alias)
        except TreeError as exc:
            raise PatchError(str(exc)) from exc
        current = _tree_at_worktree(repo_path, alias)
        if current != dict(before):
            raise PatchError("owned source preimage does not match the expected tree")
        journal_dir = repo_path / ".sync-state" / "journals"
        journal_path = journal_dir / f"{recovery_id}.json"
        if journal_path.exists():
            raise PatchError(f"patch operation already has a journal: {recovery_id}")
        files: list[dict[str, Any]] = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path, _ABSENT)
            new = after.get(path, _ABSENT)
            if old is new or (old is not _ABSENT and new is not _ABSENT and old == new):
                continue
            files.append({"path": path, "before": _encode(old), "after": _encode(new)})
        journal: dict[str, Any] = {
            "version": 1,
            "operation_id": recovery_id,
            "alias": alias,
            "expected_head": expected_head,
            "status": "prepared",
            "files": files,
            "applied": [],
        }
        _atomic_json(journal_path, journal)
        try:
            for index, item in enumerate(files, start=1):
                path = repo_path / "apps" / alias / item["path"]
                before_value = _decode(item["before"])
                after_value = _decode(item["after"])
                _assert_current(path, before_value, "patch preimage")
                journal["status"] = "running"
                _apply_one(path, after_value)
                journal["applied"].append(item["path"])
                _atomic_json(journal_path, journal)
                fail_after = os.environ.get("TEAM_PATCH_FAIL_AFTER")
                if fail_after and index >= int(fail_after):
                    raise PatchError("injected patch failure after journaled write")
            if _tree_at_worktree(repo_path, alias) != dict(after):
                raise PatchError("patched source does not match the requested result tree")
            journal["status"] = "complete"
            _atomic_json(journal_path, journal)
        except Exception as exc:
            if isinstance(exc, PatchError):
                raise
            raise PatchError("patch failed; retained journal requires recovery") from exc


def recover_files(
    operation_id: str,
    action: str,
    *,
    repo: str | Path = ".",
) -> None:
    """Finish or restore an interrupted patch without overwriting later edits."""
    if action not in {"finish", "restore"}:
        raise PatchError("recovery action must be finish or restore")
    repo_path = Path(repo)
    with _lock(repo_path):
        journal_path, journal = _load_journal(repo_path, operation_id)
        status = journal.get("status")
        if status in {"complete", "restored", "finished"}:
            return
        alias = journal.get("alias")
        if not isinstance(alias, str):
            raise PatchError("patch journal alias is malformed")
        operations = _verify_operation_paths(repo_path, alias, journal["files"])
        for relative, before, after in operations:
            path = repo_path / "apps" / alias / relative
            current = _current_file(path)
            if action == "restore":
                if current is _ABSENT and before is _ABSENT:
                    continue
                if before is not _ABSENT and current == before:
                    continue
                if after is _ABSENT:
                    if current is not _ABSENT:
                        raise PatchError(f"restore refuses changed path: {path}")
                elif current != after:
                    raise PatchError(f"restore refuses changed path: {path}")
                _apply_one(path, before)
            else:
                if current is after or (current is not _ABSENT and after is not _ABSENT and current == after):
                    continue
                if before is _ABSENT:
                    if current is not _ABSENT:
                        raise PatchError(f"finish refuses changed path: {path}")
                elif current != before:
                    raise PatchError(f"finish refuses changed path: {path}")
                _apply_one(path, after)
        journal["status"] = "restored" if action == "restore" else "finished"
        _atomic_json(journal_path, journal)

