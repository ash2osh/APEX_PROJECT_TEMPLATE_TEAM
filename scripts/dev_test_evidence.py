"""Durable evidence storage for DEV test runs (Task 2).

Public API
----------
RunEvidence(run_id: str, root: Path)
    - Named-tuple-like record; holds run_id and root (repo root, not run dir).

create_run(repo_root: Path) -> RunEvidence
    - Allocate a fresh run directory under scratch/dev-test/<run_id>/.
    - run_id format: YYYYMMDDTHHMMSSZ-<32 lowercase hex UUID>.
    - Refuses if repo_root itself (or any path component leading to it) is
      a symlink.  Uses O_EXCL / exist_ok=False so duplicate IDs cannot collide.

open_run(repo_root: Path, run_id: str) -> RunEvidence
    - Re-open an existing run.  Validates run_id format, refuses traversal /
      absolute / separator-containing IDs, refuses symlinked run directories.

freeze_inputs(run: RunEvidence, paths: Sequence[Path]) -> dict[str, str]
    - Record SHA-256 hashes of each input file atomically in inputs/manifest.json.
    - Refuses symlinked inputs.  Returns {absolute_path_str: sha256_hex}.

assert_inputs_unchanged(run: RunEvidence) -> None
    - Re-hashes every file listed in inputs/manifest.json and raises ValueError
      if any hash differs or any file is missing.

append_event(run: RunEvidence, event: Mapping) -> Path
    - Write a single JSON event file under events/ using exclusive creation
      (open with 'x').  Each call creates a unique numbered file.
    - Flushes and closes before returning; never overwrites a prior event.

save_inventory(run: RunEvidence, inventory: Mapping) -> None
    - Write/replace inventory.json atomically (temp-file + rename).

No SQL executed, no database connections, no imports beyond Python stdlib.
Requires Python 3.10+.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Run ID: YYYYMMDDTHHMMSSZ-<32 lowercase hex digits>
_RUN_ID_RE = re.compile(
    r"^\d{8}T\d{6}Z-[0-9a-f]{32}$", re.ASCII
)

_SCRATCH_SUBDIR = Path("scratch") / "dev-test"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass(frozen=True)
class RunEvidence:
    """Lightweight record pointing to one run's evidence directory.

    Attributes:
        run_id: YYYYMMDDTHHMMSSZ-<32 hex> run identifier.
        root:   Absolute path to the repository root (not the run directory).
        object_prefix: Uppercase DT_<12 hex>_ Oracle object prefix.
    """
    run_id: str
    root: Path
    object_prefix: str = ""

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        if not self.object_prefix:
            parts = self.run_id.split("-")
            if len(parts) >= 2 and len(parts[1]) >= 12:
                prefix = f"DT_{parts[1][:12].upper()}_"
            else:
                prefix = f"DT_{uuid.uuid4().hex[:12].upper()}_"
            object.__setattr__(self, "object_prefix", prefix)

    @property
    def run_dir(self) -> Path:
        """Absolute path to the run evidence directory."""
        return self.root / _SCRATCH_SUBDIR / self.run_id

    @property
    def events_dir(self) -> Path:
        return self.run_dir / "events"

    @property
    def inputs_dir(self) -> Path:
        return self.run_dir / "inputs"

    @property
    def snapshots_dir(self) -> Path:
        return self.inputs_dir / "snapshots"

    @property
    def _manifest_path(self) -> Path:
        return self.inputs_dir / "manifest.json"

    @property
    def _inventory_path(self) -> Path:
        return self.run_dir / "inventory.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate_object_prefix() -> str:
    """Generate a random DT_<12 uppercase hex>_ object prefix."""
    return f"DT_{uuid.uuid4().hex[:12].upper()}_"


def make_object_name(prefix: str, suffix: str) -> str:
    """Combine object prefix and suffix; enforce 30-byte Oracle identifier budget."""
    if not isinstance(prefix, str) or re.fullmatch(r"DT_[0-9A-F]{12}_", prefix, re.ASCII) is None:
        raise ValueError(f"Invalid object prefix: {prefix!r}")
    if not isinstance(suffix, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,13}", suffix, re.ASCII) is None:
        raise ValueError("Invalid object suffix")
    name = f"{prefix}{suffix}"
    if len(name.encode("utf-8")) > 30:
        raise ValueError(
            f"Object identifier {name!r} exceeds 30-byte budget "
            f"({len(name.encode('utf-8'))} bytes)"
        )
    return name


def _reject_symlink_components(path: Path) -> None:
    """Fail closed if path or any existing component along its path is a symlink or reparse point."""
    candidate = Path(os.path.abspath(path))
    # Check every component from filesystem root to candidate
    parts = candidate.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        try:
            meta = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError(f"Cannot inspect path component: {current}") from exc
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if current.is_symlink() or bool(getattr(meta, "st_file_attributes", 0) & reparse_flag):
            raise ValueError(f"Refused: path contains a symbolic link or reparse point: {current}")


def _check_scratch_symlinks(repo_root: Path) -> None:
    """Ensure neither repo_root/scratch nor repo_root/scratch/dev-test are symlinks."""
    scratch = repo_root / "scratch"
    if scratch.is_symlink():
        raise ValueError(f"Refused: scratch directory is a symbolic link: {scratch}")
    devtest = scratch / "dev-test"
    if devtest.is_symlink():
        raise ValueError(f"Refused: dev-test directory is a symbolic link: {devtest}")


def _mkdir_private(path: Path, exist_ok: bool = False) -> None:
    """Create directory with 0o700 private permissions."""
    _reject_symlink_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=exist_ok)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass


def _chmod_private_file(path: Path) -> None:
    """Set 0o600 private file permissions on POSIX systems."""
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _generate_run_id() -> str:
    """Return a fresh YYYYMMDDTHHMMSSZ-<32 hex> run ID."""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4().hex  # 32 lowercase hex chars
    return f"{timestamp}-{uid}"


def _validate_run_id(run_id: str) -> None:
    """Raise ValueError for any unsafe or malformed run_id."""
    if not isinstance(run_id, str):
        raise ValueError(f"run_id must be a string, got {type(run_id).__name__}")
    if "/" in run_id or "\\" in run_id:
        raise ValueError(f"run_id must not contain path separators: {run_id!r}")
    if run_id.startswith("/") or (len(run_id) >= 2 and run_id[1] == ":"):
        raise ValueError(f"run_id must not be an absolute path: {run_id!r}")
    if ".." in run_id.split("-"):
        raise ValueError(f"run_id must not contain traversal: {run_id!r}")
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(
            f"run_id {run_id!r} does not match YYYYMMDDTHHMMSSZ-<32hex> format"
        )


def _atomic_write_json(target: Path, data: object) -> None:
    """Write *data* as JSON to *target* atomically with private permissions."""
    _reject_symlink_components(target)
    _mkdir_private(target.parent, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_str = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        tmp_path = Path(tmp_str)
        _chmod_private_file(tmp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(target)
        _chmod_private_file(target)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write raw bytes to *target* atomically with private permissions."""
    _reject_symlink_components(target)
    _mkdir_private(target.parent, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_str = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        tmp_path = Path(tmp_str)
        _chmod_private_file(tmp_path)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(target)
        _chmod_private_file(target)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _has_write_intent(run: RunEvidence) -> bool:
    """Return True if any event in events/ is an intent event."""
    if not run.events_dir.exists():
        return False
    for ev_path in run.events_dir.glob("*.json"):
        try:
            ev_data = json.loads(ev_path.read_text(encoding="utf-8"))
            if not isinstance(ev_data, dict):
                raise ValueError("Write history contains an invalid event record")
            if ev_data.get("type") in {"read", "plan", "observation"} and ev_data.get("writeStarted", False) is False:
                continue
            # A missing or unfamiliar event kind cannot prove that no write began.
            return True
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Cannot establish write history from corrupt event evidence") from exc
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_run(repo_root: Path) -> RunEvidence:
    """Allocate a fresh run evidence directory with private permissions.

    Args:
        repo_root: Path to the repository root. Must not be (or traverse) a symlink.

    Returns:
        A RunEvidence for the new run.

    Raises:
        ValueError: If repo_root, scratch, or dev-test is or contains a symlink.
        FileExistsError: If the generated run directory already exists.
        OSError: On filesystem errors.
    """
    repo_root_raw = Path(repo_root)
    if repo_root_raw.is_symlink():
        raise ValueError(f"Refused: repo_root is a symbolic link: {repo_root_raw}")

    _reject_symlink_components(repo_root_raw)
    repo_root = Path(os.path.abspath(repo_root_raw))
    _check_scratch_symlinks(repo_root)

    run_id = _generate_run_id()
    run = RunEvidence(run_id=run_id, root=repo_root)

    # Refuse if run directory already exists
    if run.run_dir.exists():
        raise FileExistsError(f"Refused: run directory already exists: {run.run_dir}")

    # Create directories exclusively with private permissions (0o700)
    _mkdir_private(run.root / _SCRATCH_SUBDIR, exist_ok=True)
    _mkdir_private(run.run_dir, exist_ok=False)
    _mkdir_private(run.events_dir, exist_ok=False)
    _mkdir_private(run.inputs_dir, exist_ok=False)
    _mkdir_private(run.snapshots_dir, exist_ok=False)
    _mkdir_private(run.run_dir / "sessions", exist_ok=False)
    _mkdir_private(run.run_dir / "project", exist_ok=False)

    return run


def open_run(repo_root: Path, run_id: str) -> RunEvidence:
    """Open an existing run evidence directory.

    Args:
        repo_root: Path to the repository root.
        run_id:    The run identifier returned by a previous create_run call.

    Returns:
        A RunEvidence pointing to the existing run directory.

    Raises:
        ValueError: If run_id is malformed or the directory is a symlink.
        FileNotFoundError: If the run directory does not exist.
        OSError: On filesystem errors.
    """
    repo_root_raw = Path(repo_root)
    if repo_root_raw.is_symlink():
        raise ValueError(f"Refused: repo_root is a symbolic link: {repo_root_raw}")

    _validate_run_id(run_id)
    _reject_symlink_components(repo_root_raw)
    repo_root = Path(os.path.abspath(repo_root_raw))
    _check_scratch_symlinks(repo_root)

    run = RunEvidence(run_id=run_id, root=repo_root)
    run_dir = run.run_dir

    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    if run_dir.is_symlink():
        raise ValueError(f"Refused: run directory is a symbolic link: {run_dir}")

    _reject_symlink_components(run_dir)

    return run


def freeze_inputs(run: RunEvidence, paths: Sequence[Path]) -> dict[str, str]:
    """Hash and snapshot each input file; store the manifest and byte snapshots atomically.

    Args:
        run:   The RunEvidence handle.
        paths: Input files to freeze (SQL, manifest JSON, check files, etc.).

    Returns:
        A dict mapping str(absolute_path) -> sha256_hex for each supplied file.

    Raises:
        ValueError:       If any path is a symlink, or if write intent has already occurred.
        FileNotFoundError: If any path does not exist.
        OSError:          On read/write errors.
    """
    if _has_write_intent(run):
        raise ValueError(
            "Refused: cannot freeze or modify inputs after a write intent event has been recorded"
        )

    _mkdir_private(run.snapshots_dir, exist_ok=True)

    result: dict[str, str] = {}
    snapshots: dict[str, str] = {}

    for p in paths:
        p_raw = Path(p)
        if p_raw.is_symlink():
            raise ValueError(f"freeze_inputs refuses symbolic link: {p_raw}")

        p_abs = Path(os.path.abspath(p_raw))
        if p_abs.is_symlink():
            raise ValueError(f"freeze_inputs refuses symbolic link: {p_abs}")

        data = p_abs.read_bytes()
        digest = _sha256_hex(data)
        path_key = str(p_abs)
        result[path_key] = digest

        # Write byte snapshot atomically into inputs/snapshots/<digest>.bin
        snapshot_filename = f"{digest}.bin"
        snapshot_target = run.snapshots_dir / snapshot_filename
        if not snapshot_target.exists():
            _atomic_write_bytes(snapshot_target, data)
        snapshots[path_key] = f"snapshots/{snapshot_filename}"

    manifest_payload = {
        "schemaVersion": 1,
        "hashes": result,
        "snapshots": snapshots,
    }
    _atomic_write_json(run._manifest_path, manifest_payload)
    return result


def assert_inputs_unchanged(run: RunEvidence) -> None:
    """Re-hash frozen inputs and verify both disk files and stored byte snapshots.

    Args:
        run: The RunEvidence handle. Must have a valid inputs/manifest.json.

    Raises:
        ValueError:        If any file's hash differs from the snapshot or snapshot is tampered.
        FileNotFoundError: If a frozen file no longer exists.
        OSError:           On filesystem errors.
    """
    try:
        manifest_raw = json.loads(run._manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read frozen input manifest: {exc}") from exc

    if (not isinstance(manifest_raw, dict)
            or set(manifest_raw) != {"schemaVersion", "hashes", "snapshots"}
            or type(manifest_raw["schemaVersion"]) is not int
            or manifest_raw["schemaVersion"] != 1):
        raise ValueError("Invalid manifest structure")
    hashes = manifest_raw["hashes"]
    snapshots = manifest_raw["snapshots"]
    if not isinstance(hashes, dict) or not isinstance(snapshots, dict) or set(hashes) != set(snapshots):
        raise ValueError("Frozen input hashes and snapshots must match")

    for path_str, expected_hash in hashes.items():
        if not isinstance(path_str, str) or not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise ValueError("Invalid frozen input hash record")
        p = Path(path_str)
        _reject_symlink_components(p)
        current_data = p.read_bytes()
        current_hash = _sha256_hex(current_data)
        if current_hash != expected_hash:
            raise ValueError(
                f"Frozen input changed: {path_str}\n"
                f"  expected sha256={expected_hash}\n"
                f"  current  sha256={current_hash}"
            )

        # Also verify snapshot byte file integrity
        if snapshots[path_str] != f"snapshots/{expected_hash}.bin":
            raise ValueError("Invalid frozen snapshot path")
        snap_path = run.snapshots_dir / f"{expected_hash}.bin"
        _reject_symlink_components(snap_path)
        if not snap_path.is_file():
            raise ValueError("Frozen snapshot is missing")
        snap_data = snap_path.read_bytes()
        if _sha256_hex(snap_data) != expected_hash or snap_data != current_data:
            raise ValueError(f"Frozen snapshot corrupted for {path_str}")


def append_event(run: RunEvidence, event: Mapping) -> Path:
    """Write a single event dict as a JSON file under events/ using exclusive creation.

    Args:
        run:   The RunEvidence handle.
        event: Any JSON-serializable mapping to record.

    Returns:
        The Path of the newly written event file.

    Raises:
        OSError: On filesystem errors.
    """
    _mkdir_private(run.events_dir, exist_ok=True)

    existing = sorted(run.events_dir.glob("*.json"))
    seq = len(existing)
    uid = uuid.uuid4().hex[:8]
    filename = f"{seq:06d}-{uid}.json"
    event_path = run.events_dir / filename

    content = json.dumps(dict(event), indent=2, default=str) + "\n"
    encoded = content.encode("utf-8")

    fd = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        _chmod_private_file(event_path)

    return event_path


def save_inventory(run: RunEvidence, inventory: Mapping) -> None:
    """Write/replace inventory.json atomically with private permissions.

    Args:
        run:       The RunEvidence handle.
        inventory: Any JSON-serializable mapping.

    Raises:
        OSError: On filesystem errors.
    """
    _atomic_write_json(run._inventory_path, dict(inventory))
