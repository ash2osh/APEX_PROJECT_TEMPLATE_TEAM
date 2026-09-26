#!/usr/bin/env python3
"""Verify a post-import APEXlang export and advance its DEV drift baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from record_export_state import AppState, marker_payload, read_state
from validate_app_source import validate_app_source


IGNORED_SOURCE_PATHS = {"apex-team-export.json"}


def source_files(root: Path) -> dict[Path, Path]:
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


def verify_source_bytes(source_dir: Path, exported_dir: Path) -> None:
    source_files_by_path = source_files(source_dir)
    exported_files_by_path = source_files(exported_dir)
    missing = sorted(source_files_by_path.keys() - exported_files_by_path.keys())
    unexpected = sorted(exported_files_by_path.keys() - source_files_by_path.keys())
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing from re-export: " + ", ".join(path.as_posix() for path in missing))
        if unexpected:
            details.append("unexpected in re-export: " + ", ".join(path.as_posix() for path in unexpected))
        raise ValueError("APEXlang source file set does not match the post-import re-export (" + "; ".join(details) + ")")

    for relative, source_path in source_files_by_path.items():
        if source_path.read_bytes() != exported_files_by_path[relative].read_bytes():
            raise ValueError(f"APEXlang source bytes do not match the post-import re-export: {relative.as_posix()}")

    for required in (Path("application.apx"), Path(".apex/apexlang.json")):
        if required not in source_files_by_path:
            raise ValueError(f"required APEXlang source file is missing: {required.as_posix()}")


def read_verified_revision(app_id: int, before_file: Path, after_file: Path) -> AppState:
    before, before_observed_at = read_state(before_file)
    after, after_observed_at = read_state(after_file)
    if not before.present or not after.present:
        raise ValueError(f"APEX App {app_id} is not visible in the post-import state")
    if before != after:
        raise ValueError(
            f"APEX App {app_id} changed while its post-import source was being verified "
            f"(before: {before.describe()}, after: {after.describe()})"
        )
    if before_observed_at > after_observed_at:
        raise ValueError("database time moved backwards during post-import verification")
    # An APEXlang import leaves last_updated_on NULL, so a freshly imported app
    # has no Builder timestamp to compare; only a Builder edit would set one.
    if after.last_updated_on is None:
        return after
    if datetime.fromisoformat(after.last_updated_on) > after_observed_at:
        raise ValueError("APEX last_updated_on is later than the database-time observation")
    if after.last_updated_on in (
        before_observed_at.isoformat(timespec="seconds"),
        after_observed_at.isoformat(timespec="seconds"),
    ):
        raise ValueError(
            f"APEX App {app_id} has a last_updated_on value in the same database second "
            "as post-import verification; the revision is ambiguous"
        )
    return after


def advance_baseline(app_id: int, source_dir: Path, revision: AppState) -> None:
    marker_path = source_dir / "apex-team-export.json"
    if marker_path.is_symlink():
        raise ValueError(f"refusing to replace a symbolic-link export marker: {marker_path}")
    if marker_path.exists() and not marker_path.is_file():
        raise ValueError(f"export marker path is not a regular file: {marker_path}")

    payload = json.dumps(marker_payload(app_id, revision), indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".apex-team-export.", suffix=".tmp", dir=source_dir)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, marker_path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_id", type=int)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("exported_dir", type=Path)
    parser.add_argument("before_file", type=Path)
    parser.add_argument("after_file", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--record-baseline", action="store_true")
    args = parser.parse_args(argv)
    if args.app_id < 1:
        parser.error("app_id must be a positive integer")

    try:
        source_dir = validate_app_source(args.repo_root, args.source_dir)
        exported_dir = args.exported_dir.resolve(strict=True)
        verify_source_bytes(source_dir, exported_dir)
        revision = read_verified_revision(args.app_id, args.before_file, args.after_file)
        if args.record_baseline:
            advance_baseline(args.app_id, source_dir, revision)
    except (OSError, ValueError) as exc:
        print(f"publish verification error: {exc}", file=sys.stderr)
        return 1

    print(f"APEX_PUBLISH_SOURCE_VERIFIED:{args.app_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
