"""Bounded retention for scratch working directories and SQLcl run files.

scratch/ holds one full application export per capture or deployment, one
directory per SQLcl statement issued to the metadata target, and four files per
SQLcl process. Retention is deliberate for recent application evidence, so
captures are pruned by age while anything a recovery record still points at is
pinned. Per-statement metadata and master-check directories are transient by
construction and are removed regardless of age.

Evidence under .sync-state/ is durable and is never touched by this command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Any


class PruneError(RuntimeError):
    """Raised when scratch retention cannot be applied safely."""


_RUN_FILE_GLOBS = (".team-driver-*", ".team-payload-*", ".team-stdin-*", ".team-sqlcl-*")
# One directory per captured or imported application, retained by age.
_CAPTURE_GLOBS = (
    "apex-capture-*", "apex-import-*", "deploy-capture-*", "deploy-import-*",
)
# One directory per SQLcl statement. These are transient by construction -- the
# evidence a recovery record depends on lives in .sync-state -- so they are
# removed regardless of `keep`. Recovery evidence under .sync-state/ is never
# touched by this command.
_TRANSIENT_WORK_GLOBS = (
    "metadata/control-*", "metadata/migration-*", "master-checks/*",
)


def _referenced_work_dirs(state_root: Path) -> set[str]:
    """Collect every work_dir a retained recovery record still points at."""
    referenced: set[str] = set()
    recovery = state_root / "recovery"
    if not recovery.is_dir():
        return referenced
    for path in recovery.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        stack: list[Any] = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                work_dir = current.get("work_dir")
                if isinstance(work_dir, str) and work_dir:
                    referenced.add(str(Path(work_dir).resolve()))
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    return referenced


def prune_scratch(repo: str | Path, *, keep: int = 5, dry_run: bool = False) -> dict[str, Any]:
    """Remove old capture directories and every SQLcl run file."""
    if not isinstance(keep, int) or keep < 0:
        raise PruneError("keep must be a non-negative integer")
    root = Path(repo)
    scratch = root / "scratch"
    if not scratch.is_dir():
        return {"removed": 0, "would_remove": 0, "kept": 0, "pinned": 0}
    if scratch.is_symlink():
        raise PruneError("scratch must not be a symbolic link")

    referenced = _referenced_work_dirs(root / ".sync-state")
    captures: list[Path] = []
    for pattern in _CAPTURE_GLOBS:
        captures.extend(path for path in scratch.glob(pattern) if path.is_dir() and not path.is_symlink())
    captures.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    pinned = [path for path in captures if str(path.resolve()) in referenced]
    unpinned = [path for path in captures if str(path.resolve()) not in referenced]
    doomed = unpinned[keep:]

    run_files = [
        path
        for pattern in _RUN_FILE_GLOBS
        for path in scratch.rglob(pattern)
        if path.is_file() and not path.is_symlink()
    ]
    transient = [
        path
        for pattern in _TRANSIENT_WORK_GLOBS
        for path in scratch.glob(pattern)
        if path.is_dir() and not path.is_symlink() and str(path.resolve()) not in referenced
    ]

    if dry_run:
        return {
            "removed": 0,
            "would_remove": len(doomed) + len(run_files) + len(transient),
            "kept": len(unpinned) - len(doomed),
            "pinned": len(pinned),
        }

    removed = 0
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    for path in transient:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    for path in run_files:
        path.unlink(missing_ok=True)
        removed += 1
    return {
        "removed": removed,
        "would_remove": 0,
        "kept": len(unpinned) - len(doomed),
        "pinned": len(pinned),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prune-scratch")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--keep", type=int, default=5, help="capture directories to retain")
    parser.add_argument("--dry-run", action="store_true")
    raw = list(argv or [])
    if raw and raw[0] == "prune-scratch":
        raw = raw[1:]
    args = parser.parse_args(raw)
    report = prune_scratch(args.repo, keep=args.keep, dry_run=args.dry_run)
    print(json.dumps({"status": "success", "operation": "prune-scratch", **report}, sort_keys=True))
    return 0
