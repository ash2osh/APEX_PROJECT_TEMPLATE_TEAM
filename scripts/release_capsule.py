#!/usr/bin/env python3
"""Freeze a validated release manifest into an immutable, data-only capsule."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import baseline, directory_install, rollout
from .feature_manifest import (
    FeatureCheckpoint, FeatureInput, FeatureManifestError, _source_unit,
    load_feature_checkpoint, validate_release_units,
)
from .migration_manifest import QueryCheck
from .path_utils import relative_to_root
from .validate_migration import statement_spans


CAPSULE_SCHEMA_VERSION = 1
HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z", re.ASCII)
WORKFLOW_ENGINE_FILES = (
    "scripts/feature_manifest.py",
    "scripts/feature_sources.py",
    "scripts/feature_preflight.py",
    "scripts/release_ownership.py",
    "scripts/release_capsule.py",
    "scripts/release_state.py",
    "scripts/feature_release.py",
    "scripts/feature_cli.py",
)
ENGINE_HELPER_FILES = (
    "scripts/rollout.py",
    "scripts/verify_checks.py",
    "scripts/migration_manifest.py",
    "scripts/migration_attempts.py",
    "scripts/migration_checks.py",
    "scripts/dev_test_bindings.py",
    "scripts/validate_migration.py",
    "scripts/db_targets.py",
    "scripts/baseline.py",
    "scripts/baseline_bundle.py",
    "scripts/new_migration.py",
    "scripts/migrate.py",
    "scripts/migration_diff.py",
    "scripts/migration_rehearsal.py",
    "scripts/migration_revision.py",
    "scripts/migration_runtime.py",
    "scripts/sqlcl_version.py",
    "scripts/sqlcl_session.py",
    "scripts/windows_job.py",
    "scripts/sqlcl_transport.py",
    "scripts/path_utils.py",
    "scripts/directory_install.py",
    "scripts/deployment_descriptor.py",
    "scripts/validate_app_source.py",
    "scripts/ords_export.py",
    "scripts/compare_schema.py",
    "scripts/schema_catalog.py",
    "scripts/schema_normalization.py",
    "scripts/migrate.sql",
    "scripts/verify_migration_access.sql",
    "scripts/compare_env_catalog.sql",
    "scripts/schema_catalog.sql",
    # The existing app-deploy path is part of capsule execution too. Bind its
    # Bash adapters, Python helpers, and trusted SQL files so stage and prod
    # cannot silently run different deployment code under one engine digest.
    "scripts/deploy.sh",
    "scripts/publish_app.sh",
    "scripts/load_env.sh",
    "scripts/sqlcl_safe.sh",
    "scripts/check_db_target.sh",
    "scripts/lookup_app_schema.sql",
    "scripts/application_lifecycle.py",
    "scripts/application_lifecycle.sql",
    "scripts/check_builder_drift.py",
    "scripts/check_builder_drift.sql",
    "scripts/stamp_publish_version.py",
    "scripts/publish_app.sql",
    "scripts/publish_identity_guard.sql",
    "scripts/export_apps.sql",
    "scripts/normalize_apx.sh",
    "scripts/verify_publish_state.py",
)
SETTING_PREFIX_RE = re.compile(r"(?is)^\s*CREATE(?:\s+OR\s+REPLACE)?\s+")


class ReleaseCapsuleError(ValueError):
    """A release capsule cannot be built or loaded without changing its evidence."""


@dataclass(frozen=True)
class ReleaseCapsule:
    root: Path
    manifest_sha256: str
    payload_digest: str
    engine_digest: str
    source_commit: str
    checkpoints: tuple[FeatureCheckpoint, ...]
    input_hashes: tuple[dict, ...]
    database_object_budget_seconds: int | None = None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def _root_path(repo_root: Path) -> Path:
    requested = Path(repo_root)
    if requested.is_symlink():
        raise ReleaseCapsuleError("repository root cannot be a symlink")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise ReleaseCapsuleError(f"repository root is unavailable: {error}") from error
    if not root.is_dir():
        raise ReleaseCapsuleError("repository root must be a directory")
    return root


def _engine_file_list(repo_root: Path) -> tuple[str, ...]:
    """Return the complete ordered release runtime and trusted-template set."""
    root = Path(repo_root)
    ordered = (*WORKFLOW_ENGINE_FILES, *ENGINE_HELPER_FILES)
    present: list[str] = []
    for relative in ordered:
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            raise ReleaseCapsuleError(f"required engine file is missing: {relative}")
        _assert_ordinary_path(root, relative)
        present.append(relative)
    return tuple(present)


def _engine_digest_for_files(repo_root: Path, files: Sequence[str]) -> str:
    rows: list[dict[str, str]] = []
    for relative in files:
        path = Path(repo_root) / relative
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ReleaseCapsuleError(f"engine file is unavailable: {relative}") from error
        rows.append({"path": relative, "sha256": _sha256(raw)})
    if not rows:
        raise ReleaseCapsuleError("release capsule engine file list is empty")
    return _sha256(_canonical_bytes(rows))


def capsule_engine_digest(repo_root: Path) -> str:
    """Hash the ordered workflow engine and the helpers it calls."""
    root = _root_path(repo_root)
    return _engine_digest_for_files(root, _engine_file_list(root))


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ReleaseCapsuleError(f"{label} must be a nonempty forward-slash relative path")
    parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 for char in value)
        or re.match(r"^[A-Za-z]:", value, re.ASCII)
    ):
        raise ReleaseCapsuleError(f"{label} is not a safe relative path: {value}")
    return path.as_posix()


def _assert_ordinary_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    relative = _safe_relative(relative, "capsule input")
    path = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    try:
        root_info = current.lstat()
        if _is_reparse(current, root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ReleaseCapsuleError("repository root is not an ordinary directory")
        for part in PurePosixPath(relative).parts:
            current = current / part
            info = current.lstat()
            if _is_reparse(current, info):
                raise ReleaseCapsuleError(f"symlink or reparse point is not allowed in release inputs: {relative}")
        if directory and not stat.S_ISDIR(info.st_mode):
            raise ReleaseCapsuleError(f"release input is not a directory: {relative}")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise ReleaseCapsuleError(f"release input is not an ordinary file: {relative}")
        relative_to_root(path.resolve(strict=True), root)
    except ReleaseCapsuleError:
        raise
    except (OSError, ValueError) as error:
        raise ReleaseCapsuleError(f"release input is missing or unsafe: {relative}") from error
    return path


def _is_reparse(path: Path, info: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)


def _read_input(root: Path, relative: str) -> bytes:
    path = _assert_ordinary_path(root, relative)
    try:
        return path.read_bytes()
    except OSError as error:
        raise ReleaseCapsuleError(f"cannot read selected release input {relative}: {error}") from error


def _git_bytes(root: Path, commit: str, relative: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", f"{commit}:{relative}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ReleaseCapsuleError(f"cannot verify committed release input {relative}: {error}") from error
    if result.returncode != 0:
        raise ReleaseCapsuleError(f"selected release input is not committed at HEAD: {relative}")
    return result.stdout


def _require_committed(root: Path, commit: str, relative: str, source: bytes) -> None:
    if _git_bytes(root, commit, relative) != source:
        raise ReleaseCapsuleError(f"selected release input differs from committed Git bytes: {relative}")


def _head_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ReleaseCapsuleError(f"cannot identify the committed release source: {error}") from error
    commit = result.stdout.strip()
    if result.returncode != 0 or COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseCapsuleError("release capsules require a committed Git HEAD")
    return commit


def _check_paths(step: Mapping) -> tuple[str, ...]:
    if "checks" in step:
        values = step["checks"]
    else:
        features: list[str] = []
        for value in step["inputs"]:
            parts = PurePosixPath(value).parts
            if len(parts) < 4:
                raise ReleaseCapsuleError("feature input path has no feature namespace")
            if parts[1] not in features:
                features.append(parts[1])
        values = [f"features/{feature}/{step['schema']}/checks.json" for feature in features]
    if not isinstance(values, list) or not values:
        raise ReleaseCapsuleError("feature checkpoint has no selected check files")
    return tuple(_safe_relative(value, "feature check path") for value in values)


def _step_definition(step: Mapping) -> dict:
    kind = step["type"]
    if kind == "feature-migrate":
        return {"type": kind, "schema": step["schema"], "inputs": list(step["inputs"]), "checks": list(_check_paths(step))}
    if kind == "app-deploy":
        return {"type": kind, "appId": step["appId"]}
    if kind == "ords-import":
        return {
            "type": kind, "file": step["file"], "schema": step["schema"],
            "excludeModules": list(step.get("excludeModules", [])),
        }
    if kind == "sql-script":
        return {"type": kind, "file": step["file"], "schema": step["schema"]}
    if kind in {"migrate", "verify"}:
        result = {"type": kind, "folders": list(step["folders"])}
        if kind == "verify":
            result["phase"] = step["phase"]
        return result
    if kind == "pause":
        return {"type": kind, "message": step["message"]}
    raise ReleaseCapsuleError(f"unsupported rollout step for capsule: {kind!r}")


def _selected_shared_paths(manifest: rollout.RolloutManifest) -> tuple[tuple[int, dict, Path], ...]:
    root = manifest.repo_root
    result: list[tuple[int, dict, Path]] = []
    for number, step in enumerate(manifest.steps, start=1):
        kind = step["type"]
        if kind == "feature-migrate" or kind == "pause":
            continue
        if kind == "app-deploy":
            try:
                app_dir = rollout._find_app_directory(root, step["appId"])
                from scripts.validate_app_source import validate_app_source

                validate_app_source(root, app_dir)
                paths = rollout._directory_input_paths(app_dir, root, "staging", app_source=True)
            except (OSError, ValueError, rollout.RolloutError) as error:
                raise ReleaseCapsuleError(f"app-deploy source is invalid in step {number}: {error}") from error
            for path in paths:
                relative = relative_to_root(path, root).as_posix()
                if PurePosixPath(relative).parts[3:4] == ("deployments",):
                    continue
                result.append((number, step, path))
            continue
        try:
            paths = rollout._step_input_paths(step, root, "staging")
        except (OSError, ValueError, rollout.RolloutError) as error:
            raise ReleaseCapsuleError(f"rollout source is invalid in step {number}: {error}") from error
        for path in paths:
            relative = relative_to_root(path, root).as_posix()
            if step.get("type") in {"migrate", "verify"} and re.fullmatch(r"status\.(?:dev|staging|prod)\.json", path.name):
                continue
            result.append((number, step, path))
    return tuple(result)


def _check_step_sources(
    root: Path,
    manifest: rollout.RolloutManifest,
    checkpoints: Sequence[FeatureCheckpoint],
) -> tuple[dict[str, bytes], list[dict], list[dict]]:
    feature_steps = [step for step in manifest.steps if step["type"] == "feature-migrate"]
    if len(feature_steps) != len(checkpoints):
        raise ReleaseCapsuleError("validated feature checkpoint count does not match the rollout manifest")
    selected: dict[str, bytes] = {}
    checkpoint_records: list[dict] = []
    input_mappings: list[dict] = []
    for checkpoint_number, (step, supplied_checkpoint) in enumerate(zip(feature_steps, checkpoints, strict=True), start=1):
        try:
            validated = load_feature_checkpoint(root, step)
        except ValueError as error:
            raise ReleaseCapsuleError(f"feature checkpoint {checkpoint_number} failed revalidation: {error}") from error
        if validated != supplied_checkpoint:
            raise ReleaseCapsuleError(f"feature checkpoint {checkpoint_number} changed after validation")
        step_inputs: list[dict] = []
        for input_number, item in enumerate(validated.inputs, start=1):
            relative = _safe_relative(item.path, "feature source path")
            source = _read_input(root, relative)
            if source != item.source or _sha256(source) != item.sha256:
                raise ReleaseCapsuleError(f"feature source changed after validation: {relative}")
            if relative in selected and selected[relative] != source:
                raise ReleaseCapsuleError(f"selected feature source has conflicting bytes: {relative}")
            selected[relative] = source
            step_inputs.append({"path": relative, "sha256": item.sha256, "unit": list(item.unit) if item.unit else None, "category": item.category})
            input_mappings.append({
                "step": checkpoint_number,
                "input": input_number,
                "sourcePath": f"payload/{relative}",
                "sourceSha256": item.sha256,
                "unit": list(item.unit) if item.unit else None,
                "category": item.category,
            })
        check_paths = _check_paths(step)
        check_records = []
        for check_path in check_paths:
            source = _read_input(root, check_path)
            selected[check_path] = source
            check_records.append({"path": check_path, "sha256": _sha256(source)})
        checkpoint_records.append({
            "number": checkpoint_number,
            "schema": validated.schema,
            "inputs": step_inputs,
            "checks": check_records,
            "preconditions": [_check_to_dict(check) for check in validated.preconditions],
            "postconditions": [_check_to_dict(check) for check in validated.postconditions],
            "objectKeys": [list(key) for key in validated.object_keys],
        })
    try:
        validate_release_units(tuple(
            load_feature_checkpoint(root, step) for step in feature_steps
        ))
    except FeatureManifestError as error:
        raise ReleaseCapsuleError(f"feature release contains invalid stored source identities: {error}") from error
    return selected, checkpoint_records, input_mappings


def _check_to_dict(check: QueryCheck) -> dict:
    return {"id": check.id, "sql": check.sql, "expected": check.expected}


def _shared_sources(
    root: Path,
    manifest: rollout.RolloutManifest,
    shared_inputs: Mapping[str, bytes],
    selected: dict[str, bytes],
) -> tuple[list[dict], list[dict]]:
    enumerated: dict[str, tuple[int, dict]] = {}
    for number, step, path in _selected_shared_paths(manifest):
        relative = relative_to_root(path, root).as_posix()
        _assert_ordinary_path(root, relative)
        if relative in enumerated:
            continue
        source = _read_input(root, relative)
        if step["type"] in {"sql-script", "ords-import"}:
            try:
                text = source.decode("utf-8")
                if step["type"] == "sql-script":
                    rollout.validate_sql_script(text, number)
                else:
                    rollout.validate_ords_script(text, step["schema"], number)
            except (UnicodeError, ValueError, rollout.RolloutError) as error:
                raise ReleaseCapsuleError(f"selected script is invalid in step {number}: {error}") from error
        if relative in selected and selected[relative] != source:
            raise ReleaseCapsuleError(f"rollout source conflicts with another selected input: {relative}")
        selected[relative] = source
        enumerated[relative] = (number, step)

    supplied: dict[str, bytes] = {}
    if not isinstance(shared_inputs, Mapping):
        raise ReleaseCapsuleError("shared_inputs must be a path-to-bytes mapping")
    for raw_path, source in shared_inputs.items():
        relative = _safe_relative(raw_path, "shared input path")
        if not isinstance(source, bytes):
            raise ReleaseCapsuleError(f"shared input bytes are required: {relative}")
        if relative not in selected:
            raise ReleaseCapsuleError(f"shared input is not selected by the rollout manifest: {relative}")
        if selected[relative] != source:
            raise ReleaseCapsuleError(f"shared input bytes differ from the selected source: {relative}")
        supplied[relative] = source

    records: list[dict] = []
    mappings: list[dict] = []
    for relative, (number, step) in enumerated.items():
        source = selected[relative]
        payload_path = f"payload/{relative}"
        record = {"step": number, "sourcePath": payload_path, "sourceSha256": _sha256(source), "delivery": "copy", "driverPath": None, "driverSha256": None}
        if step["type"] == "app-deploy":
            record["delivery"] = "apex-source"
        elif step["type"] in {"sql-script", "ords-import"}:
            driver = source
            if step["type"] == "ords-import" and step.get("excludeModules"):
                try:
                    text, _removed = rollout.exclude_ords_modules(source.decode("utf-8"), step["excludeModules"], step["schema"])
                except (UnicodeError, ValueError, rollout.RolloutError) as error:
                    raise ReleaseCapsuleError(f"ORDS exclusions are invalid in step {number}: {error}") from error
                driver = text.encode("utf-8")
            driver_path = f"drivers/step-{number:03d}-{Path(relative).stem}.sql"
            record.update(delivery="ords-import" if step["type"] == "ords-import" else "sql-script", driverPath=driver_path, driverSha256=_sha256(driver))
            mappings.append({"sourcePath": payload_path, "sourceSha256": _sha256(source), "driverPath": driver_path, "driverSha256": _sha256(driver), "delivery": record["delivery"]})
        elif step["type"] in {"migrate", "verify"}:
            record["delivery"] = "legacy-migration-source"
        records.append(record)
    return records, mappings


def _stored_source_driver(item: FeatureInput) -> tuple[bytes, str]:
    if item.unit is None or item.category != "stored-source":
        raise ReleaseCapsuleError(f"feature source is not a validated stored unit: {item.path}")
    try:
        text = item.source.decode("utf-8")
        spans = statement_spans(text)
    except (UnicodeError, ValueError) as error:
        raise ReleaseCapsuleError(f"stored source cannot be read safely: {item.path}") from error
    if not spans:
        raise ReleaseCapsuleError(f"stored source is empty: {item.path}")
    body_start, body_end = spans[-1]
    body = text[body_start:body_end]
    match = SETTING_PREFIX_RE.match(body)
    if match is None:
        raise ReleaseCapsuleError(f"stored source has no CREATE statement: {item.path}")
    source_lines = body[match.end():].splitlines(keepends=True)
    if not source_lines:
        raise ReleaseCapsuleError(f"stored source body is empty: {item.path}")
    unit = {"type": item.unit[1], "name": item.unit[2], "lines": source_lines}
    if baseline._needs_clob_assembly(source_lines):
        driver = baseline._clob_source_step(unit, None)
        delivery = "clob"
    else:
        driver = baseline._direct_source_step(unit, None)
        delivery = "direct"
    prefix = text[:body_start]
    return (prefix + driver).encode("utf-8"), delivery


def _private_directory(path: Path, root: Path) -> None:
    try:
        relative_to_root(path, root)
    except ValueError as error:
        raise ReleaseCapsuleError(f"capsule directory is outside the repository: {path}") from error
    current = root
    try:
        info = current.lstat()
        if _is_reparse(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseCapsuleError("repository root must be an ordinary directory")
        parts = path.relative_to(root).parts
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                info = current.lstat()
            if _is_reparse(current, info) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseCapsuleError(f"capsule path contains an unsafe directory: {current}")
            # Do not change permissions on a pre-existing shared scratch
            # directory. The task-owned release-capsules namespace is private.
            if os.name != "nt" and index == len(parts) - 1:
                current.chmod(0o700)
    except ReleaseCapsuleError:
        raise
    except OSError as error:
        raise ReleaseCapsuleError(f"cannot prepare private capsule directory {path}: {error}") from error


def _write_stage_file(stage: Path, relative: str, source: bytes) -> None:
    relative = _safe_relative(relative, "capsule file path")
    destination = stage.joinpath(*PurePosixPath(relative).parts)
    current = stage
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        if _is_reparse(current, current.lstat()):
            raise ReleaseCapsuleError(f"capsule stage contains a reparse point: {current}")
    try:
        with destination.open("xb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            destination.chmod(0o600)
    except OSError as error:
        raise ReleaseCapsuleError(f"cannot write capsule file {relative}: {error}") from error


def _make_capsule(
    root: Path,
    manifest_path: Path | str,
    checkpoints: Sequence[FeatureCheckpoint],
    shared_inputs: Mapping[str, bytes],
) -> tuple[dict, dict[str, bytes], tuple[FeatureCheckpoint, ...], tuple[dict, ...], str, str]:
    try:
        manifest = rollout.load_manifest(manifest_path, root, version=2)
    except rollout.RolloutError as error:
        raise ReleaseCapsuleError(f"rollout manifest failed capsule validation: {error}") from error
    manifest_relative = relative_to_root(manifest.path, root).as_posix()
    manifest_raw = _read_input(root, manifest_relative)
    source_commit = _head_commit(root)
    _require_committed(root, source_commit, manifest_relative, manifest_raw)
    engine_files = _engine_file_list(root)
    engine_digest = _engine_digest_for_files(root, engine_files)

    selected, checkpoint_records, feature_mappings = _check_step_sources(root, manifest, checkpoints)
    for relative, source in selected.items():
        _require_committed(root, source_commit, relative, source)
    shared_records, _shared_mappings = _shared_sources(root, manifest, shared_inputs, selected)
    for relative, source in selected.items():
        _require_committed(root, source_commit, relative, source)

    payload_files: dict[str, bytes] = {f"payload/{path}": source for path, source in selected.items()}
    source_inputs = list(feature_mappings)
    driver_mappings: list[dict] = []

    for checkpoint_number, checkpoint in enumerate(checkpoints, start=1):
        for input_number, item in enumerate(checkpoint.inputs, start=1):
            if item.category == "stored-source":
                driver, delivery = _stored_source_driver(item)
            else:
                driver, delivery = item.source, "ddl"
            driver_path = f"drivers/checkpoint-{checkpoint_number:03d}-input-{input_number:03d}.sql"
            payload_files[driver_path] = driver
            source_record = source_inputs[sum(len(row["inputs"]) for row in checkpoint_records[:checkpoint_number - 1]) + input_number - 1]
            source_record.update({"driverPath": driver_path, "driverSha256": _sha256(driver), "delivery": delivery})

    for record in shared_records:
        if record["driverPath"] is not None:
            relative_source = record["sourcePath"].removeprefix("payload/")
            source = selected[relative_source]
            if record["delivery"] == "ords-import" and record["driverSha256"] != _sha256(source):
                text, _removed = rollout.exclude_ords_modules(source.decode("utf-8"), next(
                    step.get("excludeModules", []) for step in manifest.steps
                    if step["type"] == "ords-import" and step["file"] == relative_source
                ), next(
                    step["schema"] for step in manifest.steps
                    if step["type"] == "ords-import" and step["file"] == relative_source
                ))
                driver = text.encode("utf-8")
            else:
                driver = source
            payload_files[record["driverPath"]] = driver
            if _sha256(driver) != record["driverSha256"]:
                raise ReleaseCapsuleError(f"generated shared driver changed during capsule construction: {record['driverPath']}")
            source_inputs.append(record)
        else:
            source_inputs.append(record)

    driver_mappings = [
        {
            "sourcePath": record["sourcePath"],
            "sourceSha256": record["sourceSha256"],
            "driverPath": record["driverPath"],
            "driverSha256": record["driverSha256"],
            "delivery": record["delivery"],
        }
        for record in source_inputs
        if record["driverPath"] is not None
    ]

    manifest_path_in_capsule = f"payload/{manifest_relative}"
    payload_files[manifest_path_in_capsule] = manifest_raw
    file_records = [
        {"path": path, "sha256": _sha256(source), "kind": "driver" if path.startswith("drivers/") else "source"}
        for path, source in sorted(payload_files.items())
    ]
    step_definitions = [_step_definition(step) for step in manifest.steps]
    payload_definition = {
        "schemaVersion": 1,
        "rolloutManifest": {"path": manifest_relative, "sha256": _sha256(manifest_raw)},
        "databaseObjectBudgetSeconds": manifest.database_object_budget_seconds,
        "engineDigest": engine_digest,
        "files": file_records,
        "steps": step_definitions,
        "sourceToDriver": driver_mappings,
        "sourceInputs": source_inputs,
        "checkpoints": checkpoint_records,
    }
    payload_digest = _sha256(_canonical_bytes(payload_definition))
    capsule_manifest = {
        "schemaVersion": CAPSULE_SCHEMA_VERSION,
        "payloadDigest": payload_digest,
        "engineDigest": engine_digest,
        "engineFiles": list(engine_files),
        "sourceCommit": source_commit,
        "rolloutManifestPath": manifest_relative,
        "rolloutManifestSha256": _sha256(manifest_raw),
        "payloadDefinition": payload_definition,
        "files": file_records,
        "sourceInputs": source_inputs,
        "checkpoints": checkpoint_records,
    }
    input_hashes = tuple(
        {"path": record["path"], "sha256": record["sha256"]}
        for record in file_records
    )
    return capsule_manifest, payload_files, tuple(checkpoints), input_hashes, source_commit, payload_digest


def build_capsule(
    repo_root: Path,
    manifest_path: Path,
    checkpoints: Sequence[FeatureCheckpoint],
    shared_inputs: Mapping[str, bytes],
) -> ReleaseCapsule:
    """Build or reuse an exact committed release capsule with exclusive installation."""
    root = _root_path(repo_root)
    if not isinstance(checkpoints, Sequence):
        raise ReleaseCapsuleError("checkpoints must be an ordered sequence")
    capsule_manifest, payload_files, validated_checkpoints, input_hashes, source_commit, payload_digest = _make_capsule(
        root, manifest_path, checkpoints, shared_inputs,
    )
    capsule_raw = (json.dumps(capsule_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    capsule_dir = root / "scratch" / "release-capsules" / f"{payload_digest}-{source_commit[:12]}"
    parent = capsule_dir.parent
    _private_directory(parent, root)
    if capsule_dir.exists() or capsule_dir.is_symlink():
        existing = load_capsule(root, capsule_dir)
        if existing.payload_digest != payload_digest or existing.source_commit != source_commit:
            raise ReleaseCapsuleError(f"capsule destination is occupied by a different payload: {capsule_dir}")
        return existing

    stage = Path(tempfile.mkdtemp(prefix=".capsule-stage-", dir=parent))
    try:
        if os.name != "nt":
            stage.chmod(0o700)
        for relative, source in payload_files.items():
            _write_stage_file(stage, relative, source)
        _write_stage_file(stage, "capsule.json", capsule_raw)
        _validate_capsule_directory(root, stage, capsule_manifest, expected_engine_digest=capsule_manifest["engineDigest"])
        try:
            directory_install.install_directory_exclusive(stage, capsule_dir)
        except directory_install.DirectoryInstallError as error:
            if capsule_dir.exists():
                raise ReleaseCapsuleError(f"capsule destination became occupied; existing capsule was preserved: {capsule_dir}") from error
            raise ReleaseCapsuleError(f"capsule could not be installed exclusively: {error}") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return _capsule_from_manifest(root, capsule_dir, capsule_manifest, capsule_raw, validated_checkpoints, input_hashes)


def _read_capsule_json(path: Path) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if raw.startswith(b"\xef\xbb\xbf") or "\r" in text:
            raise ReleaseCapsuleError("capsule.json must be UTF-8/LF without a BOM")

        def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ReleaseCapsuleError(f"capsule.json contains duplicate key {key!r}")
                result[key] = value
            return result

        document = json.loads(text, object_pairs_hook=unique_pairs)
    except ReleaseCapsuleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseCapsuleError(f"cannot read capsule.json: {error}") from error
    if not isinstance(document, dict):
        raise ReleaseCapsuleError("capsule.json must contain a JSON object")
    canonical = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if raw != canonical:
        raise ReleaseCapsuleError("capsule.json is not in canonical UTF-8/LF form")
    return document, raw


def _validate_capsule_directory(
    repo_root: Path,
    capsule_dir: Path,
    document: dict,
    *,
    expected_engine_digest: str | None = None,
) -> None:
    required = {
        "schemaVersion", "payloadDigest", "engineDigest", "engineFiles", "sourceCommit",
        "rolloutManifestPath", "rolloutManifestSha256", "payloadDefinition", "files", "sourceInputs", "checkpoints",
    }
    if set(document) != required:
        raise ReleaseCapsuleError("capsule.json has unsupported or missing fields")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != CAPSULE_SCHEMA_VERSION:
        raise ReleaseCapsuleError("capsule.json schemaVersion must be integer 1")
    for field in ("payloadDigest", "engineDigest", "rolloutManifestSha256"):
        if not isinstance(document[field], str) or HASH_RE.fullmatch(document[field]) is None:
            raise ReleaseCapsuleError(f"capsule.json {field} must be a SHA-256 digest")
    if not isinstance(document["sourceCommit"], str) or COMMIT_RE.fullmatch(document["sourceCommit"]) is None:
        raise ReleaseCapsuleError("capsule.json sourceCommit is malformed")
    if not isinstance(document["engineFiles"], list) or any(not isinstance(item, str) for item in document["engineFiles"]):
        raise ReleaseCapsuleError("capsule.json engineFiles must be an ordered string list")
    if tuple(document["engineFiles"]) != _engine_file_list(repo_root):
        raise ReleaseCapsuleError("capsule engine file list differs from the current workflow")
    engine_digest = _engine_digest_for_files(repo_root, document["engineFiles"])
    if engine_digest != document["engineDigest"] or (expected_engine_digest and engine_digest != expected_engine_digest):
        raise ReleaseCapsuleError("capsule engine digest does not match the current workflow")
    manifest_relative = _safe_relative(document["rolloutManifestPath"], "rollout manifest path")
    if not isinstance(document["payloadDefinition"], dict) or not isinstance(document["files"], list):
        raise ReleaseCapsuleError("capsule payload definition or file list is malformed")
    payload_definition = document["payloadDefinition"]
    if set(payload_definition) != {
        "schemaVersion", "rolloutManifest", "databaseObjectBudgetSeconds", "engineDigest", "files", "steps",
        "sourceToDriver", "sourceInputs", "checkpoints",
    }:
        raise ReleaseCapsuleError("capsule payload definition has unsupported or missing fields")
    if payload_definition.get("schemaVersion") != 1 or payload_definition.get("engineDigest") != document["engineDigest"]:
        raise ReleaseCapsuleError("capsule payload definition version or engine digest is invalid")
    expected_payload = _sha256(_canonical_bytes(payload_definition))
    if expected_payload != document["payloadDigest"]:
        raise ReleaseCapsuleError("capsule payload digest does not match its canonical definition")
    rollout_source = payload_definition.get("rolloutManifest")
    if rollout_source != {"path": manifest_relative, "sha256": document["rolloutManifestSha256"]}:
        raise ReleaseCapsuleError("capsule rollout manifest identity is inconsistent")
    if not isinstance(document["sourceInputs"], list) or not isinstance(document["checkpoints"], list):
        raise ReleaseCapsuleError("capsule source mappings or checkpoints are malformed")
    if payload_definition["sourceInputs"] != document["sourceInputs"] or payload_definition["checkpoints"] != document["checkpoints"]:
        raise ReleaseCapsuleError("capsule checkpoint or source mappings differ from the digest-bound payload")
    if not isinstance(payload_definition["steps"], list) or not payload_definition["steps"]:
        raise ReleaseCapsuleError("capsule payload steps must be a nonempty ordered list")
    budget = payload_definition["databaseObjectBudgetSeconds"]
    if budget is not None and (type(budget) is not int or not 1 <= budget <= 300):
        raise ReleaseCapsuleError("capsule object budget is malformed")
    feature_step_count = sum(
        1 for step in payload_definition["steps"]
        if isinstance(step, dict) and step.get("type") == "feature-migrate"
    )
    if len(document["checkpoints"]) != feature_step_count:
        raise ReleaseCapsuleError("capsule checkpoint count does not match the ordered feature steps")

    file_map: dict[str, str] = {}
    for row in document["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "kind"}:
            raise ReleaseCapsuleError("capsule file record is malformed")
        relative = _safe_relative(row["path"], "capsule file record path")
        if not (relative.startswith("payload/") or relative.startswith("drivers/")):
            raise ReleaseCapsuleError(f"capsule file is outside payload/ or drivers/: {relative}")
        if not isinstance(row["sha256"], str) or HASH_RE.fullmatch(row["sha256"]) is None:
            raise ReleaseCapsuleError(f"capsule file hash is malformed: {relative}")
        expected_kind = "driver" if relative.startswith("drivers/") else "source"
        if row["kind"] != expected_kind or relative in file_map:
            raise ReleaseCapsuleError(f"capsule file record is duplicated or has an invalid kind: {relative}")
        file_map[relative] = row["sha256"]
    definition_files = payload_definition.get("files")
    if definition_files != document["files"]:
        raise ReleaseCapsuleError("capsule payload file records differ from the digest-bound definition")
    manifest_file = f"payload/{manifest_relative}"
    if file_map.get(manifest_file) != document["rolloutManifestSha256"]:
        raise ReleaseCapsuleError("capsule rollout manifest bytes are absent or have a different hash")

    actual_files: dict[str, Path] = {}
    actual_directories: set[str] = set()
    try:
        for current, directories, files in os.walk(capsule_dir, topdown=True, followlinks=False):
            directory = Path(current)
            for name in tuple(directories):
                entry = directory / name
                info = entry.lstat()
                if _is_reparse(entry, info) or not stat.S_ISDIR(info.st_mode):
                    raise ReleaseCapsuleError(f"capsule directory contains a symlink or reparse point: {entry}")
                actual_directories.add(entry.relative_to(capsule_dir).as_posix())
            for name in files:
                entry = directory / name
                info = entry.lstat()
                if _is_reparse(entry, info) or not stat.S_ISREG(info.st_mode):
                    raise ReleaseCapsuleError(f"capsule file is not an ordinary file: {entry}")
                relative = entry.relative_to(capsule_dir).as_posix()
                if relative != "capsule.json":
                    actual_files[relative] = entry
    except ReleaseCapsuleError:
        raise
    except OSError as error:
        raise ReleaseCapsuleError(f"cannot enumerate capsule files: {error}") from error
    if set(actual_files) != set(file_map):
        missing = sorted(set(file_map) - set(actual_files))
        extra = sorted(set(actual_files) - set(file_map))
        raise ReleaseCapsuleError(f"capsule file set is incomplete or has extra entries (missing={missing}, extra={extra})")
    expected_directories: set[str] = set()
    for relative in (*file_map, "capsule.json"):
        parts = PurePosixPath(relative).parts
        expected_directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    if actual_directories != expected_directories:
        raise ReleaseCapsuleError("capsule contains missing or extra directories")
    for relative, expected in file_map.items():
        if _sha256(actual_files[relative].read_bytes()) != expected:
            raise ReleaseCapsuleError(f"capsule file hash mismatch: {relative}")
    source_input_keys = {"step", "sourcePath", "sourceSha256", "delivery", "driverPath", "driverSha256"}
    expected_mappings: list[dict] = []
    for record in document["sourceInputs"]:
        if (
            not isinstance(record, dict)
            or not source_input_keys.issubset(record)
            or set(record) - source_input_keys not in (set(), {"input", "unit", "category"})
        ):
            raise ReleaseCapsuleError("capsule source-to-driver record is malformed")
        if type(record["step"]) is not int or record["step"] < 1:
            raise ReleaseCapsuleError("capsule source step number is malformed")
        if "input" in record and (type(record["input"]) is not int or record["input"] < 1):
            raise ReleaseCapsuleError("capsule feature input number is malformed")
        if not isinstance(record["sourceSha256"], str) or HASH_RE.fullmatch(record["sourceSha256"]) is None:
            raise ReleaseCapsuleError("capsule source hash is malformed")
        delivery = record["delivery"]
        if delivery not in {"ddl", "direct", "clob", "copy", "apex-source", "ords-import", "sql-script", "legacy-migration-source"}:
            raise ReleaseCapsuleError("capsule source delivery type is invalid")
        source_path = _safe_relative(record["sourcePath"], "source input path")
        if source_path not in file_map or file_map[source_path] != record["sourceSha256"]:
            raise ReleaseCapsuleError(f"capsule source mapping does not match its frozen bytes: {source_path}")
        driver_path = record["driverPath"]
        driver_hash = record["driverSha256"]
        if driver_path is not None:
            if delivery not in {"ddl", "direct", "clob", "ords-import", "sql-script"}:
                raise ReleaseCapsuleError("capsule source has a driver with an incompatible delivery type")
            if not isinstance(driver_hash, str) or HASH_RE.fullmatch(driver_hash) is None:
                raise ReleaseCapsuleError("capsule driver hash is malformed")
            driver_path = _safe_relative(driver_path, "source driver path")
            if driver_path not in file_map or file_map[driver_path] != driver_hash:
                raise ReleaseCapsuleError(f"capsule source mapping does not match its generated driver: {driver_path}")
            expected_mappings.append({
                "sourcePath": source_path,
                "sourceSha256": record["sourceSha256"],
                "driverPath": driver_path,
                "driverSha256": driver_hash,
                "delivery": delivery,
            })
        elif driver_hash is not None or delivery not in {"copy", "apex-source", "legacy-migration-source"}:
            raise ReleaseCapsuleError("capsule source has no required delivery driver")
        if ("unit" in record) != ("category" in record):
            raise ReleaseCapsuleError("capsule feature source identity is incomplete")
        if "unit" in record:
            unit = record["unit"]
            if unit is not None and (not isinstance(unit, list) or len(unit) != 3 or any(not isinstance(part, str) for part in unit)):
                raise ReleaseCapsuleError("capsule source unit identity is malformed")
            if record["category"] not in {"stored-source", "ddl"}:
                raise ReleaseCapsuleError("capsule source category is invalid")
    if payload_definition["sourceToDriver"] != expected_mappings:
        raise ReleaseCapsuleError("capsule source-to-driver mappings are inconsistent")


def _checkpoint_from_dict(root: Path, capsule_dir: Path, value: dict) -> FeatureCheckpoint:
    if set(value) != {"number", "schema", "inputs", "checks", "preconditions", "postconditions", "objectKeys"}:
        raise ReleaseCapsuleError("capsule checkpoint record has unsupported or missing fields")
    if type(value["number"]) is not int or not isinstance(value["schema"], str):
        raise ReleaseCapsuleError("capsule checkpoint number or schema is malformed")
    inputs: list[FeatureInput] = []
    for row in value["inputs"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "unit", "category"}:
            raise ReleaseCapsuleError("capsule checkpoint input record is malformed")
        source_path = _safe_relative(f"payload/{row['path']}", "checkpoint source path")
        source = (capsule_dir / source_path).read_bytes()
        if _sha256(source) != row["sha256"]:
            raise ReleaseCapsuleError(f"capsule checkpoint source hash mismatch: {row['path']}")
        unit = row["unit"]
        if unit is not None and (not isinstance(unit, list) or len(unit) != 3 or any(not isinstance(part, str) for part in unit)):
            raise ReleaseCapsuleError("capsule checkpoint stored unit identity is malformed")
        if row["category"] not in {"stored-source", "ddl"}:
            raise ReleaseCapsuleError("capsule checkpoint input category is invalid")
        if (row["category"] == "stored-source") != (unit is not None):
            raise ReleaseCapsuleError("capsule checkpoint input category and stored-unit identity disagree")
        item = FeatureInput(row["path"], source, row["sha256"], tuple(unit) if unit else None, row["category"])
        try:
            derived_unit = _source_unit(item, value["schema"])
        except (FeatureManifestError, UnicodeError, ValueError) as error:
            raise ReleaseCapsuleError(
                f"capsule stored unit identity cannot be verified against frozen source {row['path']}: {error}"
            ) from error
        if (
            (item.category == "stored-source" and derived_unit != item.unit)
            or (item.category == "ddl" and derived_unit is not None)
        ):
            raise ReleaseCapsuleError(
                f"capsule stored unit identity disagrees with frozen source bytes: {row['path']}"
            )
        inputs.append(item)

    check_files = value["checks"]
    if not isinstance(check_files, list) or not check_files:
        raise ReleaseCapsuleError("capsule checkpoint checks must be a nonempty array")
    for row in check_files:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ReleaseCapsuleError("capsule checkpoint check file record is malformed")
        check_path = _safe_relative(f"payload/{row['path']}", "checkpoint check path")
        check_source = (capsule_dir / check_path).read_bytes()
        if not isinstance(row["sha256"], str) or HASH_RE.fullmatch(row["sha256"]) is None or _sha256(check_source) != row["sha256"]:
            raise ReleaseCapsuleError(f"capsule checkpoint check hash mismatch: {row['path']}")

    def checks(field: str) -> tuple[QueryCheck, ...]:
        values = value[field]
        if not isinstance(values, list):
            raise ReleaseCapsuleError(f"capsule checkpoint {field} must be an array")
        result = []
        for row in values:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "sql", "expected"}
                or not isinstance(row["id"], str)
                or not isinstance(row["sql"], str)
                or type(row["expected"]) is not int
            ):
                raise ReleaseCapsuleError(f"capsule checkpoint {field} entry is malformed")
            result.append(QueryCheck(row["id"], row["sql"], row["expected"]))
        return tuple(result)

    object_keys = value["objectKeys"]
    if not isinstance(object_keys, list) or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(item, str) for item in pair) for pair in object_keys):
        raise ReleaseCapsuleError("capsule checkpoint objectKeys are malformed")
    return FeatureCheckpoint(value["schema"], tuple(inputs), checks("preconditions"), checks("postconditions"), tuple(tuple(pair) for pair in object_keys))


def _capsule_from_manifest(
    root: Path,
    capsule_dir: Path,
    document: dict,
    raw: bytes,
    checkpoints: Sequence[FeatureCheckpoint] | None = None,
    input_hashes: Sequence[dict] | None = None,
) -> ReleaseCapsule:
    loaded_checkpoints = tuple(checkpoints) if checkpoints is not None else tuple(
        _checkpoint_from_dict(root, capsule_dir, value) for value in document["checkpoints"]
    )
    try:
        validate_release_units(loaded_checkpoints)
    except FeatureManifestError as error:
        raise ReleaseCapsuleError(f"capsule contains invalid stored source identities: {error}") from error
    _validate_checkpoint_source_mappings(document, loaded_checkpoints)
    hashes = tuple(input_hashes) if input_hashes is not None else tuple(
        {"path": row["path"], "sha256": row["sha256"]} for row in document["files"]
    )
    return ReleaseCapsule(
        capsule_dir,
        _sha256(raw),
        document["payloadDigest"],
        document["engineDigest"],
        document["sourceCommit"],
        loaded_checkpoints,
        hashes,
        document["payloadDefinition"].get("databaseObjectBudgetSeconds"),
    )


def _validate_checkpoint_source_mappings(document: dict, checkpoints: Sequence[FeatureCheckpoint]) -> None:
    feature_records: dict[tuple[int, int], dict] = {}
    for record in document["sourceInputs"]:
        if "input" not in record:
            continue
        key = (record["step"], record["input"])
        if key in feature_records:
            raise ReleaseCapsuleError("capsule has duplicate feature checkpoint source mappings")
        feature_records[key] = record
    expected_keys: set[tuple[int, int]] = set()
    for checkpoint_number, checkpoint in enumerate(checkpoints, start=1):
        for input_number, item in enumerate(checkpoint.inputs, start=1):
            key = (checkpoint_number, input_number)
            expected_keys.add(key)
            record = feature_records.get(key)
            if record is None or (
                record["sourcePath"] != f"payload/{item.path}"
                or record["sourceSha256"] != item.sha256
                or record.get("unit") != (list(item.unit) if item.unit is not None else None)
                or record.get("category") != item.category
            ):
                raise ReleaseCapsuleError("capsule checkpoint source identity metadata is inconsistent")
    if set(feature_records) != expected_keys:
        raise ReleaseCapsuleError("capsule feature checkpoint source mappings are incomplete or unexpected")


def load_capsule(repo_root: Path, capsule_dir: Path) -> ReleaseCapsule:
    """Validate frozen capsule data and return it without executing any artifact."""
    root = _root_path(repo_root)
    requested = Path(capsule_dir)
    if not requested.is_absolute():
        requested = root / requested
    try:
        relative = relative_to_root(requested, root).as_posix()
    except ValueError as error:
        raise ReleaseCapsuleError("capsule directory must be inside the repository") from error
    if not relative.startswith("scratch/release-capsules/"):
        raise ReleaseCapsuleError("capsule directory must be under scratch/release-capsules/")
    directory = _assert_ordinary_path(root, relative, directory=True)
    manifest_path = directory / "capsule.json"
    _assert_ordinary_path(root, f"{relative}/capsule.json")
    document, raw = _read_capsule_json(manifest_path)
    _validate_capsule_directory(root, directory, document)
    expected_name = f"{document['payloadDigest']}-{document['sourceCommit'][:12]}"
    if directory.name != expected_name:
        raise ReleaseCapsuleError("capsule directory name does not match its payload and source commit")
    return _capsule_from_manifest(root, directory, document, raw)
