"""Durable local baselines, checkpoints, receipts and recovery captures."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from typing import Any
from collections.abc import Mapping

from .config import Target
from .trees import Tree, TreeError, tree_digest, tree_manifest


class StateError(RuntimeError):
    """Raised when local synchronization evidence is missing or corrupt."""


@dataclass(frozen=True)
class Baseline:
    version: int
    state_key: str
    source_commit: str
    tree_digest: str
    blobs: dict[str, str]
    tree: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class Checkpoint:
    version: int
    state_key: str
    captured: dict[str, bytes]
    reconciled: dict[str, bytes]
    original_head: str
    receipt_id: str
    anchor_commit: str | None
    required_absent: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Receipt:
    version: int
    receipt_id: str
    state_key: str
    result: dict[str, bytes]
    selected_commit: str
    required_absent: set[str]
    capture_digest: str
    kind: str = "capture"
    resolved_digest: str | None = None


@dataclass(frozen=True)
class Capture:
    recovery_id: str
    target: Target
    base: dict[str, bytes]
    source_base: dict[str, bytes]
    head: str
    mine: dict[str, bytes]
    diagnostics: Mapping[str, Any]
    head_tree: dict[str, bytes] = field(default_factory=dict)


def app_lock_key(target: Target) -> str:
    """Return the physical key used by every cooperating checkout."""
    return target.physical_key


def _state_root(root: str | Path | None) -> Path:
    path = Path(root or ".sync-state")
    if path.exists() and path.is_symlink():
        raise StateError(f"synchronization state root must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _target_descriptor(target: Target) -> dict[str, Any]:
    return {
        "project": target.project,
        "role": target.role,
        "environment": target.environment,
        "connection": target.connection,
        "instance_id": target.instance_id,
        "db_name": target.db_name,
        "service": target.service,
        "session_user": target.session_user,
        "current_schema": target.current_schema,
        "alias": target.alias,
        "workspace_id": target.workspace_id,
        "app_id": target.app_id,
        "parsing_schema": target.parsing_schema,
        "ownership_mode": target.ownership_mode,
        "binding_digest": target.binding_digest,
        "state_key": target.state_key,
        "physical_key": target.physical_key if target.workspace_id and target.app_id else None,
    }


def _validate_tree(tree: Tree) -> dict[str, bytes]:
    try:
        copied = dict(tree)
        manifest = tree_manifest(copied)
    except (TypeError, ValueError, TreeError) as exc:
        raise StateError("invalid synchronization tree") from exc
    # tree_manifest validates the values; keep this check explicit so a caller
    # cannot smuggle a mutable bytearray or a text value into blob storage.
    if any(not isinstance(path, str) or not isinstance(value, bytes) for path, value in copied.items()):
        raise StateError("synchronization trees must map paths to bytes")
    if len(manifest["files"]) != len(copied):
        raise StateError("invalid synchronization tree manifest")
    return dict(sorted(copied.items()))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise StateError(f"state manifest must not be a symlink: {path}")
    encoded = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StateError(f"could not save state manifest: {path}") from exc


def _blob_path(root: Path, digest: str) -> Path:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise StateError(f"invalid blob digest: {digest}")
    return root / "blobs" / digest


def _store_blob(root: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = _blob_path(root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise StateError(f"content-addressed blob is corrupt: {digest}")
        return digest
    fd, temp_name = tempfile.mkstemp(prefix=f".{digest}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StateError(f"could not save content-addressed blob: {digest}") from exc
    return digest


def _file_records(root: Path, tree: Tree) -> tuple[list[dict[str, Any]], dict[str, str]]:
    copied = _validate_tree(tree)
    records: list[dict[str, Any]] = []
    blobs: dict[str, str] = {}
    for path in sorted(copied):
        digest = _store_blob(root, copied[path])
        records.append({"path": path, "length": len(copied[path]), "sha256": digest})
        blobs[path] = digest
    return records, blobs


def _load_records(root: Path, records: Any) -> dict[str, bytes]:
    if not isinstance(records, list):
        raise StateError("state manifest files must be a list")
    tree: dict[str, bytes] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "length", "sha256"}:
            raise StateError("malformed state file record")
        path = record["path"]
        length = record["length"]
        digest = record["sha256"]
        if not isinstance(path, str) or not isinstance(length, int) or length < 0 or not isinstance(digest, str):
            raise StateError("malformed state file record values")
        if path in tree:
            raise StateError(f"duplicate state path: {path}")
        blob = _blob_path(root, digest)
        if blob.is_symlink() or not blob.is_file():
            raise StateError(f"missing state blob: {digest}")
        try:
            data = blob.read_bytes()
        except OSError as exc:
            raise StateError(f"could not read state blob: {digest}") from exc
        if len(data) != length or hashlib.sha256(data).hexdigest() != digest:
            raise StateError(f"corrupt state blob: {digest}")
        tree[path] = data
    return _validate_tree(tree)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise StateError(f"missing state manifest: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"invalid state manifest: {path}") from exc
    if not isinstance(data, dict):
        raise StateError(f"state manifest is not an object: {path}")
    return data


def save_verified_baseline(
    target: Target,
    commit: str,
    tree: Tree,
    *,
    root: str | Path | None = None,
) -> None:
    if not isinstance(commit, str) or not commit or any(char.isspace() for char in commit):
        raise StateError("baseline source commit is required")
    state_root = _state_root(root)
    records, blobs = _file_records(state_root, tree)
    manifest = {
        "version": 1,
        "type": "baseline",
        "target": _target_descriptor(target),
        "state_key": target.state_key,
        "source_commit": commit,
        "tree_digest": tree_digest(tree),
        "files": records,
    }
    _atomic_json(state_root / "baselines" / target.state_key / "baseline.json", manifest)


def load_baseline(target: Target, *, root: str | Path | None = None) -> Baseline:
    state_root = _state_root(root)
    data = _read_json(state_root / "baselines" / target.state_key / "baseline.json")
    if data.get("version") != 1 or data.get("type") != "baseline" or data.get("state_key") != target.state_key:
        raise StateError("baseline identity or version does not match target")
    tree = _load_records(state_root, data.get("files"))
    digest = data.get("tree_digest")
    if not isinstance(digest, str) or tree_digest(tree) != digest:
        raise StateError("baseline tree digest does not match retained blobs")
    commit = data.get("source_commit")
    if not isinstance(commit, str) or not commit:
        raise StateError("baseline source commit is missing")
    blobs = {path: hashlib.sha256(value).hexdigest() for path, value in tree.items()}
    return Baseline(1, target.state_key, commit, digest, blobs, tree)


def _check_head_ancestry(root: Path, original_head: str, head: str) -> None:
    if original_head == head:
        return
    repo = root.parent if root.name == ".sync-state" else root
    if not repo.is_dir():
        raise StateError("checkpoint ancestry cannot be verified")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", original_head, head],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise StateError("checkpoint ancestry cannot be verified") from exc
    if result.returncode != 0:
        raise StateError("current HEAD is not descended from checkpoint HEAD")


def save_checkpoint(
    target: Target,
    captured: Tree,
    reconciled: Tree,
    original_head: str,
    receipt_id: str,
    *,
    anchor_commit: str | None = None,
    required_absent: set[str] | None = None,
    root: str | Path | None = None,
) -> None:
    state_root = _state_root(root)
    captured_records, _ = _file_records(state_root, captured)
    reconciled_records, _ = _file_records(state_root, reconciled)
    absent = sorted(required_absent or set())
    manifest = {
        "version": 1,
        "type": "checkpoint",
        "target": _target_descriptor(target),
        "state_key": target.state_key,
        "captured": captured_records,
        "reconciled": reconciled_records,
        "original_head": original_head,
        "anchor_commit": anchor_commit,
        "receipt_id": receipt_id,
        "required_absent": absent,
    }
    _atomic_json(state_root / "checkpoints" / target.state_key / "checkpoint.json", manifest)


def load_checkpoint(
    target: Target,
    head: str,
    *,
    root: str | Path | None = None,
) -> Checkpoint:
    state_root = _state_root(root)
    data = _read_json(state_root / "checkpoints" / target.state_key / "checkpoint.json")
    if data.get("version") != 1 or data.get("type") != "checkpoint" or data.get("state_key") != target.state_key:
        raise StateError("checkpoint identity or version does not match target")
    original_head = data.get("original_head")
    if not isinstance(original_head, str) or not original_head:
        raise StateError("checkpoint original HEAD is missing")
    _check_head_ancestry(state_root, original_head, head)
    captured = _load_records(state_root, data.get("captured"))
    reconciled = _load_records(state_root, data.get("reconciled"))
    receipt_id = data.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        raise StateError("checkpoint receipt ID is missing")
    anchor_commit = data.get("anchor_commit")
    if anchor_commit is not None and not isinstance(anchor_commit, str):
        raise StateError("checkpoint anchor commit is malformed")
    raw_absent = data.get("required_absent", [])
    if not isinstance(raw_absent, list) or any(not isinstance(path, str) for path in raw_absent):
        raise StateError("checkpoint absence evidence is malformed")
    return Checkpoint(1, target.state_key, captured, reconciled, original_head, receipt_id, anchor_commit, set(raw_absent))


def save_capture(
    target: Target,
    base: Tree,
    source_base: Tree,
    head: str | Tree,
    mine: Tree,
    diagnostics: Mapping[str, Any],
    *,
    root: str | Path | None = None,
) -> str:
    state_root = _state_root(root)
    recovery_id = uuid.uuid4().hex
    recovery_dir = state_root / "recovery" / recovery_id
    # Write blobs before the manifest. A process interruption leaves an
    # inspectable directory but never a manifest claiming missing content.
    records: dict[str, list[dict[str, Any]]] = {}
    for name, tree in (("base", base), ("source_base", source_base), ("mine", mine)):
        records[name], _ = _file_records(state_root, tree)
    if isinstance(head, Mapping):
        head_commit = ""
        head_records, _ = _file_records(state_root, head)
    elif isinstance(head, str) and head:
        head_commit = head
        head_records = []
    else:
        raise StateError("capture HEAD must be a commit or exact source tree")
    if isinstance(diagnostics, Mapping) and isinstance(diagnostics.get("head_commit"), str):
        head_commit = diagnostics["head_commit"]
    manifest = {
        "version": 1,
        "type": "capture",
        "recovery_id": recovery_id,
        "target": _target_descriptor(target),
        "state_key": target.state_key,
        "base": records["base"],
        "source_base": records["source_base"],
        "head_commit": head_commit,
        "head_tree": head_records,
        "mine": records["mine"],
        "diagnostics": dict(diagnostics),
    }
    _atomic_json(recovery_dir / "capture.json", manifest)
    return recovery_id


def load_capture(target: Target, recovery_id: str, *, root: str | Path | None = None) -> Capture:
    state_root = _state_root(root)
    data = _read_json(state_root / "recovery" / recovery_id / "capture.json")
    if data.get("version") != 1 or data.get("type") != "capture" or data.get("state_key") != target.state_key:
        raise StateError("capture identity or version does not match target")
    diagnostics = data.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise StateError("capture diagnostics are malformed")
    head = data.get("head_commit")
    if not isinstance(head, str) or not head:
        raise StateError("capture HEAD is missing")
    base = _load_records(state_root, data.get("base"))
    source_base = _load_records(state_root, data.get("source_base"))
    mine = _load_records(state_root, data.get("mine"))
    head_tree = _load_records(state_root, data.get("head_tree", []))
    return Capture(recovery_id, target, base, source_base, head, mine, diagnostics, head_tree)


def required_absences(
    previous_required: set[str],
    base: Tree,
    source_base: Tree,
    head: Tree,
    mine: Tree,
    result: Tree,
) -> set[str]:
    """Carry tombstones forward while allowing a reconciled re-add."""
    known = set(base) | set(source_base) | set(head) | set(mine)
    return (set(previous_required) | known) - set(result)


def save_receipt(
    target: Target,
    result: Tree,
    selected_commit: str,
    required_absent: set[str],
    capture_digest: str,
    *,
    kind: str = "capture",
    resolved_digest: str | None = None,
    root: str | Path | None = None,
) -> str:
    if not isinstance(selected_commit, str) or not selected_commit:
        raise StateError("receipt selected commit is required")
    state_root = _state_root(root)
    records, _ = _file_records(state_root, result)
    receipt_id = uuid.uuid4().hex
    manifest = {
        "version": 1,
        "type": "receipt",
        "receipt_id": receipt_id,
        "target": _target_descriptor(target),
        "state_key": target.state_key,
        "result": records,
        "selected_commit": selected_commit,
        "required_absent": sorted(required_absent),
        "capture_digest": capture_digest,
        "kind": kind,
        "resolved_digest": resolved_digest,
    }
    _atomic_json(state_root / "receipts" / target.state_key / f"{receipt_id}.json", manifest)
    return receipt_id


def load_receipt(target: Target, receipt_id: str, *, root: str | Path | None = None) -> Receipt:
    state_root = _state_root(root)
    data = _read_json(state_root / "receipts" / target.state_key / f"{receipt_id}.json")
    if data.get("version") != 1 or data.get("type") != "receipt" or data.get("state_key") != target.state_key:
        raise StateError("receipt identity or version does not match target")
    selected_commit = data.get("selected_commit")
    capture_digest = data.get("capture_digest")
    if not isinstance(selected_commit, str) or not selected_commit or not isinstance(capture_digest, str) or not capture_digest:
        raise StateError("receipt provenance is incomplete")
    raw_absent = data.get("required_absent")
    if not isinstance(raw_absent, list) or any(not isinstance(path, str) for path in raw_absent):
        raise StateError("receipt absence evidence is missing or malformed")
    result = _load_records(state_root, data.get("result"))
    kind = data.get("kind", "capture")
    if not isinstance(kind, str):
        raise StateError("receipt kind is malformed")
    resolved_digest = data.get("resolved_digest")
    if resolved_digest is not None and not isinstance(resolved_digest, str):
        raise StateError("receipt resolved digest is malformed")
    return Receipt(1, receipt_id, target.state_key, result, selected_commit, set(raw_absent), capture_digest, kind, resolved_digest)
