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

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.record_export_state import AppState, marker_payload, read_apex_release, read_state
from scripts.path_utils import relative_to_root
from scripts.source_provenance import (
    VersionPair,
    describe_version_pair,
    preserve_source_provenance,
    read_source_provenance,
)
from scripts.stamp_publish_version import PUBLISH_TAG, VERSION_LINE, parse_version
from scripts.validate_app_source import validate_app_source
from scripts.deployment_descriptor import observe_deployment, read_descriptor
from scripts.apexlang_diff import collect_source_mismatches, display_path


def publish_tag(application: Path) -> str | None:
    """The DEV publish tag in an application.apx version line, if it has one."""
    try:
        match = VERSION_LINE.search(application.read_text(encoding="utf-8"))
        tag = PUBLISH_TAG.search(parse_version(match.group(1))) if match else None
    except (OSError, UnicodeError, ValueError):
        return None
    return tag.group(0).strip() if tag else None


def replaced_import_hint(source_dir: Path, exported_dir: Path) -> str:
    """Name the likely cause when the live app carries another publish's tag.

    Two developers who publish within one import's duration both pass the drift
    check; the later import wins and the earlier one fails this verification.
    """
    ours = publish_tag(source_dir / "application.apx")
    live = publish_tag(exported_dir / "application.apx")
    if ours is None or live is None or ours == live:
        return ""
    return (
        f"; the live publish tag is {live}, not the {ours} this publish stamped, "
        "so a teammate's import replaced yours: export, merge, publish"
    )


def verify_source_bytes(source_dir: Path, exported_dir: Path, deployment: dict | None = None) -> None:
    mismatches = collect_source_mismatches(source_dir, exported_dir, deployment)
    if not mismatches:
        return

    required = [item for item in mismatches if item.kind == "required-missing"]
    missing = [item.path for item in mismatches if item.kind == "missing"]
    unexpected = [item.path for item in mismatches if item.kind == "unexpected"]
    changed = [item for item in mismatches if item.kind == "bytes"]
    details = []
    if missing:
        details.append("missing from re-export: " + ", ".join(display_path(path) for path in missing))
    if unexpected:
        details.append("unexpected in re-export: " + ", ".join(display_path(path) for path in unexpected))
    if required:
        details.append("required source missing: " + ", ".join(display_path(item.path) for item in required))
    if changed:
        details.append(
            "byte differences: "
            + ", ".join(
                display_path(item.path)
                + (f" (first differing line {item.first_differing_line})" if item.first_differing_line else "")
                for item in changed
            )
        )

    if missing or unexpected:
        prefix = "APEXlang source file set does not match the post-import re-export"
    elif required:
        prefix = "required APEXlang source file is missing"
    else:
        prefix = "APEXlang source bytes do not match the post-import re-export"
    raise ValueError(prefix + ": " + "; ".join(details))


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

    # Legacy markers remain usable for full workflows. A real 26.2 canonical
    # source additionally records raw content evidence required by partial mode.
    metadata = json.loads((source_dir / ".apex/apexlang.json").read_text(encoding="utf-8"))
    canonical_source = source_dir if isinstance(metadata, dict) and "mmdVersion" in metadata else None
    payload = json.dumps(
        marker_payload(
            app_id,
            revision,
            canonical_source,
            source_provenance=preserve_source_provenance(source_dir),
        ),
        indent=2,
    ) + "\n"
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


def _target_apex_release(before_file: Path | None, after_file: Path | None) -> str | None:
    if before_file is None and after_file is None:
        return None
    if before_file is None or after_file is None:
        raise ValueError("both target APEX release observations are required")
    before_release, before_time = read_apex_release(before_file)
    after_release, after_time = read_apex_release(after_file)
    if before_release != after_release:
        raise ValueError("target APEX release changed during post-import export; rerun verification")
    if datetime.fromisoformat(before_time.replace("Z", "+00:00")) > datetime.fromisoformat(after_time.replace("Z", "+00:00")):
        raise ValueError("database UTC time moved backwards during post-import export")
    return after_release


def _publish_summary(source_dir: Path, target_release: str | None, source_verified: bool) -> dict:
    provenance = read_source_provenance(source_dir)
    pair = VersionPair(
        provenance.source_release if provenance.status == "verified" else None,
        target_release,
    )
    return {
        "schemaVersion": 1,
        "sourceVerified": source_verified,
        "lifecycleStatus": "unavailable",
        "sourceProvenanceStatus": provenance.status,
        "sourceApexRelease": provenance.source_release if provenance.status == "verified" else None,
        "targetApexRelease": target_release,
        "versionPair": describe_version_pair(pair),
    }


def _write_publish_summary(path: Path, repo_root: Path, payload: dict) -> None:
    root = repo_root.resolve(strict=True)
    output = Path(os.path.abspath(path if path.is_absolute() else root / path))
    try:
        relative = relative_to_root(output, root)
    except (OSError, ValueError) as error:
        raise ValueError("publish verification summary must remain inside the repository") from error
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("publish verification summary path contains a symbolic link")
    if output.exists() and output.is_symlink():
        raise ValueError("publish verification summary must not be a symbolic link")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
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
    parser.add_argument("--deployment-file", type=Path)
    parser.add_argument("--deployment-state", type=Path)
    parser.add_argument("--target-apex-release-before", type=Path)
    parser.add_argument("--target-apex-release-after", type=Path)
    parser.add_argument("--summary-path", type=Path)
    args = parser.parse_args(argv)
    if args.app_id < 1:
        parser.error("app_id must be a positive integer")

    source_dir: Path | None = None
    target_release: str | None = None
    summary: dict | None = None
    try:
        target_release = _target_apex_release(args.target_apex_release_before, args.target_apex_release_after)
        source_dir = validate_app_source(args.repo_root, args.source_dir)
        exported_dir = args.exported_dir.resolve(strict=True)
        deployment = None
        if args.deployment_file is not None:
            deployment = read_descriptor(args.deployment_file, args.app_id)
            if args.deployment_state is None:
                raise ValueError("effective deployment observation is required")
            observe_deployment(args.deployment_state, exported_dir / "deployments/default.json", deployment)
        try:
            verify_source_bytes(source_dir, exported_dir, deployment)
        except ValueError as mismatch:
            source_provenance = read_source_provenance(source_dir)
            pair = VersionPair(
                source_provenance.source_release if source_provenance.status == "verified" else None,
                target_release,
            )
            raise ValueError(
                f"{mismatch}{replaced_import_hint(source_dir, exported_dir)}; "
                f"version context: {describe_version_pair(pair)} "
                f"(source provenance {source_provenance.status})"
            ) from mismatch
        revision = read_verified_revision(args.app_id, args.before_file, args.after_file)
        summary = _publish_summary(source_dir, target_release, True)
        if args.summary_path is not None:
            _write_publish_summary(args.summary_path, args.repo_root, summary)
        if args.record_baseline:
            advance_baseline(args.app_id, source_dir, revision)
    except (OSError, ValueError) as exc:
        if args.summary_path is not None and source_dir is not None:
            try:
                _write_publish_summary(args.summary_path, args.repo_root, _publish_summary(source_dir, target_release, False))
            except (OSError, ValueError):
                pass
        print(f"publish verification error: {exc}", file=sys.stderr)
        return 1

    if summary is None:
        print("publish verification error: source verification summary is unavailable", file=sys.stderr)
        return 1
    print(f"APEX_PUBLISH_SOURCE_VERIFIED:{args.app_id}")
    print(f"APEX_VERSION_PAIR:{summary['versionPair']} (source provenance {summary['sourceProvenanceStatus']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
