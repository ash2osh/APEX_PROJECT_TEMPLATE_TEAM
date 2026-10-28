#!/usr/bin/env python3
"""Capture whole stored packages and write digest-bound feature source reviews."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from . import baseline, new_migration
from .path_utils import relative_to_root


FEATURE_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
PACKAGE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}\Z", re.ASCII)
QUOTED_PACKAGE_NAME_RE = re.compile(r'"(?:[^"]|"")+"\Z')
DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
CAPTURE_SCHEMA_VERSION = 1
IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)


class FeatureSourcesError(ValueError):
    """A package capture or source-review request is unsafe or incomplete."""


def _feature_scope(root: Path, feature: str, schema: str) -> tuple[Path, Path]:
    if not isinstance(feature, str) or FEATURE_RE.fullmatch(feature) is None:
        raise FeatureSourcesError("feature must be a lowercase hyphenated slug")
    if not isinstance(schema, str) or SCHEMA_RE.fullmatch(schema) is None:
        raise FeatureSourcesError("schema must be an uppercase Oracle identifier")
    feature_dir = root / "features" / feature / schema
    scratch_dir = root / "scratch" / "feature-source" / feature / schema
    return feature_dir, scratch_dir


def _is_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _ensure_directory(path: Path, root: Path) -> None:
    try:
        relative = relative_to_root(path, root)
    except ValueError as error:
        raise FeatureSourcesError(f"feature source directory is outside the repository: {path}") from error
    current = root
    try:
        metadata = current.lstat()
        if _is_reparse(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise FeatureSourcesError(f"repository root is not an ordinary directory: {root}")
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                metadata = current.lstat()
            if _is_reparse(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise FeatureSourcesError(f"feature source path contains an unsafe directory: {current}")
    except FeatureSourcesError:
        raise
    except OSError as error:
        raise FeatureSourcesError(f"cannot prepare feature source directory {path}: {error}") from error


def _ordinary_file(path: Path, root: Path) -> bytes | None:
    try:
        relative_to_root(path, root)
    except ValueError as error:
        raise FeatureSourcesError(f"feature source path is outside the repository: {path}") from error
    current = root
    try:
        if _is_reparse(current, current.lstat()):
            raise FeatureSourcesError(f"repository root cannot be a symlink or reparse point: {root}")
        relative = path.relative_to(root)
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            if _is_reparse(current, metadata):
                raise FeatureSourcesError(f"symlink or reparse point is not allowed: {current}")
        if not stat.S_ISREG(metadata.st_mode):
            raise FeatureSourcesError(f"feature source must be an ordinary file: {path}")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except FeatureSourcesError:
        raise
    except OSError as error:
        raise FeatureSourcesError(f"cannot read feature source {path}: {error}") from error


def _atomic_group_write(root: Path, outputs: Mapping[Path, bytes], deletes: Sequence[Path] = ()) -> None:
    """Install a small related group of files, restoring previous bytes on failure."""
    previous: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    removed: list[Path] = []
    try:
        for path in (*outputs.keys(), *deletes):
            if path in previous:
                raise FeatureSourcesError(f"duplicate feature source transaction path: {path}")
            previous[path] = _ordinary_file(path, root)
            _ensure_directory(path.parent, root)
        for path, data in outputs.items():
            descriptor, name = tempfile.mkstemp(prefix=".feature-source-", dir=path.parent)
            temp = Path(name)
            staged[path] = temp
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for path, temp in staged.items():
            os.replace(temp, path)
            replaced.append(path)
        for path in deletes:
            if path.exists():
                path.unlink()
                removed.append(path)
    except Exception as error:
        rollback_error = None
        for path in reversed((*replaced, *removed)):
            old = previous[path]
            try:
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    descriptor, name = tempfile.mkstemp(prefix=".feature-source-restore-", dir=path.parent)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(old)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, path)
            except OSError as restore_error:
                rollback_error = restore_error
                break
        if rollback_error is not None:
            raise FeatureSourcesError(f"feature source write failed and rollback was incomplete: {rollback_error}") from error
        if isinstance(error, FeatureSourcesError):
            raise
        raise FeatureSourcesError(f"feature source write failed; prior files were restored: {error}") from error
    finally:
        for temp in staged.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _package_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise FeatureSourcesError("package must be a nonempty Oracle identifier")
    if QUOTED_PACKAGE_NAME_RE.fullmatch(value):
        name = value[1:-1].replace('""', '"')
        if not name or any(ord(char) < 32 for char in name):
            raise FeatureSourcesError("quoted package name is empty or contains a control character")
        return name
    if PACKAGE_NAME_RE.fullmatch(value) is None:
        raise FeatureSourcesError("package must be an Oracle identifier or a double-quoted identifier")
    return value.upper()


def _package_stem(name: str) -> str:
    if name.isascii() and name.upper() == name and PACKAGE_NAME_RE.fullmatch(name) is not None:
        return name.lower()
    try:
        encoded_name = name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FeatureSourcesError("package name must be valid UTF-8") from error
    natural_stem = "quoted-" + quote(name, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$#-")
    # Preserve existing short paths, but keep percent-encoded Unicode and
    # quoted names below ordinary filesystem component limits.
    if len(natural_stem.encode("ascii")) <= 180:
        return natural_stem
    hint = re.sub(r"[^A-Za-z0-9_$#-]+", "-", name).strip("-").lower()[:32] or "unit"
    return f"quoted-{hint}-{hashlib.sha256(encoded_name).hexdigest()}"


def _package_source_path(feature_dir: Path, name: str, object_type: str) -> Path:
    suffix = "pks" if object_type == "PACKAGE" else "pkb"
    return feature_dir / "packages" / f"{_package_stem(name)}.{suffix}.sql"


def _capture_metadata_path(scratch_dir: Path, package: str) -> Path:
    return scratch_dir / f"capture-{_package_stem(package)}.json"


def _strict_capture_metadata(path: Path, root: Path) -> dict | None:
    raw = _ordinary_file(path, root)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
        if "\r" in text or text.startswith("\ufeff"):
            raise ValueError("capture metadata must be UTF-8/LF without a BOM")

        def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key {key!r}")
                result[key] = value
            return result

        value = json.loads(text, object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise FeatureSourcesError(f"feature capture metadata is invalid: {error}") from error
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "feature", "schema", "package", "identity", "units"}:
        raise FeatureSourcesError("feature capture metadata has unsupported or missing fields")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != CAPTURE_SCHEMA_VERSION:
        raise FeatureSourcesError("feature capture metadata schemaVersion must be integer 1")
    if not isinstance(value["identity"], dict) or not isinstance(value["units"], list):
        raise FeatureSourcesError("feature capture metadata identity or units are malformed")
    if set(value["identity"]) != set(IDENTITY_FIELDS) or any(
        not isinstance(value["identity"].get(field), str) or not value["identity"][field]
        for field in IDENTITY_FIELDS
    ):
        raise FeatureSourcesError("feature capture metadata observed identity is incomplete")
    return value


def _metadata_path(value: object, *, prefix: tuple[str, ...], label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise FeatureSourcesError(f"feature capture metadata {label} is not a safe repository-relative path")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or path.parts[:len(prefix)] != prefix
        or len(path.parts) <= len(prefix)
    ):
        raise FeatureSourcesError(f"feature capture metadata {label} escapes its expected tree")
    return path


def _capture_sources(
    feature: str,
    schema: str,
    package: str,
    catalog: Mapping,
) -> tuple[dict, ...]:
    sections = catalog.get("sections") if isinstance(catalog, Mapping) else None
    if not isinstance(sections, Mapping):
        raise FeatureSourcesError("DEV source capture has no baseline sections")
    source_rows = sections.get("baseline-source")
    settings_rows = sections.get("baseline-settings")
    if not isinstance(source_rows, list) or not isinstance(settings_rows, list):
        raise FeatureSourcesError("DEV source capture is missing source or compiler-settings rows")
    config = {"prefixes": [], "excludedObjects": []}
    source_units = baseline._source_units(catalog, config, schema)
    settings_map = baseline._settings_map(catalog, config, schema)
    selected = [(kind, package) for kind in ("PACKAGE", "PACKAGE BODY") if (kind, package) in source_units]
    if not selected:
        raise FeatureSourcesError(f"package {package} was not found in {schema} on DEV")
    if selected[0] != ("PACKAGE", package):
        raise FeatureSourcesError(f"package specification was not found in {schema} on DEV: {package}")

    records: list[dict] = []
    for object_type, name in selected:
        settings = settings_map.get((object_type, name))
        if settings is None:
            raise FeatureSourcesError(f"compiler settings are missing for {object_type} {name} in {schema}")
        try:
            selected_settings = baseline._setting_row([settings], name, object_type)
            sql = new_migration.step_sql(schema, object_type, name, source_units[(object_type, name)]["lines"], settings=selected_settings)
        except (baseline.BaselineError, new_migration.NewMigrationError, KeyError, TypeError, ValueError) as error:
            raise FeatureSourcesError(f"captured {object_type} {name} is incomplete: {error}") from error
        source = sql.encode("utf-8")
        relative_path = _package_source_path(Path("features") / feature / schema, name, object_type).as_posix()
        records.append({
            "type": object_type,
            "name": name,
            "path": relative_path,
            "source": source,
            "sha256": hashlib.sha256(source).hexdigest(),
        })
    return tuple(records)


def capture_package(
    repo_root: Path,
    feature: str,
    schema: str,
    package: str,
    *,
    environ: Mapping[str, str],
    refresh: bool = False,
) -> tuple[Path, ...]:
    """Capture the complete DEV package spec/body into stable feature source files."""
    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureSourcesError("repository root cannot be a symlink")
    try:
        root = requested_root.resolve(strict=True)
    except OSError as error:
        raise FeatureSourcesError(f"repository root is unavailable: {error}") from error
    feature_dir, scratch_dir = _feature_scope(root, feature, schema)
    name = _package_name(package)
    if type(refresh) is not bool:
        raise FeatureSourcesError("refresh must be a boolean")
    if not isinstance(environ, Mapping):
        raise FeatureSourcesError("environment values must be a mapping")
    _ensure_directory(scratch_dir, root)
    metadata_path = _capture_metadata_path(scratch_dir, name)
    old_metadata = _strict_capture_metadata(metadata_path, root)
    if old_metadata is not None:
        if (old_metadata["feature"], old_metadata["schema"], old_metadata["package"]) != (feature, schema, name):
            raise FeatureSourcesError("feature capture metadata scope does not match this package")

    capture_root = Path(tempfile.mkdtemp(prefix="capture-", dir=scratch_dir))
    try:
        try:
            catalog = baseline._capture(
                environ,
                "dev",
                schema,
                ("baseline-source", "baseline-settings"),
                capture_root,
                # The existing SQL filter uppercases prefixes, so it cannot select a
                # case-sensitive quoted name. In that case capture the source sections
                # completely, then retain only the exact requested package in memory.
                baseline_prefixes=[name] if name == name.upper() else [],
            )
            baseline._complete(catalog, ("baseline-source", "baseline-settings"))
            identity = baseline._source_identity(catalog.get("identity"), "DEV package capture")
        except (baseline.BaselineError, OSError, ValueError) as error:
            raise FeatureSourcesError(f"DEV package capture failed: {error}") from error
    finally:
        shutil.rmtree(capture_root, ignore_errors=True)
    if identity["current_schema"].upper() != schema:
        raise FeatureSourcesError(
            f"DEV package capture current schema {identity['current_schema']!r} does not match {schema}"
        )
    records = _capture_sources(feature, schema, name, catalog)

    outputs: dict[Path, bytes] = {}
    snapshot_dir = scratch_dir / "captured"
    source_paths: list[Path] = []
    new_units: list[dict] = []
    for record in records:
        source_path = root / PurePosixPath(record["path"])
        snapshot_path = snapshot_dir / source_path.name
        source_paths.append(source_path)
        old_bytes = _ordinary_file(source_path, root)
        if old_bytes is not None and old_bytes != record["source"] and not refresh:
            raise FeatureSourcesError(
                f"feature source differs from the current DEV capture; review it or use refresh: {record['path']}"
            )
        if old_metadata is not None and old_bytes is None and not refresh:
            raise FeatureSourcesError(
                f"captured feature source was removed; review it or use refresh: {record['path']}"
            )
        baseline_bytes = _ordinary_file(snapshot_path, root)
        if old_metadata is None or refresh:
            outputs[snapshot_path] = record["source"]
        elif baseline_bytes is None:
            raise FeatureSourcesError(f"feature capture baseline is missing; use refresh to recapture: {snapshot_path}")
        outputs[source_path] = record["source"]
        new_units.append({
            "type": record["type"],
            "name": record["name"],
            "path": record["path"],
            "capturedPath": relative_to_root(snapshot_path, root).as_posix(),
            "sha256": record["sha256"],
        })

    deletes: list[Path] = []
    if old_metadata is not None:
        current_keys = {(record["type"], record["name"]) for record in records}
        prior_keys = set()
        current_hashes = {(record["type"], record["name"]): record["sha256"] for record in records}
        for unit in old_metadata["units"]:
            if not isinstance(unit, dict) or not isinstance(unit.get("type"), str) or not isinstance(unit.get("name"), str) or not isinstance(unit.get("path"), str):
                raise FeatureSourcesError("feature capture metadata has a malformed unit record")
            key = (unit["type"], unit["name"])
            prior_keys.add(key)
            source_rel = _metadata_path(
                unit["path"], prefix=("features", feature, schema), label="source path",
            )
            captured_rel = _metadata_path(
                unit.get("capturedPath"),
                prefix=("scratch", "feature-source", feature, schema, "captured"),
                label="capturedPath",
            )
            if source_rel.suffix.lower() != ".sql" or captured_rel.name != source_rel.name:
                raise FeatureSourcesError("feature capture metadata unit paths do not match a SQL source file")
            if not isinstance(unit.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", unit["sha256"], re.ASCII) is None:
                raise FeatureSourcesError("feature capture metadata unit hash is malformed")
            if current_hashes.get(key) != unit["sha256"] and not refresh:
                raise FeatureSourcesError("live package source changed since its last capture; review it or use refresh")
            if key not in current_keys:
                if not refresh:
                    raise FeatureSourcesError("live package unit set changed; review it or use refresh")
                deletes.append(root / source_rel)
                deletes.append(root / captured_rel)
        if prior_keys != current_keys and not refresh:
            raise FeatureSourcesError("live package unit set changed; review it or use refresh")

    metadata = {
        "schemaVersion": CAPTURE_SCHEMA_VERSION,
        "feature": feature,
        "schema": schema,
        "package": name,
        "identity": identity,
        "units": new_units,
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    outputs[metadata_path] = metadata_bytes
    _atomic_group_write(root, outputs, deletes)
    return tuple(source_paths)


def _validate_day(day: str) -> None:
    if not isinstance(day, str) or DAY_RE.fullmatch(day) is None:
        raise FeatureSourcesError("day must use YYYY-MM-DD")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("noncanonical date")
    except ValueError as error:
        raise FeatureSourcesError("day must be a valid YYYY-MM-DD date") from error


def _identity_document(identity: Mapping) -> dict[str, str]:
    if not isinstance(identity, Mapping):
        raise FeatureSourcesError("observed identity must be a mapping")
    result: dict[str, str] = {}
    for key, value in identity.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value or any(ord(char) < 32 for char in key + value):
            raise FeatureSourcesError("observed identity must contain nonempty text fields")
        result[key] = value
    if not result:
        raise FeatureSourcesError("observed identity cannot be empty")
    return dict(sorted(result.items()))


def _review_diff(before: str, after: str, feature: str, schema: str, unit: tuple[str, str]) -> bytes:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{unit[0]} {unit[1]} (captured)",
        tofile=f"b/features/{feature}/{schema}/{unit[0]} {unit[1]}",
        n=3,
    )
    result = "".join(lines)
    if not result:
        result = "# no source difference\n"
    return result.encode("utf-8")


def _unit_artifact_stem(unit: tuple[str, str], *, review_content: bytes = b"") -> str:
    """Return a bounded ASCII path component bound to identity and review bytes."""
    if not isinstance(unit, tuple) or len(unit) != 2 or any(not isinstance(value, str) or not value for value in unit):
        raise FeatureSourcesError("unit must be an (object type, object name) pair")
    if not isinstance(review_content, bytes):
        raise FeatureSourcesError("review content must be bytes")
    try:
        identity = unit[0].encode("utf-8") + b"\0" + unit[1].encode("utf-8")
    except UnicodeEncodeError as error:
        raise FeatureSourcesError("unit identity must be valid UTF-8") from error
    type_hint = re.sub(r"[^a-z0-9]+", "-", unit[0].lower()).strip("-")[:8] or "unit"
    name_hint = re.sub(r"[^a-z0-9]+", "-", unit[1].lower()).strip("-")[:16] or "unit"
    identity_hash = hashlib.sha256(identity).hexdigest()[:32]
    content_hash = hashlib.sha256(review_content).hexdigest()[:32]
    return f"{type_hint}-{name_hint}-{identity_hash}-{content_hash}"


def write_source_review(
    repo_root: Path,
    feature: str,
    schema: str,
    unit: tuple[str, str],
    before: str,
    after: str,
    *,
    identity: Mapping[str, str],
    baseline_kind: str,
    payload_digest: str | None,
    day: str,
    target_plan_digest: str | None = None,
) -> tuple[Path, Path]:
    """Write a non-overwriting unified source diff and its provenance manifest."""
    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureSourcesError("repository root cannot be a symlink")
    root = requested_root.resolve(strict=True)
    _feature_dir, _scratch_dir = _feature_scope(root, feature, schema)
    _validate_day(day)
    if not isinstance(unit, tuple) or len(unit) != 2 or any(not isinstance(value, str) or not value for value in unit):
        raise FeatureSourcesError("unit must be an (object type, object name) pair")
    if not isinstance(baseline_kind, str) or re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", baseline_kind, re.ASCII) is None:
        raise FeatureSourcesError("baseline_kind must be a lowercase slug")
    if payload_digest is not None and (not isinstance(payload_digest, str) or re.fullmatch(r"[0-9a-f]{64}", payload_digest, re.ASCII) is None):
        raise FeatureSourcesError("payload_digest must be a SHA-256 hex digest or None")
    if target_plan_digest is not None and (not isinstance(target_plan_digest, str) or re.fullmatch(r"[0-9a-f]{64}", target_plan_digest, re.ASCII) is None):
        raise FeatureSourcesError("target_plan_digest must be a SHA-256 hex digest or None")
    if not isinstance(before, str) or not isinstance(after, str):
        raise FeatureSourcesError("before and after source must be text")
    try:
        before_bytes, after_bytes = before.encode("utf-8"), after.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FeatureSourcesError("source review text must be valid UTF-8") from error
    observed_identity = _identity_document(identity)
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    after_sha = hashlib.sha256(after_bytes).hexdigest()
    diff_bytes = _review_diff(before, after, feature, schema, unit)
    metadata = {
        "schemaVersion": 1,
        "feature": feature,
        "schema": schema,
        "owner": schema,
        "unitType": unit[0],
        "unitName": unit[1],
        "observedIdentity": observed_identity,
        "beforeSha256": before_sha,
        "afterSha256": after_sha,
        "payloadDigest": payload_digest,
        "baselineKind": baseline_kind,
        "diffSha256": hashlib.sha256(diff_bytes).hexdigest(),
        "targetPlanDigest": target_plan_digest,
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    artifact_content = metadata_bytes + b"\0" + diff_bytes
    unit_stem = _unit_artifact_stem(unit, review_content=artifact_content)
    directory = root / "ai_generate" / day / "feature-diffs" / feature / schema / baseline_kind / unit_stem
    diff_path = directory / "source.diff"
    review_path = directory / "review.json"
    _ensure_directory(directory, root)
    old_diff = _ordinary_file(diff_path, root)
    old_review = _ordinary_file(review_path, root)
    if old_diff is not None or old_review is not None:
        if old_diff == diff_bytes and old_review == metadata_bytes:
            return diff_path, review_path
        raise FeatureSourcesError(f"refusing to overwrite an existing feature source review: {directory}")
    _atomic_group_write(root, {diff_path: diff_bytes, review_path: metadata_bytes})
    return diff_path, review_path


def _read_text(path: Path, root: Path, label: str) -> str:
    raw = _ordinary_file(path, root)
    if raw is None:
        raise FeatureSourcesError(f"{label} is missing: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FeatureSourcesError(f"{label} is not UTF-8: {path}") from error


def diff_captured_package(repo_root: Path, feature: str, schema: str, *, day: str) -> tuple[Path, ...]:
    """Compare edited feature package files with their captured DEV source."""
    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureSourcesError("repository root cannot be a symlink")
    try:
        root = requested_root.resolve(strict=True)
    except OSError as error:
        raise FeatureSourcesError(f"repository root is unavailable: {error}") from error
    _validate_day(day)
    feature_dir, scratch_dir = _feature_scope(root, feature, schema)
    try:
        metadata_paths = sorted(scratch_dir.glob("capture-*.json"), key=lambda path: path.name)
    except OSError as error:
        raise FeatureSourcesError(f"cannot enumerate feature capture baselines: {error}") from error
    metadata_documents = [(_strict_capture_metadata(path, root), path) for path in metadata_paths]
    metadata_documents = [(metadata, path) for metadata, path in metadata_documents if metadata is not None]
    if not metadata_documents:
        raise FeatureSourcesError("feature capture baseline is missing; run feature capture first")
    artifacts: list[Path] = []
    for metadata, _metadata_file in metadata_documents:
        if (metadata["feature"], metadata["schema"]) != (feature, schema):
            continue
        identity = _identity_document(metadata["identity"])
        for unit in metadata["units"]:
            if not isinstance(unit, dict) or set(unit) != {"type", "name", "path", "capturedPath", "sha256"}:
                raise FeatureSourcesError("feature capture metadata has a malformed unit record")
            source_rel = _metadata_path(
                unit["path"], prefix=("features", feature, schema), label="source path",
            )
            captured_rel = _metadata_path(
                unit["capturedPath"],
                prefix=("scratch", "feature-source", feature, schema, "captured"),
                label="capturedPath",
            )
            if source_rel.suffix.lower() != ".sql" or captured_rel.name != source_rel.name:
                raise FeatureSourcesError("feature capture metadata unit paths do not match a SQL source file")
            if not isinstance(unit["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", unit["sha256"], re.ASCII) is None:
                raise FeatureSourcesError("feature capture metadata unit hash is malformed")
            source_path = root / source_rel
            captured_path = root / captured_rel
            before = _read_text(captured_path, root, "captured DEV source").encode("utf-8")
            if hashlib.sha256(before).hexdigest() != unit["sha256"]:
                raise FeatureSourcesError(f"captured DEV source does not match its stored hash: {unit['capturedPath']}")
            after_text = _read_text(source_path, root, "feature source")
            diff_path, review_path = write_source_review(
                root,
                feature,
                schema,
                (unit["type"], unit["name"]),
                before.decode("utf-8"),
                after_text,
                identity=identity,
                baseline_kind="dev-capture",
                payload_digest=None,
                day=day,
            )
            artifacts.extend((diff_path, review_path))
    if not artifacts:
        raise FeatureSourcesError(f"no package source units are recorded for {feature}/{schema}")
    return tuple(artifacts)
