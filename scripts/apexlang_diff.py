#!/usr/bin/env python3
"""Collect sanitized, strict APEXlang source mismatches."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.deployment_descriptor import remap_subscription_source, source_projection


IGNORED_SOURCE_PATHS = {"apex-team-export.json"}
EMPTY_MAY_BE_MISSING = {Path("supporting-objects/deinstall-script.sql")}
REQUIRED_SOURCE_PATHS = (Path("application.apx"), Path(".apex/apexlang.json"))


@dataclass(frozen=True)
class SourceMismatch:
    path: str
    kind: str
    first_differing_line: int | None = None


def source_files(root: Path) -> dict[Path, Path]:
    """List the same canonical files used by strict publish verification."""
    if not root.is_dir():
        raise ValueError(f"APEXlang directory does not exist: {root}")
    files: dict[Path, Path] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == "deployments" or relative.as_posix() in IGNORED_SOURCE_PATHS:
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic links are not supported in an APEXlang export: {path}")
        if path.is_file():
            files[relative] = path
    return files


def first_differing_line(expected: bytes, observed: bytes) -> int | None:
    """Return a one-based line number without decoding or exposing either value."""
    left = expected.split(b"\n")
    right = observed.split(b"\n")
    for index in range(max(len(left), len(right))):
        if index >= len(left) or index >= len(right) or left[index] != right[index]:
            return index + 1
    return None


def display_path(path: str) -> str:
    """Escape control characters so a filename cannot forge diagnostic lines."""
    return "".join(
        character if character.isprintable() and character != "\\" else f"\\u{ord(character):04x}"
        for character in path
    )


def collect_source_mismatches(
    source_dir: Path,
    exported_dir: Path,
    deployment: dict | None = None,
) -> tuple[SourceMismatch, ...]:
    """Report all file-set and byte mismatches using only relative paths.

    The same narrow deployment projections as the existing verifier are applied:
    the top-level application name projection and the explicit subscription map.
    The exported bytes are never normalized or reordered.
    """
    source = source_files(source_dir)
    exported = source_files(exported_dir)
    mismatches: list[SourceMismatch] = []

    for required in REQUIRED_SOURCE_PATHS:
        if required not in source:
            mismatches.append(SourceMismatch(required.as_posix(), "required-missing"))

    for relative in sorted(source.keys() - exported.keys()):
        if relative in EMPTY_MAY_BE_MISSING and source[relative].stat().st_size == 0:
            continue
        mismatches.append(SourceMismatch(relative.as_posix(), "missing"))

    for relative in sorted(exported.keys() - source.keys()):
        mismatches.append(SourceMismatch(relative.as_posix(), "unexpected"))

    for relative in sorted(source.keys() & exported.keys()):
        expected = source[relative].read_bytes()
        observed = exported[relative].read_bytes()
        if deployment is not None and relative == Path("application.apx"):
            expected = source_projection(expected, deployment)
            observed = source_projection(observed, deployment)
        if deployment is not None and relative.suffix == ".apx":
            expected = remap_subscription_source(expected, deployment)
        if expected != observed:
            mismatches.append(
                SourceMismatch(
                    relative.as_posix(),
                    "bytes",
                    first_differing_line(expected, observed),
                )
            )

    return tuple(sorted(mismatches, key=lambda item: (item.path, item.kind)))
