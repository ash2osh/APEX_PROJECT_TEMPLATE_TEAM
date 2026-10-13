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
from scripts.apexlang_diff import compare_source_files, display_path


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


class VerificationError(ValueError):
    def __init__(self, message: str, cosmetic_files: tuple[str, ...] = ()):
        super().__init__(message)
        self.cosmetic_files = cosmetic_files


def _describe_mismatches(mismatches) -> str:
    return ", ".join(
        display_path(item.path)
        + (f" (line {item.first_differing_line})" if item.first_differing_line else "")
        for item in mismatches
    )


def verify_source_bytes(
    source_dir: Path,
    exported_dir: Path,
    deployment: dict | None = None,
    *,
    strict_bytes: bool = False,
) -> tuple[str, ...]:
    mismatches, cosmetic = compare_source_files(
        source_dir,
        exported_dir,
        deployment,
        normalize_apx=not strict_bytes,
    )
    if not mismatches:
        return cosmetic

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
        details.append("byte differences: " + _describe_mismatches(changed))

    if missing or unexpected:
        prefix = "APEXlang source file set does not match the post-import re-export"
    elif required:
        prefix = "required APEXlang source file is missing"
    else:
        prefix = "APEXlang source bytes do not match the post-import re-export"
    raise VerificationError(prefix + ": " + "; ".join(details), cosmetic)


def verify_stable_reexport(
    app_id: int,
    first_dir: Path,
    second_dir: Path,
    first_before: Path,
    first_after: Path,
    second_before: Path,
    second_after: Path,
    deployment: dict | None = None,
    *,
    strict_bytes: bool = False,
) -> tuple[str, ...]:
    mismatches, cosmetic = compare_source_files(
        first_dir,
        second_dir,
        deployment,
        normalize_apx=not strict_bytes,
        remap_observed_subscription=True,
    )
    problems = []
    if mismatches:
        file_set = [item for item in mismatches if item.kind in {"missing", "unexpected"}]
        changed = [item for item in mismatches if item.kind in {"bytes", "required-missing"}]
        if file_set:
            problems.append("file set changed: " + _describe_mismatches(file_set))
        if changed:
            problems.append("source changed: " + _describe_mismatches(changed))

    first_revision = read_verified_revision(app_id, first_before, first_after)
    second_revision = read_verified_revision(app_id, second_before, second_after)
    _, first_end = read_state(first_after)
    second_start_state, second_start = read_state(second_before)
    if first_revision != second_revision:
        problems.append("application revision changed between re-exports")
    if second_start < first_end:
        problems.append("database time moved backwards between post-import re-exports")
    if second_start_state != second_revision:
        problems.append("second re-export revision does not match its initial observation")

    if problems:
        raise VerificationError(
            "post-import APEXlang source was not stable across the second post-import re-export: "
            + "; ".join(problems),
            cosmetic,
        )
    return cosmetic


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
    parser.add_argument("--strict-bytes", action="store_true", help="require byte-for-byte APEXlang content equality")
    parser.add_argument("--stability-export-dir", type=Path)
    parser.add_argument("--stability-before-file", type=Path)
    parser.add_argument("--stability-after-file", type=Path)
    parser.add_argument("--stability-deployment-state", type=Path)
    parser.add_argument("--target-apex-release-before", type=Path)
    parser.add_argument("--target-apex-release-after", type=Path)
    parser.add_argument("--stability-target-apex-release-before", type=Path)
    parser.add_argument("--stability-target-apex-release-after", type=Path)
    parser.add_argument("--summary-path", type=Path)
    args = parser.parse_args(argv)
    if args.app_id < 1:
        parser.error("app_id must be a positive integer")

    source_dir: Path | None = None
    target_release: str | None = None
    summary: dict | None = None
    cosmetic_files: list[str] = []
    try:
        target_release = _target_apex_release(args.target_apex_release_before, args.target_apex_release_after)
        source_dir = validate_app_source(args.repo_root, args.source_dir)
        exported_dir = args.exported_dir.resolve(strict=True)
        stability_args = (args.stability_export_dir, args.stability_before_file, args.stability_after_file)
        if any(value is not None for value in stability_args) and any(value is None for value in stability_args):
            raise ValueError("all second post-import re-export paths are required together")
        if args.record_baseline and args.stability_export_dir is None:
            raise ValueError("baseline recording requires a stable second post-import re-export")
        first_release_files = (args.target_apex_release_before, args.target_apex_release_after)
        stable_release_files = (
            args.stability_target_apex_release_before,
            args.stability_target_apex_release_after,
        )
        has_stability_export = args.stability_export_dir is not None
        has_first_release = any(value is not None for value in first_release_files)
        has_stable_release = any(value is not None for value in stable_release_files)
        if has_stability_export and has_first_release != has_stable_release:
            raise ValueError("both post-import exports require APEX release observations")
        if not has_stability_export and has_stable_release:
            raise ValueError("second APEX release observations require a second post-import export")
        stable_target_release = _target_apex_release(*stable_release_files)
        if has_stability_export and target_release is not None and stable_target_release != target_release:
            raise ValueError("target APEX release changed between post-import exports")

        deployment = None
        if args.deployment_file is not None:
            deployment = read_descriptor(args.deployment_file, args.app_id)
            if args.deployment_state is None:
                raise ValueError("effective deployment observation is required")
            observe_deployment(args.deployment_state, exported_dir / "deployments/default.json", deployment)
        try:
            cosmetic_files.extend(
                verify_source_bytes(source_dir, exported_dir, deployment, strict_bytes=args.strict_bytes)
            )
            if args.stability_export_dir is not None:
                stability_dir = args.stability_export_dir.resolve(strict=True)
                if deployment is not None:
                    if args.stability_deployment_state is None:
                        raise ValueError("second effective deployment observation is required")
                    observe_deployment(
                        args.stability_deployment_state,
                        stability_dir / "deployments/default.json",
                        deployment,
                    )
                cosmetic_files.extend(
                    verify_stable_reexport(
                        args.app_id,
                        exported_dir,
                        stability_dir,
                        args.before_file,
                        args.after_file,
                        args.stability_before_file,
                        args.stability_after_file,
                        deployment,
                        strict_bytes=args.strict_bytes,
                    )
                )
        except VerificationError as mismatch:
            cosmetic_files.extend(mismatch.cosmetic_files)
            source_provenance = read_source_provenance(source_dir)
            pair = VersionPair(
                source_provenance.source_release if source_provenance.status == "verified" else None,
                target_release,
            )
            raise VerificationError(
                f"{mismatch}{replaced_import_hint(source_dir, exported_dir)}; "
                f"version context: {describe_version_pair(pair)} "
                f"(source provenance {source_provenance.status})",
                tuple(cosmetic_files),
            ) from mismatch
        revision = read_verified_revision(args.app_id, args.before_file, args.after_file)
        summary = _publish_summary(source_dir, target_release, True)
        if args.summary_path is not None:
            _write_publish_summary(args.summary_path, args.repo_root, summary)
        if args.record_baseline:
            advance_baseline(args.app_id, source_dir, revision)
    except (OSError, ValueError) as exc:
        if isinstance(exc, VerificationError) and exc.cosmetic_files:
            print("APEXlang cosmetic-only differences: " + ", ".join(sorted(set(exc.cosmetic_files))))
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
    if cosmetic_files:
        print("APEXlang cosmetic-only differences: " + ", ".join(sorted(set(cosmetic_files))))
    print(f"APEX_PUBLISH_SOURCE_VERIFIED:{args.app_id}")
    print(f"APEX_VERSION_PAIR:{summary['versionPair']} (source provenance {summary['sourceProvenanceStatus']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
