#!/usr/bin/env python3
"""Offline feature scaffolding, DEV package capture, review diffs, and capsule cleanup."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import date
from pathlib import Path
from collections.abc import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import feature_sources, release_capsule, release_state, rollout
from scripts.feature_manifest import FEATURE_RE, SCHEMA_RE, FeatureManifestError, load_feature_checkpoint
from scripts.path_utils import relative_to_root


class FeatureCliError(ValueError):
    """A feature command is incomplete, unsafe, or unavailable."""


def _root(repo_root: Path) -> Path:
    requested = Path(repo_root)
    if requested.is_symlink():
        raise FeatureCliError("repository root cannot be a symlink")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise FeatureCliError(f"repository root is unavailable: {error}") from error
    if not root.is_dir():
        raise FeatureCliError("repository root must be a directory")
    return root


def _init_feature(repo_root: Path, feature: str, schema: str) -> tuple[Path, ...]:
    if FEATURE_RE.fullmatch(feature) is None:
        raise FeatureCliError("feature must be a lowercase hyphenated slug")
    if SCHEMA_RE.fullmatch(schema) is None:
        raise FeatureCliError("schema must be an uppercase Oracle identifier")
    root = _root(repo_root)
    schema_dir, _scratch_dir = feature_sources._feature_scope(root, feature, schema)
    feature_dir = root / "features" / feature
    readme_path = feature_dir / "README.md"
    checks_path = schema_dir / "checks.json"
    readme_contents = feature_sources._ordinary_file(readme_path, root)
    existing_checks = feature_sources._ordinary_file(checks_path, root)
    if existing_checks is not None:
        raise FeatureCliError(
            f"feature scaffold refuses to overwrite existing file: {relative_to_root(checks_path, root)}"
        )

    outputs: dict[Path, bytes] = {}
    if readme_contents is None:
        outputs[readme_path] = (
            f"# {feature}\n\n"
            f"Feature source for schema `{schema}`. Keep package files whole and edit them in place.\n"
            "Replace the scaffold check with a reviewed condition before preparing a release.\n"
        ).encode()
    outputs[checks_path] = (
        b'{\n'
        b'  "schemaVersion": 1,\n'
        b'  "preconditions": [],\n'
        b'  "postconditions": [\n'
        b'    {\n'
        b'      "id": "replace-me",\n'
        b'      "sql": "SELECT 1 FROM dual WHERE 1 = 0",\n'
        b'      "expected": 1\n'
        b'    }\n'
        b'  ]\n'
        b'}\n'
    )

    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        feature_sources._ensure_directory(feature_dir, root)
        feature_sources._ensure_directory(schema_dir, root)
        for path, raw in outputs.items():
            stream = path.open("xb")
            with stream:
                descriptor_metadata = os.fstat(stream.fileno())
                path_metadata = path.lstat()
                file_identity = (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                if file_identity != (path_metadata.st_dev, path_metadata.st_ino):
                    raise FeatureCliError(f"feature scaffold path changed during creation: {relative_to_root(path, root)}")
                created.append((path, file_identity))
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception as error:
        for path, file_identity in reversed(created):
            try:
                metadata = path.lstat()
                if (
                    feature_sources._is_reparse(path, metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != file_identity
                ):
                    continue
                path.unlink()
            except OSError:
                pass
        if isinstance(error, (FeatureCliError, feature_sources.FeatureSourcesError)):
            raise
        raise FeatureCliError(f"feature scaffold failed; created files were removed: {error}") from error
    return tuple(path for path, _identity in created)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create an offline feature scaffold")
    init_parser.add_argument("feature", help="lowercase feature slug")
    init_parser.add_argument("--schema", required=True, help="uppercase Oracle schema")

    capture_parser = subparsers.add_parser("capture", help="capture a complete package from DEV")
    capture_parser.add_argument("feature", help="lowercase feature slug")
    capture_parser.add_argument("--package", required=True, help="package identifier")
    capture_parser.add_argument("--schema", required=True, help="uppercase Oracle schema")
    capture_parser.add_argument("--env", required=True, action="append", choices=("dev",), help="package capture is DEV-only")
    capture_parser.add_argument("--refresh", action="store_true", help="replace source and its retained DEV baseline after review")

    diff_parser = subparsers.add_parser("diff", help="write package diffs against the retained DEV capture")
    diff_parser.add_argument("feature", help="lowercase feature slug")
    diff_parser.add_argument("--schema", required=True, help="uppercase Oracle schema")
    diff_parser.add_argument("--day", default=date.today().isoformat(), help="review directory date, YYYY-MM-DD")

    freeze_parser = subparsers.add_parser("freeze", help="validate committed inputs and build a frozen release capsule")
    freeze_parser.add_argument("manifest", type=Path, help="schema-version 2 rollout manifest")

    clean_parser = subparsers.add_parser("clean-capsule", help="preview or remove an eligible local capsule")
    clean_parser.add_argument("payload_digest", help="64-character capsule payload digest")
    clean_parser.add_argument("--yes", action="store_true", help="apply eligible local cleanup")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    environment = os.environ if environ is None else environ
    try:
        root = _root(args.repo_root if args.repo_root is not None else repo_root)
        if args.command == "init":
            created = _init_feature(root, args.feature, args.schema)
            for path in created:
                print(f"Created {relative_to_root(path, root).as_posix()}")
            print("Replace the scaffold postcondition with a reviewed check before release.")
            return 0
        if args.command == "capture":
            if len(args.env) != 1:
                raise FeatureCliError("feature capture accepts only one --env value")
            paths = feature_sources.capture_package(
                root, args.feature, args.schema, args.package,
                environ=environment, refresh=args.refresh,
            )
            for path in paths:
                print(f"Captured {relative_to_root(path, root).as_posix()}")
            return 0
        if args.command == "diff":
            artifacts = feature_sources.diff_captured_package(
                root, args.feature, args.schema, day=args.day,
            )
            for path in artifacts:
                print(f"Wrote {relative_to_root(path, root).as_posix()}")
            return 0
        if args.command == "freeze":
            manifest = rollout.load_manifest(args.manifest, root, version=2)
            checkpoints = tuple(
                load_feature_checkpoint(root, step)
                for step in manifest.steps
                if step["type"] == "feature-migrate"
            )
            capsule = release_capsule.build_capsule(root, manifest.path, checkpoints, {})
            print(f"Payload SHA-256: {capsule.payload_digest}")
            print(f"Workflow engine SHA-256: {capsule.engine_digest}")
            print(f"Source commit: {capsule.source_commit}")
            print(f"Capsule: {relative_to_root(capsule.root, root).as_posix()}")
            return 0
        if args.command == "clean-capsule":
            result = release_state.cleanup_capsule(root, args.payload_digest, apply=args.yes)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        raise FeatureCliError(f"unsupported feature command: {args.command}")
    except (
        FeatureCliError, feature_sources.FeatureSourcesError, FeatureManifestError,
        release_state.ReleaseStateError, release_capsule.ReleaseCapsuleError, rollout.RolloutError,
    ) as error:
        print(f"feature error: {error}", file=sys.stderr)
        if isinstance(error, release_state.ReleaseStateError) and str(error).startswith("capsule cleanup refused:"):
            return 1
        return 2


if __name__ == "__main__":
    raise SystemExit(main(repo_root=Path(__file__).resolve().parents[1]))
