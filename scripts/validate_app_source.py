#!/usr/bin/env python3
"""Ensure a publish source tree is physically contained in its repository."""

from __future__ import annotations

import os
import re
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

    # publish_app.sql receives this path as a SQLcl substitution argument. A
    # quote ends the value early and '&' starts another substitution, so the
    # import would read a different, truncated path.
    unsafe = sorted({character for character in str(candidate) if character in "'\"&"})
    if unsafe:
        raise ValueError(
            "the application path contains characters SQLcl cannot pass to its import script "
            f"({' '.join(unsafe)}); move the repository to a path without them: {candidate}"
        )

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
            if name.casefold() == "login.sql":
                raise ValueError(f"SQLcl startup file is not allowed in an APEXlang source tree: {entry}")
            if _is_reparse_point(entry):
                raise ValueError(f"symbolic links or reparse points are not supported in an APEXlang source tree: {entry}")

    return candidate


def validate_import_effects(source_dir: Path) -> None:
    """Refuse Oracle's automatic supporting-object mode before an app import.

    Database scripts must use the separately reviewed migration workflow. This
    deliberately conservative check also refuses the setting inside comments
    or script text: move that text out of the APEX source before publication.
    """
    for path in source_dir.rglob('*.apx'):
        text = path.read_text(encoding='utf-8')
        if re.search(r'(?m)^\s*includeInAppExport\s*:\s*["\']?autoInstall(?:["\']?\s|$)', text):
            raise ValueError('automatic supporting-object execution is not authorized by app publication; apply reviewed schema migrations separately and remove autoInstall')


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) not in (2, 3) or (len(arguments) == 3 and arguments[2] != '--for-import'):
        print("usage: validate_app_source.py <repository-root> <app-source-directory> [--for-import]", file=sys.stderr)
        return 2
    try:
        source = validate_app_source(Path(arguments[0]), Path(arguments[1]))
        if len(arguments) == 3:
            validate_import_effects(source)
    except (OSError, ValueError) as exc:
        print(f"application source validation error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
