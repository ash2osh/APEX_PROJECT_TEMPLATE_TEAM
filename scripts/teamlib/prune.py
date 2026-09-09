"""Bounded retention for scratch working directories and SQLcl run files.

scratch/ holds one full application export per capture and four files per SQLcl
process, and nothing removed them. Retention is deliberate for recent evidence,
so this prunes by age while pinning anything a recovery record still points at.
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
_CAPTURE_GLOBS = ("apex-capture-*", "apex-import-*")


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

    if dry_run:
        return {
            "removed": 0,
            "would_remove": len(doomed) + len(run_files),
            "kept": len(unpinned) - len(doomed),
            "pinned": len(pinned),
        }

    removed = 0
    for path in doomed:
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
