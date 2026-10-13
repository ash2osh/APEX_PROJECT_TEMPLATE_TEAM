"""Path comparisons that tolerate equivalent Windows 8.3 aliases safely."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class PathReparseError(ValueError):
    """A path under the matched root contains a symbolic link or reparse point."""


def _reject_reparse_points_under(path: Path, matched_root: Path) -> None:
    """Fail closed on reparse points at or below the matched repository root."""
    current = matched_root
    candidates = [current]
    for part in path.relative_to(matched_root).parts:
        current = current / part
        candidates.append(current)
    for current in candidates:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError(f"cannot inspect path component: {current}") from error
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if current.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag):
            raise PathReparseError(f"path contains a symbolic link or reparse point: {current}")


def relative_to_root(path: Path | str, root: Path | str) -> Path:
    """Return ``path`` relative to ``root``, accepting a same-directory 8.3 alias.

    Windows may hand a temporary path to a process through its short name while
    another process resolved the same directory to its long name. A lexical
    comparison rejects those equivalent paths. The fallback uses filesystem
    identity and checks the original path at and below the matched root so a
    junction inside the repository cannot smuggle an outside path into it.
    """
    candidate = Path(os.path.abspath(path))
    base = Path(os.path.abspath(root))
    try:
        return candidate.relative_to(base)
    except ValueError as lexical_error:
        outside_error = lexical_error

    suffix: list[str] = []
    ancestor = candidate
    while True:
        try:
            same_root = os.path.samefile(ancestor, base)
        except OSError:
            same_root = False
        if same_root:
            _reject_reparse_points_under(candidate, ancestor)
            return Path(*reversed(suffix)) if suffix else Path(".")
        if ancestor.parent == ancestor:
            break
        suffix.append(ancestor.name)
        ancestor = ancestor.parent
    raise ValueError(f"{path!s} is not under {root!s}") from outside_error
