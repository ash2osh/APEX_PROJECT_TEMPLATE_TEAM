#!/usr/bin/env python3
"""Ensure a publish source tree is physically contained in its repository."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"application source path is unavailable: {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def validate_app_source(repo_root: Path, source_dir: Path) -> Path:
    """Return the source directory after rejecting links and checkout escapes."""
    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"repository root is unavailable: {repo_root}: {exc}") from exc
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")

    candidate = source_dir if source_dir.is_absolute() else root / source_dir
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"application source is outside the repository: {candidate}") from exc

    current = root
    for component in relative.parts:
        current = current / component
        if _is_reparse_point(current):
            raise ValueError(f"symbolic links or reparse points are not supported in an APEXlang source tree: {current}")

    if not candidate.is_dir():
        raise ValueError(f"APEXlang application directory does not exist: {candidate}")

    def fail_walk(error: OSError) -> None:
        raise ValueError(f"cannot inspect the complete APEXlang source tree: {error}") from error

    for current_name, directory_names, file_names in os.walk(
        candidate,
        topdown=True,
        onerror=fail_walk,
        followlinks=False,
    ):
        current_dir = Path(current_name)
        for name in directory_names + file_names:
            entry = current_dir / name
            if _is_reparse_point(entry):
                raise ValueError(f"symbolic links or reparse points are not supported in an APEXlang source tree: {entry}")

    return candidate


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print("usage: validate_app_source.py <repository-root> <app-source-directory>", file=sys.stderr)
        return 2
    try:
        validate_app_source(Path(arguments[0]), Path(arguments[1]))
    except (OSError, ValueError) as exc:
        print(f"application source validation error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
